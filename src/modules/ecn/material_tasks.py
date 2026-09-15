"""物料执行责任项的人工改派；用于恢复因人员停用而失去处理人的待办。"""

import uuid
from datetime import datetime

from ...ecn_access import (
    can_execute_ecn_assistant_stage,
    can_view_ecn,
    get_active_ecn_actor_role,
    is_active_ecn_user,
)
from ...ecn_management_config import (
    ECNState,
    ECN_EXECUTION_STAGE_MATERIAL,
)
from .editing import ECNConflict
from .repository import mutate_record
from .task_labels import material_task_label


async def update_material_task_assignee(
    ecn_id: str,
    item_id: str,
    key: str,
    baseline: dict,
    *,
    username: str,
    role: str,
    service,
    assignee: str,
    storage=None,
):
    """把未完成物料节点改派给具体人员；空值恢复节点原始责任路线。"""

    async def operation(record, connection):
        del connection
        if record.get("workflow", {}).get("current_state") != ECNState.ECN_EXECUTING:
            raise ECNConflict("ECN已不在执行中，请刷新。")
        actor = service.get_user(username)
        actor_role = get_active_ecn_actor_role(username, role, user_service=service)
        if (
            not isinstance(actor, dict)
            or actor_role is None
            or not can_view_ecn(actor_role, username, user_service=service)
            or not can_execute_ecn_assistant_stage(actor_role, username, user_service=service)
        ):
            raise ECNConflict("仅在职且有执行助理权限的人员可以改派物料责任项。")
        execution = record.get("execution_info", {})
        if not isinstance(execution, dict) or execution.get("stage") != ECN_EXECUTION_STAGE_MATERIAL:
            raise ECNConflict("当前已不在物料执行确认阶段，请刷新。")
        confirmations = execution.get("material_confirmations", {})
        entry = confirmations.get(item_id) if isinstance(confirmations, dict) else None
        tasks = entry.get("traceability_tasks", {}) if isinstance(entry, dict) else {}
        task = tasks.get(key) if isinstance(tasks, dict) else None
        if not isinstance(task, dict) or task != baseline:
            raise ECNConflict("该责任项的负责人或确认状态已变化，请刷新后重试。")
        if task.get("confirmed") is True:
            raise ECNConflict("已确认的物料责任项不能改派。")
        normalized = str(assignee or "").strip()
        if normalized and not is_active_ecn_user(
            normalized, user_service=service, require_material_permission=True
        ):
            raise ECNConflict("接收人必须在职、可查看ECN并拥有ECN执行权限。")
        old = str(task.get("manual_assignee") or "").strip()
        if normalized == old:
            return record
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task["manual_assignee"] = normalized
        task["assignment_revision"] = uuid.uuid4().hex
        task.setdefault("assignment_history", []).append(
            {"from": old, "to": normalized, "user": username, "role": actor_role, "time": now}
        )
        original = old or "原责任路线"
        target = normalized or "原责任路线"
        record.setdefault("approval_log", []).append(
            {
                "user": username,
                "role": actor_role,
                "time": now,
                "action": f"{material_task_label(record, item_id, task)} 改派：{original} → {target}",
            }
        )
        return record

    return await mutate_record(ecn_id, operation, storage=storage)
