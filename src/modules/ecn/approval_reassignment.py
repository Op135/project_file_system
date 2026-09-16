"""ECR与ECN方案审批节点的人工审核人调整。"""

import uuid
from datetime import datetime
from pathlib import Path

from ... import db_storage
from ...ecn_management_config import ECNState
from ...ecn_workflow import (
    ECN_ECR_ASSIGNMENT_KEY,
    ECN_SCHEME_ASSIGNMENT_KEY,
    ECN_WORKFLOW_MODULE,
)
from ...permission_catalog import (
    ECN_APPROVAL_REASSIGN_PERMISSION,
    ECN_VIEW_PERMISSION,
    ignores_legacy_role_grants,
)
from .editing import ECNConflict
from .repository import mutate_record


async def _has_permission(connection, username: str, permission_code: str) -> bool:
    ignore_legacy_roles = int(ignores_legacy_role_grants(permission_code))
    async with connection.execute(
        "SELECT 1 FROM iam_users u WHERE u.username=? COLLATE NOCASE AND u.status='active' AND ("
        "(u.username='admin' COLLATE NOCASE AND EXISTS ("
        "SELECT 1 FROM iam_permissions p WHERE p.code=? COLLATE NOCASE)) OR "
        "EXISTS (SELECT 1 FROM iam_user_roles ur "
        "JOIN iam_security_roles r ON r.role_id=ur.role_id AND r.status='active' "
        "JOIN iam_role_permissions rp ON rp.role_id=r.role_id "
        "JOIN iam_permissions p ON p.permission_id=rp.permission_id "
        "WHERE ur.user_id=u.user_id AND p.code=? COLLATE NOCASE "
        "AND (?=0 OR r.code NOT LIKE 'legacy.%')) OR "
        "EXISTS (SELECT 1 FROM org_memberships m "
        "JOIN iam_positions position ON position.position_id=m.position_id AND position.status='active' "
        "JOIN iam_position_permissions pp ON pp.position_id=position.position_id "
        "JOIN iam_permissions p ON p.permission_id=pp.permission_id "
        "WHERE m.user_id=u.user_id AND m.status='active' AND m.is_primary=1 "
        "AND p.code=? COLLATE NOCASE)) LIMIT 1",
        (
            username,
            permission_code,
            permission_code,
            ignore_legacy_roles,
            permission_code,
        ),
    ) as cursor:
        return await cursor.fetchone() is not None


async def _actor_role(connection, username: str) -> str | None:
    async with connection.execute(
        "SELECT u.legacy_role, position.name AS position_name FROM iam_users u "
        "LEFT JOIN org_memberships m ON m.user_id=u.user_id AND m.is_primary=1 AND m.status='active' "
        "LEFT JOIN iam_positions position ON position.position_id=m.position_id AND position.status='active' "
        "WHERE u.username=? COLLATE NOCASE AND u.status='active' "
        "ORDER BY m.updated_at DESC LIMIT 1",
        (username,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row[1] or row[0] or "").strip() or "未配置岗位"


async def _pending_usernames(
    connection,
    ecn_id: str,
    task_key: str,
    *,
    active_only: bool = True,
) -> list[str]:
    active_join = " AND u.status='active'" if active_only else ""
    async with connection.execute(
        "SELECT u.username FROM work_assignments a "
        f"JOIN iam_users u ON u.user_id=a.assignee_user_id{active_join} "
        "WHERE a.module=? AND a.entity_id=? AND a.task_key=? AND a.status='pending' "
        "ORDER BY u.username",
        (ECN_WORKFLOW_MODULE, ecn_id, task_key),
    ) as cursor:
        rows = await cursor.fetchall()
    return [str(row[0]) for row in rows]


async def _replace_pending_assignment(
    connection,
    *,
    ecn_id: str,
    task_key: str,
    source_username: str,
    target_username: str,
    source_policy_code: str,
    now: str,
) -> None:
    cursor = await connection.execute(
        "UPDATE work_assignments SET status='superseded', updated_at=? "
        "WHERE module=? AND entity_id=? AND task_key=? AND status='pending' "
        "AND assignee_user_id IN ("
        "SELECT user_id FROM iam_users WHERE username=? COLLATE NOCASE)",
        (now, ECN_WORKFLOW_MODULE, ecn_id, task_key, source_username),
    )
    if not cursor.rowcount:
        raise ECNConflict("原审核人的当前待办已经处理或变化。")
    async with connection.execute(
        "SELECT user_id FROM iam_users WHERE username=? COLLATE NOCASE AND status='active'",
        (target_username,),
    ) as target_cursor:
        row = await target_cursor.fetchone()
    if row is None:
        raise ECNConflict(f"审核人已停用或不存在：{target_username}")
    await connection.execute(
        "INSERT INTO work_assignments(assignment_id, module, entity_id, task_key, "
        "assignment_type, assignee_user_id, status, source_policy_code, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, 'approval', ?, 'pending', ?, ?, ?) "
        "ON CONFLICT(module, entity_id, task_key, assignee_user_id) DO UPDATE SET "
        "status='pending', source_policy_code=excluded.source_policy_code, "
        "updated_at=excluded.updated_at, completed_at=NULL",
        (
            str(uuid.uuid4()),
            ECN_WORKFLOW_MODULE,
            ecn_id,
            task_key,
            row[0],
            source_policy_code,
            now,
            now,
        ),
    )


async def reassign_ecn_approval_reviewer(
    ecn_id: str,
    assignment_key: str,
    node_index: int,
    source_username: str,
    target_username: str,
    baseline_node: dict,
    *,
    actor_username: str,
    user_service,
    storage=None,
):
    """把一个尚未完成的节点审核人替换为另一名合资格在职人员。"""
    storage = storage or db_storage
    normalized_source = str(source_username or "").strip()
    normalized_target = str(target_username or "").strip()
    if not normalized_source or not normalized_target:
        from .editing import ECNResult

        return ECNResult(False, "请选择原审核人和接手人。")

    async def operation(record, connection):
        if user_service is None or getattr(user_service, "storage_mode", "") != "database":
            raise ECNConflict("审批人调整只支持数据库身份与审批流程。")
        if Path(user_service.identity_store.db_path).resolve() != Path(storage.DB_PATH).resolve():
            raise ECNConflict("ECN审批需要身份数据与业务数据使用同一数据库。")
        actor_role = await _actor_role(connection, actor_username)
        if actor_role is None or not await _has_permission(
            connection,
            actor_username,
            ECN_APPROVAL_REASSIGN_PERMISSION,
        ):
            raise ECNConflict("当前用户没有调整ECN审批人的权限。")

        workflow = record.get("workflow", {})
        expected_context = {
            ECN_ECR_ASSIGNMENT_KEY: (ECNState.ECR_REVIEWING, "ECR_PHASE"),
            ECN_SCHEME_ASSIGNMENT_KEY: (ECNState.ECN_REVIEWING, "ECN_SCHEME_REVIEW_PHASE"),
        }.get(assignment_key)
        if expected_context is None or (
            workflow.get("current_state"),
            workflow.get("current_phase"),
        ) != expected_context:
            raise ECNConflict("当前已不在对应审批阶段，请刷新后重试。")

        assignment = workflow.get(assignment_key, {})
        nodes = assignment.get("nodes", []) if isinstance(assignment, dict) else []
        if not isinstance(nodes, list) or not 0 <= node_index < len(nodes):
            raise ECNConflict("审批节点不存在或已经变化。")
        node = nodes[node_index]
        if not isinstance(node, dict) or node != baseline_node:
            raise ECNConflict("审批节点或审核人已经变化，请刷新后重试。")
        current_index = int(assignment.get("current_node_index", 0))
        if node_index < current_index or node.get("status") == "completed":
            raise ECNConflict("已完成节点保留原审核记录，不能再调整。")

        assignees = [str(value) for value in node.get("assignee_usernames", []) if str(value)]
        if normalized_source not in assignees:
            raise ECNConflict("原审核人已不在该节点，请刷新后重试。")
        if normalized_target == normalized_source:
            raise ECNConflict("接手人与原审核人相同，无需调整。")
        if normalized_target in assignees:
            raise ECNConflict("接手人已经是该节点审核人。")

        required_permission = str(node.get("required_permission_code") or "")
        if not required_permission or not await _has_permission(
            connection,
            normalized_target,
            required_permission,
        ):
            raise ECNConflict("接手人缺少该节点要求的审批权限。")
        if not await _has_permission(connection, normalized_target, ECN_VIEW_PERMISSION):
            raise ECNConflict("接手人没有ECN查看权限。")

        updated_assignees = [
            normalized_target if username == normalized_source else username
            for username in assignees
        ]
        node["assignee_usernames"] = list(dict.fromkeys(updated_assignees))
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        node.setdefault("reassignment_history", []).append(
            {
                "from": normalized_source,
                "to": normalized_target,
                "user": actor_username,
                "role": actor_role,
                "time": now,
            }
        )

        if node_index == current_index:
            task_key = str(assignment.get("current_task_key") or "")
            if not task_key:
                raise ECNConflict("当前审批节点缺少有效待办。")
            pending = await _pending_usernames(
                connection,
                ecn_id,
                task_key,
                active_only=False,
            )
            if normalized_source not in pending:
                raise ECNConflict("原审核人的当前待办已经处理或变化。")
            await _replace_pending_assignment(
                connection,
                ecn_id=ecn_id,
                task_key=task_key,
                source_username=normalized_source,
                target_username=normalized_target,
                source_policy_code=str(assignment.get("source_policy_code") or ""),
                now=now,
            )
            workflow["pending_roles"] = await _pending_usernames(connection, ecn_id, task_key)

        node_name = str(node.get("name") or f"节点{node_index + 1}")
        record.setdefault("approval_log", []).append(
            {
                "user": actor_username,
                "role": actor_role,
                "action": f"调整审批人：{node_name} · {normalized_source} → {normalized_target}",
                "time": now,
            }
        )
        return record

    return await mutate_record(ecn_id, operation, storage=storage)
