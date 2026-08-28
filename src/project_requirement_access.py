"""项目需求配置正文的稳定权限与项目责任范围判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .permission_catalog import (
    PROJECT_REQUIREMENT_DRAFT_MANAGE_ALL_PERMISSION,
    PROJECT_REQUIREMENT_EDIT_PERMISSION,
    PROJECT_REQUIREMENT_REVIEW_ALL_PERMISSION,
    PROJECT_REQUIREMENT_REVIEW_ASSIGNED_PERMISSION,
    PROJECT_REQUIREMENT_REVOKE_PERMISSION,
    PROJECT_REQUIREMENT_VIEW_PERMISSION,
)

PROJECT_REQUIREMENT_LEGACY_EDIT_ROLES = ("销售", "销售主管", "销售总监", "admin")
PROJECT_REQUIREMENT_LEGACY_REVIEW_ALL_ROLES = ("研发经理",)


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_view_project_requirement(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """数据库模式按稳定权限查看需求，旧模式保持登录用户均可查看。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_REQUIREMENT_VIEW_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=None,
    )


def can_edit_project_requirement(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否可以新建、暂存、自动保存和提交需求正文。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_REQUIREMENT_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_REQUIREMENT_LEGACY_EDIT_ROLES,
    )


def can_review_all_project_requirements(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否拥有不受项目分配限制的需求审批权限。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_REQUIREMENT_REVIEW_ALL_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_REQUIREMENT_LEGACY_REVIEW_ALL_ROLES,
    )


def has_assigned_requirement_review_permission(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否具备审批本人负责项目的基础资格。"""
    service = _service(user_service)
    if service is None or not current_user:
        return False
    if getattr(service, "storage_mode", "legacy_excel") != "database":
        # 旧模式只要被项目明确指定为工程师即可审批，不另设角色白名单。
        return True
    return can(service, current_user, PROJECT_REQUIREMENT_REVIEW_ASSIGNED_PERMISSION)


def can_review_project_requirement(
    current_role: object,
    current_user: str,
    project_name: str,
    project_engineers: dict,
    *,
    user_service=None,
) -> bool:
    """按全局审批资格或项目工程师具体责任判断单个项目需求配置。"""
    if can_review_all_project_requirements(
        current_role,
        current_user,
        user_service=user_service,
    ):
        return True
    assigned_user = str((project_engineers or {}).get(project_name) or "").strip()
    return assigned_user == str(current_user or "").strip() and has_assigned_requirement_review_permission(
        current_role,
        current_user,
        user_service=user_service,
    )


def can_revoke_project_requirement_approval(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否可以撤销已通过的需求审批。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_REQUIREMENT_REVOKE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_REQUIREMENT_LEGACY_REVIEW_ALL_ROLES,
    )


def can_manage_all_project_requirement_drafts(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否可以查看全部用户的需求草稿。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_REQUIREMENT_DRAFT_MANAGE_ALL_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=PROJECT_REQUIREMENT_LEGACY_REVIEW_ALL_ROLES,
    )
