"""ECN 模块接入通用审批流程引擎的运行时适配层。"""

from __future__ import annotations

import copy
from typing import Any

from nicegui import app

from .approval_workflow import (
    advance_approval_sequence,
    create_approval_sequence_assignments,
    is_assigned_approver,
)

ECN_WORKFLOW_MODULE = "ecn"
ECN_ECR_REVIEW_EVENT = "ecr_review"
ECN_ECR_REVIEW_TASK_KEY = "ecr_review"
ECN_SCHEME_REVIEW_EVENT = "scheme_review"
ECN_SCHEME_REVIEW_TASK_KEY = "scheme_review"
ECN_ECR_ASSIGNMENT_KEY = "ecr_workflow_assignment"
ECN_SCHEME_ASSIGNMENT_KEY = "scheme_workflow_assignment"


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def is_ecn_database_workflow_enabled(*, user_service=None) -> bool:
    """数据库身份模式下启用管理员可配置的 ECN 审批流程。"""
    service = _service(user_service)
    return service is not None and getattr(service, "storage_mode", "legacy_excel") == "database"


def ecn_workflow_error_message(result: dict[str, Any], subject: str) -> str:
    """把流程解析错误转换成管理员可直接处理的提示。"""
    status = str(result.get("status") or "error")
    detail = str(result.get("message") or "审批流程解析失败")
    hints = {
        "missing_membership": "请先配置申请人的主部门和主岗位",
        "no_match": "请在系统管理中发布能匹配申请人的审批流程",
        "ambiguous": "请调整重复命中流程的条件或优先级",
        "no_approver": "请检查审批岗位、在职人员及ECR审批权限",
        "invalid_policy": "请修正流程节点使用的审批权限",
    }
    return f"{subject}无法提交：{detail}；{hints.get(status, '请检查系统管理中的审批流程配置')}"


def start_ecr_approval(
    ecn_id: str,
    requester_username: str,
    *,
    user_service=None,
) -> dict[str, Any]:
    """解析 ECR 流程，固化全部节点快照并激活首节点待办。"""
    service = _service(user_service)
    if not is_ecn_database_workflow_enabled(user_service=service):
        return {"status": "legacy_mode", "assignment": {}}
    return create_approval_sequence_assignments(
        service,
        module=ECN_WORKFLOW_MODULE,
        event=ECN_ECR_REVIEW_EVENT,
        entity_id=str(ecn_id),
        task_key=ECN_ECR_REVIEW_TASK_KEY,
        requester_username=requester_username,
    )


def start_scheme_approval(
    ecn_id: str,
    requester_username: str,
    *,
    user_service=None,
) -> dict[str, Any]:
    """解析 ECN 方案评审流程并激活首节点待办。"""
    service = _service(user_service)
    if not is_ecn_database_workflow_enabled(user_service=service):
        return {"status": "legacy_mode", "assignment": {}}
    return create_approval_sequence_assignments(
        service,
        module=ECN_WORKFLOW_MODULE,
        event=ECN_SCHEME_REVIEW_EVENT,
        entity_id=str(ecn_id),
        task_key=ECN_SCHEME_REVIEW_TASK_KEY,
        requester_username=requester_username,
    )


def _assignment(ecn_data: Any, assignment_key: str = ECN_ECR_ASSIGNMENT_KEY) -> dict[str, Any]:
    if not isinstance(ecn_data, dict):
        return {}
    workflow = ecn_data.get("workflow", {})
    if not isinstance(workflow, dict):
        return {}
    assignment = workflow.get(assignment_key, {})
    return assignment if isinstance(assignment, dict) else {}


def get_ecr_pending_usernames(ecn_data: Any, *, user_service=None) -> list[str]:
    """返回当前 ECR 节点尚未处理的具体用户名。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data)
    task_key = str(assignment.get("current_task_key") or "")
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    if not service or not task_key or not ecn_id:
        return []
    return service.list_pending_assignment_usernames(
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        task_key=task_key,
    )


def get_scheme_pending_usernames(ecn_data: Any, *, user_service=None) -> list[str]:
    """返回当前 ECN 方案评审节点尚未处理的具体用户名。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data, ECN_SCHEME_ASSIGNMENT_KEY)
    task_key = str(assignment.get("current_task_key") or "")
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    if not service or not task_key or not ecn_id:
        return []
    return service.list_pending_assignment_usernames(
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        task_key=task_key,
    )


def is_ecr_assigned_approver(
    ecn_data: Any,
    username: str,
    *,
    user_service=None,
) -> bool:
    """判断用户是否仍有当前 ECR 节点的具体审批待办。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data)
    task_key = str(assignment.get("current_task_key") or "")
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    if not service or not task_key or not ecn_id or not username:
        return False
    return is_assigned_approver(
        service,
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        task_key=task_key,
        username=username,
    )


def is_scheme_assigned_approver(
    ecn_data: Any,
    username: str,
    *,
    user_service=None,
) -> bool:
    """判断用户是否仍有当前 ECN 方案评审节点的具体待办。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data, ECN_SCHEME_ASSIGNMENT_KEY)
    task_key = str(assignment.get("current_task_key") or "")
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    if not service or not task_key or not ecn_id or not username:
        return False
    return is_assigned_approver(
        service,
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        task_key=task_key,
        username=username,
    )


def advance_ecr_approval(
    ecn_data: Any,
    username: str,
    *,
    user_service=None,
) -> dict[str, Any]:
    """完成当前人的 ECR 待办，并在节点结束后激活下一节点。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data)
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    if not service or not assignment or not ecn_id:
        return {"status": "invalid_assignment", "message": "ECR审批快照不存在"}
    return advance_approval_sequence(
        service,
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        assignment=assignment,
        username=username,
    )


def advance_scheme_approval(
    ecn_data: Any,
    username: str,
    *,
    user_service=None,
) -> dict[str, Any]:
    """完成当前人的方案评审待办，并按配置推进后续节点。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data, ECN_SCHEME_ASSIGNMENT_KEY)
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    if not service or not assignment or not ecn_id:
        return {"status": "invalid_assignment", "message": "ECN方案评审快照不存在"}
    return advance_approval_sequence(
        service,
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        assignment=assignment,
        username=username,
    )


def finish_ecr_approval(
    ecn_data: Any,
    username: str,
    *,
    rejected: bool,
    user_service=None,
) -> dict[str, Any]:
    """处理 ECR 同意或驳回，并返回应写回单据的流程快照。"""
    if not rejected:
        return advance_ecr_approval(ecn_data, username, user_service=user_service)

    return _reject_approval(
        ecn_data,
        username,
        assignment_key=ECN_ECR_ASSIGNMENT_KEY,
        subject="ECR",
        user_service=user_service,
    )


def finish_scheme_approval(
    ecn_data: Any,
    username: str,
    *,
    rejected: bool,
    user_service=None,
) -> dict[str, Any]:
    """处理 ECN 方案同意或驳回，并返回应写回单据的流程快照。"""
    if not rejected:
        return advance_scheme_approval(ecn_data, username, user_service=user_service)
    return _reject_approval(
        ecn_data,
        username,
        assignment_key=ECN_SCHEME_ASSIGNMENT_KEY,
        subject="ECN方案",
        user_service=user_service,
    )


def _reject_approval(
    ecn_data: Any,
    username: str,
    *,
    assignment_key: str,
    subject: str,
    user_service=None,
) -> dict[str, Any]:
    """校验并终止当前审批节点，生成可审计的驳回快照。"""

    service = _service(user_service)
    assignment = _assignment(ecn_data, assignment_key)
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    task_key = str(assignment.get("current_task_key") or "")
    if not service or not assignment or not ecn_id or not task_key:
        return {"status": "invalid_assignment", "message": f"{subject}审批快照不存在"}
    if username.casefold() not in {
        value.casefold()
        for value in service.list_pending_assignment_usernames(
            module=ECN_WORKFLOW_MODULE,
            entity_id=ecn_id,
            task_key=task_key,
        )
    }:
        return {"status": "not_assigned", "message": f"当前用户没有该{subject}节点的有效待办"}

    nodes = assignment.get("nodes", [])
    try:
        node_index = int(assignment.get("current_node_index", 0))
        current_node = nodes[node_index]
    except (TypeError, ValueError, IndexError):
        return {"status": "invalid_assignment", "message": f"当前{subject}审批节点无效"}
    required_permission = str(current_node.get("required_permission_code") or "")
    if not service.has_permission(username, required_permission):
        return {"status": "forbidden", "message": f"当前用户缺少该节点要求的{subject}审批权限"}

    service.replace_work_assignments(
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        task_key=task_key,
        assignee_usernames=[],
        source_policy_code=str(assignment.get("source_policy_code") or ""),
    )
    snapshot = copy.deepcopy(assignment)
    snapshot["status"] = "rejected"
    snapshot["rejected_by"] = username
    snapshot.pop("current_task_key", None)
    snapshot_nodes = snapshot.get("nodes", [])
    if isinstance(snapshot_nodes, list) and 0 <= node_index < len(snapshot_nodes):
        snapshot_node = snapshot_nodes[node_index]
        if isinstance(snapshot_node, dict):
            snapshot_node["status"] = "rejected"
            snapshot_node["rejected_by"] = username
    return {"status": "rejected", "message": f"{subject}已驳回", "assignment": snapshot}


def cancel_ecr_approval(ecn_data: Any, *, user_service=None) -> None:
    """撤回、作废或落盘失败时终止当前 ECR 待办。"""
    _cancel_approval(ecn_data, ECN_ECR_ASSIGNMENT_KEY, user_service=user_service)


def cancel_scheme_approval(ecn_data: Any, *, user_service=None) -> None:
    """方案评审落盘失败时终止本次已创建的待办。"""
    _cancel_approval(ecn_data, ECN_SCHEME_ASSIGNMENT_KEY, user_service=user_service)


def _cancel_approval(ecn_data: Any, assignment_key: str, *, user_service=None) -> None:
    """终止指定审批快照的当前节点待办。"""
    service = _service(user_service)
    assignment = _assignment(ecn_data, assignment_key)
    ecn_id = str(ecn_data.get("ecn_id") or "") if isinstance(ecn_data, dict) else ""
    task_key = str(assignment.get("current_task_key") or "")
    if not service or not ecn_id or not task_key:
        return
    service.replace_work_assignments(
        module=ECN_WORKFLOW_MODULE,
        entity_id=ecn_id,
        task_key=task_key,
        assignee_usernames=[],
        source_policy_code=str(assignment.get("source_policy_code") or ""),
    )
