"""统计信息入口、概述统计和概述负责人维护权限判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .permission_catalog import (
    STATISTICS_OVERVIEW_OWNER_MANAGE_PERMISSION,
    STATISTICS_OVERVIEW_VIEW_PERMISSION,
    STATISTICS_VIEW_PERMISSION,
)


STATISTICS_LEGACY_VIEW_KEYWORDS = ("总监", "经理", "主管", "boss", "admin")
STATISTICS_LEGACY_OVERVIEW_ROLES = ("研发经理", "研发电子主管")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_view_statistics(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以进入统计信息页面。"""
    role = str(current_role or "")
    legacy_allowed = any(keyword in role for keyword in STATISTICS_LEGACY_VIEW_KEYWORDS)
    return can(
        _service(user_service),
        current_user,
        STATISTICS_VIEW_PERMISSION,
        legacy_role=role,
        legacy_allowed_roles=(role,) if legacy_allowed else (),
    )


def can_view_overview_statistics(
    current_role: object,
    current_user: str,
    *,
    legacy_allowed_roles=STATISTICS_LEGACY_OVERVIEW_ROLES,
    user_service=None,
) -> bool:
    """判断是否可以查看全体概述待办及负责人统计。"""
    return can(
        _service(user_service),
        current_user,
        STATISTICS_OVERVIEW_VIEW_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=tuple(legacy_allowed_roles),
    )


def can_manage_overview_owners(
    current_role: object,
    current_user: str,
    *,
    legacy_allowed_roles=STATISTICS_LEGACY_OVERVIEW_ROLES,
    user_service=None,
) -> bool:
    """判断是否可以跨项目手工指定各专业概述负责人。"""
    return can(
        _service(user_service),
        current_user,
        STATISTICS_OVERVIEW_OWNER_MANAGE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=tuple(legacy_allowed_roles),
    )
