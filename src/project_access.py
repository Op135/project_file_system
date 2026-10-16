"""项目资料基础维护、全状态/阶段专项切换和项目工程师指定的权限判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .permission_catalog import (
    PROJECT_BASE_EDIT_PERMISSION,
    PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
    PROJECT_STATUS_EDIT_PERMISSION,
    PROJECT_STATUS_TRANSFER_TO_TRIAL_PERMISSION,
    PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION,
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
    """判断是否拥有项目全状态修改权限。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_STATUS_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_LEGACY_STATUS_ENGINEER_ROLES,
    )


def can_edit_any_project_status(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否拥有全状态权限或任一阶段专项切换权限。"""
    service = _service(user_service)
    return any(
        can(service, current_user, permission_code)
        for permission_code in (
            PROJECT_STATUS_EDIT_PERMISSION,
            PROJECT_STATUS_TRANSFER_TO_TRIAL_PERMISSION,
            PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION,
        )
    )


def can_transition_project_status(
    current_role: object,
    current_user: str,
    previous_state: object,
    target_state: object,
    *,
    user_service=None,
) -> bool:
    """全状态权限允许所有合法迁移；专项权限只允许对应的单向阶段切换。"""
    service = _service(user_service)
    if can(
        service,
        current_user,
        PROJECT_STATUS_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_LEGACY_STATUS_ENGINEER_ROLES,
    ):
        return True
    transition_permission = {
        ("转产", "试产"): PROJECT_STATUS_TRANSFER_TO_TRIAL_PERMISSION,
        ("试产", "量产"): PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION,
    }.get((str(previous_state or ""), str(target_state or "")))
    return bool(transition_permission and can(service, current_user, transition_permission))


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
