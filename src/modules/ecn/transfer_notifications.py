"""移交取消告知：事务内保存事件，后台逐收件人占用、去重和重试。"""

import hashlib
import json
import logging
import time
import uuid

from ...ecn_management_config import get_ecn_special_confirmations
from .special_task_messages import get_special_message_item, build_special_card

logger = logging.getLogger(__name__)
STATE_KEY = "ecn_transfer_notification_state"


async def send_transfer_cancellations(ecn_id: str, record: dict, settings: dict, service, storage) -> tuple[int, int]:
    from . import notifications as notify

    sent, failed = 0, 0
    route = hashlib.sha256(
        json.dumps(
            [settings["test_mode"], settings["test_notify_targets"] if settings["test_mode"] else []],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    for event_id, notice in record.get("execution_info", {}).get("transfer_notices", {}).items():
        name = str(notice["recipient"])
        user = service.get_user(name)
        if not user or user.get("status", "active") != "active":
            continue
        targets = await notify.resolve_delivery_targets({name: str(user.get("role") or "")}, settings, service)
        cc_ids: set[str] = set()
        if not settings["test_mode"] and settings["cc_manager_enabled"]:
            try:
                cc_ids = {
                    value
                    for value in (
                        await notify.resolve_wecom_recipients([{"position": "研发经理"}], fallback_touser="")
                    ).split("|")
                    if value and value != "@all"
                }
                for value in cc_ids:
                    targets[value] = [name]
            except Exception:
                logger.exception("特定事项取消告知抄送解析失败")
        for recipient in targets:
            path = [STATE_KEY, ecn_id, event_id, route, recipient]
            token = uuid.uuid4().hex
            claimed = False
            now = time.time()

            def claim(current):
                nonlocal claimed
                current = current if isinstance(current, dict) else {}
                if (
                    current.get("done")
                    or current.get("lease", 0) > now
                    or current.get("attempt", 0) > now - settings["retry_seconds"]
                ):
                    return storage.ATOMIC_NO_UPDATE
                claimed = True
                return {"token": token, "lease": now + 300, "attempt": now}

            if not await storage.atomic_deep_update(path, claim) or not claimed:
                continue
            success = False
            obsolete = False
            try:
                records = await storage.get_fresh_item("ecn_management_data", {})
                fresh = records.get(ecn_id, {})
                current = get_ecn_special_confirmations(fresh.get("execution_info")).get(notice["key"])
                # 人员已重新接回同一事项时，不发送误导性的旧取消提醒。
                obsolete = not current or current.get("assignee") == name
                if obsolete:
                    continue
                item = get_special_message_item(fresh, notice["key"], name)
                message = f"已取消，无需继续处理。\n事项/方案：{item['subject']}\n项目：{item['projects']}\n应执行内容：{item['content']}"
                task = {name: [message]}
                if settings["public_base_url"]:
                    _, description = build_special_card(
                        fresh,
                        [item],
                        test_mode=settings["test_mode"],
                        is_cc=recipient in cc_ids,
                        cancelled=True,
                        observer_names=[name],
                    )
                    success, _ = await notify.send_wecom_textcard_message(
                        description,
                        recipient,
                        title="🔧【ECN工程变更】移交任务取消",
                        link_url=f"{settings['public_base_url']}/ecn_management",
                        module="ecn_management",
                        business_key=f"{ecn_id}:transfer:{event_id}",
                        message_type="transfer_cancel",
                    )
                else:
                    success, _ = await notify.send_wecom_text_message(
                        notify.build_notification_content(fresh, task, [name], settings, is_cc=recipient in cc_ids),
                        recipient,
                        module="ecn_management",
                        business_key=f"{ecn_id}:transfer:{event_id}",
                        message_type="transfer_cancel",
                        retry_tracking=False,
                        alert_on_max_failure=False,
                    )
                sent += int(success)
                failed += int(not success)
            except Exception:
                failed += 1
                logger.exception("特定事项取消告知失败：%s", ecn_id)
            finally:

                def finish(current):
                    if not isinstance(current, dict) or current.get("token") != token:
                        return storage.ATOMIC_NO_UPDATE
                    current.update(lease=0, done=success or obsolete)
                    return current

                await storage.atomic_deep_update(path, finish)
    return sent, failed
