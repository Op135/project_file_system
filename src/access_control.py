"""已迁移业务模块统一使用的权限判断入口。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .permission_catalog import tool_permission_code


def can(
    user_service: Any,
    username: str,
    permission_code: str,
    *,
    legacy_role: str = "",
    legacy_allowed_roles: Iterable[str] | None = None,
) -> bool:
    """判断用户是否拥有指定的稳定权限。

    只有部署环境仍使用 ``users.xlsx`` 时才读取兼容参数；完成用户迁移后，以数据库中
    的角色与权限关系为唯一依据。
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
