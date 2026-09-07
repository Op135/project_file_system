"""在最新单据上合并协作编辑；不依赖 NiceGUI 控件或页面快照写回。"""

import copy
from dataclasses import (
    dataclass,
)
from typing import (
    Any,
)

from ...ecn_management_config import (
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION,
    ECNState,
    build_ecn_scheme_snapshot,
    confirm_revised_scheme_items,
    has_unrevised_rejected_scheme_items,
    mark_rejected_scheme_item_revised,
    merge_ecn_impact_audit_log,
    register_ecn_impact_handler,
)


class ECNConflict(ValueError):
    """业务条件已变化，拒绝保存并交由界面提示。"""


@dataclass
class ECNResult:
    ok: bool
    message: str = ""
    record: dict | None = None


def workflow_context(record: dict) -> tuple:
    workflow = record.get("workflow", {})
    return tuple(
        workflow.get(key)
        for key in (
            "current_state",
            "current_phase",
            "current_step_index",
            "approval_round",
        )
    )


def require_current_context(current: dict, expected: dict) -> None:
    if not current:
        raise ECNConflict("单据已被删除，请关闭后刷新列表。")
    if workflow_context(current) != workflow_context(expected):
        raise ECNConflict("单据流程已变化，请关闭后重新打开再操作。")


def require_scheming(current: dict, expected: dict) -> None:
    require_current_context(current, expected)
    if current["workflow"]["current_state"] != ECNState.ECN_SCHEMING:
        raise ECNConflict("当前已不在方案编写阶段，不能继续修改或确认。")


def merge_fields(current: Any, baseline: Any, submitted: Any, path: str = "") -> Any:
    """三方合并：未改字段保留后台值；同字段不同修改拒绝；项目集合按增删合并。"""
    if submitted == baseline:
        return copy.deepcopy(current)
    if all(isinstance(value, dict) for value in (current, baseline, submitted)):
        result = copy.deepcopy(current)
        for key in baseline.keys() | submitted.keys():
            if key == "impact_change_log":
                continue
            result[key] = merge_fields(current.get(key), baseline.get(key), submitted.get(key), f"{path}/{key}")
        return result
    if path.rsplit("/", 1)[-1] in {"expanded_projects_mass", "expanded_projects_non_mass"}:
        removed = set(baseline or []) - set(submitted or [])
        added = [value for value in submitted or [] if value not in (baseline or [])]
        return list(dict.fromkeys([value for value in current or [] if value not in removed] + added))
    if current != baseline and current != submitted:
        raise ECNConflict(f"字段“{path.strip('/')}”已被其他页面修改，本次未保存，请刷新后核对。")
    return copy.deepcopy(submitted)


def sync_review_snapshot(local: dict, baseline: dict, fresh: dict) -> None:
    """轮询仅更新未编辑字段；保留正在输入的值和对应旧基线以便保存时检测冲突。"""
    for key in fresh.keys() | baseline.keys():
        if key == "impact_change_log":
            merge_ecn_impact_audit_log(local, fresh.get(key, []))
            baseline[key] = copy.deepcopy(fresh.get(key, []))
        elif all(isinstance(value, dict) for value in (local.get(key), baseline.get(key), fresh.get(key))):
            sync_review_snapshot(local[key], baseline[key], fresh[key])
        elif local.get(key) == baseline.get(key):
            local[key] = copy.deepcopy(fresh.get(key))
            baseline[key] = copy.deepcopy(fresh.get(key))


def update_review(current: dict, expected: dict, baseline: dict, submitted: dict, user: str) -> dict:
    require_scheming(current, expected)
    current["review_info"] = merge_fields(current.get("review_info", {}), baseline, submitted)
    merge_ecn_impact_audit_log(current["review_info"], submitted.get("impact_change_log", []))
    register_ecn_impact_handler(current, user, current["review_info"])
    return current


def save_scheme(current: dict, expected: dict, item: dict, original: dict | None, user: str) -> dict:
    require_scheming(current, expected)
    items = current.setdefault("change_items", [])
    existing = next((value for value in items if value.get("item_id") == item.get("item_id")), None)
    if item.get("author") != user:
        raise ECNConflict("只能保存本人编写的方案。")
    if original is not None:
        if existing is None or existing.get("author") != user:
            raise ECNConflict("方案已被删除或不属于本人，请刷新。")
        if existing != original:
            raise ECNConflict("该方案在打开编辑窗口后已被修改或确认，本次未保存，请重新打开编辑。")
    elif existing is not None:
        raise ECNConflict("方案已经保存，请勿重复添加。")
    updated = copy.deepcopy(item)
    if existing is not None:
        if existing.get("rejection_history") and build_ecn_scheme_snapshot(existing) == build_ecn_scheme_snapshot(
            updated
        ):
            raise ECNConflict("被驳回方案的内容尚未修改，请完成整改后再保存。")
        updated["review_status"] = existing.get("review_status")
        updated["rejection_history"] = copy.deepcopy(existing.get("rejection_history", []))
        if updated["rejection_history"]:
            updated["review_status"] = ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
        mark_rejected_scheme_item_revised(updated)
        items[items.index(existing)] = updated
    else:
        items.append(updated)
    current["workflow"].setdefault("scheme_participants", {})[user] = (
        ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
        if updated.get("review_status") == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
        else ECN_PARTICIPANT_STATUS_EDITING
    )
    return current


def delete_scheme(current: dict, expected: dict, original: dict, user: str) -> dict:
    require_scheming(current, expected)
    items = current.get("change_items", [])
    existing = next((value for value in items if value.get("item_id") == original.get("item_id")), None)
    if existing is None or existing.get("author") != user or existing != original:
        raise ECNConflict("方案已变化或不属于本人，请刷新后再删除。")
    items.remove(existing)
    participants = current["workflow"].setdefault("scheme_participants", {})
    if any(value.get("author") == user for value in items):
        participants[user] = ECN_PARTICIPANT_STATUS_EDITING
    else:
        participants.pop(user, None)
    return current


def confirm_participant(current: dict, expected: dict, user: str, status: str) -> dict:
    require_scheming(current, expected)
    if status not in {ECN_PARTICIPANT_STATUS_CONFIRMED, ECN_PARTICIPANT_STATUS_EDITING}:
        raise ECNConflict("无效的方案确认状态。")
    if status == ECN_PARTICIPANT_STATUS_CONFIRMED:

        def own_items(record):
            return [item for item in record.get("change_items", []) if item.get("author") == user]

        if own_items(current) != own_items(expected):
            raise ECNConflict("本人的方案已变化，请核对最新内容后再确认。")
        if ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION and has_unrevised_rejected_scheme_items(current, user):
            raise ECNConflict("仍有被驳回方案尚未修改，请先整改后再确认。")
        confirm_revised_scheme_items(current, user)
    current["workflow"].setdefault("scheme_participants", {})[user] = status
    return current
