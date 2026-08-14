# -*- encoding: utf-8 -*-
"""概述批量维护工具的项目筛选、数据构造与原子写入逻辑。"""

import copy
import re
import uuid
from datetime import datetime
from typing import Iterable, Optional

from . import db_storage

BATCH_OVERVIEW_TOOL_ROLES = {
    "admin",
    "研发经理",
    "研发电子主管",
    "研发结构",
    "研发软件",
    "研发光学",
    "研发硬件",
    "NPI工程",
}
BATCH_OVERVIEW_ALLOWED_PROJECT_STATES = ("待定", "研发", "转产")


def build_batch_result_lines(
    success_count: int,
    skipped: Iterable[str],
    failed: Iterable[str],
) -> list[str]:
    """生成不截断的批量处理结果明细。"""
    skipped_items = list(skipped)
    failed_items = list(failed)
    return [
        f"批量处理完成：成功 {success_count} 项，跳过 {len(skipped_items)} 项，失败 {len(failed_items)} 项。",
        *(f"跳过｜{item}" for item in skipped_items),
        *(f"失败｜{item}" for item in failed_items),
    ]


def build_project_category_map(category_names: Iterable[str]) -> dict[str, list[str]]:
    """按 project_table 页面的口径，把对外产品型号拆成大类与两位小类。"""
    categories: dict[str, list[str]] = {"所有": ["所有"]}
    for project in sorted({str(name) for name in category_names if name}):
        if "-" in project:
            parts = project.split("-")
            major = parts[0]
            sub = parts[1][:2]
            categories.setdefault(major, ["所有"])
            if sub and sub not in categories[major]:
                categories[major].append(sub)
        else:
            categories.setdefault("其它", ["所有"])
            if project not in categories["其它"]:
                categories["其它"].append(project)

    for major, values in categories.items():
        if major != "所有":
            values[1:] = sorted(values[1:], reverse=True)
    return categories


def build_project_model_range_options(
    category_names: Iterable[str],
    major: str,
    sub: str,
) -> list[str]:
    """生成指定大类/小类下的四位型号范围，如 RFFM-1007。"""
    if major in {"所有", "其它"} or sub == "所有":
        return ["所有"]
    ranges = set()
    for name in category_names:
        project = str(name or "")
        parts = project.split("-")
        if len(parts) < 2 or parts[0] != major or not parts[1].startswith(sub):
            continue
        if len(parts[1]) > len(sub):
            ranges.add(f"{major}-{parts[1][:4]}")
    return ["所有", *sorted(ranges, reverse=True)]


def project_matches_category(
    project: str,
    major: str,
    sub: str,
    model_range: str = "所有",
) -> bool:
    if major == "所有":
        return True
    if major == "其它":
        return "-" not in project and (sub == "所有" or project == sub)
    if not project.startswith(f"{major}-"):
        return False
    if sub != "所有" and not project.startswith(f"{major}-{sub}"):
        return False
    if model_range == "所有":
        return True
    return project == model_range or project.startswith(f"{model_range}-")


def filter_batch_projects(
    project_summary: dict,
    selected_states: Iterable[str],
    major: str,
    sub: str,
    model_range: str = "所有",
) -> list[str]:
    """按对外产品型号分类筛选，返回用于概述数据操作的内部产品型号。"""
    states = set(selected_states or [])
    projects = []
    for key, summary in project_summary.items():
        project = str(summary.get("sub_project") or key)
        category_project = str(summary.get("project") or project)
        if summary.get("state") not in states:
            continue
        if project_matches_category(category_project, major, sub, model_range):
            projects.append(project)
    return sorted(set(projects))


def find_projects_without_row_anchors(
    projects: Iterable[str],
    row_anchors: dict,
) -> list[str]:
    """返回尚未选择表格基准行的目标项目。"""
    return [str(project) for project in projects if not row_anchors.get(project)]


def collect_editable_overview_configs(over_config: dict, user_role: str, render_registry: dict) -> list[dict]:
    """展平当前角色有编辑权限的概述配置，并补齐分组/基准列元数据。"""
    result = []
    for role, groups in over_config.items():
        for group_name, items in groups.items():
            if not items:
                continue
            first_col_label = items[0].get("label", "")
            is_table_group = render_registry.get(group_name) == "OverviewTableGroup"
            for item in items:
                if user_role not in item.get("permission", {}).get("edit_role", []):
                    continue
                normalized = copy.deepcopy(item)
                normalized.update(
                    {
                        "role": role,
                        "group_name": group_name,
                        "first_col_label": first_col_label,
                        "is_table_group": is_table_group,
                    }
                )
                result.append(normalized)
    return result


def build_select_activ_dic(req_max_ver: str) -> dict[str, bool]:
    """生成与单项新增相同的版本激活字典。"""
    try:
        max_version = int(float(req_max_ver))
    except (TypeError, ValueError):
        max_version = 0
        req_max_ver = "0.0"
    return {f"{i}.0": f"{i}.0" == str(req_max_ver) for i in range(0, max_version + 1)}


def get_chip_state_visuals(processing_type: str, state: Optional[bool]) -> tuple[Optional[str], Optional[bool], str]:
    """返回与单项状态修改一致的 icon、enabled 与背景色。"""
    if state is None:
        return "question_mark", None, "bg-amber-5"
    if state is False:
        return "block", False, "bg-grey-5"
    active_icons = {
        "file": "attachment",
        "image": "image",
        "video": "play_circle",
        "search": "saved_search",
        "svn": "saved_search",
    }
    return active_icons.get(processing_type), True, "bg-light-blue-1"


def validate_overview_content(content: str, config: dict) -> bool:
    patterns = config.get("content_regular", [])
    return bool(content) and (not patterns or any(re.search(pattern, content) for pattern in patterns))


def build_new_overview_chip(
    *,
    project: str,
    config: dict,
    content: str,
    notes: str,
    creator: str,
    req_max_ver: str,
    row_id: Optional[str] = None,
    processing_type: Optional[str] = None,
    extra_data: Optional[dict] = None,
) -> dict:
    """按单项新增的数据结构构造一个新 chip。"""
    actual_type = processing_type or config.get("processing_type", "text")
    icon, enabled, bg_color = get_chip_state_visuals(actual_type, True)
    select_activ_dic = build_select_activ_dic(req_max_ver)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chip = {
        "id": str(uuid.uuid4()),
        "role": config.get("role", ""),
        "icon": icon,
        "enabled": enabled,
        "bg_color": bg_color,
        "type": actual_type,
        "content": content,
        "notes": notes,
        "creator": creator,
        "req_ver": req_max_ver,
        "select_activ_dic": select_activ_dic,
        "timestamp": {"%s" % now_str: {"creator": creator, "select_activ_dic": copy.deepcopy(select_activ_dic)}},
    }
    if config.get("is_table_group"):
        chip["row_id"] = row_id or str(uuid.uuid4())
    for key, value in (extra_data or {}).items():
        if value is not None:
            chip[key] = copy.deepcopy(value)
    return chip


def _is_duplicate_chip(existing: dict, candidate: dict) -> bool:
    if candidate.get("type") == "text" and candidate.get("row_id"):
        return existing.get("content") == candidate.get("content") and existing.get("row_id") == candidate.get("row_id")
    if candidate.get("type") == "test":
        return existing.get("content") == candidate.get("content") and existing.get(
            "test_select_data"
        ) == candidate.get("test_select_data")
    if candidate.get("type") == "svn":
        return existing.get("content") == candidate.get("content") and existing.get("warehouse") == candidate.get(
            "warehouse"
        )
    return existing.get("content") == candidate.get("content")


async def insert_overview_chip(project: str, label: str, chip: dict) -> tuple[bool, str]:
    """原子查重并新增 chip。"""
    inserted = {"value": False}

    def insert(current):
        current = current or {}
        if any(_is_duplicate_chip(existing, chip) for existing in current.values()):
            return db_storage.ATOMIC_NO_UPDATE
        current[chip["id"]] = copy.deepcopy(chip)
        inserted["value"] = True
        return current

    success = await db_storage.atomic_deep_update([f"{project}_over_data", label], insert)
    if not success:
        return False, "数据库写入失败"
    if not inserted["value"]:
        return False, "相同概述内容已存在"
    return True, "已新增"


async def update_overview_chip_state(
    project: str,
    label: str,
    chip_id: str,
    req_max_ver: str,
    target_state: Optional[bool],
    creator: str,
) -> tuple[bool, str, Optional[dict]]:
    """原子更新一个 chip 当前版本的激活状态。"""
    outcome: dict[str, object] = {"changed": False, "chip": None}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def update(chip):
        if not chip:
            return db_storage.ATOMIC_NO_UPDATE
        old_state = chip.get("select_activ_dic", {}).get(req_max_ver, chip.get("enabled"))
        if old_state is target_state:
            outcome["chip"] = copy.deepcopy(chip)
            return db_storage.ATOMIC_NO_UPDATE
        chip.setdefault("select_activ_dic", {})[req_max_ver] = target_state
        icon, enabled, bg_color = get_chip_state_visuals(chip.get("type", "text"), target_state)
        chip["icon"] = icon
        chip["enabled"] = enabled
        chip["bg_color"] = bg_color
        chip["creator"] = creator
        chip.setdefault("timestamp", {})[now_str] = {
            "creator": creator,
            "select_activ_dic": copy.deepcopy(chip["select_activ_dic"]),
        }
        outcome["changed"] = True
        outcome["chip"] = copy.deepcopy(chip)
        return chip

    success = await db_storage.atomic_deep_update([f"{project}_over_data", label, chip_id], update)
    if not success:
        return False, "数据库写入失败", None
    updated_chip = outcome["chip"]
    if not isinstance(updated_chip, dict):
        return False, "概述条目已不存在", None
    if outcome["changed"] is not True:
        return False, "目标状态与当前状态相同", updated_chip
    return True, "状态已修改", updated_chip


def is_first_column_row_active(project: str, first_col_label: str, row_id: str, req_max_ver: str) -> bool:
    chips = db_storage.get_deep_item([f"{project}_over_data", first_col_label], {})
    return any(
        chip.get("row_id") == row_id and chip.get("select_activ_dic", {}).get(req_max_ver, chip.get("enabled")) is True
        for chip in chips.values()
    )


async def cascade_deactivate_table_row(
    project: str,
    labels: Iterable[str],
    source_label: str,
    row_id: str,
    req_max_ver: str,
    creator: str,
) -> set[str]:
    """基准列失活时，按单项逻辑级联失活同行其它列。"""
    changed_labels = set()
    for label in labels:
        if label == source_label:
            continue
        chips = db_storage.get_deep_item([f"{project}_over_data", label], {})
        for chip_id, chip in chips.items():
            if chip.get("row_id") != row_id:
                continue
            changed, _, _ = await update_overview_chip_state(project, label, chip_id, req_max_ver, False, creator)
            if changed:
                changed_labels.add(label)
    return changed_labels


async def archive_related_record(project: str, label: str, chip_id: str, creator: str) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def archive(record):
        if not record or "open" not in record:
            return db_storage.ATOMIC_NO_UPDATE
        open_record = record.pop("open")
        open_record["close_time"] = now_str
        open_record["close_related_user"] = creator
        record[now_str] = open_record
        return record

    await db_storage.atomic_deep_update([f"{project}_over_related_record", label, chip_id], archive)


async def apply_related_overview_impacts(
    *,
    project: str,
    related_labels: Iterable[str],
    source_content: str,
    source_state: Optional[bool],
    operation_type: str,
    creator: str,
    config_flat: dict,
    overview_role: dict,
) -> set[str]:
    """把选定的关联概述中所有当前激活 chip 改为待定并记录来源。"""
    changed_labels = set()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record_entry = {
        "operate_user": creator,
        "operate_type": operation_type,
        "operate_chip_content": source_content,
        "operate_chip_state": source_state,
    }
    overview_data = db_storage.get_item(f"{project}_over_data", {})
    for related_label in related_labels:
        chips = overview_data.get(related_label, {})
        label_changed = False
        for chip_id, snapshot in chips.items():
            versions = list(snapshot.get("select_activ_dic", {}))
            if not versions:
                continue
            latest_version = max(
                versions,
                key=lambda value: float(value) if str(value).replace(".", "", 1).isdigit() else -1,
            )
            pending_result = {"eligible": False, "changed": False}

            def mark_pending(chip):
                if not chip:
                    return db_storage.ATOMIC_NO_UPDATE
                current_state = chip.get("select_activ_dic", {}).get(latest_version)
                if current_state is False:
                    return db_storage.ATOMIC_NO_UPDATE
                pending_result["eligible"] = True
                if current_state is True:
                    chip["select_activ_dic"][latest_version] = None
                    chip["enabled"] = None
                    chip["icon"] = "question_mark"
                    chip["bg_color"] = "bg-amber-5"
                    pending_result["changed"] = True
                    return chip
                return db_storage.ATOMIC_NO_UPDATE

            await db_storage.atomic_deep_update([f"{project}_over_data", related_label, chip_id], mark_pending)
            if not pending_result["eligible"]:
                continue

            related_role = config_flat.get(related_label, {}).get("role", "")
            related_user = overview_role.get(project, {}).get(related_role, {}).get("latest_user", "匿名用户")

            def append_record(open_record):
                if not open_record:
                    return {
                        "open_time": now_str,
                        "open_related_user": related_user,
                        "close_time": "",
                        "close_related_user": "",
                        "record": {now_str: record_entry},
                    }
                open_record.setdefault("record", {})[now_str] = record_entry
                return open_record

            await db_storage.atomic_deep_update(
                [f"{project}_over_related_record", related_label, chip_id, "open"], append_record
            )
            label_changed = True
        if label_changed:
            changed_labels.add(related_label)
    return changed_labels
