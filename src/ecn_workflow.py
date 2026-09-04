"""ECN 模块接入通用审批流程引擎的运行时适配层。"""

from __future__ import annotations

import copy
from typing import Any

from nicegui import app

from .approval_workflow import (
    advance_approval_sequence,
    approval_sequence_node_task_key,
    create_approval_sequence_assignments,
    is_assigned_approver,
)
from .ecn_management_config import ECNState
from .legacy_compatibility import record_legacy_compatibility_hit

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
        record_legacy_compatibility_hit(
            "legacy_workflow_route",
            "ecn.ecr_review",
            username=requester_username,
            detail="旧 Excel 模式使用 ECN 原审批角色路由",
        )
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
        record_legacy_compatibility_hit(
            "legacy_workflow_route",
            "ecn.scheme_review",
            username=requester_username,
            detail="旧 Excel 模式使用 ECN 原方案评审角色路由",
        )
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


def _snapshot_node_index(assignment: dict[str, Any]) -> int | None:
    """安全读取审批快照的当前节点序号。"""
    value = assignment.get("current_node_index", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _unique_usernames(values: Any) -> list[str]:
    """清理审批快照中的用户名并按原顺序去重。"""
    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        username = str(value or "").strip()
        normalized = username.casefold()
        if not username or normalized in seen:
            continue
        seen.add(normalized)
        result.append(username)
    return result


def _reconcile_assignment_tasks(
    service,
    *,
    ecn_id: str,
    assignment: dict[str, Any],
    active: bool,
) -> tuple[int, list[str]]:
    """按一份 ECN 审批快照恢复当前节点，并关闭被意外激活的其它节点。"""
    nodes = assignment.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return 0, [f"{ecn_id} 的审批快照没有有效节点"]
    current_index = _snapshot_node_index(assignment)
    if active and (current_index is None or not 0 <= current_index < len(nodes)):
        return 0, [f"{ecn_id} 的审批快照当前节点序号无效"]
    if active and assignment.get("status") != "pending":
        return 0, [f"{ecn_id} 正在审批，但审批快照状态不是 pending"]

    users = service.load_users()
    active_usernames = {
        str(username).casefold(): str(username)
        for username, info in users.items()
        if isinstance(info, dict) and info.get("status", "active") == "active"
    }
    base_task_key = str(assignment.get("base_task_key") or "").strip()
    source_policy_code = str(assignment.get("source_policy_code") or "").strip()
    if not base_task_key:
        return 0, [f"{ecn_id} 的审批快照缺少基础待办编码"]

    repaired = 0
    warnings: list[str] = []
    for index, raw_node in enumerate(nodes):
        if not isinstance(raw_node, dict):
            warnings.append(f"{ecn_id} 的第 {index + 1} 个审批节点无效")
            continue
        try:
            task_key = approval_sequence_node_task_key(base_task_key, raw_node)
        except (TypeError, ValueError, OverflowError):
            warnings.append(f"{ecn_id} 的第 {index + 1} 个审批节点序号无效")
            continue
        expected_usernames: list[str] = []
        inactive_usernames: list[str] = []
        if active and index == current_index and assignment.get("status") == "pending":
            approved = {
                username.casefold()
                for username in _unique_usernames(raw_node.get("approved_usernames"))
            }
            for username in _unique_usernames(raw_node.get("assignee_usernames")):
                normalized = username.casefold()
                if normalized in approved:
                    continue
                canonical_username = active_usernames.get(normalized)
                if canonical_username:
                    expected_usernames.append(canonical_username)
                else:
                    inactive_usernames.append(username)
            if inactive_usernames:
                warnings.append(
                    f"{ecn_id} 当前审批节点包含已停用人员：{'、'.join(inactive_usernames)}"
                )

        actual_usernames = service.list_pending_assignment_usernames(
            module=ECN_WORKFLOW_MODULE,
            entity_id=ecn_id,
            task_key=task_key,
        )
        actual_set = {username.casefold() for username in actual_usernames}
        expected_set = {username.casefold() for username in expected_usernames}
        if actual_set == expected_set and not inactive_usernames:
            continue
        service.replace_work_assignments(
            module=ECN_WORKFLOW_MODULE,
            entity_id=ecn_id,
            task_key=task_key,
            assignee_usernames=expected_usernames,
            source_policy_code=source_policy_code,
        )
        repaired += 1
    return repaired, warnings


def reconcile_ecn_work_assignments(
    all_ecns: Any,
    *,
    user_service=None,
) -> dict[str, Any]:
    """以 ECN 单据快照为准校准审批待办，修复两套存储分步写入造成的不一致。"""
    service = _service(user_service)
    if service is None or not is_ecn_database_workflow_enabled(user_service=service):
        return {
            "status": "skipped",
            "scanned": 0,
            "repaired": 0,
            "orphaned": 0,
            "warnings": [],
        }
    if not isinstance(all_ecns, dict):
        return {
            "status": "invalid_data",
            "scanned": 0,
            "repaired": 0,
            "orphaned": 0,
            "warnings": [],
        }

    repaired = 0
    orphaned = 0
    scanned = 0
    warnings: list[str] = []
    existing_ecn_ids: set[str] = set()
    for source_ecn_id, ecn_data in all_ecns.items():
        if not isinstance(ecn_data, dict):
            continue
        ecn_id = str(ecn_data.get("ecn_id") or source_ecn_id).strip()
        if ecn_id:
            existing_ecn_ids.add(ecn_id)
        workflow = ecn_data.get("workflow")
        if not ecn_id or not isinstance(workflow, dict):
            continue
        scanned += 1
        current_phase = str(workflow.get("current_phase") or "")
        current_state = str(workflow.get("current_state") or "")
        active_assignment_key = ""
        if current_phase == "ECR_PHASE" and current_state == ECNState.ECR_REVIEWING:
            active_assignment_key = ECN_ECR_ASSIGNMENT_KEY
        elif (
            current_phase == "ECN_SCHEME_REVIEW_PHASE"
            and current_state == ECNState.ECN_REVIEWING
        ):
            active_assignment_key = ECN_SCHEME_ASSIGNMENT_KEY

        for assignment_key in (ECN_ECR_ASSIGNMENT_KEY, ECN_SCHEME_ASSIGNMENT_KEY):
            assignment = workflow.get(assignment_key)
            if not isinstance(assignment, dict) or not assignment:
                continue
            try:
                repaired_count, assignment_warnings = _reconcile_assignment_tasks(
                    service,
                    ecn_id=ecn_id,
                    assignment=assignment,
                    active=assignment_key == active_assignment_key,
                )
            except Exception as exc:
                warnings.append(f"{ecn_id} 的审批待办校准失败：{exc}")
                continue
            repaired += repaired_count
            warnings.extend(assignment_warnings)

    # 首次创建单据时若进程在待办生成后、ECN落盘前中断，会留下没有业务实体的孤儿待办。
    try:
        pending_refs = service.list_pending_work_assignment_refs(
            module=ECN_WORKFLOW_MODULE,
        )
        for reference in pending_refs:
            entity_id = str(reference.get("entity_id") or "").strip()
            task_key = str(reference.get("task_key") or "").strip()
            if not entity_id or not task_key or entity_id in existing_ecn_ids:
                continue
            orphaned += service.supersede_pending_work_assignments(
                module=ECN_WORKFLOW_MODULE,
                entity_id=entity_id,
                task_key=task_key,
            )
    except Exception as exc:
        warnings.append(f"ECN孤立审批待办扫描失败：{exc}")

    return {
        "status": "repaired" if repaired or orphaned else "unchanged",
        "scanned": scanned,
        "repaired": repaired,
        "orphaned": orphaned,
        "warnings": warnings,
    }


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
