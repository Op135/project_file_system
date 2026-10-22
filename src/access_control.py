"""已迁移业务模块统一使用的权限判断入口。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .permission_catalog import TOOL_PERMISSIONS, tool_permission_code


def can(
    user_service: Any,
    username: str,
    permission_code: str,
    *,
    legacy_role: str = "",
    legacy_allowed_roles: Iterable[str] | None = None,
) -> bool:
    """判断用户是否拥有指定的稳定权限。

    ``legacy_*`` 参数暂时保留调用兼容性，但不再参与授权。
    """
    if user_service is None or not username:
        return False
    return bool(
        user_service.has_permission(
            username,
            permission_code,
            legacy_role=legacy_role,
            legacy_allowed_roles=legacy_allowed_roles,
        )
    )


def can_use_tool(
    user_service: Any,
    username: str,
    tool_key: str,
    *,
    legacy_role: str = "",
    legacy_allowed_roles: Iterable[str] | None = None,
) -> bool:
    return can(
        user_service,
        username,
        tool_permission_code(tool_key),
        legacy_role=legacy_role,
        legacy_allowed_roles=legacy_allowed_roles,
    )


def can_view_tools(
    user_service: Any,
    username: str,
    *,
    legacy_role: str = "",
) -> bool:
    """至少拥有一项工具权限时才显示分析工具入口。"""
    return any(
        can(
            user_service,
            username,
            permission.code,
            legacy_role=legacy_role,
        )
        for permission in TOOL_PERMISSIONS
    )
