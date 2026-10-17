"""ECN 系统内资料方案对正式概述条目的占用规则。"""

from __future__ import annotations

from typing import Any
import uuid

from ...ecn_management_config import (
    ECN_EXECUTION_RESULT_SUCCESS,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_UPDATE,
    ECNState,
)


ECN_OVERVIEW_LOCKED_ACTIONS = {
    ECN_OVERVIEW_ACTION_UPDATE,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
}
ECN_OVERVIEW_UNLOCKED_STATES = {ECNState.CLOSED, ECNState.CANCEL}


def active_overview_row_exists(project_data: Any, label: str, row_id: str, version: str) -> bool:
    label_data = project_data.get(label, {}) if isinstance(project_data, dict) else {}
    return bool(row_id) and isinstance(label_data, dict) and any(
        isinstance(chip, dict)
        and str(chip.get("row_id") or "") == row_id
        and chip.get("select_activ_dic", {}).get(version) is True
        for chip in label_data.values()
    )


def collect_ecn_overview_row_dependencies(records: Any) -> list[dict[str, str]]:
    """新增子项对已有基准行的依赖；多个方案可以共同依赖同一行。"""
    dependencies: list[dict[str, str]] = []
    if not isinstance(records, dict):
        return dependencies
    for ecn_id, record in records.items():
        if not isinstance(record, dict) or record.get("workflow", {}).get("current_state") in ECN_OVERVIEW_UNLOCKED_STATES:
            continue
        for item in record.get("change_items", []):
            if (
                not isinstance(item, dict) or item.get("type") != "overview_update"
                or item.get("execute_status") == ECN_EXECUTION_RESULT_SUCCESS
                or not item.get("first_col_label") or item.get("label") == item.get("first_col_label")
            ):
                continue
            for project, state in item.get("project_states", {}).items():
                row_id = str(state.get("anchor_row_id") or "") if isinstance(state, dict) else ""
                if not isinstance(state, dict) or state.get("action") != "add" or not row_id:
                    continue
                if row_id.startswith("PENDING_NEW_"):
                    source_id = row_id[len("PENDING_NEW_"):]
                    row_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ecn:{ecn_id}:{source_id}:{project}:row"))
                dependencies.append({
                    "ecn_id": str(ecn_id), "item_id": str(item.get("item_id") or ""),
                    "project": str(project), "label": str(item["first_col_label"]), "row_id": row_id,
                })
    return dependencies


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
                if not isinstance(project_state, dict):
                    continue
                project_name = str(project or "").strip()
                if not project_name:
                    continue
                action = project_state.get("action")
                chip_ids: list[str] = []
                if action in ECN_OVERVIEW_LOCKED_ACTIONS:
                    chip_ids.append(str(project_state.get("chip_id") or "").strip())
                if action in {ECN_OVERVIEW_ACTION_ADD, ECN_OVERVIEW_ACTION_UPDATE}:
                    # 落盘和进度检查点是两个事务；提前派生新节点锁，覆盖两者之间的窗口。
                    chip_ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"ecn:{ecn_id}:{item_id}:{project_name}:chip")))
                for chip_id in chip_ids:
                    if chip_id:
                        locks[(project_name, label, chip_id)] = {"ecn_id": ecn_id, "item_id": item_id}
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
    for dependency in collect_ecn_overview_row_dependencies(records):
        if dependency["project"] != project:
            continue
        label, row_id = dependency["label"], dependency["row_id"]

        def row_contents(data: dict) -> dict:
            values = data.get(label, {})
            return {
                key: chip for key, chip in values.items()
                if isinstance(chip, dict) and str(chip.get("row_id") or "") == row_id
            } if isinstance(values, dict) else {}

        if row_contents(before_data) != row_contents(after_data):
            changed.append({**dependency, "chip_id": ""})
    return changed

