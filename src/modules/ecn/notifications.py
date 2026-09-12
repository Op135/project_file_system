"""ECN 企业微信待办提醒：复用首页角标规则，调试转发与正式收件人严格分离。"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from html import escape

from nicegui import app

from ... import db_storage
from ...ecn_access import can_confirm_ecn_material_spec, can_view_ecn, is_ecn_pending_for_user
from ...ecn_management_config import (
    ECN_DATA_KEY,
    ECN_WECOM_CONFIG,
    ECNState,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    get_ecn_material_execution_specs,
    is_ecn_scheme_ready_for_review,
)
from ...wecom_service import resolve_wecom_recipients, send_wecom_text_message, send_wecom_textcard_message

logger = logging.getLogger(__name__)
NOTIFICATION_STATE_KEY = "ecn_wecom_notification_state"
_scan_lock = asyncio.Lock()


def collect_pending_users(record: dict, service) -> dict[str, str]:
    """保留首页可见且确有该单待办的在职用户，不按岗位名称额外扩大正式收件范围。"""
    pending: dict[str, str] = {}
    for username, info in service.load_users().items():
        if not isinstance(info, dict) or info.get("status", "active") != "active":
            continue
        role = str(info.get("role") or "")
        if can_view_ecn(role, username, user_service=service) and is_ecn_pending_for_user(
            record,
            username,
            role,
            user_service=service,
        ):
            pending[username] = role
    return pending


def pending_task_details(record: dict, pending: dict[str, str], service) -> dict[str, list[str]]:
    """细化当前用户可执行的物料责任项，用于正文和去重指纹。"""
    state = record.get("workflow", {}).get("current_state")
    execution = record.get("execution_info", {})
    stage = execution.get("stage")
    if state == ECNState.ECN_EXECUTING and stage == ECN_EXECUTION_STAGE_MATERIAL:
        items = {str(item.get("item_id")): item for item in record.get("change_items", []) if isinstance(item, dict)}
        tasks: dict[str, list[str]] = {name: [] for name in pending}
        for item_id, entry in execution.get("material_confirmations", {}).items():
            for spec in get_ecn_material_execution_specs(items.get(str(item_id), {}), entry):
                if spec.get("available") is not True:
                    continue
                for name, role in pending.items():
                    if can_confirm_ecn_material_spec(spec, role, name, user_service=service):
                        tasks[name].append(f"{item_id} / {spec.get('key', '')}")
        return tasks
    if state == ECNState.ECN_EXECUTING:
        description = {
            ECN_EXECUTION_STAGE_ASSISTANT: "核对资料及ERP完成情况，并启动系统内资料执行",
            ECN_EXECUTION_STAGE_OVERVIEW_RUNNING: "系统内资料正在执行，请关注执行结果",
            ECN_EXECUTION_STAGE_OVERVIEW_FAILED: "系统内资料执行失败，请检查并重试",
        }.get(stage, "处理执行待办")
    elif state in {ECNState.DRAFT, ECNState.REJECTED}:
        description = "完善并提交ECR申请" if state == ECNState.DRAFT else "修改被驳回的ECR申请并重新提交"
    elif state in {ECNState.ECR_REVIEWING, ECNState.ECN_REVIEWING}:
        description = "处理当前审批节点"
    elif is_ecn_scheme_ready_for_review(record):
        description = "所有参与人已确认且方案覆盖完整，请发起方案评审"
    else:
        description = "完善影响评估或本人方案；被驳回方案请整改后重新确认"
    return {name: [description] for name in pending}


def build_notification_fingerprint(record: dict, tasks: dict, config: dict) -> str:
    workflow = record.get("workflow", {})
    payload = {
        "state": workflow.get("current_state"),
        "phase": workflow.get("current_phase"),
        "round": workflow.get("approval_round"),
        "step": workflow.get("current_step_index"),
        "stage": record.get("execution_info", {}).get("stage"),
        "participants": workflow.get("scheme_participants", {}),
        "tasks": tasks,
        "test_mode": config["test_mode"],
        "test_notify_targets": config["test_notify_targets"] if config["test_mode"] else [],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def resolve_delivery_targets(pending: dict[str, str], config: dict, service) -> dict[str, list[str]]:
    """返回企业微信账号到原始待办用户名的映射；调试解析失败绝不回退到正式人员。"""
    if config["test_mode"]:
        touser = await resolve_wecom_recipients(config["test_notify_targets"], fallback_touser="")
        return {userid: list(pending) for userid in touser.split("|") if userid and userid != "@all"}
    result: dict[str, list[str]] = {}
    database_mode = getattr(service, "storage_mode", "legacy_excel") == "database"
    bindings = service.list_wecom_bindings() if database_mode else {}
    for name in pending:
        if database_mode:
            touser = str(bindings.get(name, {}).get("external_userid") or "").strip()
        else:
            touser = await resolve_wecom_recipients([{"name": name}], fallback_touser="")
        if not touser:
            logger.warning("ECN待办人员未匹配到企业微信账号，跳过：%s", name)
        for userid in touser.split("|"):
            if userid and userid != "@all":
                result.setdefault(userid, []).append(name)
    return result


def build_notification_content(
    record: dict, tasks: dict[str, list[str]], names: list[str], config: dict, *, is_cc: bool = False
) -> str:
    basic = record.get("basic_info", {})
    lines = [
        "【ECN待办提醒 · 调试转发】"
        if config["test_mode"]
        else ("【ECN待办提醒 · 研发经理抄送】" if is_cc else "【ECN待办提醒】"),
        f"单号：{record.get('ecn_id', '')}",
        f"主题：{basic.get('title') or '工程变更申请'}",
        f"申请人：{basic.get('applicant', '')}",
        f"当前状态：{record.get('workflow', {}).get('current_state', '')}",
        f"{'原应通知人员' if config['test_mode'] else '待处理人员'}：{'、'.join(names)}",
    ]
    for name in names:
        lines.append(f"{name}：{'；'.join(tasks.get(name, []))}")
    if config["test_mode"]:
        lines.append("调试模式：本消息仅发给配置的调试收件人，未发给上述实际待办人员。")
    elif is_cc:
        lines.append("观察抄送：汇总上述人员的待办提醒，便于观察提醒内容及频度；如含本人待办，请按原职责处理。")
    text = "\n".join(lines)
    # 企业微信文本有长度限制；完整责任项可从系统列表查看。
    if len(text.encode("utf-8")) > 1700:
        text = text.encode("utf-8")[:1600].decode("utf-8", errors="ignore") + "\n更多待办请进入系统查看。"
    return text


def build_notification_card(
    record: dict, tasks: dict[str, list[str]], names: list[str], config: dict, *, is_cc: bool = False
) -> tuple[str, str]:
    """用户内容先转义再按字节裁剪，保留完整HTML标签及实体。"""
    state = record.get("workflow", {}).get("current_state", "")
    event = (
        "待发起方案评审"
        if is_ecn_scheme_ready_for_review(record)
        else {
            ECNState.DRAFT: "申请待提交",
            ECNState.REJECTED: "申请待修改",
            ECNState.ECR_REVIEWING: "ECR待审批",
            ECNState.ECN_REVIEWING: "方案待审批",
            ECNState.ECN_SCHEMING: "方案待完善与确认",
            ECNState.ECN_EXECUTING: "执行待办",
        }.get(state, "待办提醒")
    )
    title = f"🔧【ECN工程变更】{event}"
    nature = (
        "调试转发 · 未通知实际处理人"
        if config["test_mode"]
        else ("研发经理抄送 · 含本人待办时请处理" if is_cc else "待办提醒")
    )
    basic = record.get("basic_info", {})
    lines = [
        f"单号：{record.get('ecn_id', '')}",
        f"主题：{str(basic.get('title') or '工程变更申请')[:45]}",
        f"{'原应通知人员' if config['test_mode'] else '待处理人员'}：{'、'.join(names)}",
        *[f"待办：{task}" for task in dict.fromkeys(task for name in names for task in tasks.get(name, []))],
    ]
    prefix = f'<div class="gray">{escape(nature)}</div><div class="normal">'
    suffix = "</div>"
    hint = "…（进入系统查看完整待办）"
    budget = 512 - len((prefix + suffix + hint).encode("utf-8"))
    parts: list[str] = []
    used = 0
    for char in "\n".join(lines):
        # 客户端会忽略正文中的br，使用与灰色说明相同的独立div分行。
        part = '</div><div class="normal">' if char == "\n" else escape(char)
        size = len(part.encode("utf-8"))
        if used + size > budget:
            parts.append(hint)
            break
        parts.append(part)
        used += size
    return title, prefix + "".join(parts) + suffix


async def check_and_send_ecn_reminders(*, config=None, user_service=None, storage=None) -> tuple[int, int]:
    """定时扫描最新待办；独立通知状态保存在SQLite中，不改动ECN业务单据。"""
    settings = config if config is not None else ECN_WECOM_CONFIG
    if not settings["enabled"] or _scan_lock.locked():
        return 0, 0
    service = user_service or getattr(app.state, "user_service", None)
    if service is None:
        return 0, 0
    storage = storage or db_storage
    sent, failed = 0, 0
    async with _scan_lock:
        all_records = await storage.get_fresh_item(ECN_DATA_KEY, {})
        if not isinstance(all_records, dict):
            return 0, 0
        for ecn_id, record in all_records.items():
            if not isinstance(record, dict):
                continue
            pending = collect_pending_users(record, service)
            if not pending:
                # 清除已解决待办的去重状态，后续重新出现同一待办时可以再次通知。
                await storage.del_deep_item([NOTIFICATION_STATE_KEY, ecn_id])
                continue
            tasks = pending_task_details(record, pending, service)
            fingerprint = build_notification_fingerprint(record, tasks, settings)
            targets = await resolve_delivery_targets(pending, settings, service)
            if not targets:
                logger.warning("ECN通知没有可用收件人，未发送：%s（test_mode=%s）", ecn_id, settings["test_mode"])
                continue
            cc_recipients: set[str] = set()
            if not settings["test_mode"] and settings["cc_manager_enabled"]:
                # 抄送失败不阻断正式通知；按账号合并，经理本人有待办时也只发一条。
                try:
                    cc_users = await resolve_wecom_recipients([{"position": "研发经理"}], fallback_touser="")
                    cc_recipients = {userid for userid in cc_users.split("|") if userid and userid != "@all"}
                    if not cc_recipients:
                        logger.warning("ECN研发经理抄送未匹配到微信账号：%s", ecn_id)
                except Exception:
                    logger.exception("ECN研发经理抄送解析失败：%s", ecn_id)
                names_to_copy = list(dict.fromkeys(name for names in targets.values() for name in names))
                for userid in sorted(cc_recipients):
                    targets[userid] = names_to_copy.copy()
            # 抄送开关不参与业务指纹，避免切换时向实际处理人重发同一待办。
            for recipient, names in targets.items():
                now = time.time()
                token = uuid.uuid4().hex
                claimed = False

                def claim(current):
                    nonlocal claimed
                    entry = current if isinstance(current, dict) else {}
                    if entry.get("fingerprint") != fingerprint:
                        entry = {"fingerprint": fingerprint, "recipients": {}}
                    delivery = entry.setdefault("recipients", {}).get(recipient, {})
                    if delivery.get("lease_until", 0) > now:
                        return storage.ATOMIC_NO_UPDATE
                    if delivery.get("sent_at", 0) > now - settings["repeat_hours"] * 3600:
                        return storage.ATOMIC_NO_UPDATE
                    if delivery.get("attempted_at", 0) > now - settings["retry_seconds"]:
                        return storage.ATOMIC_NO_UPDATE
                    entry["recipients"][recipient] = {
                        **delivery,
                        "token": token,
                        "lease_until": now + 300,
                        "attempted_at": now,
                    }
                    claimed = True
                    return entry

                if not await storage.atomic_deep_update([NOTIFICATION_STATE_KEY, ecn_id], claim) or not claimed:
                    continue
                success = False
                try:
                    # 发送前再次核对当前单据，避免通讯录解析期间流程已被处理。
                    fresh_records = await storage.get_fresh_item(ECN_DATA_KEY, {})
                    fresh = fresh_records.get(ecn_id, {})
                    fresh_pending = collect_pending_users(fresh, service)
                    fresh_tasks = pending_task_details(fresh, fresh_pending, service)
                    if not fresh_pending or build_notification_fingerprint(fresh, fresh_tasks, settings) != fingerprint:
                        continue
                    if settings["public_base_url"]:
                        title, description = build_notification_card(
                            fresh, fresh_tasks, names, settings, is_cc=recipient in cc_recipients
                        )
                        success, message = await send_wecom_textcard_message(
                            description,
                            recipient,
                            title=title,
                            link_url=f"{settings['public_base_url']}/ecn_management",
                            module="ecn_management",
                            business_key=f"{ecn_id}:{fingerprint}",
                        )
                    else:
                        # 卡片必须带有效链接；未配置系统地址时保留文字提醒。
                        success, message = await send_wecom_text_message(
                            build_notification_content(
                                fresh, fresh_tasks, names, settings, is_cc=recipient in cc_recipients
                            ),
                            recipient,
                            module="ecn_management",
                            business_key=f"{ecn_id}:{fingerprint}",
                            message_type="pending",
                            link_url=f"{settings['public_base_url']}/ecn_management"
                            if settings["public_base_url"]
                            else "",
                            # ECN自身重试会重新检查待办/调试开关，避免全局重试发送旧任务或通知其它人员。
                            retry_tracking=False,
                            alert_on_max_failure=False,
                        )
                    sent += int(success)
                    failed += int(not success)
                    if not success:
                        logger.warning("ECN微信发送失败，将按配置重试：%s %s", ecn_id, message)
                except Exception:
                    failed += 1
                    logger.exception("ECN微信发送异常：%s", ecn_id)
                finally:

                    def finish(current):
                        if not isinstance(current, dict) or current.get("fingerprint") != fingerprint:
                            return storage.ATOMIC_NO_UPDATE
                        delivery = current.get("recipients", {}).get(recipient, {})
                        if delivery.get("token") != token:
                            return storage.ATOMIC_NO_UPDATE
                        delivery["lease_until"] = 0
                        if success:
                            delivery["sent_at"] = time.time()
                        return current

                    await storage.atomic_deep_update([NOTIFICATION_STATE_KEY, ecn_id], finish)
    return sent, failed


def init_ecn_reminder_task() -> None:
    if not ECN_WECOM_CONFIG["enabled"]:
        logger.info("ECN企业微信提醒已通过配置禁用。")
        return

    async def check():
        try:
            sent, failed = await check_and_send_ecn_reminders()
            if sent or failed:
                logger.info("ECN微信提醒：成功 %s 条，失败 %s 条", sent, failed)
        except Exception:
            logger.exception("ECN微信待办检查失败，等待下次检查")

    app.timer(ECN_WECOM_CONFIG["initial_delay_seconds"], check, once=True)
    app.timer(ECN_WECOM_CONFIG["check_interval_seconds"], check)
