"""ECN 工程变更模块的稳定权限判断入口。"""

from __future__ import annotations

from typing import Any

from nicegui import app

from .access_control import can
from .ecn_management_config import (
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    ECN_IMPACT_FOLLOWUP_STATES,
    ECN_PARTICIPANT_STATUS_CONFIG,
    classify_ecn_change_item,
    get_ecn_impact_handlers,
    get_ecn_material_execution_specs,
    get_ecn_missing_material_code_items,
    get_ecn_level_code,
    get_ecn_validation_report,
    is_ecn_impact_blank,
    role_matches_keywords,
    is_ecn_scheme_ready_for_review,
    get_ecn_special_confirmations,
    ECNState,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_LEVEL_COMPLEX,
)
from .permission_catalog import (
    ECN_CREATE_PERMISSION,
    ECN_APPROVAL_REASSIGN_PERMISSION,
    ECN_DELETE_PERMISSION,
    ECN_ECR_APPROVE_PERMISSION,
    ECN_EXECUTION_ASSISTANT_PERMISSION,
    ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
    ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
    ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
    ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
    ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
    ECN_EXECUTION_VERIFY_PERMISSION,
    ECN_IMPACT_EDIT_PERMISSION,
    ECN_IMPACT_INITIAL_REMINDER_PERMISSION,
    ECN_LEVEL_CLASSIFY_ECR_PERMISSION,
    ECN_LEVEL_CLASSIFY_SCHEME_PERMISSION,
    ECN_MATERIAL_CODE_EDIT_PERMISSION,
    ECN_SCHEME_APPROVE_PERMISSION,
    ECN_SCHEME_EDIT_PERMISSION,
    ECN_SCHEME_REVIEW_SUBMIT_PERMISSION,
    ECN_VIEW_PERMISSION,
    ECN_VALIDATION_DESIGNATE_PERMISSION,
    ECN_VALIDATION_REPORT_APPROVE_PERMISSION,
    ECN_VALIDATION_REPORT_VIEW_PERMISSION,
    ecn_ordinary_file_view_permission,
)
from .project_overview_access import can_view_overview_item
from .ecn_workflow import is_ecr_assigned_approver, is_scheme_assigned_approver


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def _database_mode(user_service=None) -> bool:
    service = _service(user_service)
    return service is not None and getattr(service, "storage_mode", "legacy_excel") == "database"


def get_active_ecn_actor_role(
    username: str,
    fallback_role: object = "",
    *,
    user_service=None,
) -> str | None:
    """提交操作时重新确认账号在职，并返回当前主任职名称用于审计留痕。"""
    service = _service(user_service)
    if service is None:
        return str(fallback_role or "").strip() or None
    user_loader = getattr(service, "get_user", None)
    if not callable(user_loader):
        return str(fallback_role or "").strip() or None
    actor = user_loader(username)
    if not isinstance(actor, dict) or actor.get("status", "active") != "active":
        return None
    if _database_mode(service):
        membership_loader = getattr(service, "get_primary_membership", None)
        membership = membership_loader(username) if callable(membership_loader) else {}
        if isinstance(membership, dict):
            position_name = str(membership.get("position_name") or "").strip()
            if position_name:
                return position_name
    return str(actor.get("role") or fallback_role or "").strip() or None


def build_ecn_access_snapshot(user_service=None) -> dict[str, Any]:
    """一次读取当前用户及权限，供同一轮列表或通知扫描复用。"""
    service = _service(user_service)
    if service is None:
        return {"database_mode": False, "users": {}, "permissions": {}, "memberships": {}}
    users = service.load_users()
    database_mode = _database_mode(service)
    membership_loader = getattr(service, "list_primary_memberships", None)
    memberships = membership_loader() if database_mode and callable(membership_loader) else {}
    permission_loader = getattr(service, "list_active_user_permission_codes", None)
    if database_mode and callable(permission_loader):
        permissions = permission_loader()
    elif database_mode:
        relevant_codes = {
            ECN_VIEW_PERMISSION,
            ECN_CREATE_PERMISSION,
            ECN_IMPACT_EDIT_PERMISSION,
            ECN_SCHEME_EDIT_PERMISSION,
            ECN_SCHEME_REVIEW_SUBMIT_PERMISSION,
            ECN_LEVEL_CLASSIFY_ECR_PERMISSION,
            ECN_LEVEL_CLASSIFY_SCHEME_PERMISSION,
            ECN_VALIDATION_DESIGNATE_PERMISSION,
            ECN_VALIDATION_REPORT_VIEW_PERMISSION,
            ECN_VALIDATION_REPORT_APPROVE_PERMISSION,
            ECN_APPROVAL_REASSIGN_PERMISSION,
            ECN_MATERIAL_CODE_EDIT_PERMISSION,
            ECN_EXECUTION_ASSISTANT_PERMISSION,
            ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
            ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
            ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
            ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
            ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
            ECN_EXECUTION_VERIFY_PERMISSION,
        }
        permissions = {
            username: {code for code in relevant_codes if service.has_permission(username, code)}
            for username, info in users.items()
            if isinstance(info, dict) and info.get("status", "active") == "active"
        }
    else:
        permissions = {}
    return {
        "database_mode": database_mode,
        "users": users,
        "permissions": permissions,
        "memberships": memberships,
    }


def _snapshot_permission(access_snapshot: dict[str, Any] | None, username: str, permission_code: str) -> bool:
    if not isinstance(access_snapshot, dict) or access_snapshot.get("database_mode") is not True:
        return False
    permission_map = access_snapshot.get("permissions", {})
    codes = permission_map.get(username, set()) if isinstance(permission_map, dict) else set()
    return permission_code in codes if isinstance(codes, (set, list, tuple)) else False


def _snapshot_active_user(access_snapshot: dict[str, Any] | None, username: str) -> dict:
    if not isinstance(access_snapshot, dict):
        return {}
    users = access_snapshot.get("users", {})
    info = users.get(username, {}) if isinstance(users, dict) else {}
    return info if isinstance(info, dict) and info.get("status", "active") == "active" else {}


def _can_execute_assistant_with_snapshot(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return can_execute_ecn_assistant_stage(
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_view_ecn(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以进入并查看 ECN 工程变更。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_VIEW_PERMISSION)
    return can(
        _service(user_service),
        current_user,
        ECN_VIEW_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=None,
    )


def can_create_ecn_request(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以新建、保存并提交本人的 ECR 申请。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_CREATE_PERMISSION)
    return can(
        _service(user_service),
        current_user,
        ECN_CREATE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=None,
    )


def can_edit_ecn_impact(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以维护 ECN 影响评估。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_IMPACT_EDIT_PERMISSION)
    if not _database_mode(user_service):
        return False
    return can(
        _service(user_service),
        current_user,
        ECN_IMPACT_EDIT_PERMISSION,
    )


def receives_ecn_initial_impact_reminder(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否接收尚无人认领的 ECN 影响评估兜底提醒。"""
    if not _database_mode(user_service):
        return False
    return can(
        _service(user_service),
        current_user,
        ECN_IMPACT_INITIAL_REMINDER_PERMISSION,
    )


def can_edit_ecn_scheme(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以编写并确认本人负责的 ECN 方案。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_SCHEME_EDIT_PERMISSION)
    if not _database_mode(user_service):
        return False
    return can(
        _service(user_service),
        current_user,
        ECN_SCHEME_EDIT_PERMISSION,
    )


def can_submit_ecn_scheme_review(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以发起 ECN 方案评审。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_SCHEME_REVIEW_SUBMIT_PERMISSION)
    if not _database_mode(user_service):
        return False
    return can(
        _service(user_service),
        current_user,
        ECN_SCHEME_REVIEW_SUBMIT_PERMISSION,
    )


def _can_ecn_permission(
    permission_code: str,
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, permission_code)
    if not _database_mode(user_service):
        return False
    return can(_service(user_service), current_user, permission_code)


def can_classify_ecn_level_during_ecr(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return _can_ecn_permission(
        ECN_LEVEL_CLASSIFY_ECR_PERMISSION,
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_classify_ecn_level_before_scheme_review(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return _can_ecn_permission(
        ECN_LEVEL_CLASSIFY_SCHEME_PERMISSION,
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_designate_ecn_validation_report(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return _can_ecn_permission(
        ECN_VALIDATION_DESIGNATE_PERMISSION,
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_view_ecn_validation_report(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return _can_ecn_permission(
        ECN_VALIDATION_REPORT_VIEW_PERMISSION,
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_approve_ecn_validation_report(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return _can_ecn_permission(
        ECN_VALIDATION_REPORT_APPROVE_PERMISSION,
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_verify_ecn_execution(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    return _can_ecn_permission(
        ECN_EXECUTION_VERIFY_PERMISSION,
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=access_snapshot,
    )


def can_approve_ecn_ecr(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否具备 ECR 审批候选资格；具体单据还必须有流程待办。"""
    return can(
        _service(user_service),
        current_user,
        ECN_ECR_APPROVE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=(str(current_role or ""),),
    )


def can_approve_ecn_scheme(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否具备 ECN 方案审批候选资格；具体单据还必须有流程待办。"""
    return can(
        _service(user_service),
        current_user,
        ECN_SCHEME_APPROVE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=(str(current_role or ""),),
    )


def can_reassign_ecn_approval(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以调整一张ECN单据中尚未完成节点的具体审核人。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_APPROVAL_REASSIGN_PERMISSION)
    return can(
        _service(user_service),
        current_user,
        ECN_APPROVAL_REASSIGN_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=(),
    )


def can_execute_ecn_assistant_stage(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以处理资料准备和系统内资料落盘阶段。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_EXECUTION_ASSISTANT_PERMISSION)
    return can(
        _service(user_service),
        current_user,
        ECN_EXECUTION_ASSISTANT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=(),
    )


def can_edit_ecn_material_codes(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断是否可以在评审通过后的专门阶段补充物料料号。"""
    if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True:
        return _snapshot_permission(access_snapshot, current_user, ECN_MATERIAL_CODE_EDIT_PERMISSION)
    return can(
        _service(user_service),
        current_user,
        ECN_MATERIAL_CODE_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=(),
    )


def has_ecn_material_execution_qualification(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否具备处理已分配物料追溯责任项的基础资格。"""
    if not _database_mode(user_service):
        return True
    return can(
        _service(user_service),
        current_user,
        ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=(),
    )


ECN_EXECUTION_RESPONSIBILITY_PERMISSIONS = {
    "研发助理": ECN_EXECUTION_ASSISTANT_PERMISSION,
    "采购": ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
    "采购（量产）": ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
    "PMC": ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
    "生产经理": ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
    "销售主管": ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
}


def _ecn_identity_title(access_snapshot: dict[str, Any], username: str) -> str:
    users = access_snapshot.get("users", {})
    memberships = access_snapshot.get("memberships", {})
    user = users.get(username, {}) if isinstance(users, dict) else {}
    membership = memberships.get(username, {}) if isinstance(memberships, dict) else {}
    values = [
        str(user.get("role") or "") if isinstance(user, dict) else "",
        str(membership.get("position_name") or "") if isinstance(membership, dict) else "",
    ]
    return " / ".join(dict.fromkeys(value for value in values if value))


def _ecn_identity_levels(access_snapshot: dict[str, Any], username: str) -> set[str]:
    users = access_snapshot.get("users", {})
    memberships = access_snapshot.get("memberships", {})
    user = users.get(username, {}) if isinstance(users, dict) else {}
    membership = memberships.get(username, {}) if isinstance(memberships, dict) else {}
    return {
        value
        for value in (
            str(user.get("role") or "").strip() if isinstance(user, dict) else "",
            str(membership.get("position_name") or "").strip() if isinstance(membership, dict) else "",
        )
        if value
    }


def _is_available_material_manager(access_snapshot: dict[str, Any], username: str) -> bool:
    return bool(
        _snapshot_active_user(access_snapshot, username)
        and _snapshot_permission(access_snapshot, username, ECN_VIEW_PERMISSION)
        and has_ecn_material_execution_permission(username, access_snapshot=access_snapshot)
    )


def _walk_ecn_direct_managers(
    access_snapshot: dict[str, Any],
    seed_usernames: list[str],
) -> list[str]:
    """逐级查找第一层可处理的直属上级，不跨过可用的当前上级继续扩大范围。"""
    memberships = access_snapshot.get("memberships", {})
    if not isinstance(memberships, dict):
        return []
    frontier = list(dict.fromkeys(name for name in seed_usernames if name))
    visited = set(frontier)
    while frontier:
        managers: list[str] = []
        for username in frontier:
            membership = memberships.get(username, {})
            manager = str(membership.get("manager_username") or "") if isinstance(membership, dict) else ""
            if manager and manager not in visited:
                visited.add(manager)
                managers.append(manager)
        if not managers:
            return []
        available = [name for name in managers if _is_available_material_manager(access_snapshot, name)]
        if available:
            return available
        frontier = managers
    return []


def _matches_responsibility_level(
    access_snapshot: dict[str, Any],
    username: str,
    responsible_key: str,
    responsible_roles: list[str],
) -> bool:
    levels = _ecn_identity_levels(access_snapshot, username)
    # 销售主管历史任务曾把总监写进 roles；当前责任键必须优先，不能借旧列表越级。
    expected = (
        {"销售主管"}
        if responsible_key == "销售主管"
        else {value for value in (responsible_roles or [responsible_key]) if value}
    )
    # “采购”应能匹配“采购（量产）/采购专员（量产）”等细分岗位；关键词本身仍保持
    # 层级含义，所以“销售主管”不会误中“销售总监”。
    return any(keyword in level for keyword in expected for level in levels)


def resolve_ecn_material_spec_responsibility(
    spec: Any,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按主任职的直属上级链解析当前责任人；只停在首个可处理层级。"""
    if not isinstance(spec, dict):
        return {}
    resolved = dict(spec)
    if not _database_mode(user_service) or spec.get("manual_assignment") is True:
        return resolved
    snapshot = access_snapshot or build_ecn_access_snapshot(user_service)
    if snapshot.get("database_mode") is not True:
        return resolved
    raw_users = [str(value).strip() for value in spec.get("users", []) if str(value).strip()]
    responsible_type = str(spec.get("responsible_type") or "role")
    responsible_key = str(spec.get("responsible_key") or "").strip()
    responsible_roles = [str(value).strip() for value in spec.get("roles", []) if str(value).strip()]

    if responsible_type == "workflow_users":
        required_permission = str(spec.get("required_permission_code") or "").strip()
        available_users = [
            username
            for username in raw_users
            if _snapshot_active_user(snapshot, username)
            and _snapshot_permission(snapshot, username, ECN_VIEW_PERMISSION)
            and required_permission
            and _snapshot_permission(snapshot, username, required_permission)
        ]
        if available_users:
            resolved["users"] = available_users
            return resolved
        raw_position_ids = spec.get("position_ids", [])
        position_ids = {
            str(value).strip()
            for value in raw_position_ids
            if str(value).strip()
        } if isinstance(raw_position_ids, (list, tuple, set)) else set()
        memberships = snapshot.get("memberships", {})
        users = snapshot.get("users", {})
        current_position_users = [
            str(username)
            for username in users
            if isinstance(users, dict)
            and isinstance(memberships, dict)
            and isinstance(memberships.get(username), dict)
            and str(memberships[username].get("position_id") or "") in position_ids
            and _snapshot_active_user(snapshot, str(username))
            and _snapshot_permission(snapshot, str(username), ECN_VIEW_PERMISSION)
            and required_permission
            and _snapshot_permission(snapshot, str(username), required_permission)
        ]
        if current_position_users:
            original = "、".join(raw_users) or responsible_key or "原负责人"
            resolved.update(
                users=list(dict.fromkeys(current_position_users)),
                roles=[],
                label=f"{responsible_key or '流程岗位'}现负责人：{'、'.join(current_position_users)}",
                escalated_from=original,
                resolution_mode="position_successor",
            )
            return resolved
        target_users = _walk_ecn_direct_managers(snapshot, raw_users)
        if not target_users:
            resolved.update(
                responsible_type="hierarchy_users",
                users=[],
                roles=[],
                label=f"{responsible_key or '原责任层级'}无人可处理，且未找到可用直属上级",
                escalated_from=responsible_key or "原责任层级",
                resolution_mode="manager_escalation",
            )
            return resolved
        target_titles = [
            f"{name}（{_ecn_identity_title(snapshot, name) or '直属上级'}）" for name in target_users
        ]
        resolved.update(
            responsible_type="hierarchy_users",
            users=list(dict.fromkeys(target_users)),
            roles=[],
            label=(
                f"{responsible_key or '原责任层级'}不可处理，转上级：{'、'.join(target_titles)}"
            ),
            escalated_from=responsible_key or "原责任层级",
            resolution_mode="manager_escalation",
        )
        return resolved

    project_sales_available = responsible_type == "project_sales" and any(
        _snapshot_active_user(snapshot, username)
        and _snapshot_permission(snapshot, username, ECN_VIEW_PERMISSION)
        and _snapshot_permission(snapshot, username, ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION)
        for username in raw_users
    )
    if project_sales_available:
        return resolved

    users = snapshot.get("users", {})
    all_usernames = list(users) if isinstance(users, dict) else []
    level_users: list[str] = []
    stayed_at_level = False
    if responsible_type != "project_sales":
        permission_code = ECN_EXECUTION_RESPONSIBILITY_PERMISSIONS.get(responsible_key, "")
        level_users = [
            username
            for username in all_usernames
            if _matches_responsibility_level(snapshot, username, responsible_key, responsible_roles)
            and _snapshot_active_user(snapshot, username)
            and _snapshot_permission(snapshot, username, ECN_VIEW_PERMISSION)
            and (
                _is_available_material_manager(snapshot, username)
                if responsible_key == "销售主管"
                else bool(permission_code) and _snapshot_permission(snapshot, username, permission_code)
            )
        ]
        if level_users:
            target_users = level_users
            stayed_at_level = True
        else:
            level_seeds = [
                username
                for username in all_usernames
                if _matches_responsibility_level(snapshot, username, responsible_key, responsible_roles)
            ]
            target_users = _walk_ecn_direct_managers(snapshot, level_seeds)
            if not target_users and responsible_key == "销售主管":
                target_users = [
                    username
                    for username in all_usernames
                    if "销售总监" in _ecn_identity_title(snapshot, username)
                    and _is_available_material_manager(snapshot, username)
                ]
    else:
        target_users = _walk_ecn_direct_managers(snapshot, raw_users)

    if not target_users and responsible_type == "project_sales":
        return resolved
    if not target_users:
        original = responsible_key or "原责任层级"
        resolved.update(
            responsible_type="hierarchy_users",
            users=[],
            roles=[],
            label=f"{original}层级无人可处理，且未找到可用直属上级",
            escalated_from=original,
        )
        return resolved
    target_users = list(dict.fromkeys(target_users))
    target_titles = [
        f"{name}（{_ecn_identity_title(snapshot, name) or '直属上级'}）" for name in target_users
    ]
    original = "、".join(raw_users) or responsible_key or "原责任层级"
    project = str(spec.get("project") or "").strip()
    prefix = f"{project} · " if project else ""
    target_text = "、".join(target_users) if stayed_at_level else "、".join(target_titles)
    missing_project_sales = responsible_key == "销售主管" and "::项目销售::" in str(spec.get("key") or "")
    if stayed_at_level:
        label = (
            f"{prefix}项目销售未识别，转销售主管：{target_text}"
            if missing_project_sales
            else f"{prefix}{responsible_key}：{target_text}"
        )
    else:
        label = f"{prefix}{original}不可处理，转上级：{target_text}"
    resolved.update(
        responsible_type="hierarchy_users",
        users=target_users,
        roles=[],
        label=label,
        escalated_from=original,
        resolution_mode="current_level" if stayed_at_level else "manager_escalation",
    )
    return resolved


def can_confirm_ecn_material_spec(
    spec: Any,
    current_role: object,
    current_user: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    """判断用户能否处理一条已经固化到 ECN 的物料追溯责任项。"""
    if not isinstance(spec, dict):
        return False
    if _database_mode(user_service) and str(spec.get("responsible_type") or "") != "hierarchy_users":
        spec = resolve_ecn_material_spec_responsibility(
            spec,
            user_service=user_service,
            access_snapshot=access_snapshot,
        )
    responsible_users = {str(value).strip() for value in spec.get("users", []) if str(value).strip()}
    responsible_roles = [str(value).strip() for value in spec.get("roles", []) if str(value).strip()]
    if not _database_mode(user_service):
        return current_user in responsible_users or role_matches_keywords(
            str(current_role or ""),
            responsible_roles,
        )

    service = _service(user_service)
    responsible_type = str(spec.get("responsible_type") or "role")
    responsible_key = str(spec.get("responsible_key") or "").strip()
    if responsible_type == "hierarchy_users":
        return bool(
            current_user in responsible_users
            and has_ecn_material_execution_permission(
                current_user, user_service=service, access_snapshot=access_snapshot
            )
        )
    if responsible_type in {"project_sales", "assigned_user", "workflow_users"} and responsible_users:
        if current_user not in responsible_users:
            return False
        if responsible_type == "assigned_user":
            return has_ecn_material_execution_permission(
                current_user, user_service=service, access_snapshot=access_snapshot
            )
        required_permission = (
            str(spec.get("required_permission_code") or "")
            if responsible_type == "workflow_users"
            else ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION
        )
        return current_user in responsible_users and (
            _snapshot_permission(access_snapshot, current_user, required_permission)
            if access_snapshot is not None
            else can(service, current_user, required_permission)
        )
    permission_code = ECN_EXECUTION_RESPONSIBILITY_PERMISSIONS.get(responsible_key, "")
    return bool(
        permission_code
        and (
            _snapshot_permission(access_snapshot, current_user, permission_code)
            if access_snapshot is not None
            else can(service, current_user, permission_code)
        )
    )


def has_ecn_material_execution_permission(
    username: str, *, user_service=None, access_snapshot: dict[str, Any] | None = None
) -> bool:
    """人工改派仍要求接收人具备一项稳定的ECN执行权限。"""
    service = _service(user_service)
    return any(
        (
            _snapshot_permission(access_snapshot, username, permission_code)
            if access_snapshot is not None
            else can(service, username, permission_code)
        )
        for permission_code in (
            ECN_EXECUTION_ASSISTANT_PERMISSION,
            ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
            ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
            ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
            ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
            ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
        )
    )


def is_active_ecn_user(
    username: str,
    *,
    user_service=None,
    require_material_permission: bool = False,
    access_snapshot: dict[str, Any] | None = None,
) -> bool:
    service = _service(user_service)
    if service is None:
        return False
    use_snapshot = isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True
    info = _snapshot_active_user(access_snapshot, username) if use_snapshot else service.get_user(username)
    if not isinstance(info, dict) or info.get("status", "active") != "active":
        return False
    role = str(info.get("role") or "")
    can_view = (
        _snapshot_permission(access_snapshot, username, ECN_VIEW_PERMISSION)
        if use_snapshot
        else can_view_ecn(role, username, user_service=service)
    )
    if not can_view:
        return False
    return not require_material_permission or has_ecn_material_execution_permission(
        username, user_service=service, access_snapshot=access_snapshot
    )


def is_ecn_material_spec_orphaned(
    spec: Any, *, user_service=None, access_snapshot: dict[str, Any] | None = None
) -> bool:
    """当前可执行责任项没有任何在职且具备权限的处理人时返回True。"""
    if not isinstance(spec, dict) or spec.get("available") is not True:
        return False
    service = _service(user_service)
    if service is None:
        return False
    snapshot = access_snapshot or build_ecn_access_snapshot(service)
    use_snapshot = snapshot.get("database_mode") is True
    users = snapshot.get("users", {})
    for username, info in users.items() if isinstance(users, dict) else []:
        if not isinstance(info, dict) or info.get("status", "active") != "active":
            continue
        role = str(info.get("role") or "")
        can_view = (
            _snapshot_permission(snapshot, username, ECN_VIEW_PERMISSION)
            if use_snapshot
            else can_view_ecn(role, username, user_service=service)
        )
        if can_view and can_confirm_ecn_material_spec(
            spec,
            role,
            username,
            user_service=service,
            access_snapshot=snapshot if use_snapshot else None,
        ):
            return False
    return True


def get_ecn_execution_assignment_issues(
    ecn_data: Any, *, user_service=None, access_snapshot: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """列出因停用、离职或权限撤销而无人可处理的执行待办。"""
    if not isinstance(ecn_data, dict):
        return []
    workflow = ecn_data.get("workflow", {})
    execution = ecn_data.get("execution_info", {})
    if (
        not isinstance(workflow, dict)
        or workflow.get("current_state") != ECNState.ECN_EXECUTING
        or not isinstance(execution, dict)
    ):
        return []
    snapshot = access_snapshot or build_ecn_access_snapshot(user_service)
    issues: list[dict[str, str]] = []
    for key, item in get_ecn_special_confirmations(execution).items():
        assignee = str(item.get("assignee") or "").strip()
        if assignee and item.get("confirmed") is not True and not is_active_ecn_user(
            assignee, user_service=user_service, access_snapshot=snapshot
        ):
            issues.append({"kind": "special", "item_id": key, "key": key, "owner": assignee})
    if execution.get("stage") != ECN_EXECUTION_STAGE_MATERIAL:
        return issues
    change_items = {
        str(item.get("item_id")): item
        for item in ecn_data.get("change_items", [])
        if isinstance(item, dict) and item.get("item_id")
    }
    material_confirmations = execution.get("material_confirmations", {})
    if not isinstance(material_confirmations, dict):
        return issues
    for item_id, entry in material_confirmations.items():
        for raw_spec in get_ecn_material_execution_specs(change_items.get(str(item_id), {}), entry):
            spec = resolve_ecn_material_spec_responsibility(
                raw_spec,
                user_service=user_service,
                access_snapshot=snapshot,
            )
            if is_ecn_material_spec_orphaned(
                spec, user_service=user_service, access_snapshot=snapshot
            ):
                raw_users = spec.get("users", [])
                users = (
                    [str(value) for value in raw_users if str(value).strip()]
                    if isinstance(raw_users, (list, tuple, set))
                    else []
                )
                issues.append(
                    {
                        "kind": "material",
                        "item_id": str(item_id),
                        "key": str(spec.get("key") or ""),
                        "owner": "、".join(users) or str(spec.get("label") or "原责任岗位"),
                    }
                )
    return issues


def can_delete_ecn(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以永久删除 ECN 单据。"""
    return can(
        _service(user_service),
        current_user,
        ECN_DELETE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=("admin",),
    )


def can_view_ecn_scheme_non_image_file(
    item: Any,
    current_role: object,
    current_user: str,
    overview_config_flat: Any = None,
    *,
    user_service=None,
) -> bool:
    """按方案分类判断非图片附件查看权限。"""
    service = _service(user_service)
    if not _database_mode(service):
        return False

    category = classify_ecn_change_item(item)
    if category == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
        configs = overview_config_flat if isinstance(overview_config_flat, dict) else {}
        config = configs.get(item.get("label"), {}) if isinstance(item, dict) else {}
        return bool(
            isinstance(config, dict)
            and can_view_overview_item(
                config,
                current_role,
                current_user,
                user_service=service,
            )
        )
    if category == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT:
        change_type = str(item.get("change_type") or "") if isinstance(item, dict) else ""
        permission_code = ecn_ordinary_file_view_permission(change_type)
        return bool(permission_code and can(service, current_user, permission_code))
    return False


def is_ecn_pending_for_user(
    ecn_data: Any,
    current_user: str,
    current_role: str,
    *,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
    assignment_issues: list[dict[str, str]] | None = None,
) -> bool:
    """返回一张 ECN 是否属于当前用户可实际处理的待办。"""
    if not isinstance(ecn_data, dict):
        return False
    if not _database_mode(user_service) and not (
        isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True
    ):
        return False
    workflow = ecn_data.get("workflow", {}) if isinstance(ecn_data, dict) else {}
    basic_info = ecn_data.get("basic_info", {})
    if not isinstance(workflow, dict) or not isinstance(basic_info, dict):
        return False
    if workflow.get("current_state") == ECNState.ECN_EXECUTING and any(
        item.get("assignee") == current_user and item.get("confirmed") is not True
        for item in get_ecn_special_confirmations(ecn_data.get("execution_info")).values()
    ):
        return (
            _snapshot_permission(access_snapshot, current_user, ECN_VIEW_PERMISSION)
            if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True
            else can_view_ecn(current_role, current_user, user_service=user_service)
        )
    current_assignment_issues = (
        assignment_issues
        if assignment_issues is not None
        else get_ecn_execution_assignment_issues(
            ecn_data, user_service=user_service, access_snapshot=access_snapshot
        )
    )
    if (
        workflow.get("current_state") == ECNState.ECN_EXECUTING
        and current_assignment_issues
        and _can_execute_assistant_with_snapshot(
            current_role,
            current_user,
            user_service=user_service,
            access_snapshot=access_snapshot,
        )
    ):
        return True
    if (
        workflow.get("current_state") == ECNState.MATERIAL_CODE_PENDING
        and get_ecn_missing_material_code_items(ecn_data)
        and can_edit_ecn_material_codes(
            current_role,
            current_user,
            user_service=user_service,
            access_snapshot=access_snapshot,
        )
    ):
        return (
            _snapshot_permission(access_snapshot, current_user, ECN_VIEW_PERMISSION)
            if isinstance(access_snapshot, dict) and access_snapshot.get("database_mode") is True
            else can_view_ecn(current_role, current_user, user_service=user_service)
        )
    current_state = workflow.get("current_state")
    if workflow.get("current_phase") == "ECR_PHASE" and current_state == ECNState.ECR_REVIEWING:
        return is_ecr_assigned_approver(
            ecn_data,
            current_user,
            user_service=_service(user_service),
        )
    if workflow.get("current_phase") == "ECN_SCHEME_REVIEW_PHASE" and current_state == ECNState.ECN_REVIEWING:
        return is_scheme_assigned_approver(
            ecn_data,
            current_user,
            user_service=_service(user_service),
        )
    if current_state == ECNState.ECN_EXECUTING:
        execution_info = ecn_data.get("execution_info", {})
        if not isinstance(execution_info, dict):
            return False
        stage = execution_info.get("stage")
        if stage in {
            ECN_EXECUTION_STAGE_ASSISTANT,
            ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
            ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
        }:
            return _can_execute_assistant_with_snapshot(
                current_role,
                current_user,
                user_service=user_service,
                access_snapshot=access_snapshot,
            )
        if stage == ECN_EXECUTION_STAGE_MATERIAL:
            change_items = {
                str(item.get("item_id")): item
                for item in ecn_data.get("change_items", [])
                if isinstance(item, dict) and item.get("item_id")
            }
            material_confirmations = execution_info.get("material_confirmations", {})
            if not isinstance(material_confirmations, dict):
                return False
            for item_id, material_entry in material_confirmations.items():
                item = change_items.get(str(item_id), {})
                for spec in get_ecn_material_execution_specs(item, material_entry):
                    if spec.get("available") is True and can_confirm_ecn_material_spec(
                        spec,
                        current_role,
                        current_user,
                        user_service=user_service,
                        access_snapshot=access_snapshot,
                    ):
                        return True
            return False

    if current_state in {ECNState.REJECTED, ECNState.DRAFT}:
        return basic_info.get("applicant") == current_user and can_create_ecn_request(
            current_role, current_user, user_service=user_service
        )
    if current_state == ECNState.ECN_SCHEMING and get_ecn_level_code(ecn_data) == ECN_LEVEL_COMPLEX:
        items = ecn_data.get("change_items", [])
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            report = get_ecn_validation_report(item)
            if report.get("required") is not True:
                continue
            status = str(report.get("status") or "pending_upload")
            if (
                item.get("author") == current_user
                and status in {"pending_upload", "rejected"}
                and can_edit_ecn_scheme(
                    current_role,
                    current_user,
                    user_service=user_service,
                    access_snapshot=access_snapshot,
                )
            ):
                return True
            if (
                status == "pending_review"
                and item.get("author") != current_user
                and can_view_ecn_validation_report(
                    current_role,
                    current_user,
                    user_service=user_service,
                    access_snapshot=access_snapshot,
                )
                and can_approve_ecn_validation_report(
                    current_role,
                    current_user,
                    user_service=user_service,
                    access_snapshot=access_snapshot,
                )
            ):
                return True
    if is_ecn_scheme_ready_for_review(ecn_data):
        return can_submit_ecn_scheme_review(current_role, current_user, user_service=user_service)
    if current_state not in ECN_IMPACT_FOLLOWUP_STATES:
        return False

    participants = workflow.get("scheme_participants", {})
    if isinstance(participants, dict) and current_user in participants:
        participant_status = participants.get(current_user)
        status_info = ECN_PARTICIPANT_STATUS_CONFIG.get(participant_status, {})
        return bool(
            status_info.get("remind") is True
            and can_edit_ecn_scheme(current_role, current_user, user_service=user_service)
        )
    if isinstance(participants, dict) and participants:
        return False
    if is_ecn_impact_blank(ecn_data):
        return receives_ecn_initial_impact_reminder(
            current_role,
            current_user,
            user_service=user_service,
        )
    return current_user in get_ecn_impact_handlers(ecn_data) and can_edit_ecn_impact(
        current_role, current_user, user_service=user_service
    )


def get_ecn_dashboard_pending_count(
    all_ecns: Any,
    current_user: str,
    current_role: str,
    *,
    user_service=None,
) -> int:
    """统计当前用户的 ECN 待办数量。"""
    if not isinstance(all_ecns, dict):
        return 0
    return sum(
        1
        for ecn_data in all_ecns.values()
        if is_ecn_pending_for_user(
            ecn_data,
            current_user,
            current_role,
            user_service=user_service,
        )
    )
