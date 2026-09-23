"""执行复核撤销通知：原确认人不可送达时沿直属上级逐级传导。"""

from __future__ import annotations

import logging
import time
import uuid
from html import escape
from typing import Any

from ...notification_recipients import is_system_admin_username

logger = logging.getLogger(__name__)
STATE_KEY = "ecn_execution_review_notification_state"


def _unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def _deliverable(usernames: list[str], users: dict, bindings: dict) -> list[str]:
    return _unique(
        username
        for username in usernames
        if not is_system_admin_username(username)
        and isinstance(users.get(username), dict)
        and users[username].get("status", "active") == "active"
        and str(bindings.get(username, {}).get("external_userid") or "").strip()
    )


def _manager_recipients(seed: str, users: dict, memberships: dict, bindings: dict) -> list[str]:
    frontier = [seed] if seed else []
    visited = set(frontier)
    while frontier:
        managers: list[str] = []
        for username in frontier:
            membership = memberships.get(username, {})
            manager = (
                str(membership.get("manager_username") or "").strip()
                if isinstance(membership, dict)
                else ""
            )
            if manager and manager not in visited:
                visited.add(manager)
                managers.append(manager)
        if not managers:
            return []
        targets = _deliverable(managers, users, bindings)
        if targets:
            return targets
        frontier = managers
    return []


def resolve_review_notice_recipients(notice: dict, service) -> tuple[list[str], bool]:
    users = service.load_users()
    memberships = service.list_primary_memberships()
    bindings = service.list_wecom_bindings()
    original = str(notice.get("recipient") or "").strip()
    direct = _deliverable([original], users, bindings)
    if direct:
        return direct, False
    return _manager_recipients(original, users, memberships, bindings), True


def _card_description(record: dict, notice: dict, *, note: str, recipients: list[str]) -> str:
    lines = [
        f"单号：{record.get('ecn_id') or '—'}",
        f"主题：{record.get('basic_info', {}).get('title') or '工程变更申请'}",
        f"执行项：{notice.get('subject') or '—'}",
        f"原确认人：{notice.get('recipient') or '—'}",
        f"撤销人：{notice.get('reviewer') or '—'}",
        f"撤销理由：{notice.get('reason') or '—'}",
    ]
    if recipients and recipients != [str(notice.get("recipient") or "")]:
        lines.append(f"传导至：{'、'.join(recipients)}")
    prefix = f'<div class="gray">{escape(note)}</div>'
    overflow = '<div class="normal">…（进入系统查看完整记录）</div>'
    budget = 512 - len((prefix + overflow).encode("utf-8"))
    body = ""
    for line in lines:
        block = f'<div class="normal">{escape(str(line))}</div>'
        if len((body + block).encode("utf-8")) > budget:
            body += overflow
            break
        body += block
    return prefix + body


async def send_execution_review_notices(
    ecn_id: str,
    record: dict,
    settings: dict,
    service,
    storage,
) -> tuple[int, int]:
    from . import notifications as notify

    verification = record.get("execution_info", {}).get("verification", {})
    notices = verification.get("notices", {}) if isinstance(verification, dict) else {}
    if not isinstance(notices, dict):
        return 0, 0
    sent = failed = 0
    bindings = service.list_wecom_bindings()
    for event_id, notice in notices.items():
        if not isinstance(notice, dict):
            continue
        recipients, escalated = resolve_review_notice_recipients(notice, service)
        target_ids: dict[str, str] = {}
        debug_ids: set[str] = set()
        cc_ids: set[str] = set()
        if settings.get("test_mode") is True:
            resolved = await notify.resolve_wecom_recipients(
                settings.get("test_notify_targets", []), fallback_touser=""
            )
            debug_ids = {value for value in resolved.split("|") if value and value != "@all"}
            target_ids = {value: "调试转发 · 未通知实际确认人或其上级" for value in debug_ids}
        else:
            for username in recipients:
                userid = str(bindings.get(username, {}).get("external_userid") or "").strip()
                if userid:
                    target_ids[userid] = "原确认人不可送达 · 已沿直属上级传导" if escalated else "执行确认被复核撤销"
            if settings.get("cc_manager_enabled") is True:
                resolved = await notify.resolve_wecom_recipients(
                    [{"position": "研发经理"}], fallback_touser=""
                )
                cc_ids = {value for value in resolved.split("|") if value and value != "@all"}
                for value in cc_ids:
                    target_ids[value] = "研发经理观察抄送"
        if not target_ids:
            logger.warning("ECN执行复核撤销通知没有可送达人员：%s %s", ecn_id, event_id)
            continue
        for recipient_id, note in target_ids.items():
            path = [STATE_KEY, ecn_id, str(event_id), recipient_id]
            token = uuid.uuid4().hex
            now = time.time()
            claimed = False

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
            try:
                link_url = (
                    f"{settings.get('public_base_url', '').rstrip('/')}/ecn_management"
                    if settings.get("public_base_url")
                    else ""
                )
                description = _card_description(record, notice, note=note, recipients=recipients)
                business_key = f"{ecn_id}:execution_review:{event_id}:{recipient_id}"
                if link_url:
                    success, _ = await notify.send_wecom_textcard_message(
                        description,
                        recipient_id,
                        title="🔧【ECN工程变更】执行确认被撤销",
                        link_url=link_url,
                        module="ecn_management",
                        business_key=business_key,
                        message_type="execution_review_revoked",
                    )
                else:
                    success, _ = await notify.send_wecom_text_message(
                        "\n".join(
                            [
                                "【ECN工程变更】执行确认被撤销",
                                f"单号：{ecn_id}",
                                f"执行项：{notice.get('subject') or '—'}",
                                f"撤销理由：{notice.get('reason') or '—'}",
                            ]
                        ),
                        recipient_id,
                        module="ecn_management",
                        business_key=business_key,
                        message_type="execution_review_revoked",
                        retry_tracking=False,
                        alert_on_max_failure=False,
                    )
                sent += int(success)
                failed += int(not success)
            except Exception:
                failed += 1
                logger.exception("ECN执行复核撤销通知失败：%s %s", ecn_id, event_id)
            finally:

                def finish(current):
                    if not isinstance(current, dict) or current.get("token") != token:
                        return storage.ATOMIC_NO_UPDATE
                    current.update(lease=0, done=success)
                    return current

                await storage.atomic_deep_update(path, finish)
    return sent, failed
