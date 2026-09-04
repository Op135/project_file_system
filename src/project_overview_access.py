"""项目概述的 label 级条目、维护工具和审批权限判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .approval_workflow import resolve_approval_workflow
from .permission_catalog import (
    PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_BATCH_SUBMIT_PERMISSION,
    PROJECT_OVERVIEW_CONTENT_MANAGE_ALL_PERMISSION,
    PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_INACTIVE_VIEW_PERMISSION,
    permission_catalog_rows,
    project_overview_item_permission,
)

OVERVIEW_LEGACY_CONTENT_MANAGE_ROLES = ("研发经理",)
OVERVIEW_LEGACY_INACTIVE_ROLE_KEYWORDS = ("研发",)
OVERVIEW_LEGACY_INACTIVE_ROLES = ("工程NPI", "admin")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def _database_mode(user_service=None) -> bool:
    service = _service(user_service)
    return service is not None and getattr(service, "storage_mode", "legacy_excel") == "database"


def _permission_roles(config: dict, action: str) -> tuple[str, ...]:
    permission = config.get("permission", {}) if isinstance(config, dict) else {}
    values = permission.get(f"{action}_role", []) if isinstance(permission, dict) else []
    return tuple(str(value) for value in values if str(value))


def _item_permission(config: dict, action: str) -> str:
    label = str(config.get("label") or "").strip().lower()
    return project_overview_item_permission(label, action) if label else ""


def can_view_overview_item(
    config: dict,
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断用户能否查看指定 label 的概述项；维护权限自动包含查看。"""
    service = _service(user_service)
    if not current_user or not _item_permission(config, "view") or not _database_mode(service):
        role = str(current_role or "")
        return role in _permission_roles(config, "read") or role in _permission_roles(config, "edit")
    view_code = _item_permission(config, "view")
    edit_code = _item_permission(config, "edit")
    return bool(view_code and (can(service, current_user, view_code) or can(service, current_user, edit_code)))


def can_edit_overview_item(
    config: dict,
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断用户能否维护指定 label 的概述项。"""
    service = _service(user_service)
    edit_code = _item_permission(config, "edit")
    if not current_user or not _database_mode(service):
        return str(current_role or "") in _permission_roles(config, "edit")
    return bool(
        edit_code
        and can(
            service,
            current_user,
            edit_code,
            legacy_role=str(current_role or ""),
            legacy_allowed_roles=_permission_roles(config, "edit"),
        )
    )


def can_view_any_project_overview(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断用户是否至少能查看一个概述 label。"""
    service = _service(user_service)
    if service is None or not _database_mode(service):
        return bool(current_user)
    active_item_codes = {
        str(item["code"])
        for item in permission_catalog_rows(strict_overview=False)
        if str(item["code"]).startswith("project_overview.item.")
    }
    return bool(active_item_codes.intersection(service.get_user_permission_codes(current_user)))


def can_view_inactive_project_overview(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以查看失活概述。"""
    role = str(current_role or "")
    legacy_allowed = role in OVERVIEW_LEGACY_INACTIVE_ROLES or any(
        keyword in role for keyword in OVERVIEW_LEGACY_INACTIVE_ROLE_KEYWORDS
    )
    if not _database_mode(user_service):
        return legacy_allowed
    return can(_service(user_service), current_user, PROJECT_OVERVIEW_INACTIVE_VIEW_PERMISSION)


def can_manage_all_overview_content(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以使用直接修改概述原始内容的管理工具。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_OVERVIEW_CONTENT_MANAGE_ALL_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=OVERVIEW_LEGACY_CONTENT_MANAGE_ROLES,
    )


def can_submit_batch_overview(current_role: object, current_user: str, legacy_roles=(), *, user_service=None) -> bool:
    """判断是否可以使用并提交批量概述变更工具。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_OVERVIEW_BATCH_SUBMIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=tuple(legacy_roles),
    )


def can_review_batch_overview(request: dict, current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以审批批量概述申请；数据库模式以流程明确指派为准。"""
    if not current_user:
        return False
    service = _service(user_service)
    if not _database_mode(service):
        return (
            current_user != request.get("submitter")
            and str(current_role or "") in request.get("reviewer_roles", [])
        )
    assignment = request.get("workflow_assignment", {})
    assignees = assignment.get("assignee_usernames", []) if isinstance(assignment, dict) else []
    # 数据库审批流允许管理员明确把申请人本人配置为审批人；
    # 若流程没有明确指派到本人，仍不能仅凭审批权限进行自审。
    if current_user == request.get("submitter") and current_user not in assignees:
        return False
    return (
        (not assignees or current_user in assignees)
        and can(service, current_user, PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION)
    )


def can_review_overview_correction(request: dict, current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以审批单项概述纠错；数据库模式不再读取角色快照。"""
    if not current_user or current_user == request.get("submitter"):
        return False
    service = _service(user_service)
    if not _database_mode(service):
        reviewer_roles = request.get("reviewer_roles", [])
        if reviewer_roles:
            return str(current_role or "") in reviewer_roles
        # 兼容早期没有审批角色快照的单项目概述变更记录。
        return str(current_role or "") == "研发经理"
    assignment = request.get("workflow_assignment", {})
    assignees = assignment.get("assignee_usernames", []) if isinstance(assignment, dict) else []
    return (
        (not assignees or current_user in assignees)
        and can(service, current_user, PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION)
    )


def resolve_project_overview_workflow(event: str, requester_username: str, *, user_service=None) -> dict:
    """数据库模式解析并固化概述审批人；旧模式返回兼容标记。"""
    service = _service(user_service)
    if not _database_mode(service):
        return {"status": "legacy_mode", "assignment": {}}
    result = resolve_approval_workflow(
        service,
        module="project_overview",
        event=event,
        requester_username=requester_username,
    )
    if result.get("status") != "matched":
        return result
    workflow = result["workflow"]
    version = result["version"]
    result["assignment"] = {
        "workflow_id": workflow["workflow_id"],
        "workflow_code": workflow["code"],
        "workflow_name": workflow["name"],
        "version_id": version["version_id"],
        "version_number": version["version_number"],
        "required_permission_code": version["required_permission_code"],
        "approval_mode": version.get("approval_mode", "any"),
        "assignee_usernames": [item["username"] for item in result["approvers"]],
        "assignee_names": [item.get("display_name") or item["username"] for item in result["approvers"]],
    }
    return result


def overview_workflow_error_message(result: dict, subject: str) -> str:
    """把流程解析结果转换成管理员可操作的错误提示。"""
    detail = str(result.get("message") or "审批流程解析失败")
    return f"{subject}无法提交：{detail}；请检查系统管理中的审批流程、组织岗位及审批权限配置"
