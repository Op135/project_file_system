"""特定事项的移交、确认及整体执行完成判定；所有修改基于最新事务记录。"""

import uuid
from datetime import datetime

from ...ecn_access import can_execute_ecn_assistant_stage, can_view_ecn, get_active_ecn_actor_role
from ...ecn_management_config import (
    ECNState,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_COMPLETED,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    get_ecn_special_confirmations,
    is_ecn_special_execution_complete,
)
from .editing import ECNConflict
from .repository import mutate_record
from .task_labels import special_task_label


def finish_if_complete(record: dict, username: str, role: str, now: str) -> bool:
    execution = record.get("execution_info", {})
    if execution.get("stage") != ECN_EXECUTION_STAGE_MATERIAL or not is_ecn_special_execution_complete(execution):
        return False
    if any(item.get("status") != "closed" for item in execution.get("material_confirmations", {}).values()):
        return False
    execution.update(stage=ECN_EXECUTION_STAGE_COMPLETED, completed_time=now)
    record["workflow"].update(current_state=ECNState.CLOSED, pending_roles=[])
    record.setdefault("approval_log", []).append(
        {"user": username, "role": role, "time": now, "action": "全部特定事项及物料执行完成，ECN关闭"}
    )
    return True


async def update_special_task(
    ecn_id: str,
    key: str,
    baseline: dict,
    *,
    username: str,
    role: str,
    service,
    assignee: str | None = None,
    confirmed: bool | None = None,
    storage=None,
):
    """assignee为空字符串表示收回；移交及确认采用同一事项的窗口快照防止覆盖。"""

    async def operation(record, connection):
        if record.get("workflow", {}).get("current_state") != ECNState.ECN_EXECUTING:
            raise ECNConflict("ECN已不在执行中，请刷新。")
        actor = service.get_user(username)
        actor_role = get_active_ecn_actor_role(username, role, user_service=service)
        if (
            not isinstance(actor, dict)
            or actor_role is None
            or not can_view_ecn(actor_role, username, user_service=service)
        ):
            raise ECNConflict("当前用户无权处理该事项。")
        execution = record.get("execution_info", {})
        if execution.get("stage") not in {
            ECN_EXECUTION_STAGE_ASSISTANT,
            ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
            ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
            ECN_EXECUTION_STAGE_MATERIAL,
        }:
            raise ECNConflict("当前执行阶段不允许调整特定事项，请刷新。")
        item = get_ecn_special_confirmations(execution).get(key)
        if item is None or item != baseline:
            raise ECNConflict("该事项的负责人或确认状态已变化，请刷新后重试。")
        old = str(item.get("assignee") or "")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if assignee is not None:
            if not can_execute_ecn_assistant_stage(actor_role, username, user_service=service):
                raise ECNConflict("仅执行助理可以移交或调整负责人。")
            if item.get("confirmed") is True:
                raise ECNConflict("已确认事项不能移交。")
            if not assignee and execution.get("stage") != ECN_EXECUTION_STAGE_ASSISTANT:
                raise ECNConflict("系统内资料已开始执行，请指定新的负责人，不能收回为未移交事项。")
            if assignee:
                target = service.get_user(assignee)
                if (
                    not target
                    or target.get("status", "active") != "active"
                    or get_active_ecn_actor_role(assignee, target.get("role"), user_service=service) is None
                    or not can_view_ecn(str(target.get("role") or ""), assignee, user_service=service)
                ):
                    raise ECNConflict("接收人必须在职且拥有ECN查看权限。")
            if assignee == old:
                return record
            revision = uuid.uuid4().hex
            item.update(assignee=assignee, assignment_revision=revision, assigned_by=username, assigned_time=now)
            item["suppress_assignment_notice_for"] = username if old and assignee in {"", username} else ""
            item.setdefault("assignment_history", []).append(
                {"from": old, "to": assignee, "user": username, "time": now}
            )
            if old:
                execution.setdefault("transfer_notices", {})[revision] = {
                    "key": key,
                    "recipient": old,
                    "new_assignee": assignee,
                    "time": now,
                }
            action = f"{special_task_label(record, key)} 移交：{old or '执行助理'} → {assignee or '执行助理'}"
        else:
            if confirmed is None:
                raise ECNConflict("缺少确认操作。")
            if old:
                if username != old:
                    raise ECNConflict("仅当前指定接收人可以确认该移交事项。")
            elif execution.get("stage") != ECN_EXECUTION_STAGE_ASSISTANT or not can_execute_ecn_assistant_stage(
                actor_role, username, user_service=service
            ):
                raise ECNConflict("当前用户或阶段不允许确认未移交事项。")
            item.update(confirmed=confirmed, user=username, role=actor_role, time=now)
            if confirmed:
                item.pop("review_revocation", None)
            item.setdefault("history", []).append(
                {"confirmed": confirmed, "user": username, "role": actor_role, "time": now}
            )
            action = f"{special_task_label(record, key)} {'确认完成' if confirmed else '取消确认'}"
        record.setdefault("approval_log", []).append(
            {"user": username, "role": actor_role, "time": now, "action": action}
        )
        finish_if_complete(record, username, actor_role, now)
        return record

    return await mutate_record(ecn_id, operation, storage=storage)
