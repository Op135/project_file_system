"""基于稳定组织岗位和具体用户的通用审批流程解析器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .permission_catalog import (
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
    SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
)


@dataclass(frozen=True)
class ApprovalWorkflowEventDefinition:
    module: str
    event: str
    name: str
    permission_codes: tuple[str, ...]

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
    required_permission_code = str(version.get("required_permission_code", ""))
    if required_permission_code not in event_definition.permission_codes:
        return {
            "status": "invalid_policy",
            "message": "流程使用了当前业务事件不支持的审批权限",
            "requester_membership": requester_membership,
            "workflow": {
                "workflow_id": workflow["workflow_id"],
                "code": workflow["code"],
                "name": workflow["name"],
                "module": workflow["module"],
                "event": workflow["event"],
            },
            "version": version,
        }
    eligible, excluded = _resolve_approver_candidates(
        user_service,
        version.get("approver", {}),
        requester_membership,
        required_permission_code,
    )
    warnings: list[str] = []
    if excluded:
        warnings.append("部分候选人缺少流程要求的审批权限")
    if eligible and any(not item.get("wecom_bound") for item in eligible):
        warnings.append("部分实际审批人尚未绑定企业微信账号")
    status = "matched" if eligible else "no_approver"
    message = "流程匹配成功" if eligible else "流程已命中，但没有符合条件且拥有权限的在职审批人"
    return {
        "status": status,
        "message": message,
        "requester_membership": requester_membership,
        "workflow": {
            "workflow_id": workflow["workflow_id"],
            "code": workflow["code"],
            "name": workflow["name"],
            "module": workflow["module"],
            "event": workflow["event"],
        },
        "version": version,
        "approvers": eligible,
        "excluded_approvers": excluded,
        "warnings": warnings,
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
