"""把稳定通知权限解析为已绑定的企业微信成员账号。"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from nicegui import app

from .legacy_compatibility import record_legacy_compatibility_hit
from .wecom_service import resolve_wecom_recipients

logger = logging.getLogger(__name__)


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
