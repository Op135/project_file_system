"""ECN 模块接入通用审批流程引擎的运行时适配层。"""

from __future__ import annotations

import copy
from typing import Any

from nicegui import app

from .approval_workflow import (
    ECN_EXECUTION_EVENT_BY_LEVEL,
    advance_approval_sequence,
    approval_sequence_node_task_key,
    create_approval_sequence_assignments,
    is_assigned_approver,
    resolve_approval_workflow,
)
from .ecn_management_config import (
    ECN_LEVEL_SIMPLE,
    ECN_SCHEME_GROUP_MATERIAL,
    ECNState,
    build_ecn_execution_info,
    classify_ecn_change_item,
)
from .permission_catalog import ECN_EXECUTION_ASSISTANT_PERMISSION

ECN_WORKFLOW_MODULE = "ecn"
ECN_ECR_REVIEW_EVENT = "ecr_review"
ECN_SIMPLE_ECR_REVIEW_EVENT = "ecr_review_simple"
ECN_ECR_REVIEW_TASK_KEY = "ecr_review"
ECN_SCHEME_REVIEW_EVENT = "scheme_review"
ECN_SIMPLE_SCHEME_REVIEW_EVENT = "scheme_review_simple"
ECN_SCHEME_REVIEW_TASK_KEY = "scheme_review"
ECN_ECR_ASSIGNMENT_KEY = "ecr_workflow_assignment"
ECN_SCHEME_ASSIGNMENT_KEY = "scheme_workflow_assignment"


def _execution_workflow_task(
    level: str,
    node: dict[str, Any],
    workflow_result: dict[str, Any],
    *,
    project: str = "",
) -> tuple[str, dict[str, Any]]:
    responsible_key = str(node.get("responsible_key") or node.get("name") or "执行人").strip()
    approvers = node.get("approvers", [])
    users = [
        str(item.get("username") or "").strip()
        for item in approvers
        if isinstance(item, dict) and str(item.get("username") or "").strip()
    ]
    node_approver = node.get("approver", {})
    strategy = str(node_approver.get("strategy") or "") if isinstance(node_approver, dict) else ""
    position_ids = (
        [str(value).strip() for value in node_approver.get("position_ids", []) if str(value).strip()]
        if isinstance(node_approver, dict) and isinstance(node_approver.get("position_ids"), list)
        else []
    )
    responsible_type = "project_sales" if strategy == "project_sales" else "workflow_users"
    suffix = f"::{project}" if project else ""
    task_key = f"{level}::{responsible_key}{suffix}"
    workflow = workflow_result.get("workflow", {})
    version = workflow_result.get("version", {})
    label = (
        f"{project} · {users[0]}"
        if project and users
        else f"{project} · {responsible_key}"
        if project
        else responsible_key
    )
    workflow_code = str(workflow.get("code") or "") if isinstance(workflow, dict) else ""
    version_number = version.get("version_number") if isinstance(version, dict) else None
    return task_key, {
        "level": level,
        "responsible_key": responsible_key,
        "responsible_type": responsible_type,
        "label": label,
        "project": project,
        "stage_index": int(node.get("stage_index", 0)),
        "roles": [],
        "users": list(dict.fromkeys(users)),
        "required_permission_code": str(node.get("required_permission_code") or ""),
        "position_ids": list(dict.fromkeys(position_ids)),
        "workflow_assignment": {
            "workflow_id": str(workflow.get("workflow_id") or "") if isinstance(workflow, dict) else "",
            "workflow_code": workflow_code,
            "workflow_name": str(workflow.get("name") or "") if isinstance(workflow, dict) else "",
            "version_id": str(version.get("version_id") or "") if isinstance(version, dict) else "",
            "version_number": version_number,
            "node_key": str(node.get("node_key") or ""),
            "source_policy_code": f"{workflow_code}@{version_number}",
            "notification": copy.deepcopy(version.get("notification", {}))
            if isinstance(version, dict)
            else {},
        },
        "confirmed": False,
        "history": [],
    }


def _node_uses_project_sales_route(node: dict[str, Any]) -> bool:
    approver = node.get("approver", {})
    return bool(
        node.get("project_scoped") is True
        and isinstance(approver, dict)
        and str(approver.get("strategy") or "") == "project_sales"
    )


def _node_is_sales_supervisor_fallback(node: dict[str, Any]) -> bool:
    return bool(
        node.get("project_scoped") is True
        and str(node.get("responsible_key") or node.get("name") or "").strip() == "销售主管"
    )


def build_ecn_execution_info_from_workflows(
    change_items: Any,
    project_sales: Any,
    requester_username: str,
    *,
    user_service,
) -> dict[str, Any]:
    """按已发布数据库流程生成执行清单，并把流程版本和具体责任人随单固化。"""
    execution = build_ecn_execution_info(change_items)
    execution["assistant_users"] = user_service.list_usernames_with_permission(
        ECN_EXECUTION_ASSISTANT_PERMISSION
    )
    sales_by_project = project_sales if isinstance(project_sales, dict) else {}
    material_confirmations = execution.get("material_confirmations", {})
    source_items = change_items if isinstance(change_items, list) else []
    for item in source_items:
        if not isinstance(item, dict) or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
            continue
        item_id = str(item.get("item_id") or "")
        material_entry = material_confirmations.get(item_id) if isinstance(material_confirmations, dict) else None
        if not isinstance(material_entry, dict):
            continue
        raw_levels = item.get("traceability_levels", [])
        levels = (
            [str(value).strip() for value in raw_levels if str(value).strip()]
            if isinstance(raw_levels, list)
            else []
        )
        raw_projects = item.get("projects", [])
        projects = (
            [str(value).strip() for value in raw_projects if str(value).strip()]
            if isinstance(raw_projects, list)
            else []
        )
        disposition_measure = str(item.get("disposition_measure") or "").strip()
        disposition_instruction = (
            f"旧料处置：{disposition_measure}"
            if disposition_measure
            else ""
        )
        tasks: dict[str, dict[str, Any]] = {}
        for level in levels:
            event = ECN_EXECUTION_EVENT_BY_LEVEL.get(level)
            if not event:
                raise ValueError(f"追溯范围“{level}”尚未注册数据库执行流程")
            contexts = projects if level == "客户/在途" and projects else [""]
            for context_index, project in enumerate(contexts):
                seller = str(sales_by_project.get(project) or "").strip()
                result = resolve_approval_workflow(
                    user_service,
                    module="ecn",
                    event=event,
                    requester_username=requester_username,
                    context={
                        "project_sales_usernames": [seller]
                        if seller and seller != "未指定"
                        else []
                    },
                )
                if result.get("status") != "matched":
                    detail = str(result.get("message") or "流程解析失败")
                    raise ValueError(f"ECN执行“{level}”无法生成：{detail}")
                nodes = result.get("approval_nodes", [])
                workflow_nodes = [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []
                has_project_sales_route = any(_node_uses_project_sales_route(node) for node in workflow_nodes)
                for node in workflow_nodes:
                    if not isinstance(node, dict):
                        continue
                    project_scoped = node.get("project_scoped") is True
                    if context_index > 0 and not project_scoped:
                        continue
                    # “销售主管”是项目销售任务的上一级兜底，不再并列生成第二个确认项。
                    # 项目销售缺失、离职或无权限时，责任解析会把同一任务逐级传给主管/总监。
                    if has_project_sales_route and _node_is_sales_supervisor_fallback(node):
                        continue
                    task_key, task = _execution_workflow_task(
                        level,
                        node,
                        result,
                        project=project if project_scoped else "",
                    )
                    task["disposition_instruction"] = disposition_instruction
                    tasks[task_key] = task
        material_entry["traceability_tasks"] = tasks
    execution["workflow_source"] = "approval_workflows"
    return execution


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
    level_code: str = "",
    user_service=None,
) -> dict[str, Any]:
    """解析 ECR 流程，固化全部节点快照并激活首节点待办。"""
    service = _service(user_service)
    if not is_ecn_database_workflow_enabled(user_service=service):
        return {"status": "database_required", "message": "ECN审批必须使用数据库身份与已发布流程"}
    return create_approval_sequence_assignments(
        service,
        module=ECN_WORKFLOW_MODULE,
        event=ECN_SIMPLE_ECR_REVIEW_EVENT if level_code == ECN_LEVEL_SIMPLE else ECN_ECR_REVIEW_EVENT,
        entity_id=str(ecn_id),
        task_key=ECN_ECR_REVIEW_TASK_KEY,
        requester_username=requester_username,
    )


def start_scheme_approval(
    ecn_id: str,
    requester_username: str,
    *,
    level_code: str = "",
    scheme_author_usernames: list[str] | None = None,
    user_service=None,
) -> dict[str, Any]:
    """解析 ECN 方案评审流程并激活首节点待办。"""
    service = _service(user_service)
    if not is_ecn_database_workflow_enabled(user_service=service):
        return {"status": "database_required", "message": "ECN审批必须使用数据库身份与已发布流程"}
    return create_approval_sequence_assignments(
        service,
        module=ECN_WORKFLOW_MODULE,
        event=(
            ECN_SIMPLE_SCHEME_REVIEW_EVENT
            if level_code == ECN_LEVEL_SIMPLE
            else ECN_SCHEME_REVIEW_EVENT
        ),
        entity_id=str(ecn_id),
        task_key=ECN_SCHEME_REVIEW_TASK_KEY,
        requester_username=requester_username,
        context={"scheme_author_usernames": scheme_author_usernames or []},
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
