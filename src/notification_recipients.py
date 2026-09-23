"""把稳定通知权限解析为已绑定的企业微信成员账号。"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from nicegui import app

from .legacy_compatibility import record_legacy_compatibility_hit
from .wecom_service import resolve_wecom_recipients

logger = logging.getLogger(__name__)
SYSTEM_ADMIN_USERNAME = "admin"


def is_system_admin_username(value: Any) -> bool:
    """系统管理员拥有全权限，但默认不作为业务通知收件人。"""
    return str(value or "").strip().casefold() == SYSTEM_ADMIN_USERNAME


def _unique_values(values: Iterable[Any]) -> list[str]:
    """按原顺序清理并去重非空文本。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


async def resolve_permission_wecom_recipients(
    permission_code: str,
    *,
    legacy_targets=None,
    fallback_touser: str = "",
    user_service=None,
) -> str:
    """按通知接收权限解析企业微信账号，并兼容尚未迁移的 Excel 用户模式。

    数据库模式只信任稳定权限和系统用户的企业微信绑定，不再读取旧 JSON 中的角色、
    职务接收规则。Excel 模式仍使用旧规则，保证服务器执行用户迁移前可安全部署新代码。
    """
    service = user_service or getattr(app.state, "user_service", None)
    if service is None or getattr(service, "storage_mode", "legacy_excel") != "database":
        target_count = len(legacy_targets) if isinstance(legacy_targets, (list, tuple, set)) else 0
        record_legacy_compatibility_hit(
            "legacy_notification_route",
            str(permission_code or "unknown").strip().lower(),
            detail=f"legacy_targets={target_count}; fallback={bool(fallback_touser)}",
        )
        return await resolve_wecom_recipients(
            legacy_targets or [],
            fallback_touser=fallback_touser,
        )

    usernames = service.list_usernames_with_permission(
        permission_code,
        include_system_admin=False,
    )
    bindings = service.list_wecom_bindings()
    recipients: list[str] = []
    missing_bindings: list[str] = []
    for username in usernames:
        binding = bindings.get(username, {})
        external_userid = str(binding.get("external_userid", "")).strip()
        if external_userid:
            recipients.append(external_userid)
        else:
            missing_bindings.append(username)
    if missing_bindings:
        logger.warning(
            "拥有通知权限但未绑定企业微信账号：permission=%s, users=%s",
            permission_code,
            "、".join(missing_bindings),
        )
    resolved = _unique_values(recipients)
    if resolved:
        return "|".join(resolved)
    fallback = "|".join(_unique_values(str(fallback_touser or "").split("|")))
    logger.warning("通知权限未解析到已绑定成员：permission=%s", permission_code)
    return fallback


def resolve_position_usernames(
    position_ids: Iterable[Any],
    *,
    user_service=None,
) -> tuple[list[str], list[str]]:
    """解析指定稳定岗位当前的在职主任职人员，并返回无任职岗位。"""
    service = user_service or getattr(app.state, "user_service", None)
    normalized_ids = _unique_values(position_ids)
    if not normalized_ids or service is None:
        return [], normalized_ids

    users = service.load_users()
    memberships = service.list_primary_memberships()
    matched_positions: set[str] = set()
    usernames: list[str] = []
    for username, membership in memberships.items():
        position_id = str(membership.get("position_id") or "").strip()
        user = users.get(username, {})
        if (
            is_system_admin_username(username)
            or position_id not in normalized_ids
            or user.get("status") != "active"
        ):
            continue
        matched_positions.add(position_id)
        usernames.append(username)
    missing_positions = [value for value in normalized_ids if value not in matched_positions]
    return _unique_values(usernames), missing_positions


async def resolve_position_wecom_recipients(
    position_ids: Iterable[Any],
    *,
    user_service=None,
) -> str:
    """把流程完成抄送岗位解析为已绑定的企业微信成员账号。"""
    service = user_service or getattr(app.state, "user_service", None)
    if service is None or getattr(service, "storage_mode", "legacy_excel") != "database":
        logger.warning("流程完成抄送岗位只支持数据库身份模式")
        return ""

    usernames, missing_positions = resolve_position_usernames(
        position_ids,
        user_service=service,
    )
    if missing_positions:
        logger.warning("流程完成抄送岗位当前没有在职主任职人员：%s", "、".join(missing_positions))
    bindings = service.list_wecom_bindings()
    recipients: list[str] = []
    missing_bindings: list[str] = []
    for username in usernames:
        external_userid = str(bindings.get(username, {}).get("external_userid", "")).strip()
        if external_userid:
            recipients.append(external_userid)
        else:
            missing_bindings.append(username)
    if missing_bindings:
        logger.warning("流程完成抄送人员未绑定企业微信：%s", "、".join(missing_bindings))
    return "|".join(_unique_values(recipients))
