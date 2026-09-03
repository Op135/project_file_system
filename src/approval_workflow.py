"""基于稳定组织岗位和具体用户的通用审批流程解析器。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .permission_catalog import (
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
    SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
    ECN_ECR_APPROVE_PERMISSION,
    ECN_SCHEME_APPROVE_PERMISSION,
)


@dataclass(frozen=True)
class ApprovalWorkflowEventDefinition:
    module: str
    event: str
    name: str
    permission_codes: tuple[str, ...]
    supports_sequential: bool = False

    @property
    def key(self) -> str:
        return f"{self.module}:{self.event}"


APPROVAL_WORKFLOW_EVENTS = (
    ApprovalWorkflowEventDefinition(
        module="sample_issue",
        event="close_request",
        name="样品问题关闭申请",
        permission_codes=(SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,),
    ),
    ApprovalWorkflowEventDefinition(
        module="design_knowledge",
        event="knowledge_review",
        name="设计知识发布审核",
        permission_codes=(DESIGN_KNOWLEDGE_REVIEW_PERMISSION,),
    ),
    ApprovalWorkflowEventDefinition(
        module="design_knowledge",
        event="tag_review",
        name="设计知识新标签审核",
        permission_codes=(DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,),
    ),
    ApprovalWorkflowEventDefinition(
        module="project_overview",
        event="batch_change",
        name="项目概述批量变更",
        permission_codes=(PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,),
    ),
    ApprovalWorkflowEventDefinition(
        module="project_overview",
        event="correction",
        name="项目概述原记录纠错",
        permission_codes=(PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,),
    ),
    ApprovalWorkflowEventDefinition(
        module="ecn",
        event="ecr_review",
        name="ECR申请审批",
        permission_codes=(ECN_ECR_APPROVE_PERMISSION,),
        supports_sequential=True,
    ),
    ApprovalWorkflowEventDefinition(
        module="ecn",
        event="scheme_review",
        name="ECN方案评审",
        permission_codes=(ECN_SCHEME_APPROVE_PERMISSION,),
        supports_sequential=True,
    ),
)

APPROVER_STRATEGY_NAMES = {
    "position": "指定岗位",
    "direct_manager": "申请人的直属上级",
    "users": "指定人员",
    "permission": "拥有审批权限的人员",
}


def get_workflow_event_definition(module: str, event: str) -> ApprovalWorkflowEventDefinition | None:
    """返回业务代码已经注册的审批事件。"""
    normalized_module = str(module or "").strip().lower()
    normalized_event = str(event or "").strip().lower()
    return next(
        (
            item
            for item in APPROVAL_WORKFLOW_EVENTS
            if item.module == normalized_module and item.event == normalized_event
        ),
        None,
    )


def _org_unit_ids_with_descendants(org_units: list[dict[str, Any]], roots: list[str]) -> set[str]:
    """展开指定部门及全部下级部门。"""
    result = {str(value) for value in roots if str(value)}
    changed = True
    while changed:
        changed = False
        for unit in org_units:
            unit_id = str(unit.get("org_unit_id", ""))
            parent_id = str(unit.get("parent_org_unit_id", ""))
            if unit_id and parent_id in result and unit_id not in result:
                result.add(unit_id)
                changed = True
    return result


def _condition_matches(
    condition: dict[str, Any],
    membership: dict[str, Any],
    org_units: list[dict[str, Any]],
) -> bool:
    """使用稳定部门和岗位 ID 判断申请人是否命中流程条件。"""
    position_ids = {
        str(value) for value in condition.get("requester_position_ids", []) if str(value)
    }
    if position_ids and str(membership.get("position_id", "")) not in position_ids:
        return False

    org_unit_ids = [
        str(value) for value in condition.get("requester_org_unit_ids", []) if str(value)
    ]
    if org_unit_ids:
        allowed_org_ids = (
            _org_unit_ids_with_descendants(org_units, org_unit_ids)
            if condition.get("include_child_org_units", True)
            else set(org_unit_ids)
        )
        if str(membership.get("org_unit_id", "")) not in allowed_org_ids:
            return False
    return True


def _candidate_details(user_service, usernames: list[str]) -> list[dict[str, Any]]:
    """补齐审批候选人的显示名、组织岗位和企业微信绑定状态。"""
    users = user_service.load_users()
    bindings = user_service.list_wecom_bindings()
    result: list[dict[str, Any]] = []
    for username in usernames:
        user = users.get(username, {})
        if not user or user.get("status", "active") != "active":
            continue
        membership = user_service.get_primary_membership(username)
        result.append(
            {
                "user_id": user.get("user_id"),
                "username": username,
                "display_name": user.get("display_name") or username,
                "org_unit_id": membership.get("org_unit_id"),
                "org_name": membership.get("org_name", ""),
                "position_id": membership.get("position_id"),
                "position_name": membership.get("position_name", ""),
                "manager_username": membership.get("manager_username", ""),
                "wecom_bound": bool(bindings.get(username, {}).get("external_userid")),
            }
        )
    return result


def _resolve_approver_candidates(
    user_service,
    approver: dict[str, Any],
    requester_membership: dict[str, Any],
    required_permission_code: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按审批人策略解析候选人，并分离缺少审批权限的人员。"""
    strategy = str(approver.get("strategy", "")).strip().lower()
    users = user_service.load_users()
    usernames: list[str] = []

    if strategy == "direct_manager":
        manager_username = str(requester_membership.get("manager_username", "")).strip()
        if manager_username:
            usernames = [manager_username]
    elif strategy == "users":
        selected_user_ids = {str(value) for value in approver.get("user_ids", []) if str(value)}
        usernames = [
            username
            for username, user in users.items()
            if str(user.get("user_id", "")) in selected_user_ids
        ]
    elif strategy == "permission":
        permission_code = str(approver.get("permission_code") or required_permission_code)
        usernames = user_service.list_usernames_with_permission(
            permission_code,
            include_system_admin=False,
        )
    elif strategy == "position":
        selected_position_ids = {
            str(value) for value in approver.get("position_ids", []) if str(value)
        }
        org_scope = str(approver.get("org_scope", "any")).strip().lower()
        selected_org_ids = {str(value) for value in approver.get("org_unit_ids", []) if str(value)}
        if org_scope == "requester":
            selected_org_ids = {str(requester_membership.get("org_unit_id", ""))}
        elif org_scope == "any":
            selected_org_ids = set()
        for username, user in users.items():
            if user.get("status", "active") != "active":
                continue
            membership = user_service.get_primary_membership(username)
            if str(membership.get("position_id", "")) not in selected_position_ids:
                continue
            if selected_org_ids and str(membership.get("org_unit_id", "")) not in selected_org_ids:
                continue
            usernames.append(username)

    candidates = _candidate_details(user_service, list(dict.fromkeys(usernames)))
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for candidate in candidates:
        if user_service.has_permission(candidate["username"], required_permission_code):
            eligible.append(candidate)
        else:
            missing = dict(candidate)
            missing["excluded_reason"] = "缺少流程要求的审批权限"
            excluded.append(missing)
    return eligible, excluded


def _version_approval_nodes(version: dict[str, Any]) -> list[dict[str, Any]]:
    """把旧单节点版本和新串行版本统一转换为运行时节点。"""
    approval_mode = str(version.get("approval_mode") or "any").strip().lower()
    approver = version.get("approver", {})
    if not isinstance(approver, dict):
        raise ValueError("审批人规则不是有效对象")
    if approval_mode == "sequential":
        raw_nodes = approver.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("串行流程没有审批节点")
        nodes: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                raise ValueError(f"第 {index + 1} 个审批节点无效")
            node_key = str(raw_node.get("node_key") or "").strip().lower()
            if not node_key or node_key in seen_keys:
                raise ValueError("审批节点编码为空或重复")
            seen_keys.add(node_key)
            node_approver = raw_node.get("approver")
            if not isinstance(node_approver, dict):
                raise ValueError(f"节点 {node_key} 的审批人规则无效")
            node_mode = str(raw_node.get("approval_mode") or "any").strip().lower()
            if node_mode not in {"any", "all"}:
                raise ValueError(f"节点 {node_key} 的审批方式无效")
            nodes.append(
                {
                    "node_key": node_key,
                    "name": str(raw_node.get("name") or f"审批节点 {index + 1}").strip(),
                    "approval_mode": node_mode,
                    "approver": node_approver,
                    "required_permission_code": str(
                        raw_node.get("required_permission_code")
                        or version.get("required_permission_code")
                        or ""
                    ).strip().lower(),
                }
            )
        return nodes
    if approval_mode not in {"any", "all"}:
        raise ValueError("审批方式无效")
    return [
        {
            "node_key": "approval",
            "name": "审批",
            "approval_mode": approval_mode,
            "approver": approver,
            "required_permission_code": str(
                version.get("required_permission_code") or ""
            ).strip().lower(),
        }
    ]


def get_approval_workflow_editor_nodes(version: dict[str, Any]) -> list[dict[str, Any]]:
    """把任意历史版本转换为管理界面可编辑的独立节点副本。"""
    return copy.deepcopy(_version_approval_nodes(version))


def resolve_approval_workflow(
    user_service,
    *,
    module: str,
    event: str,
    requester_username: str,
) -> dict[str, Any]:
    """匹配已发布流程并解析具体审批人，不产生任何数据库写入。"""
    event_definition = get_workflow_event_definition(module, event)
    if event_definition is None:
        return {"status": "unsupported_event", "message": "业务模块尚未注册该审批事件"}
    if getattr(user_service, "storage_mode", "legacy_excel") != "database":
        return {"status": "legacy_mode", "message": "旧 Excel 模式继续使用原业务审批规则"}

    requester_membership = user_service.get_primary_membership(requester_username)
    if not requester_membership:
        return {"status": "missing_membership", "message": "申请人尚未配置主部门和主岗位"}
    org_units = user_service.list_org_units()
    workflows = [
        workflow
        for workflow in user_service.list_approval_workflows(module=module, event=event)
        if workflow.get("status") == "active" and workflow.get("active_version")
    ]
    matched = [
        (workflow, workflow["active_version"])
        for workflow in workflows
        if _condition_matches(workflow["active_version"].get("condition", {}), requester_membership, org_units)
    ]
    if not matched:
        return {
            "status": "no_match",
            "message": "没有命中已发布审批流程",
            "requester_membership": requester_membership,
        }
    matched.sort(key=lambda item: (int(item[1].get("priority", 100)), str(item[0].get("code", ""))))
    best_priority = int(matched[0][1].get("priority", 100))
    same_priority = [item for item in matched if int(item[1].get("priority", 100)) == best_priority]
    if len(same_priority) > 1:
        return {
            "status": "ambiguous",
            "message": "多条流程以相同优先级命中，请调整流程条件或优先级",
            "requester_membership": requester_membership,
            "matched_workflows": [item[0]["name"] for item in same_priority],
        }

    workflow, version = matched[0]
    workflow_summary = {
        "workflow_id": workflow["workflow_id"],
        "code": workflow["code"],
        "name": workflow["name"],
        "module": workflow["module"],
        "event": workflow["event"],
    }
    try:
        approval_nodes = _version_approval_nodes(version)
    except ValueError as exc:
        return {
            "status": "invalid_policy",
            "message": str(exc),
            "requester_membership": requester_membership,
            "workflow": workflow_summary,
            "version": version,
        }

    warnings: list[str] = []
    resolved_nodes: list[dict[str, Any]] = []
    all_excluded: list[dict[str, Any]] = []
    for index, node in enumerate(approval_nodes):
        required_permission_code = str(node["required_permission_code"])
        if required_permission_code not in event_definition.permission_codes:
            return {
                "status": "invalid_policy",
                "message": f"{node['name']}使用了当前业务事件不支持的审批权限",
                "requester_membership": requester_membership,
                "workflow": workflow_summary,
                "version": version,
            }
        eligible, excluded = _resolve_approver_candidates(
            user_service,
            node["approver"],
            requester_membership,
            required_permission_code,
        )
        for item in excluded:
            excluded_item = dict(item)
            excluded_item["node_key"] = node["node_key"]
            all_excluded.append(excluded_item)
        if excluded:
            warnings.append(f"{node['name']}有候选人缺少流程要求的审批权限")
        if eligible and any(not item.get("wecom_bound") for item in eligible):
            warnings.append(f"{node['name']}有审批人尚未绑定企业微信账号")
        if not eligible:
            return {
                "status": "no_approver",
                "message": f"流程已命中，但{node['name']}没有符合条件且拥有权限的在职审批人",
                "requester_membership": requester_membership,
                "workflow": workflow_summary,
                "version": version,
                "approval_nodes": resolved_nodes,
                "excluded_approvers": all_excluded,
                "warnings": list(dict.fromkeys(warnings)),
            }
        resolved_nodes.append(
            {
                "node_key": node["node_key"],
                "name": node["name"],
                "node_index": index,
                "approval_mode": node["approval_mode"],
                "required_permission_code": required_permission_code,
                "approvers": eligible,
            }
        )
    first_approvers = resolved_nodes[0]["approvers"] if resolved_nodes else []
    return {
        "status": "matched",
        "message": "流程匹配成功",
        "requester_membership": requester_membership,
        "workflow": workflow_summary,
        "version": version,
        # 兼容现有单节点业务：顶层审批人仍表示首节点审批人。
        "approvers": first_approvers,
        "approval_nodes": resolved_nodes,
        "excluded_approvers": all_excluded,
        "warnings": list(dict.fromkeys(warnings)),
    }


def create_approval_assignments(
    user_service,
    *,
    module: str,
    event: str,
    entity_id: str,
    task_key: str,
    requester_username: str,
) -> dict[str, Any]:
    """解析流程并把审批人固化为具体待办。"""
    result = resolve_approval_workflow(
        user_service,
        module=module,
        event=event,
        requester_username=requester_username,
    )
    if result.get("status") != "matched":
        return result
    version = result["version"]
    if str(version.get("approval_mode") or "any").strip().lower() == "sequential":
        return {
            **result,
            "status": "sequence_api_required",
            "message": "该流程包含多个审批节点，业务模块必须使用多节点待办接口",
        }
    workflow = result["workflow"]
    usernames = [item["username"] for item in result["approvers"]]
    source_policy_code = f"{workflow['code']}@{version['version_number']}"
    user_service.replace_work_assignments(
        module=module,
        entity_id=entity_id,
        task_key=task_key,
        assignee_usernames=usernames,
        source_policy_code=source_policy_code,
    )
    result["assignment"] = {
        "task_key": task_key,
        "source_policy_code": source_policy_code,
        "assignee_usernames": usernames,
        "required_permission_code": version["required_permission_code"],
        "approval_mode": version.get("approval_mode", "any"),
    }
    return result


def _sequence_node_task_key(base_task_key: str, node: dict[str, Any]) -> str:
    """生成不会与其它节点冲突的具体待办键。"""
    return (
        f"{str(base_task_key)}:node:{int(node.get('node_index', 0)) + 1}:"
        f"{str(node.get('node_key') or 'approval')}"
    )


def create_approval_sequence_assignments(
    user_service,
    *,
    module: str,
    event: str,
    entity_id: str,
    task_key: str,
    requester_username: str,
) -> dict[str, Any]:
    """解析全部节点并固化审批人快照，只激活第一个节点的具体待办。"""
    result = resolve_approval_workflow(
        user_service,
        module=module,
        event=event,
        requester_username=requester_username,
    )
    if result.get("status") != "matched":
        return result
    workflow = result["workflow"]
    version = result["version"]
    resolved_nodes = result.get("approval_nodes", [])
    if not isinstance(resolved_nodes, list) or not resolved_nodes:
        return {**result, "status": "invalid_policy", "message": "流程没有可执行的审批节点"}

    source_policy_code = f"{workflow['code']}@{version['version_number']}"
    snapshot_nodes: list[dict[str, Any]] = []
    for node in resolved_nodes:
        approvers = node.get("approvers", [])
        usernames = [
            str(item.get("username"))
            for item in approvers
            if isinstance(item, dict) and str(item.get("username") or "")
        ]
        snapshot_nodes.append(
            {
                "node_key": str(node.get("node_key") or "approval"),
                "name": str(node.get("name") or "审批"),
                "node_index": int(node.get("node_index", len(snapshot_nodes))),
                "approval_mode": str(node.get("approval_mode") or "any"),
                "required_permission_code": str(node.get("required_permission_code") or ""),
                "assignee_usernames": usernames,
                "approved_usernames": [],
                "status": "waiting" if snapshot_nodes else "pending",
            }
        )

    assignment = {
        "workflow_id": workflow["workflow_id"],
        "workflow_code": workflow["code"],
        "workflow_name": workflow["name"],
        "version_id": version["version_id"],
        "version_number": version["version_number"],
        "source_policy_code": source_policy_code,
        "base_task_key": str(task_key),
        "current_node_index": 0,
        "status": "pending",
        "nodes": snapshot_nodes,
    }
    first_node = snapshot_nodes[0]
    current_task_key = _sequence_node_task_key(task_key, first_node)
    user_service.replace_work_assignments(
        module=module,
        entity_id=entity_id,
        task_key=current_task_key,
        assignee_usernames=first_node["assignee_usernames"],
        source_policy_code=source_policy_code,
    )
    assignment["current_task_key"] = current_task_key
    result["assignment"] = assignment
    return result


def advance_approval_sequence(
    user_service,
    *,
    module: str,
    entity_id: str,
    assignment: dict[str, Any],
    username: str,
) -> dict[str, Any]:
    """完成当前人的节点待办，并在节点完成后串行激活下一节点。"""
    snapshot = copy.deepcopy(assignment)
    if snapshot.get("status") != "pending":
        return {"status": "not_pending", "message": "审批流程已经结束", "assignment": snapshot}
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return {"status": "invalid_assignment", "message": "审批节点快照无效", "assignment": snapshot}
    try:
        current_index = int(snapshot.get("current_node_index", 0))
        current_node = nodes[current_index]
    except (TypeError, ValueError, IndexError):
        return {"status": "invalid_assignment", "message": "当前审批节点无效", "assignment": snapshot}
    if not isinstance(current_node, dict):
        return {"status": "invalid_assignment", "message": "当前审批节点无效", "assignment": snapshot}

    required_permission_code = str(current_node.get("required_permission_code") or "")
    if not user_service.has_permission(username, required_permission_code):
        return {
            "status": "forbidden",
            "message": "当前用户缺少该节点要求的审批权限",
            "assignment": snapshot,
        }
    base_task_key = str(snapshot.get("base_task_key") or "approval")
    current_task_key = _sequence_node_task_key(base_task_key, current_node)
    completed = user_service.complete_work_assignment(
        module=module,
        entity_id=entity_id,
        task_key=current_task_key,
        username=username,
        approval_mode=str(current_node.get("approval_mode") or "any"),
    )
    if not completed:
        return {
            "status": "not_assigned",
            "message": "当前用户没有该节点的有效待办",
            "assignment": snapshot,
        }
    approved_usernames = current_node.setdefault("approved_usernames", [])
    if username not in approved_usernames:
        approved_usernames.append(username)
    remaining = user_service.list_pending_assignment_usernames(
        module=module,
        entity_id=entity_id,
        task_key=current_task_key,
    )
    if remaining:
        snapshot["current_task_key"] = current_task_key
        return {
            "status": "node_pending",
            "message": "当前会签节点仍有审批人未处理",
            "remaining_usernames": remaining,
            "assignment": snapshot,
        }

    current_node["status"] = "completed"
    next_index = current_index + 1
    if next_index >= len(nodes):
        snapshot["status"] = "completed"
        snapshot["current_node_index"] = next_index
        snapshot.pop("current_task_key", None)
        return {"status": "completed", "message": "全部审批节点已经完成", "assignment": snapshot}

    next_node = nodes[next_index]
    if not isinstance(next_node, dict):
        return {"status": "invalid_assignment", "message": "下一审批节点无效", "assignment": snapshot}
    next_node["status"] = "pending"
    next_task_key = _sequence_node_task_key(base_task_key, next_node)
    user_service.replace_work_assignments(
        module=module,
        entity_id=entity_id,
        task_key=next_task_key,
        assignee_usernames=next_node.get("assignee_usernames", []),
        source_policy_code=str(snapshot.get("source_policy_code") or ""),
    )
    snapshot["current_node_index"] = next_index
    snapshot["current_task_key"] = next_task_key
    return {
        "status": "advanced",
        "message": f"已进入下一审批节点：{next_node.get('name', '')}",
        "assignment": snapshot,
    }


def is_assigned_approver(
    user_service,
    *,
    module: str,
    entity_id: str,
    task_key: str,
    username: str,
) -> bool:
    """判断用户是否仍有该单据的具体审批待办。"""
    return str(username).casefold() in {
        value.casefold()
        for value in user_service.list_pending_assignment_usernames(
            module=module,
            entity_id=entity_id,
            task_key=task_key,
        )
    }


def import_sample_issue_legacy_workflows(user_service, *, actor_username: str) -> tuple[int, list[str]]:
    """把样品问题旧 JSON 关闭路由转换为可检查、可发布的流程草稿。"""
    from .sample_issue_config import SAMPLE_CLOSE_APPROVER_ROLES, SAMPLE_CLOSE_ROUTING_RULES

    existing_codes = {
        str(item.get("code", "")).casefold()
        for item in user_service.list_approval_workflows(
            module="sample_issue",
            event="close_request",
        )
    }
    positions = user_service.list_positions()
    warnings: list[str] = []
    created = 0

    def position_ids_matching(keywords: list[str]) -> list[str]:
        matched = [
            str(position["position_id"])
            for position in positions
            if any(
                str(keyword).strip().casefold() in str(position.get("name", "")).casefold()
                for keyword in keywords
                if str(keyword).strip()
            )
        ]
        return list(dict.fromkeys(matched))

    rules = [
        {
            "key": str(rule.get("key", "")).strip(),
            "name": str(rule.get("label") or rule.get("key") or "特殊关闭审批").strip(),
            "requester_keywords": list(rule.get("requester_role_keywords", [])),
            "approver_keywords": list(rule.get("approver_roles", [])),
            "permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            "priority": 10 + index,
        }
        for index, rule in enumerate(SAMPLE_CLOSE_ROUTING_RULES)
    ]
    rules.append(
        {
            "key": "default",
            "name": "样品问题默认关闭审批",
            "requester_keywords": [],
            "approver_keywords": list(SAMPLE_CLOSE_APPROVER_ROLES),
            "permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            "priority": 1000,
        }
    )

    for rule in rules:
        workflow_code = f"sample_issue.close.{rule['key']}"
        if workflow_code.casefold() in existing_codes:
            continue
        requester_position_ids = position_ids_matching(rule["requester_keywords"])
        approver_position_ids = position_ids_matching(rule["approver_keywords"])
        if rule["requester_keywords"] and not requester_position_ids:
            warnings.append(f"{rule['name']}：未匹配到申请人岗位，请手工选择")
        if not approver_position_ids:
            warnings.append(f"{rule['name']}：未匹配到审批岗位，请手工选择")
        user_service.save_approval_workflow_draft(
            code=workflow_code,
            module="sample_issue",
            event="close_request",
            name=rule["name"],
            priority=rule["priority"],
            condition={
                "requester_org_unit_ids": [],
                "requester_position_ids": requester_position_ids,
                "include_child_org_units": True,
                "migration_requires_review": bool(
                    rule["requester_keywords"] and not requester_position_ids
                ),
            },
            approver={
                "strategy": "position",
                "position_ids": approver_position_ids,
                "org_scope": "any",
                "org_unit_ids": [],
            },
            required_permission_code=rule["permission_code"],
            approval_mode="any",
            notification={"notify_assignees": True, "notify_requester_on_result": True},
            actor_username=actor_username,
        )
        created += 1
    return created, warnings


def import_ecn_legacy_workflows(user_service, *, actor_username: str) -> tuple[int, list[str]]:
    """把 ECN 旧审批路线转换为三个可检查的多节点流程草稿。"""
    from .ecn_management_config import ECN_WORKFLOW_ROUTES

    positions = user_service.list_positions()
    org_units = user_service.list_org_units()
    existing_codes = {
        str(item.get("code", "")).casefold()
        for item in user_service.list_approval_workflows(module="ecn")
    }
    warnings: list[str] = []
    created = 0

    def matching_position_ids(keywords: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                str(position["position_id"])
                for position in positions
                if any(
                    str(keyword).strip().casefold()
                    in str(position.get("name", "")).strip().casefold()
                    for keyword in keywords
                    if str(keyword).strip()
                )
            )
        )

    sales_org_ids = [
        str(unit["org_unit_id"])
        for unit in org_units
        if "销售" in str(unit.get("name", ""))
    ]

    def build_nodes(route: Any, workflow_name: str, permission_code: str) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        stages = route if isinstance(route, list) else []
        for index, stage in enumerate(stages):
            keywords = [str(value) for value in stage if str(value)] if isinstance(stage, list) else []
            position_ids = matching_position_ids(keywords)
            node_name = " / ".join(keywords) or f"审批节点 {index + 1}"
            if not position_ids:
                warnings.append(f"{workflow_name} · {node_name}：未匹配到审批岗位，请手工选择")
            nodes.append(
                {
                    "node_key": f"approval_{index + 1}",
                    "name": node_name,
                    "approval_mode": "all" if len(keywords) > 1 else "any",
                    "required_permission_code": permission_code,
                    "approver": {
                        "strategy": "position",
                        "position_ids": position_ids,
                        "org_scope": "any",
                        "org_unit_ids": [],
                    },
                }
            )
        return nodes

    ecr_routes = ECN_WORKFLOW_ROUTES.get("ECR_PHASE", {})
    route_specs = [
        (
            "ecn.ecr.sales_initiated",
            "ECR审批 · 销售部门发起",
            10,
            sales_org_ids,
            ecr_routes.get("SALES_INITIATED", []),
        ),
        (
            "ecn.ecr.default",
            "ECR审批 · 默认路线",
            1000,
            [],
            ecr_routes.get("RD_INITIATED", []),
        ),
    ]
    if not sales_org_ids:
        warnings.append("ECR审批 · 销售部门发起：未匹配到销售部门，请手工选择")
    for code, name, priority, requester_org_ids, route in route_specs:
        if code.casefold() in existing_codes:
            continue
        nodes = build_nodes(route, name, ECN_ECR_APPROVE_PERMISSION)
        user_service.save_approval_workflow_draft(
            code=code,
            module="ecn",
            event="ecr_review",
            name=name,
            priority=priority,
            condition={
                "requester_org_unit_ids": requester_org_ids,
                "requester_position_ids": [],
                "include_child_org_units": True,
                "migration_requires_review": bool(code.endswith("sales_initiated") and not sales_org_ids),
            },
            approver={"nodes": nodes},
            required_permission_code=ECN_ECR_APPROVE_PERMISSION,
            approval_mode="sequential",
            notification={"notify_assignees": True, "notify_requester_on_result": True},
            actor_username=actor_username,
        )
        existing_codes.add(code.casefold())
        created += 1

    scheme_code = "ecn.scheme.default"
    if scheme_code.casefold() not in existing_codes:
        scheme_name = "ECN方案评审 · 默认路线"
        scheme_route = ECN_WORKFLOW_ROUTES.get("ECN_SCHEME_REVIEW_PHASE", [])
        nodes = build_nodes(scheme_route, scheme_name, ECN_SCHEME_APPROVE_PERMISSION)
        user_service.save_approval_workflow_draft(
            code=scheme_code,
            module="ecn",
            event="scheme_review",
            name=scheme_name,
            priority=100,
            condition={
                "requester_org_unit_ids": [],
                "requester_position_ids": [],
                "include_child_org_units": True,
            },
            approver={"nodes": nodes},
            required_permission_code=ECN_SCHEME_APPROVE_PERMISSION,
            approval_mode="sequential",
            notification={"notify_assignees": True, "notify_requester_on_result": True},
            actor_username=actor_username,
        )
        created += 1
    return created, warnings


def import_project_overview_legacy_workflows(
    user_service,
    *,
    actor_username: str,
) -> tuple[int, list[str]]:
    """把项目概述 JSON 中的两类角色路由转换为可检查的流程草稿。"""
    from .overview_change_workflow_config import OVERVIEW_CHANGE_WORKFLOW_CONFIG

    positions = user_service.list_positions()
    existing_codes = {
        str(item.get("code", "")).casefold()
        for item in user_service.list_approval_workflows(module="project_overview")
    }
    warnings: list[str] = []
    created = 0

    def matching_position_ids(names: list[str]) -> list[str]:
        expected = {str(name).strip().casefold() for name in names if str(name).strip()}
        return list(
            dict.fromkeys(
                str(position["position_id"])
                for position in positions
                if str(position.get("name", "")).strip().casefold() in expected
            )
        )

    events = (
        (
            "batch_change",
            "批量概述变更",
            PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
            OVERVIEW_CHANGE_WORKFLOW_CONFIG["batch_overview"]["approval_role_targets"],
        ),
        (
            "correction",
            "概述原记录纠错",
            PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
            OVERVIEW_CHANGE_WORKFLOW_CONFIG["single_correction"]["approval_role_targets"],
        ),
    )
    for event, event_name, permission_code, routes in events:
        for index, (reviewer_role, requester_roles) in enumerate(routes.items()):
            # 旧角色名称可能含中文，流程编码只能使用稳定 ASCII 字符。
            rule_key = f"route_{index + 1}"
            workflow_code = f"project_overview.{event}.{rule_key}"
            if workflow_code.casefold() in existing_codes:
                continue
            requester_position_ids = matching_position_ids(list(requester_roles))
            approver_position_ids = matching_position_ids([reviewer_role])
            display_name = f"{event_name} · {reviewer_role}审批"
            if requester_roles and not requester_position_ids:
                warnings.append(f"{display_name}：未匹配到申请人岗位，请手工选择")
            if not approver_position_ids:
                warnings.append(f"{display_name}：未匹配到审批岗位，请手工选择")
            user_service.save_approval_workflow_draft(
                code=workflow_code,
                module="project_overview",
                event=event,
                name=display_name,
                priority=10 + index,
                condition={
                    "requester_org_unit_ids": [],
                    "requester_position_ids": requester_position_ids,
                    "include_child_org_units": True,
                    "migration_requires_review": bool(requester_roles and not requester_position_ids),
                },
                approver={
                    "strategy": "position",
                    "position_ids": approver_position_ids,
                    "org_scope": "any",
                    "org_unit_ids": [],
                },
                required_permission_code=permission_code,
                approval_mode="any",
                notification={"notify_assignees": True, "notify_requester_on_result": True},
                actor_username=actor_username,
            )
            existing_codes.add(workflow_code.casefold())
            created += 1
    return created, warnings


def import_design_knowledge_legacy_workflows(
    user_service,
    *,
    actor_username: str,
) -> tuple[int, list[str]]:
    """把设计知识库旧角色路由转换为两个业务事件的流程草稿。"""
    from .design_knowledge_config import (
        DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES,
        DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES,
    )

    positions = user_service.list_positions()
    existing_codes = {
        str(item.get("code", "")).casefold()
        for item in user_service.list_approval_workflows(module="design_knowledge")
    }
    warnings: list[str] = []
    created = 0

    def position_ids_matching(keywords: list[str]) -> list[str]:
        matched = [
            str(position["position_id"])
            for position in positions
            if any(
                str(keyword).strip().casefold() in str(position.get("name", "")).casefold()
                for keyword in keywords
                if str(keyword).strip()
            )
        ]
        return list(dict.fromkeys(matched))

    rules = [
        {
            "key": str(rule.get("key", "")).strip(),
            "name": str(rule.get("label") or rule.get("key") or "知识审核").strip(),
            "requester_keywords": list(rule.get("submitter_role_keywords", [])),
            "approver_keywords": list(rule.get("approver_roles", [])),
            "priority": 10 + index,
        }
        for index, rule in enumerate(DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES)
    ]
    rules.append(
        {
            "key": "default",
            "name": "设计知识默认审核",
            "requester_keywords": [],
            "approver_keywords": list(DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES),
            "priority": 1000,
        }
    )
    events = (
        ("knowledge_review", DESIGN_KNOWLEDGE_REVIEW_PERMISSION, "知识发布"),
        ("tag_review", DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION, "新标签"),
    )

    for event, permission_code, event_name in events:
        for rule in rules:
            workflow_code = f"design_knowledge.{event}.{rule['key']}"
            if workflow_code.casefold() in existing_codes:
                continue
            requester_position_ids = position_ids_matching(rule["requester_keywords"])
            approver_position_ids = position_ids_matching(rule["approver_keywords"])
            display_name = f"{event_name} · {rule['name']}"
            if rule["requester_keywords"] and not requester_position_ids:
                warnings.append(f"{display_name}：未匹配到申请人岗位，请手工选择")
            if not approver_position_ids:
                warnings.append(f"{display_name}：未匹配到审批岗位，请手工选择")
            user_service.save_approval_workflow_draft(
                code=workflow_code,
                module="design_knowledge",
                event=event,
                name=display_name,
                priority=rule["priority"],
                condition={
                    "requester_org_unit_ids": [],
                    "requester_position_ids": requester_position_ids,
                    "include_child_org_units": True,
                    "migration_requires_review": bool(
                        rule["requester_keywords"] and not requester_position_ids
                    ),
                },
                approver={
                    "strategy": "position",
                    "position_ids": approver_position_ids,
                    "org_scope": "any",
                    "org_unit_ids": [],
                },
                required_permission_code=permission_code,
                approval_mode="any",
                notification={
                    "notify_assignees": True,
                    "notify_requester_on_result": True,
                },
                actor_username=actor_username,
            )
            existing_codes.add(workflow_code.casefold())
            created += 1
    return created, warnings
