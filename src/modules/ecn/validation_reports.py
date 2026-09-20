"""复杂 ECN 方案验证报告的指定与审批事务。"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from nicegui import app

from ... import db_storage
from ...ecn_access import (
    can_approve_ecn_validation_report,
    can_designate_ecn_validation_report,
    can_view_ecn_validation_report,
    get_active_ecn_actor_role,
)
from ...ecn_management_config import ECN_LEVEL_COMPLEX, ECNState, get_ecn_level_code
from .editing import ECNConflict
from .models import append_ecn_approval_log_once
from .repository import mutate_record


def _find_item(record: dict, item_id: str) -> dict:
    items = record.get("change_items", [])
    item = next(
        (
            value
            for value in items
            if isinstance(value, dict) and str(value.get("item_id") or "") == item_id
        ),
        None,
    ) if isinstance(items, list) else None
    if not isinstance(item, dict):
        raise ECNConflict("方案已不存在，请刷新后重试。")
    return item


def _require_complex_scheming(record: dict) -> None:
    workflow = record.get("workflow", {})
    if (
        not isinstance(workflow, dict)
        or workflow.get("current_state") != ECNState.ECN_SCHEMING
        or workflow.get("current_phase") != "ECN_SCHEME_PHASE"
    ):
        raise ECNConflict("验证报告只能在ECN方案评审发起前维护。")
    if get_ecn_level_code(record) != ECN_LEVEL_COMPLEX:
        raise ECNConflict("只有复杂等级ECN可以指定验证报告。")


async def set_validation_report_required(
    ecn_id: str,
    item_id: str,
    expected_report: dict,
    required: bool,
    user: str,
    role: str,
    *,
    user_service=None,
    storage=None,
):
    service = user_service or getattr(app.state, "user_service", None)

    async def operation(record: dict, connection: Any) -> dict:
        del connection
        actor_role = get_active_ecn_actor_role(user, role, user_service=service)
        if actor_role is None:
            raise ECNConflict("当前账号已停用或不存在。")
        if not can_designate_ecn_validation_report(actor_role, user, user_service=service):
            raise ECNConflict("当前用户没有指定验证报告的权限。")
        _require_complex_scheming(record)
        item = _find_item(record, item_id)
        current_report = item.get("validation_report", {})
        current_report = current_report if isinstance(current_report, dict) else {}
        if current_report != expected_report:
            raise ECNConflict("验证报告状态已被其他人修改，请刷新后重试。")
        report = copy.deepcopy(current_report)
        attachments = report.get("attachments", [])
        attachments = attachments if isinstance(attachments, list) else []
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report.update(
            {
                "required": bool(required),
                "status": (
                    "pending_review"
                    if required and attachments
                    else "pending_upload"
                    if required
                    else "not_required"
                ),
                "designated_by": user,
                "designated_at": now,
                "attachments": attachments,
            }
        )
        for key in ("reviewed_by", "reviewed_at", "review_note"):
            report.pop(key, None)
        item["validation_report"] = report
        append_ecn_approval_log_once(
            record.setdefault("approval_log", []),
            {
                "user": user,
                "role": actor_role,
                "action": (
                    f"方案验证报告：指定提交（{item_id}）"
                    if required
                    else f"方案验证报告：取消指定（{item_id}）"
                ),
                "time": now,
            },
        )
        return record

    return await mutate_record(ecn_id, operation, storage=storage or db_storage)


async def review_validation_report(
    ecn_id: str,
    item_id: str,
    expected_report: dict,
    approved: bool,
    note: str,
    user: str,
    role: str,
    *,
    user_service=None,
    storage=None,
):
    service = user_service or getattr(app.state, "user_service", None)

    async def operation(record: dict, connection: Any) -> dict:
        del connection
        actor_role = get_active_ecn_actor_role(user, role, user_service=service)
        if actor_role is None:
            raise ECNConflict("当前账号已停用或不存在。")
        if not can_view_ecn_validation_report(
            actor_role, user, user_service=service
        ) or not can_approve_ecn_validation_report(actor_role, user, user_service=service):
            raise ECNConflict("当前用户需要同时具备查看和审批验证报告权限。")
        _require_complex_scheming(record)
        item = _find_item(record, item_id)
        if item.get("author") == user:
            raise ECNConflict("方案出具人不能审批本人提交的验证报告。")
        current_report = item.get("validation_report", {})
        current_report = current_report if isinstance(current_report, dict) else {}
        if current_report != expected_report:
            raise ECNConflict("验证报告状态已被其他人修改，请刷新后重试。")
        attachments = current_report.get("attachments", [])
        if current_report.get("required") is not True or not isinstance(attachments, list) or not attachments:
            raise ECNConflict("方案尚未提交验证报告。")
        if current_report.get("status") != "pending_review":
            raise ECNConflict("验证报告当前不在待审批状态，请刷新后重试。")
        if not approved and not note.strip():
            raise ECNConflict("不通过时请填写审核意见。")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report = copy.deepcopy(current_report)
        report.update(
            {
                "status": "approved" if approved else "rejected",
                "reviewed_by": user,
                "reviewed_at": now,
                "review_note": note.strip(),
            }
        )
        item["validation_report"] = report
        append_ecn_approval_log_once(
            record.setdefault("approval_log", []),
            {
                "user": user,
                "role": actor_role,
                "action": f"方案验证报告：{'通过' if approved else '不通过'}（{item_id}）",
                "note": note.strip(),
                "time": now,
            },
        )
        return record

    return await mutate_record(ecn_id, operation, storage=storage or db_storage)
