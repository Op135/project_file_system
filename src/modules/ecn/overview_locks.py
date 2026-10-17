"""ECN 系统内资料方案对正式概述条目的占用规则。"""

from __future__ import annotations

from typing import Any

from ...ecn_management_config import (
    ECN_EXECUTION_RESULT_SUCCESS,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_UPDATE,
    ECNState,
)


ECN_OVERVIEW_LOCKED_ACTIONS = {
    ECN_OVERVIEW_ACTION_UPDATE,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
}
ECN_OVERVIEW_UNLOCKED_STATES = {ECNState.CLOSED, ECNState.CANCEL}


def collect_ecn_overview_locks(records: Any) -> dict[tuple[str, str, str], dict[str, str]]:
    """从最新 ECN 集合派生仍在占用中的概述条目，不保存容易残留的第二份锁。"""
    locks: dict[tuple[str, str, str], dict[str, str]] = {}
    if not isinstance(records, dict):
        return locks
    for record_key, record in records.items():
        if not isinstance(record, dict):
            continue
        workflow = record.get("workflow", {})
        if not isinstance(workflow, dict) or workflow.get("current_state") in ECN_OVERVIEW_UNLOCKED_STATES:
            continue
        ecn_id = str(record.get("ecn_id") or record_key or "").strip()
        items = record.get("change_items", [])
        if not isinstance(items, list):
            continue
        for item in items:
            if (
                not isinstance(item, dict)
                or item.get("type") != "overview_update"
                or item.get("execute_status") == ECN_EXECUTION_RESULT_SUCCESS
            ):
                continue
            item_id = str(item.get("item_id") or "").strip()
            label = str(item.get("label") or "").strip()
            project_states = item.get("project_states", {})
            if not item_id or not label or not isinstance(project_states, dict):
                continue
            for project, project_state in project_states.items():
                if (
                    not isinstance(project_state, dict)
                    or project_state.get("action") not in ECN_OVERVIEW_LOCKED_ACTIONS
                ):
                    continue
                chip_id = str(project_state.get("chip_id") or "").strip()
                project_name = str(project or "").strip()
                if not project_name or not chip_id:
                    continue
                locks[(project_name, label, chip_id)] = {
                    "ecn_id": ecn_id,
                    "item_id": item_id,
                }
    return locks


def find_changed_ecn_locked_overviews(
    top_key: str,
    before: Any,
    after: Any,
    records: Any,
) -> list[dict[str, str]]:
    """返回一次普通写入中被改动或删除的 ECN 占用概述。"""
    suffix = "_over_data"
    if not top_key.endswith(suffix):
        return []
    project = top_key[: -len(suffix)]
    before_data = before if isinstance(before, dict) else {}
    after_data = after if isinstance(after, dict) else {}
    changed: list[dict[str, str]] = []
    for (locked_project, label, chip_id), owner in collect_ecn_overview_locks(records).items():
        if locked_project != project:
            continue
        before_label = before_data.get(label, {})
        after_label = after_data.get(label, {})
        before_chip = before_label.get(chip_id) if isinstance(before_label, dict) else None
        after_chip = after_label.get(chip_id) if isinstance(after_label, dict) else None
        if before_chip != after_chip:
            changed.append(
                {
                    "project": project,
                    "label": label,
                    "chip_id": chip_id,
                    **owner,
                }
            )
    return changed

