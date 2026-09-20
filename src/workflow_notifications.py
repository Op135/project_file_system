"""通用审批流程完成后的附加抄送解析与发送。"""

from __future__ import annotations

import logging
from html import escape
from typing import Any, Iterable

from nicegui import app

from .notification_recipients import resolve_position_wecom_recipients
from .wecom_service import send_wecom_text_message, send_wecom_textcard_message

logger = logging.getLogger(__name__)


def completion_cc_position_ids(assignment_or_version: Any) -> list[str]:
    """读取流程快照中的完成抄送岗位；旧版本默认关闭。"""
    if not isinstance(assignment_or_version, dict):
        return []
    notification = assignment_or_version.get("notification", {})
    if not isinstance(notification, dict):
        return []
    completion_cc = notification.get("completion_cc", {})
    if not isinstance(completion_cc, dict) or completion_cc.get("enabled") is not True:
        return []
    raw_position_ids = completion_cc.get("position_ids", [])
    if not isinstance(raw_position_ids, list):
        return []
    return list(
        dict.fromkeys(
            str(position_id).strip()
            for position_id in raw_position_ids
            if str(position_id).strip()
        )
    )


async def resolve_workflow_completion_cc_recipients(
    assignment_or_version: Any,
    *,
    user_service=None,
) -> str:
    """把流程版本配置的额外抄送岗位解析为企业微信账号。"""
    position_ids = completion_cc_position_ids(assignment_or_version)
    if not position_ids:
        return ""
    service = user_service or getattr(app.state, "user_service", None)
    return await resolve_position_wecom_recipients(position_ids, user_service=service)


def merge_wecom_userids(*values: str) -> str:
    """按企业微信 userid 合并收件人，避免原通知与额外抄送重复。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for userid in str(value or "").split("|"):
            normalized = userid.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
    return "|".join(result)


async def send_workflow_completion_cc(
    assignment_or_version: Any,
    *,
    title: str,
    lines: Iterable[str],
    link_url: str,
    module: str,
    business_key: str,
    user_service=None,
) -> tuple[bool, str]:
    """仅在配置了附加岗位时发送一张流程完成卡片。"""
    recipients = await resolve_workflow_completion_cc_recipients(
        assignment_or_version,
        user_service=user_service,
    )
    if not recipients:
        return True, "未配置可送达的流程完成抄送人员"
    normalized_lines = [str(line) for line in lines if str(line).strip()]
    description = "".join(
        f'<div class="normal">{escape(str(line))}</div>'
        for line in normalized_lines
    )
    try:
        if not link_url:
            return await send_wecom_text_message(
                "\n".join([title, *normalized_lines]),
                recipients,
                module=module,
                business_key=business_key,
                message_type="workflow_completion_cc",
            )
        return await send_wecom_textcard_message(
            description,
            recipients,
            title=title,
            link_url=link_url,
            module=module,
            business_key=business_key,
        )
    except Exception:
        logger.exception("流程完成额外抄送发送异常：%s", business_key)
        return False, "流程完成额外抄送发送异常"
