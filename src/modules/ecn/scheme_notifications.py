"""ECN方案评审全部通过后的项目销售及上级传导通知。"""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from ...ecn_management_config import ECN_WECOM_CONFIG, get_ecn_scheme_target_projects
from ...wecom_service import (
    resolve_wecom_recipients,
    send_wecom_text_message,
    send_wecom_textcard_message,
)

logger = logging.getLogger(__name__)


def _unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        normalized = text.casefold()
        if text and normalized not in seen:
            seen.add(normalized)
            result.append(text)
    return result


def _identity_title(users: dict, memberships: dict, username: str) -> str:
    user = users.get(username, {})
    membership = memberships.get(username, {})
    return " / ".join(
        _unique(
            (
                user.get("role") if isinstance(user, dict) else "",
                membership.get("position_name") if isinstance(membership, dict) else "",
            )
        )
    )


def _canonical_username(value: Any, users: dict) -> str:
    candidate = str(value or "").strip()
    if not candidate or candidate == "未指定":
        return ""
    if candidate in users:
        return candidate
    matches = [
        str(username)
        for username, user in users.items()
        if isinstance(user, dict)
        and str(user.get("display_name") or "").strip() == candidate
    ]
    return matches[0] if len(matches) == 1 else ""


def _deliverable_usernames(
    usernames: list[str],
    users: dict,
    bindings: dict,
) -> list[str]:
    return _unique(
        username
        for username in usernames
        if isinstance(users.get(username), dict)
        and users[username].get("status", "active") == "active"
        and str(bindings.get(username, {}).get("external_userid") or "").strip()
    )


def _first_deliverable_manager_level(
    seed_usernames: list[str],
    users: dict,
    memberships: dict,
    bindings: dict,
) -> list[str]:
    """沿直属上级逐级查找，停在第一层存在可送达人员的层级。"""
    frontier = _unique(seed_usernames)
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
        deliverable = _deliverable_usernames(managers, users, bindings)
        if deliverable:
            return deliverable
        frontier = managers
    return []


def _fallback_sales_management(
    users: dict,
    memberships: dict,
    bindings: dict,
) -> list[str]:
    """项目销售无法识别时，从销售主管开始，仍不可送达再上提。"""
    supervisors = [
        str(username)
        for username in users
        if "销售主管" in _identity_title(users, memberships, str(username))
    ]
    deliverable = _deliverable_usernames(supervisors, users, bindings)
    if deliverable:
        return deliverable
    managers = _first_deliverable_manager_level(supervisors, users, memberships, bindings)
    if managers:
        return managers
    directors = [
        str(username)
        for username in users
        if "销售总监" in _identity_title(users, memberships, str(username))
    ]
    return _deliverable_usernames(directors, users, bindings)


def resolve_scheme_sales_notification_routes(
    record: dict,
    project_sales: Any,
    *,
    user_service,
) -> list[dict[str, Any]]:
    """按项目解析实际通知人；销售无法送达时沿组织上级链传导。"""
    projects = get_ecn_scheme_target_projects(record)
    sales_by_project = project_sales if isinstance(project_sales, dict) else {}
    users = user_service.load_users()
    memberships = user_service.list_primary_memberships()
    bindings = user_service.list_wecom_bindings()
    routes: list[dict[str, Any]] = []
    for project in projects:
        raw_sales = str(sales_by_project.get(project) or "").strip()
        seller = _canonical_username(raw_sales, users)
        recipients = _deliverable_usernames([seller], users, bindings) if seller else []
        escalated = False
        if not recipients and seller:
            recipients = _first_deliverable_manager_level(
                [seller], users, memberships, bindings
            )
            escalated = bool(recipients)
        if not recipients:
            recipients = _fallback_sales_management(users, memberships, bindings)
            escalated = bool(recipients)
        routes.append(
            {
                "project": project,
                "project_sales": raw_sales or "未识别",
                "recipient_usernames": recipients,
                "escalated": escalated,
            }
        )
    return routes


def _card_description(record: dict, routes: list[dict[str, Any]], *, note: str) -> str:
    recipients = _unique(
        username
        for route in routes
        for username in route.get("recipient_usernames", [])
    )
    projects = _unique(route.get("project") for route in routes)
    sales = _unique(route.get("project_sales") for route in routes)
    lines = [
        f"单号：{record.get('ecn_id') or '—'}",
        f"主题：{record.get('basic_info', {}).get('title') or '工程变更申请'}",
        f"涉及项目：{'、'.join(projects) or '—'}",
        f"项目销售：{'、'.join(sales) or '未识别'}",
        f"通知人员：{'、'.join(recipients) or '未找到可送达人员'}",
        "结果：ECN方案全部审批节点已通过",
    ]
    if any(route.get("escalated") is True for route in routes):
        lines.append("传导说明：项目销售无法送达，已沿上级逐级通知")
    prefix = f'<div class="gray">{escape(note)}</div>'
    suffix = ""
    overflow_block = '<div class="normal">…（进入系统查看详情）</div>'
    budget = 512 - len((prefix + overflow_block).encode("utf-8"))
    used = 0
    for line in lines:
        block = f'<div class="normal">{escape(str(line))}</div>'
        size = len(block.encode("utf-8"))
        if used + size > budget:
            suffix += overflow_block
            break
        suffix += block
        used += size
    return prefix + suffix


async def send_scheme_sales_completion_notifications(
    record: dict,
    project_sales: Any,
    *,
    approval_round: str,
    config: dict | None = None,
    user_service,
) -> tuple[int, int]:
    """方案评审全部通过时通知项目销售；遵循ECN调试转发和经理观察抄送设置。"""
    settings = config if config is not None else ECN_WECOM_CONFIG
    if not settings.get("enabled", False):
        return 0, 0
    routes = resolve_scheme_sales_notification_routes(
        record,
        project_sales,
        user_service=user_service,
    )
    if not routes:
        return 0, 0
    bindings = user_service.list_wecom_bindings()
    targets: dict[str, list[dict[str, Any]]] = {}
    debug_recipients: set[str] = set()
    cc_recipients: set[str] = set()
    if settings.get("test_mode") is True:
        resolved = await resolve_wecom_recipients(
            settings.get("test_notify_targets", []), fallback_touser=""
        )
        debug_recipients = {
            userid for userid in resolved.split("|") if userid and userid != "@all"
        }
        for userid in debug_recipients:
            targets[userid] = routes
    else:
        for route in routes:
            for username in route.get("recipient_usernames", []):
                userid = str(bindings.get(username, {}).get("external_userid") or "").strip()
                if userid:
                    targets.setdefault(userid, []).append(route)
        if settings.get("cc_manager_enabled") is True:
            resolved = await resolve_wecom_recipients(
                [{"position": "研发经理"}], fallback_touser=""
            )
            cc_recipients = {
                userid for userid in resolved.split("|") if userid and userid != "@all"
            }
            for userid in cc_recipients:
                targets[userid] = routes
    if not targets:
        logger.warning("ECN方案评审通过通知没有可送达人员：%s", record.get("ecn_id"))
        return 0, 0

    link_url = (
        f"{settings.get('public_base_url', '').rstrip('/')}/ecn_management"
        if settings.get("public_base_url")
        else ""
    )
    sent = failed = 0
    for recipient, recipient_routes in targets.items():
        if recipient in debug_recipients:
            note = "调试转发 · 未通知实际项目销售或其上级"
        elif recipient in cc_recipients:
            note = "研发经理观察抄送 · 汇总全部项目销售通知"
        elif any(route.get("escalated") is True for route in recipient_routes):
            note = "项目销售通知 · 由上级代收"
        else:
            note = "项目销售通知"
        description = _card_description(record, recipient_routes, note=note)
        business_key = (
            f"{record.get('ecn_id')}:{approval_round}:scheme_sales_completion:{recipient}"
        )
        try:
            if link_url:
                success, message = await send_wecom_textcard_message(
                    description,
                    recipient,
                    title="🔧【ECN工程变更】方案评审已通过",
                    link_url=link_url,
                    module="ecn_management",
                    business_key=business_key,
                    message_type="scheme_sales_completion",
                )
            else:
                plain_lines = [
                    "【ECN工程变更】方案评审已通过",
                    f"单号：{record.get('ecn_id') or '—'}",
                    f"主题：{record.get('basic_info', {}).get('title') or '工程变更申请'}",
                    "涉及项目：" + "、".join(_unique(route.get("project") for route in recipient_routes)),
                    "结果：ECN方案全部审批节点已通过",
                ]
                success, message = await send_wecom_text_message(
                    "\n".join(plain_lines),
                    recipient,
                    module="ecn_management",
                    business_key=business_key,
                    message_type="scheme_sales_completion",
                    retry_tracking=False,
                    alert_on_max_failure=False,
                )
            sent += int(success)
            failed += int(not success)
            if not success:
                logger.warning("ECN方案评审通过通知发送失败：%s %s", record.get("ecn_id"), message)
        except Exception:
            failed += 1
            logger.exception("ECN方案评审通过通知异常：%s", record.get("ecn_id"))
    return sent, failed
