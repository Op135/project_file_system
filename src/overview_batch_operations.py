# -*- encoding: utf-8 -*-
"""概述批量维护工具的项目筛选、数据构造与原子写入逻辑。"""

import copy
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from nicegui import app

from . import db_storage
from .overview_change_workflow_config import OVERVIEW_CHANGE_WORKFLOW_CONFIG

BATCH_OVERVIEW_CONFIG = OVERVIEW_CHANGE_WORKFLOW_CONFIG["batch_overview"]
BATCH_OVERVIEW_TOOL_ROLES = BATCH_OVERVIEW_CONFIG["tool_roles"]
BATCH_OVERVIEW_ALLOWED_PROJECT_STATES = BATCH_OVERVIEW_CONFIG["allowed_project_states"]
BATCH_OVERVIEW_PREVENT_SELF_APPROVAL = BATCH_OVERVIEW_CONFIG["prevent_self_approval"]
BATCH_OVERVIEW_REQUESTS_KEY = "overview_batch_change_requests"
BATCH_OVERVIEW_STAGING_DIR = Path(__file__).resolve().parents[1] / ".overview_batch_staging"

# 单独维护“审核角色 -> 被审核的申请人角色”，避免把工具使用权限误当成审批权限。
BATCH_OVERVIEW_APPROVAL_ROLE_TARGETS = BATCH_OVERVIEW_CONFIG["approval_role_targets"]


def get_batch_overview_reviewer_roles(applicant_role: str) -> list[str]:
    """返回负责审核指定申请人角色的角色列表。"""
    return sorted(
        reviewer_role
        for reviewer_role, target_roles in BATCH_OVERVIEW_APPROVAL_ROLE_TARGETS.items()
        if applicant_role in target_roles
    )


def can_review_batch_overview_request(request: dict, reviewer: str, reviewer_role: str) -> bool:
    """判断当前用户能否审核申请；同一用户不能自审。"""
    configured_roles = get_batch_overview_reviewer_roles(str(request.get("submitter_role") or ""))
    return bool(
        reviewer
        and (not BATCH_OVERVIEW_PREVENT_SELF_APPROVAL or reviewer != request.get("submitter"))
        and reviewer_role in configured_roles
        and reviewer_role in request.get("reviewer_roles", [])
    )


def get_batch_overview_pending_count(
    requests: dict,
    current_user: str,
    current_role: str,
) -> int:
    """计算待办角标：审核人的待审批 + 申请人被驳回/执行失败后待处理。"""
    count = 0
    for request in requests.values():
        status = request.get("status")
        if status == "pending" and can_review_batch_overview_request(request, current_user, current_role):
            count += 1
        elif status in {"rejected", "failed"} and request.get("submitter") == current_user:
            count += 1
    return count


async def create_batch_overview_request(request: dict) -> tuple[bool, str]:
    """持久化一条批量概述申请。"""
    request_id = str(request.get("id") or uuid.uuid4())
    record = copy.deepcopy(request)
    record["id"] = request_id

    def insert(records):
        records = records or {}
        if request_id in records:
            return db_storage.ATOMIC_NO_UPDATE
        records[request_id] = record
        return records

    success = await db_storage.atomic_deep_update([BATCH_OVERVIEW_REQUESTS_KEY], insert)
    return success, request_id


async def update_batch_overview_request(request_id: str, changes: dict) -> bool:
    """原子更新申请，申请不存在时不创建。"""

    def update(request):
        if not request:
            return db_storage.ATOMIC_NO_UPDATE
        request.update(copy.deepcopy(changes))
        request["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return request

    return await db_storage.atomic_deep_update([BATCH_OVERVIEW_REQUESTS_KEY, request_id], update)


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


def overview_state_rank(state: Optional[bool]) -> int:
    """概述状态等级：激活 2、待定 1、失活 0。"""
    if state is True:
        return 2
    if state is None:
        return 1
    return 0


def is_overview_state_at_or_below(
    target_state: Optional[bool],
    reference_state: Optional[bool],
) -> bool:
    """目标状态等级是否低于或等于参照状态。"""
    return overview_state_rank(target_state) <= overview_state_rank(reference_state)


def get_first_column_row_state(
    project: str,
    first_col_label: str,
    row_id: Optional[str],
    req_ver: str,
) -> tuple[bool, Optional[bool]]:
    """读取同行首列在指定版本的最高状态；没有同行首列时返回 found=False。"""
    chips = db_storage.get_deep_item([f"{project}_over_data", first_col_label], {})
    states = [
        chip.get("select_activ_dic", {}).get(req_ver, chip.get("enabled"))
        for chip in chips.values()
        if chip.get("row_id") == row_id
    ]
    if not states:
        return False, None
    return True, max(states, key=overview_state_rank)


def is_table_child_state_allowed(
    project: str,
    first_col_label: str,
    row_id: Optional[str],
    req_ver: str,
    target_state: Optional[bool],
) -> bool:
    """非首列目标状态不得高于同行首列状态。"""
    found, first_col_state = get_first_column_row_state(project, first_col_label, row_id, req_ver)
    return found and is_overview_state_at_or_below(target_state, first_col_state)


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
    """兼容旧调用：判断同行首列是否允许子项设为激活。"""
    return is_table_child_state_allowed(project, first_col_label, row_id, req_max_ver, True)


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


def _refresh_batch_pending_label(project: str, label: str) -> None:
    """按现有单项维护口径刷新概述负责人待办。"""
    from .utils import update_overview_charge_pending_dic

    flat_config = app.storage.general.get("over_config_data_flat", {}).get(label, {})
    role = flat_config.get("role", "")
    latest_user = app.storage.general.get("overview_role", {}).get(project, {}).get(role, {}).get("latest_user", "")
    target_user = latest_user.split("：", 1)[1] if "：" in latest_user else latest_user
    if target_user and target_user != "——":
        update_overview_charge_pending_dic(
            scope="local",
            des_user=target_user,
            project_name=project,
            des_label=label,
        )


def _publish_batch_overview_changes(changed_pairs: set[tuple[str, str]], fallback_role: str) -> None:
    """刷新版本号、待办与负责人最近操作信息。"""
    from .components import OverviewVersionManager
    from .utils import overview_role_update

    for project, label in changed_pairs:
        OverviewVersionManager.bump(project, label)
        _refresh_batch_pending_label(project, label)
    flat_config = app.storage.general.get("over_config_data_flat", {})
    changed_roles = {
        (project, flat_config.get(label, {}).get("role", fallback_role)) for project, label in changed_pairs
    }
    for project, role in changed_roles:
        overview_role_update(project, role)


def _install_staged_media(payload: dict, config: dict) -> tuple[bool, str]:
    staged_path_value = str(payload.get("staged_file_path") or "").strip()
    if not staged_path_value:
        return True, ""
    staged_path = Path(staged_path_value).resolve()
    if not staged_path.is_relative_to(BATCH_OVERVIEW_STAGING_DIR.resolve()):
        return False, "申请暂存文件路径无效"
    upload_path_value = str(config.get("upload_path") or "").strip()
    if not upload_path_value:
        return False, "概述项未配置正式上传目录"
    upload_path = Path(upload_path_value)
    if not upload_path.is_dir():
        return False, f"正式上传目录不存在：{upload_path}"
    target_path = upload_path / Path(str(payload.get("content") or staged_path.name)).name
    if target_path.exists():
        staged_path.unlink(missing_ok=True)
        try:
            staged_path.parent.rmdir()
        except OSError:
            pass
        return True, ""
    if not staged_path.is_file():
        return False, "申请暂存文件已不存在"
    try:
        shutil.move(str(staged_path), str(target_path))
        try:
            staged_path.parent.rmdir()
        except OSError:
            pass
    except Exception as exc:
        return False, f"暂存文件转入正式目录失败：{exc}"
    return True, ""


async def execute_batch_overview_request(request: dict) -> dict:
    """审批通过后执行申请，并返回可归档的完整结果。"""
    from .utils import validate_search_path, validate_svn_url

    payload = copy.deepcopy(request.get("payload") or {})
    config_snapshot = copy.deepcopy(payload.get("config") or {})
    label = str(config_snapshot.get("label") or payload.get("label") or "")
    live_config = copy.deepcopy(app.storage.general.get("over_config_data_flat", {}).get(label, {}))
    config = live_config or config_snapshot
    for metadata_key in ("role", "group_name", "first_col_label", "is_table_group"):
        if metadata_key not in config and metadata_key in config_snapshot:
            config[metadata_key] = config_snapshot[metadata_key]
    action = payload.get("action")
    submitter = str(request.get("submitter") or "匿名用户")
    submitter_role = str(request.get("submitter_role") or "")
    selected_projects = [str(project) for project in payload.get("projects", [])]
    live_summary = {
        str(summary.get("sub_project") or key): summary
        for key, summary in app.storage.general.get("project_summary", {}).items()
    }
    selected_projects = [
        project
        for project in selected_projects
        if live_summary.get(project, {}).get("state") in BATCH_OVERVIEW_ALLOWED_PROJECT_STATES
    ]
    if not config or action not in {"add", "state"}:
        return {"ok": False, "message": "申请数据不完整，无法执行", "successes": [], "skipped": [], "failed": []}
    if submitter_role not in config.get("permission", {}).get("edit_role", []):
        return {
            "ok": False,
            "message": "申请人已不再具有该概述项的编辑权限",
            "successes": [],
            "skipped": [],
            "failed": [],
        }
    if not selected_projects:
        return {
            "ok": False,
            "message": "目标项目均已不在允许批量处理的状态范围内",
            "successes": [],
            "skipped": [],
            "failed": [],
        }

    media_ok, media_message = _install_staged_media(payload, config)
    if not media_ok:
        return {"ok": False, "message": media_message, "successes": [], "skipped": [], "failed": []}

    successes: list[dict] = []
    skipped: list[str] = []
    failed: list[str] = []
    changed_pairs: set[tuple[str, str]] = set()
    configured_related = {str(item) for item in config.get("impact_list", []) if item}
    related_labels = [
        str(item) for item in payload.get("related_labels", []) if item and str(item) in configured_related
    ]

    if action == "add":
        content = str(payload.get("content") or "")
        notes = str(payload.get("notes") or "")
        actual_type = str(payload.get("actual_type") or config.get("processing_type") or "text")
        common_extra = copy.deepcopy(payload.get("extra_data") or {})
        row_anchors = payload.get("row_anchors") or {}
        for project in selected_projects:
            try:
                req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                extra = copy.deepcopy(common_extra)
                if actual_type == "search":
                    valid, url_path, file_type, _, message = await validate_search_path(content, config, [project])
                    if not valid:
                        failed.append(f"{project}：{message}")
                        continue
                    extra.update({"url_path": url_path, "file_type": file_type})
                elif actual_type == "svn":
                    valid, url_path, file_type, message = await validate_svn_url(content, config, [project])
                    if not valid:
                        failed.append(f"{project}：{message}")
                        continue
                    extra.update(
                        {
                            "url_path": url_path,
                            "file_type": file_type,
                            "warehouse": config.get("state_path", {}).get(live_summary.get(project, {}).get("state")),
                        }
                    )
                row_id = None
                if config.get("is_table_group"):
                    if label == config.get("first_col_label"):
                        row_id = str(uuid.uuid4())
                    else:
                        row_id = row_anchors.get(project)
                        first_chips = db_storage.get_deep_item(
                            [f"{project}_over_data", config.get("first_col_label", "")], {}
                        )
                        anchor_active = any(
                            chip.get("row_id") == row_id
                            and chip.get("select_activ_dic", {}).get(req_max_ver, chip.get("enabled")) is True
                            for chip in first_chips.values()
                        )
                        if not anchor_active:
                            failed.append(f"{project}：表格绑定行已不存在或不再激活")
                            continue
                chip = build_new_overview_chip(
                    project=project,
                    config=config,
                    content=content,
                    notes=notes,
                    creator=submitter,
                    req_max_ver=req_max_ver,
                    row_id=row_id,
                    processing_type=actual_type,
                    extra_data=extra,
                )
                inserted, message = await insert_overview_chip(project, label, chip)
                if not inserted:
                    skipped.append(f"{project}：{message}")
                    continue
                successes.append(
                    {
                        "project": project,
                        "label": label,
                        "chip_id": chip["id"],
                        "content": content,
                        "state": True,
                        "operation_type": "add_chip",
                    }
                )
                changed_pairs.add((project, label))
            except Exception as exc:
                failed.append(f"{project}：{exc}")
    else:
        target_state = payload.get("target_state")
        live_groups = app.storage.general.get("over_config_data", {}).get(config.get("role", ""), {})
        live_group_items = live_groups.get(config.get("group_name", ""), [])
        group_labels = [str(item.get("label")) for item in live_group_items if item.get("label")]
        if not group_labels:
            group_labels = [str(item) for item in payload.get("group_labels", []) if item]
        for target in payload.get("chip_targets", []):
            project = str(target.get("project") or "")
            chip_id = str(target.get("chip_id") or "")
            if project not in selected_projects:
                skipped.append(f"{project}：项目已不在当前允许范围")
                continue
            try:
                req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                current_chip = db_storage.get_deep_item([f"{project}_over_data", label, chip_id], {})
                if not current_chip:
                    skipped.append(f"{project}：概述条目已不存在")
                    continue
                row_id = current_chip.get("row_id")
                if (
                    config.get("is_table_group")
                    and label != config.get("first_col_label")
                    and not is_table_child_state_allowed(
                        project,
                        str(config.get("first_col_label") or ""),
                        row_id,
                        req_max_ver,
                        target_state,
                    )
                ):
                    failed.append(f"{project}：目标状态等级不能高于同行首列概述状态")
                    continue
                if target_state is True and current_chip.get("type") == "search":
                    valid, _, _, _, message = await validate_search_path(current_chip.get("content", ""), config, [project])
                    if not valid:
                        failed.append(f"{project}：{message}")
                        continue
                if target_state is True and current_chip.get("type") == "svn":
                    valid, _, _, message = await validate_svn_url(current_chip.get("content", ""), config, [project])
                    if not valid:
                        failed.append(f"{project}：{message}")
                        continue
                changed, message, updated_chip = await update_overview_chip_state(
                    project, label, chip_id, req_max_ver, target_state, submitter
                )
                if not changed or updated_chip is None:
                    skipped.append(f"{project}：{message}")
                    continue
                await archive_related_record(project, label, chip_id, submitter)
                successes.append(
                    {
                        "project": project,
                        "label": label,
                        "chip_id": chip_id,
                        "content": updated_chip.get("content", ""),
                        "state": target_state,
                        "operation_type": "activ_change",
                    }
                )
                changed_pairs.add((project, label))
                if target_state is False and config.get("is_table_group") and label == config.get("first_col_label") and row_id:
                    cascaded = await cascade_deactivate_table_row(
                        project, group_labels, label, row_id, req_max_ver, submitter
                    )
                    changed_pairs.update((project, changed_label) for changed_label in cascaded)
            except Exception as exc:
                failed.append(f"{project}：{exc}")

    if related_labels:
        flat_config = app.storage.general.get("over_config_data_flat", {})
        overview_role = app.storage.general.get("overview_role", {})
        for operation in successes:
            changed_related = await apply_related_overview_impacts(
                project=operation["project"],
                related_labels=related_labels,
                source_content=operation["content"],
                source_state=operation["state"],
                operation_type=operation["operation_type"],
                creator=submitter,
                config_flat=flat_config,
                overview_role=overview_role,
            )
            changed_pairs.update((operation["project"], changed_label) for changed_label in changed_related)

    if changed_pairs:
        _publish_batch_overview_changes(changed_pairs, str(config.get("role") or ""))
    message = build_batch_result_lines(len(successes), skipped, failed)[0]
    return {
        "ok": bool(successes) and not failed,
        "message": message,
        "successes": successes,
        "skipped": skipped,
        "failed": failed,
    }
