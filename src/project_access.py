"""项目资料基础维护、状态和项目工程师指定的独立权限判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .permission_catalog import (
    PROJECT_BASE_EDIT_PERMISSION,
    PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
    PROJECT_STATUS_EDIT_PERMISSION,
)


PROJECT_LEGACY_BASE_EDIT_ROLES = ("研发经理",)
PROJECT_LEGACY_STATUS_ENGINEER_ROLES = ("研发经理", "研发助理", "admin")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_manage_project_records(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否可以新增项目或维护状态之外的项目基础资料。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_BASE_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_LEGACY_BASE_EDIT_ROLES,
    )


def can_edit_project_status(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否可以独立修改任意项目的项目状态。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_STATUS_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_LEGACY_STATUS_ENGINEER_ROLES,
    )


def can_assign_all_project_engineers(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否可以为任意项目指定项目工程师负责人。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_LEGACY_STATUS_ENGINEER_ROLES,
    )
