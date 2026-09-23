"""ECN执行结果复核：保留原确认历史，撤销当前完成态并记录复核证据。"""

from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from nicegui import app

from ... import db_storage
from ...ecn_access import (
    can_verify_ecn_execution,
    can_view_ecn,
    get_active_ecn_actor_role,
)
from ...ecn_management_config import (
    ECN_EXECUTION_STAGE_COMPLETED,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    ECN_EXECUTION_VERIFICATION_CORRECTION_REQUIRED,
    ECN_EXECUTION_VERIFICATION_VERIFIED,
    ECNState,
    get_ecn_material_execution_specs,
    get_ecn_scheme_target_projects,
    get_ecn_special_confirmations,
)
from .attachments import cleanup_staged, publish_pending
from .editing import ECNConflict, ECNResult
from .models import append_ecn_approval_log_once
from .repository import mutate_record
from .task_labels import special_task_label


ExecutionTargetKind = Literal["special", "material"]


def _find_change_item(record: dict, item_id: str) -> dict:
    item = next(
        (
            value
            for value in record.get("change_items", [])
            if isinstance(value, dict) and str(value.get("item_id") or "") == item_id
        ),
        None,
    )
    if not isinstance(item, dict):
        raise ECNConflict("执行方案已不存在，请刷新后重试。")
    return item


def _material_target(record: dict, item_id: str, task_key: str) -> tuple[dict, dict, dict, str]:
    item = _find_change_item(record, item_id)
    execution = record.get("execution_info", {})
    confirmations = execution.get("material_confirmations", {}) if isinstance(execution, dict) else {}
    entry = confirmations.get(item_id) if isinstance(confirmations, dict) else None
    if not isinstance(entry, dict):
        raise ECNConflict("物料执行方案已不存在，请刷新后重试。")
    tasks = entry.get("traceability_tasks", {})
    target = tasks.get(task_key) if isinstance(tasks, dict) else None
    spec = next(
        (
            value
            for value in get_ecn_material_execution_specs(item, entry)
            if str(value.get("key") or "") == task_key
        ),
        None,
    )
    if not isinstance(target, dict) or not isinstance(spec, dict):
        raise ECNConflict("物料执行责任项已发生变化，请刷新后重试。")
    projects = "、".join(get_ecn_scheme_target_projects({"target_projects": item.get("projects", [])}))
    subject = " · ".join(
        value
        for value in (
            str(item.get("change_type") or "物料变更"),
            str(spec.get("level") or "追溯节点"),
            str(spec.get("label") or ""),
        )
        if value
    )
    if projects:
        subject += f"（{projects}）"
    return item, entry, target, subject


def _special_target(record: dict, item_id: str) -> tuple[dict, str]:
    execution = record.get("execution_info", {})
    target = get_ecn_special_confirmations(execution).get(item_id)
    if not isinstance(target, dict):
        raise ECNConflict("特定事项执行项已发生变化，请刷新后重试。")
    return target, special_task_label(record, item_id)


def _all_execution_confirmations_complete(record: dict) -> bool:
    execution = record.get("execution_info", {})
    if not isinstance(execution, dict):
        return False
    special = get_ecn_special_confirmations(execution)
    if any(value.get("confirmed") is not True for value in special.values()):
        return False
    material = execution.get("material_confirmations", {})
    if not isinstance(material, dict):
        return False
    for entry in material.values():
        if not isinstance(entry, dict):
            return False
        tasks = entry.get("traceability_tasks", {})
        if not isinstance(tasks, dict) or any(
            not isinstance(task, dict) or task.get("confirmed") is not True
            for task in tasks.values()
        ):
            return False
    return True


def _require_reviewer(user: str, role: str, service: Any) -> str:
    actor_role = get_active_ecn_actor_role(user, role, user_service=service)
    if actor_role is None:
        raise ECNConflict("当前账号已停用或不存在。")
    if not can_view_ecn(actor_role, user, user_service=service) or not can_verify_ecn_execution(
        actor_role,
        user,
        user_service=service,
    ):
        raise ECNConflict("当前用户没有ECN执行复核权限。")
    return actor_role


async def revoke_execution_confirmation(
    ecn_id: str,
    target_kind: ExecutionTargetKind,
    item_id: str,
    task_key: str,
    expected_confirmation: dict,
    reason: str,
    staged_attachments: list[dict],
    user: str,
    role: str,
    *,
    user_service=None,
    storage=None,
):
    """撤销一个已确认执行项；文件先归档，业务事务失败时回收已归档副本。"""
    service = user_service or getattr(app.state, "user_service", None)
    storage = storage or db_storage
    normalized_reason = reason.strip()
    if not normalized_reason:
        return ECNResult(False, "撤销确认时必须填写文字理由。")

    event_id = uuid.uuid4().hex
    published: list[dict] = []
    published_paths: list[Path] = []
    try:
        for attachment in staged_attachments:
            if not isinstance(attachment, dict) or attachment.get("uploaded_by") != user:
                raise ECNConflict("复核附件上传人无效，请重新上传。")
            saved, path = await asyncio.to_thread(
                publish_pending,
                attachment,
                ecn_id,
                user,
                f"execution_review_{event_id}",
            )
            published.append(saved)
            published_paths.append(path)
    except Exception as exc:
        for path in published_paths:
            path.unlink(missing_ok=True)
        return ECNResult(False, str(exc))

    async def operation(record: dict, connection: Any) -> dict:
        del connection
        actor_role = _require_reviewer(user, role, service)
        workflow = record.get("workflow", {})
        execution = record.get("execution_info", {})
        if (
            not isinstance(workflow, dict)
            or workflow.get("current_state") not in {ECNState.ECN_EXECUTING, ECNState.CLOSED}
            or not isinstance(execution, dict)
        ):
            raise ECNConflict("当前ECN不在执行或已关闭状态，不能复核执行项。")
        current_stage = str(execution.get("stage") or "")
        if current_stage == ECN_EXECUTION_STAGE_OVERVIEW_RUNNING:
            raise ECNConflict("系统内资料正在执行，请等待执行结束后再复核撤销。")

        material_entry: dict | None = None
        if target_kind == "special":
            target, subject = _special_target(record, item_id)
        elif target_kind == "material":
            _, material_entry, target, subject = _material_target(record, item_id, task_key)
        else:
            raise ECNConflict("执行复核目标无效。")
        if target != expected_confirmation:
            raise ECNConflict("该执行项已被其他人修改，请刷新后重试。")
        if target.get("confirmed") is not True:
            raise ECNConflict("该执行项当前未确认，无需撤销。")

        original_user = str(target.get("user") or target.get("assignee") or "").strip()
        original_role = str(target.get("role") or "").strip()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        event = {
            "event_id": event_id,
            "action": "revoked",
            "target_kind": target_kind,
            "item_id": item_id,
            "task_key": task_key,
            "subject": subject,
            "reason": normalized_reason,
            "attachments": copy.deepcopy(published),
            "original_user": original_user,
            "original_role": original_role,
            "reviewer": user,
            "reviewer_role": actor_role,
            "time": now,
        }
        target["confirmed"] = False
        target["review_revocation"] = copy.deepcopy(event)
        target.setdefault("history", []).append(
            {
                "confirmed": False,
                "action": "verification_revoked",
                "user": user,
                "role": actor_role,
                "time": now,
                "reason": normalized_reason,
                "event_id": event_id,
            }
        )
        if material_entry is not None:
            material_entry["status"] = "open"
            material_entry.pop("closed_time", None)
            assignment = target.get("workflow_assignment", {})
            if isinstance(assignment, dict):
                marker_key = ":".join(
                    (
                        str(assignment.get("workflow_id") or ""),
                        str(assignment.get("version_id") or ""),
                        str(target.get("project") or ""),
                    )
                )
                markers = material_entry.get("workflow_completion_notifications", {})
                if isinstance(markers, dict):
                    markers.pop(marker_key, None)

        verification = execution.setdefault("verification", {})
        if not isinstance(verification, dict):
            verification = {}
            execution["verification"] = verification
        verification.update(
            status=ECN_EXECUTION_VERIFICATION_CORRECTION_REQUIRED,
            last_event_id=event_id,
            last_reviewer=user,
            last_time=now,
        )
        verification.pop("verified_by", None)
        verification.pop("verified_role", None)
        verification.pop("verified_at", None)
        verification.setdefault("history", []).append(copy.deepcopy(event))
        if original_user:
            verification.setdefault("notices", {})[event_id] = {
                "event_id": event_id,
                "recipient": original_user,
                "subject": subject,
                "reason": normalized_reason,
                "reviewer": user,
                "time": now,
            }

        if current_stage == ECN_EXECUTION_STAGE_COMPLETED or target_kind == "material":
            execution["stage"] = ECN_EXECUTION_STAGE_MATERIAL
        execution.pop("completed_time", None)
        workflow["current_state"] = ECNState.ECN_EXECUTING
        workflow["pending_roles"] = []
        append_ecn_approval_log_once(
            record.setdefault("approval_log", []),
            {
                "user": user,
                "role": actor_role,
                "action": f"执行复核撤销确认：{subject}",
                "note": normalized_reason,
                "time": now,
            },
        )
        return record

    result = await mutate_record(ecn_id, operation, storage=storage)
    if result.ok:
        cleanup_staged(staged_attachments)
    else:
        for path in published_paths:
            path.unlink(missing_ok=True)
    return result


async def verify_execution_result(
    ecn_id: str,
    user: str,
    role: str,
    *,
    user_service=None,
    storage=None,
):
    """全部执行勾选项完成后，记录独立的最终核验结论。"""
    service = user_service or getattr(app.state, "user_service", None)

    async def operation(record: dict, connection: Any) -> dict:
        del connection
        actor_role = _require_reviewer(user, role, service)
        workflow = record.get("workflow", {})
        execution = record.get("execution_info", {})
        if (
            not isinstance(workflow, dict)
            or workflow.get("current_state") != ECNState.CLOSED
            or not isinstance(execution, dict)
            or execution.get("stage") != ECN_EXECUTION_STAGE_COMPLETED
            or not _all_execution_confirmations_complete(record)
        ):
            raise ECNConflict("仍有执行勾选项未完成，不能标记核验无误。")
        verification = execution.setdefault("verification", {})
        if not isinstance(verification, dict):
            verification = {}
            execution["verification"] = verification
        if verification.get("status") == ECN_EXECUTION_VERIFICATION_VERIFIED:
            raise ECNConflict("该ECN已经核验无误。")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        event = {
            "event_id": uuid.uuid4().hex,
            "action": "verified",
            "reviewer": user,
            "reviewer_role": actor_role,
            "time": now,
        }
        verification.update(
            status=ECN_EXECUTION_VERIFICATION_VERIFIED,
            verified_by=user,
            verified_role=actor_role,
            verified_at=now,
            last_event_id=event["event_id"],
            last_reviewer=user,
            last_time=now,
        )
        verification.setdefault("history", []).append(event)
        append_ecn_approval_log_once(
            record.setdefault("approval_log", []),
            {
                "user": user,
                "role": actor_role,
                "action": "ECN执行结果已核验无误",
                "time": now,
            },
        )
        return record

    return await mutate_record(ecn_id, operation, storage=storage or db_storage)
