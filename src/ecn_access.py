"""ECN 工程变更模块的稳定权限判断入口。"""

from __future__ import annotations

from typing import Any

from nicegui import app

from .access_control import can
from .ecn_management_config import (
    ECN_EXECUTION_ASSISTANT_ROLES,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    ECN_IMPACT_FOLLOWUP_STATES,
    ECN_ORDINARY_DOCUMENT_FILE_VIEW_ROLES_BY_TYPE,
    ECN_PARTICIPANT_STATUS_CONFIG,
    ECN_SCHEME_INITIATOR_ROLES,
    ECN_SCHEME_WRITER_ROLES,
    can_view_ecn_scheme_non_image_file as can_view_legacy_ecn_scheme_non_image_file,
    classify_ecn_change_item,
    get_ecn_impact_handlers,
    get_ecn_material_execution_specs,
    is_ecn_impact_blank,
    role_matches_keywords,
    is_ecn_scheme_ready_for_review,
    ECNState,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    is_ecn_pending_for_user as is_legacy_ecn_pending_for_user,
)
from .permission_catalog import (
    ECN_CREATE_PERMISSION,
    ECN_DELETE_PERMISSION,
    ECN_ECR_APPROVE_PERMISSION,
    ECN_EXECUTION_ASSISTANT_PERMISSION,
    ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
    ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
    ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
    ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
    ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
    ECN_IMPACT_EDIT_PERMISSION,
    ECN_IMPACT_INITIAL_REMINDER_PERMISSION,
    ECN_SCHEME_APPROVE_PERMISSION,
    ECN_SCHEME_EDIT_PERMISSION,
    ECN_SCHEME_REVIEW_SUBMIT_PERMISSION,
    ECN_VIEW_PERMISSION,
    ecn_ordinary_file_view_permission,
)
from .project_overview_access import can_view_overview_item
from .ecn_workflow import is_ecr_assigned_approver, is_scheme_assigned_approver


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def _database_mode(user_service=None) -> bool:
    service = _service(user_service)
    return service is not None and getattr(service, "storage_mode", "legacy_excel") == "database"


def _matched_legacy_role(current_role: object, keywords: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """把旧关键词命中转换为权限兼容层要求的精确角色集合。"""
    role = str(current_role or "").strip()
    return (role,) if role_matches_keywords(role, list(keywords)) else ()


def can_view_ecn(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以进入并查看 ECN 工程变更。"""
    return can(
        _service(user_service),
        current_user,
        ECN_VIEW_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=None,
    )


def can_create_ecn_request(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以新建、保存并提交本人的 ECR 申请。"""
    return can(
        _service(user_service),
        current_user,
        ECN_CREATE_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=None,
    )


def can_edit_ecn_impact(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以维护 ECN 影响评估。"""
    return can(
        _service(user_service),
        current_user,
        ECN_IMPACT_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=_matched_legacy_role(current_role, ECN_SCHEME_WRITER_ROLES),
    )


def receives_ecn_initial_impact_reminder(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断是否接收尚无人认领的 ECN 影响评估兜底提醒。"""
    from .ecn_management_config import ECN_IMPACT_INITIAL_REMINDER_ROLES

    return can(
        _service(user_service),
        current_user,
        ECN_IMPACT_INITIAL_REMINDER_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=_matched_legacy_role(current_role, ECN_IMPACT_INITIAL_REMINDER_ROLES),
    )


def can_edit_ecn_scheme(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以编写并确认本人负责的 ECN 方案。"""
    return can(
        _service(user_service),
        current_user,
        ECN_SCHEME_EDIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=_matched_legacy_role(current_role, ECN_SCHEME_WRITER_ROLES),
    )


def can_submit_ecn_scheme_review(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以发起 ECN 方案评审。"""
    return can(
        _service(user_service),
        current_user,
        ECN_SCHEME_REVIEW_SUBMIT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=_matched_legacy_role(current_role, ECN_SCHEME_INITIATOR_ROLES),
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


def can_execute_ecn_assistant_stage(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断是否可以处理资料准备和系统内资料落盘阶段。"""
    return can(
        _service(user_service),
        current_user,
        ECN_EXECUTION_ASSISTANT_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=_matched_legacy_role(current_role, ECN_EXECUTION_ASSISTANT_ROLES),
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
    "PMC": ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
    "生产经理": ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
    "销售主管": ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
}


def can_confirm_ecn_material_spec(
    spec: Any,
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """判断用户能否处理一条已经固化到 ECN 的物料追溯责任项。"""
    if not isinstance(spec, dict):
        return False
    responsible_users = {
        str(value).strip()
        for value in spec.get("users", [])
        if str(value).strip()
    }
    responsible_roles = [
        str(value).strip()
        for value in spec.get("roles", [])
        if str(value).strip()
    ]
    if not _database_mode(user_service):
        return current_user in responsible_users or role_matches_keywords(
            str(current_role or ""),
            responsible_roles,
        )

    service = _service(user_service)
    responsible_type = str(spec.get("responsible_type") or "role")
    responsible_key = str(spec.get("responsible_key") or "").strip()
    if responsible_type == "project_sales" and responsible_users:
        return current_user in responsible_users and can(
            service,
            current_user,
            ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
        )
    permission_code = ECN_EXECUTION_RESPONSIBILITY_PERMISSIONS.get(responsible_key, "")
    return bool(permission_code and can(service, current_user, permission_code))


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
        return can_view_legacy_ecn_scheme_non_image_file(
            item,
            current_role,
            overview_config_flat,
            ECN_ORDINARY_DOCUMENT_FILE_VIEW_ROLES_BY_TYPE,
        )

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
) -> bool:
    """返回一张 ECN 是否属于当前用户可实际处理的待办。"""
    if not isinstance(ecn_data, dict):
        return False
    workflow = ecn_data.get("workflow", {}) if isinstance(ecn_data, dict) else {}
    basic_info = ecn_data.get("basic_info", {})
    if not isinstance(workflow, dict) or not isinstance(basic_info, dict):
        return False
    if not _database_mode(user_service):
        return is_legacy_ecn_pending_for_user(ecn_data, current_user, current_role)

    current_state = workflow.get("current_state")
    if (
        workflow.get("current_phase") == "ECR_PHASE"
        and current_state == ECNState.ECR_REVIEWING
    ):
        return is_ecr_assigned_approver(
            ecn_data,
            current_user,
            user_service=_service(user_service),
        )
    if (
        workflow.get("current_phase") == "ECN_SCHEME_REVIEW_PHASE"
        and current_state == ECNState.ECN_REVIEWING
    ):
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
            return can_execute_ecn_assistant_stage(
                current_role,
                current_user,
                user_service=user_service,
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
                    ):
                        return True
            return False

    if current_state in {ECNState.REJECTED, ECNState.DRAFT}:
        return (
            basic_info.get("applicant") == current_user
            and can_create_ecn_request(current_role, current_user, user_service=user_service)
        )
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
    return (
        current_user in get_ecn_impact_handlers(ecn_data)
        and can_edit_ecn_impact(current_role, current_user, user_service=user_service)
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
