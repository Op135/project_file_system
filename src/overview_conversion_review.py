# -*- encoding: utf-8 -*-
"""研发转转产时按职能汇总概述待定确认任务。"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from nicegui import app

from . import db_storage
from .overview_batch_operations import (
    archive_related_record,
    is_table_child_state_allowed,
    update_overview_chip_state,
)
from .overview_operation import (
    get_automatic_overview_reason,
    get_latest_overview_reason,
    get_overview_timestamp_items,
)
from .project_overview_access import can_edit_overview_item


OVERVIEW_CONVERSION_REVIEWS_KEY = "overview_conversion_reviews"
OPEN_REVIEW_STATUSES = frozenset({"pending", "exceptions"})
_review_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(frozen=True)
class PendingReason:
    """一条仍应辅助负责人判断的待定原因。"""

    category: str
    text: str
    time: str = ""
    operator: str = ""
    source: str = ""


@dataclass(frozen=True)
class ConversionPendingItem:
    """职能汇总待办中的一个“概述条目 × 当前需求版本”。"""

    target_id: str
    role: str
    group_name: str
    label: str
    title: str
    chip_id: str
    version: str
    content: str
    creator: str
    origin: str
    reasons: tuple[PendingReason, ...]
    config_order: int
    is_table_group: bool
    first_col_label: str
    row_id: str

    def is_special_for_origin(self, origin: str) -> bool:
        conversion_reason = get_automatic_overview_reason("conversion_pending")
        return origin != "conversion" or any(
            reason.category != "conversion" or reason.text != conversion_reason for reason in self.reasons
        )

    @property
    def has_special_reason(self) -> bool:
        return self.is_special_for_origin(self.origin)


@dataclass(frozen=True)
class ConversionRolePreview:
    role: str
    assignee: str
    total_count: int
    conversion_count: int
    existing_pending_count: int
    special_items: tuple[ConversionPendingItem, ...]
    permission_missing_titles: tuple[str, ...]


@dataclass(frozen=True)
class ConversionReviewResult:
    ok: bool
    message: str
    changed_count: int = 0
    remaining_count: int = 0
    skipped_count: int = 0
    changed_labels: frozenset[str] = frozenset()
    failures: tuple[str, ...] = ()


def conversion_target_id(label: object, chip_id: object, version: object) -> str:
    """用无歧义 JSON 表达汇总任务目标。"""
    return json.dumps(
        [str(label), str(chip_id), str(version)],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_conversion_target_id(value: object) -> tuple[str, str, str] | None:
    try:
        parts = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parts, list) or len(parts) != 3:
        return None
    return str(parts[0]), str(parts[1]), str(parts[2])


def _role_assignee(project_name: str, role: str) -> str:
    raw_value = (
        app.storage.general.get("overview_role", {})
        .get(project_name, {})
        .get(role, {})
        .get("latest_user", "")
    )
    value = str(raw_value or "").strip()
    if "：" in value:
        value = value.split("：", 1)[1].strip()
    return "" if value == "——" else value


def _config_index(over_config: Mapping[str, object]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for role_value, groups_value in over_config.items():
        if not isinstance(groups_value, Mapping):
            continue
        role = str(role_value or "未分类")
        for group_value, configs_value in groups_value.items():
            if not isinstance(configs_value, list):
                continue
            group_name = str(group_value or "未分组")
            first_col_label = ""
            if configs_value and isinstance(configs_value[0], Mapping):
                first_col_label = str(configs_value[0].get("label") or "")
            for config_order, config_value in enumerate(configs_value):
                if not isinstance(config_value, Mapping):
                    continue
                label = str(config_value.get("label") or "")
                if not label:
                    continue
                config = dict(config_value)
                config.update(
                    {
                        "role": role,
                        "group_name": group_name,
                        "title": str(config_value.get("title") or label),
                        "config_order": config_order,
                        "first_col_label": first_col_label,
                    }
                )
                index[label] = config
    return index


def _pending_transition_reason(chip: Mapping[str, object], version: str) -> PendingReason:
    missing = object()
    previous_state: object = missing
    selected_time = ""
    selected_record: Mapping[str, object] = {}
    for time_text, record in get_overview_timestamp_items(chip):
        states = record.get("select_activ_dic")
        if not isinstance(states, Mapping) or version not in states:
            continue
        state = states[version]
        if state is None and previous_state is not None:
            selected_time = time_text
            selected_record = record
        previous_state = state

    reason_text = str(selected_record.get("reason") or "").strip()
    if not reason_text:
        reason_text = get_latest_overview_reason(chip).strip() or "未记录待定原因"
    conversion_reason = get_automatic_overview_reason("conversion_pending")
    if reason_text == conversion_reason:
        category = "conversion"
    elif "关联" in reason_text or "影响" in reason_text:
        category = "related"
    elif "需求" in reason_text:
        category = "requirement"
    else:
        category = "manual"
    return PendingReason(
        category=category,
        text=reason_text,
        time=selected_time,
        operator=str(selected_record.get("creator") or chip.get("creator") or "未知"),
        source=str(selected_record.get("source_id") or ""),
    )


def _related_pending_reasons(
    related_records: Mapping[str, object],
    label: str,
    chip_id: str,
) -> list[PendingReason]:
    chip_records = related_records.get(label, {})
    if not isinstance(chip_records, Mapping):
        return []
    record_value = chip_records.get(chip_id, {})
    if not isinstance(record_value, Mapping):
        return []
    open_value = record_value.get("open", {})
    if not isinstance(open_value, Mapping):
        return []
    causes_value = open_value.get("record", {})
    if not isinstance(causes_value, Mapping):
        return []

    reasons: list[PendingReason] = []
    for time_value, cause_value in causes_value.items():
        if not isinstance(cause_value, Mapping):
            continue
        source_content = str(cause_value.get("operate_chip_content") or "").strip()
        operation_type = str(cause_value.get("operate_type") or "概述变动").strip()
        operation_text = {
            "add_chip": "新增概述",
            "activ_change": "概述状态变更",
        }.get(operation_type, operation_type)
        detail = f"{operation_text}影响"
        if source_content:
            detail += f"：{source_content}"
        reasons.append(
            PendingReason(
                category="related",
                text=detail,
                time=str(time_value),
                operator=str(cause_value.get("operate_user") or open_value.get("open_related_user") or "未知"),
            )
        )
    return reasons


def collect_conversion_pending_items(
    project_name: str,
    version: str,
    *,
    target_origins: Mapping[str, str] | None = None,
) -> dict[str, list[ConversionPendingItem]]:
    """按职能收集当前版本的真实待定条目，并合并未关闭的关联影响原因。"""
    overview_data = db_storage.get_item(f"{project_name}_over_data", {})
    over_config = app.storage.general.get("over_config_data", {})
    related_records = db_storage.get_item(f"{project_name}_over_related_record", {})
    if not isinstance(overview_data, Mapping) or not isinstance(over_config, Mapping):
        return {}
    if not isinstance(related_records, Mapping):
        related_records = {}
    config_by_label = _config_index(over_config)
    origins = target_origins or {}
    grouped: dict[str, list[ConversionPendingItem]] = {}

    for label_value, chips_value in overview_data.items():
        label = str(label_value)
        config = config_by_label.get(label)
        if config is None or not isinstance(chips_value, Mapping):
            continue
        role = str(config.get("role") or "未分类")
        for chip_id_value, chip_value in chips_value.items():
            if not isinstance(chip_value, Mapping):
                continue
            states = chip_value.get("select_activ_dic")
            if not isinstance(states, Mapping) or version not in states or states.get(version) is not None:
                continue
            chip_id = str(chip_id_value)
            target_id = conversion_target_id(label, chip_id, version)
            reason_values = [_pending_transition_reason(chip_value, version)]
            reason_values.extend(_related_pending_reasons(related_records, label, chip_id))
            deduplicated: list[PendingReason] = []
            seen_reasons: set[tuple[str, str, str, str]] = set()
            for reason in reason_values:
                reason_key = (reason.category, reason.text, reason.time, reason.operator)
                if reason_key in seen_reasons:
                    continue
                seen_reasons.add(reason_key)
                deduplicated.append(reason)
            item = ConversionPendingItem(
                target_id=target_id,
                role=role,
                group_name=str(config.get("group_name") or "未分组"),
                label=label,
                title=str(config.get("title") or label),
                chip_id=chip_id,
                version=version,
                content=str(chip_value.get("content") or "无内容"),
                creator=str(chip_value.get("creator") or "未知"),
                origin=str(origins.get(target_id) or "post_conversion"),
                reasons=tuple(deduplicated),
                config_order=int(config.get("config_order") or 0),
                is_table_group=bool(config.get("first_col_label")) and bool(chip_value.get("row_id")),
                first_col_label=str(config.get("first_col_label") or ""),
                row_id=str(chip_value.get("row_id") or ""),
            )
            grouped.setdefault(role, []).append(item)

    for role_items in grouped.values():
        role_items.sort(
            key=lambda item: (
                0 if item.has_special_reason else 1,
                item.group_name,
                item.config_order,
                item.title,
                item.content,
                item.chip_id,
            )
        )
    return grouped


def build_conversion_preview(project_name: str, version: str) -> list[ConversionRolePreview]:
    """在真正切换前预览会进入各职能任务的激活项和既有待定项。"""
    overview_data = db_storage.get_item(f"{project_name}_over_data", {})
    over_config = app.storage.general.get("over_config_data", {})
    if not isinstance(overview_data, Mapping) or not isinstance(over_config, Mapping):
        return []
    config_by_label = _config_index(over_config)
    conversion_ids: set[str] = set()
    existing_ids: set[str] = set()
    for label_value, chips_value in overview_data.items():
        label = str(label_value)
        if label not in config_by_label or not isinstance(chips_value, Mapping):
            continue
        for chip_id_value, chip_value in chips_value.items():
            if not isinstance(chip_value, Mapping):
                continue
            states = chip_value.get("select_activ_dic")
            if not isinstance(states, Mapping):
                continue
            state = states.get(version)
            target_id = conversion_target_id(label, chip_id_value, version)
            if state is True:
                conversion_ids.add(target_id)
            elif state is None:
                existing_ids.add(target_id)

    origins = {target_id: "conversion" for target_id in conversion_ids}
    origins.update({target_id: "preexisting" for target_id in existing_ids})
    pending_items = collect_conversion_pending_items(project_name, version, target_origins=origins)
    # 预览发生在状态变更前，因此把仍为激活的目标临时转成展示用的待定项快照。
    config_by_role: dict[str, list[tuple[str, str, Mapping[str, object]]]] = {}
    for label_value, chips_value in overview_data.items():
        label = str(label_value)
        config = config_by_label.get(label)
        if config is None or not isinstance(chips_value, Mapping):
            continue
        role = str(config.get("role") or "未分类")
        for chip_id_value, chip_value in chips_value.items():
            if not isinstance(chip_value, Mapping):
                continue
            states = chip_value.get("select_activ_dic")
            if isinstance(states, Mapping) and states.get(version) is True:
                config_by_role.setdefault(role, []).append((label, str(chip_id_value), chip_value))

    previews: list[ConversionRolePreview] = []
    roles = set(pending_items) | set(config_by_role)
    for role in sorted(roles):
        existing_items = pending_items.get(role, [])
        conversion_count = len(config_by_role.get(role, []))
        total_count = conversion_count + len(existing_items)
        if total_count == 0:
            continue
        assignee = _role_assignee(project_name, role)
        target_labels = {item.label for item in existing_items}
        target_labels.update(label for label, _chip_id, _chip in config_by_role.get(role, []))
        permission_missing_titles: list[str] = []
        if assignee:
            actor_role = _actor_role(assignee)
            for label in sorted(target_labels):
                config = config_by_label.get(label)
                if config is not None and not can_edit_overview_item(config, actor_role, assignee):
                    permission_missing_titles.append(str(config.get("title") or label))
        previews.append(
            ConversionRolePreview(
                role=role,
                assignee=assignee,
                total_count=total_count,
                conversion_count=conversion_count,
                existing_pending_count=len(existing_items),
                special_items=tuple(existing_items),
                permission_missing_titles=tuple(permission_missing_titles),
            )
        )
    return previews


def get_conversion_reviews() -> dict[str, dict]:
    value = db_storage.get_item(OVERVIEW_CONVERSION_REVIEWS_KEY, {})
    return value if isinstance(value, dict) else {}


def get_actionable_conversion_reviews(username: str) -> list[dict]:
    reviews = get_conversion_reviews()
    result: list[dict] = []
    for review in reviews.values():
        if (
            not isinstance(review, dict)
            or review.get("status") not in OPEN_REVIEW_STATUSES
            or review.get("assignee") != username
        ):
            continue
        items = collect_review_pending_items(review)
        if items and all(_actor_can_edit_item(username, item) for item in items):
            result.append(copy.deepcopy(review))
    result.sort(key=lambda review: (str(review.get("project") or ""), str(review.get("role") or "")))
    return result


def get_conversion_review_pending_count(username: str) -> int:
    return len(get_actionable_conversion_reviews(username))


def get_conversion_covered_labels(username: str) -> dict[str, set[str]]:
    covered: dict[str, set[str]] = {}
    for review in get_actionable_conversion_reviews(username):
        project = str(review.get("project") or "")
        for target_id in review.get("current_target_ids", []):
            parsed = parse_conversion_target_id(target_id)
            if parsed is not None:
                covered.setdefault(project, set()).add(parsed[0])
    return covered


def _target_origin_map(review: Mapping[str, object]) -> dict[str, str]:
    known_targets = review.get("known_targets", {})
    if not isinstance(known_targets, Mapping):
        return {}
    return {
        str(target_id): str(target.get("origin") or "post_conversion")
        for target_id, target in known_targets.items()
        if isinstance(target, Mapping)
    }


def collect_review_pending_items(review: Mapping[str, object]) -> list[ConversionPendingItem]:
    project = str(review.get("project") or "")
    version = str(review.get("version") or "")
    role = str(review.get("role") or "")
    if not project or not version or not role:
        return []
    return collect_conversion_pending_items(
        project,
        version,
        target_origins=_target_origin_map(review),
    ).get(role, [])


def _inferred_origin(item: ConversionPendingItem) -> str:
    """兼容功能上线前已经转产的项目：明确只有转产原因时仍归为常规转产项。"""
    if item.reasons and all(reason.category == "conversion" for reason in item.reasons):
        return "conversion"
    return "post_conversion"


async def reconcile_conversion_reviews(
    project_name: str,
    *,
    initial_origins: Mapping[str, str] | None = None,
    transition_at: str = "",
    actor: str = "",
    create_missing: bool = True,
) -> bool:
    """以概述真实状态校准当前项目的职能汇总任务。"""
    version_value = app.storage.general.get("project_req_max_ver", {}).get(project_name)
    if version_value is None:
        return False
    version = str(version_value)
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    initial_origin_map = {str(key): str(value) for key, value in (initial_origins or {}).items()}
    project_state = str(app.storage.general.get("project_summary", {}).get(project_name, {}).get("state") or "")

    existing_reviews = get_conversion_reviews()
    open_origin_by_role: dict[str, dict[str, str]] = {}
    for review in existing_reviews.values():
        if not isinstance(review, Mapping):
            continue
        if (
            review.get("project") == project_name
            and str(review.get("version") or "") == version
            and review.get("status") in OPEN_REVIEW_STATUSES
        ):
            open_origin_by_role[str(review.get("role") or "")] = _target_origin_map(review)
    combined_origins = dict(initial_origin_map)
    for role_origins in open_origin_by_role.values():
        combined_origins.update(role_origins)
    pending_by_role = collect_conversion_pending_items(project_name, version, target_origins=combined_origins)

    def update_reviews(current):
        reviews = current if isinstance(current, dict) else {}
        active_by_role: dict[str, tuple[str, dict]] = {}
        rounds_by_role: dict[str, int] = {}
        for review_id, review_value in reviews.items():
            if not isinstance(review_value, dict) or review_value.get("project") != project_name:
                continue
            if str(review_value.get("version") or "") != version:
                continue
            role_name = str(review_value.get("role") or "")
            rounds_by_role[role_name] = max(rounds_by_role.get(role_name, 0), int(review_value.get("round") or 0))
            if review_value.get("status") in OPEN_REVIEW_STATUSES:
                active_by_role[role_name] = (str(review_id), review_value)

        for role_name, (review_id, review) in active_by_role.items():
            items = pending_by_role.get(role_name, [])
            known_targets = review.setdefault("known_targets", {})
            for item in items:
                if item.target_id not in known_targets:
                    known_targets[item.target_id] = {
                        "origin": initial_origin_map.get(item.target_id, _inferred_origin(item)),
                        "joined_at": now_text,
                    }
            review["current_target_ids"] = [item.target_id for item in items]
            review["current_count"] = len(items)
            review["special_count"] = sum(
                1
                for item in items
                if item.is_special_for_origin(
                    str(known_targets.get(item.target_id, {}).get("origin") or "post_conversion")
                )
            )
            assignee = _role_assignee(project_name, role_name)
            if assignee:
                review["assignee"] = assignee
            review["updated_at"] = now_text
            if not items:
                review["status"] = "completed"
                review["completed_at"] = now_text
                review["exception_target_ids"] = []
            reviews[review_id] = review

        may_create = create_missing and project_state == "转产"
        if may_create:
            for role_name, items in pending_by_role.items():
                if not items or role_name in active_by_role:
                    continue
                assignee = _role_assignee(project_name, role_name)
                if not assignee:
                    continue
                review_id = str(uuid.uuid4())
                round_value = rounds_by_role.get(role_name, 0) + 1
                known_targets = {
                    item.target_id: {
                        "origin": initial_origin_map.get(item.target_id, _inferred_origin(item)),
                        "joined_at": transition_at or now_text,
                    }
                    for item in items
                }
                reviews[review_id] = {
                    "id": review_id,
                    "project": project_name,
                    "role": role_name,
                    "assignee": assignee,
                    "version": version,
                    "round": round_value,
                    "status": "pending",
                    "created_at": transition_at or now_text,
                    "updated_at": now_text,
                    "created_by": actor,
                    "known_targets": known_targets,
                    "current_target_ids": [item.target_id for item in items],
                    "current_count": len(items),
                    "special_count": sum(
                        1
                        for item in items
                        if item.is_special_for_origin(
                            str(known_targets.get(item.target_id, {}).get("origin") or "post_conversion")
                        )
                    ),
                    "exception_target_ids": [],
                }
        return reviews

    return await db_storage.atomic_deep_update([OVERVIEW_CONVERSION_REVIEWS_KEY], update_reviews)


def schedule_conversion_review_reconciliation(project_name: str) -> None:
    """从同步的既有概述刷新链路中安全触发汇总任务校准。"""
    import asyncio

    if str(app.storage.general.get("project_summary", {}).get(project_name, {}).get("state") or "") != "转产":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(reconcile_conversion_reviews(project_name))


def _actor_role(actor: str) -> str:
    service = getattr(app.state, "user_service", None)
    if service is None:
        return ""
    user = service.get_user(actor)
    return str(user.get("role") or "") if isinstance(user, dict) else ""


def _actor_can_edit_item(actor: str, item: ConversionPendingItem) -> bool:
    config = app.storage.general.get("over_config_data_flat", {}).get(item.label)
    return isinstance(config, dict) and can_edit_overview_item(config, _actor_role(actor), actor)


async def _validate_item_activation(project_name: str, item: ConversionPendingItem) -> tuple[bool, str]:
    config = app.storage.general.get("over_config_data_flat", {}).get(item.label, {})
    if not isinstance(config, dict):
        return False, "概述配置已不存在"
    chip = db_storage.get_deep_item([f"{project_name}_over_data", item.label, item.chip_id], {})
    if not isinstance(chip, dict):
        return False, "概述条目已不存在"
    if item.is_table_group and item.label != item.first_col_label and not is_table_child_state_allowed(
        project_name,
        item.first_col_label,
        item.row_id,
        item.version,
        True,
    ):
        return False, "同行首列仍为待定或失活，子项不能先激活"
    processing_type = str(chip.get("type") or config.get("processing_type") or "")
    if processing_type == "search":
        from .utils import validate_search_path

        valid, _a, _b, _c, message = await validate_search_path(item.content, config, [project_name])
        return bool(valid), str(message or "检索文件校验失败")
    if processing_type == "svn":
        from .utils import validate_svn_url

        valid, _a, _b, message = await validate_svn_url(item.content, config, [project_name])
        return bool(valid), str(message or "SVN文件校验失败")
    return True, ""


async def _process_conversion_review_item(
    review_id: str,
    target_id: str,
    *,
    actor: str,
    target_state: bool,
) -> ConversionReviewResult:
    """从汇总任务直接处理一条继续待定的内容；项目概述页仍可走原有状态入口。"""
    reviews = get_conversion_reviews()
    review = reviews.get(review_id)
    if not isinstance(review, dict) or review.get("status") not in OPEN_REVIEW_STATUSES:
        return ConversionReviewResult(False, "汇总待办不存在或已经完成")
    if review.get("assignee") != actor:
        return ConversionReviewResult(False, "当前用户不是该职能汇总待办负责人")
    items = {item.target_id: item for item in collect_review_pending_items(review)}
    item = items.get(target_id)
    if item is None:
        return ConversionReviewResult(False, "该概述已经处理或不再属于本待办")
    if not _actor_can_edit_item(actor, item):
        return ConversionReviewResult(False, "当前用户没有该概述项的维护权限")
    if target_state:
        valid, message = await _validate_item_activation(str(review.get("project") or ""), item)
        if not valid:
            return ConversionReviewResult(False, message)

    project_name = str(review.get("project") or "")
    action_text = "激活" if target_state else "失活"
    changed, message, _updated_chip = await update_overview_chip_state(
        project_name,
        item.label,
        item.chip_id,
        item.version,
        target_state,
        actor,
        f"转产职能汇总确认：设为{action_text}",
        expected_state=None,
    )
    if not changed:
        await reconcile_conversion_reviews(project_name)
        return ConversionReviewResult(False, message)
    await archive_related_record(project_name, item.label, item.chip_id, actor)
    await _publish_changed_labels(project_name, item.role, {item.label})
    await reconcile_conversion_reviews(project_name)
    refreshed = get_conversion_reviews().get(review_id, {})
    remaining = int(refreshed.get("current_count") or 0) if isinstance(refreshed, dict) else 0
    return ConversionReviewResult(
        True,
        f"已将该概述设为{action_text}",
        changed_count=1,
        remaining_count=remaining,
        changed_labels=frozenset({item.label}),
    )


async def process_conversion_review_item(
    review_id: str,
    target_id: str,
    *,
    actor: str,
    target_state: bool,
) -> ConversionReviewResult:
    async with _review_locks[review_id]:
        return await _process_conversion_review_item(
            review_id,
            target_id,
            actor=actor,
            target_state=target_state,
        )


async def _process_conversion_review_batch(
    review_id: str,
    *,
    actor: str,
    exception_target_ids: Iterable[str],
) -> ConversionReviewResult:
    """激活未选择保留待定的全部内容，所选内容继续保持待定。"""
    reviews = get_conversion_reviews()
    review = reviews.get(review_id)
    if not isinstance(review, dict) or review.get("status") not in OPEN_REVIEW_STATUSES:
        return ConversionReviewResult(False, "汇总待办不存在或已经完成")
    if review.get("assignee") != actor:
        return ConversionReviewResult(False, "当前用户不是该职能汇总待办负责人")
    project_name = str(review.get("project") or "")
    role = str(review.get("role") or "")
    items = collect_review_pending_items(review)
    if not items:
        await reconcile_conversion_reviews(project_name)
        return ConversionReviewResult(False, "当前职能已经没有待定概述")

    exception_ids = {str(target_id) for target_id in exception_target_ids}
    normal_items = [item for item in items if item.target_id not in exception_ids]
    normal_items.sort(
        key=lambda item: (
            item.group_name,
            item.config_order,
            0 if item.label == item.first_col_label else 1,
            item.title,
        )
    )
    changed_count = 0
    skipped_count = 0
    changed_labels: set[str] = set()
    failures: list[str] = []
    blocked_ids: set[str] = set()

    for item in normal_items:
        if not _actor_can_edit_item(actor, item):
            failures.append(f"{item.title}：当前用户没有维护权限")
            blocked_ids.add(item.target_id)
            continue
        valid, validation_message = await _validate_item_activation(project_name, item)
        if not valid:
            failures.append(f"{item.title}：{validation_message}")
            blocked_ids.add(item.target_id)
            continue
        changed, message, _updated_chip = await update_overview_chip_state(
            project_name,
            item.label,
            item.chip_id,
            item.version,
            True,
            actor,
            "转产职能汇总确认：确认激活",
            expected_state=None,
        )
        if not changed:
            if message in {"概述当前状态已变化", "概述条目已不存在", "目标状态与当前状态相同"}:
                skipped_count += 1
            else:
                failures.append(f"{item.title}：{message}")
                blocked_ids.add(item.target_id)
            continue
        await archive_related_record(project_name, item.label, item.chip_id, actor)
        changed_count += 1
        changed_labels.add(item.label)

    remaining_exception_ids = exception_ids | blocked_ids
    reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def mark_reviewed(current):
        if not isinstance(current, dict) or current.get("status") not in OPEN_REVIEW_STATUSES:
            return db_storage.ATOMIC_NO_UPDATE
        if current.get("assignee") != actor:
            return db_storage.ATOMIC_NO_UPDATE
        current["status"] = "exceptions"
        current["reviewed_at"] = reviewed_at
        current["updated_at"] = reviewed_at
        current["exception_target_ids"] = sorted(remaining_exception_ids)
        return current

    await db_storage.atomic_deep_update([OVERVIEW_CONVERSION_REVIEWS_KEY, review_id], mark_reviewed)
    if changed_labels:
        await _publish_changed_labels(project_name, role, changed_labels)
    await reconcile_conversion_reviews(project_name)
    refreshed = get_conversion_reviews().get(review_id, {})
    remaining = int(refreshed.get("current_count") or 0) if isinstance(refreshed, dict) else 0
    if changed_count == 0 and failures:
        message = "没有概述被激活，请检查失败原因"
    elif remaining:
        message = f"已确认激活 {changed_count} 个，剩余 {remaining} 个继续待定"
    else:
        message = f"已确认激活 {changed_count} 个，该职能汇总待办已完成"
    return ConversionReviewResult(
        not failures,
        message,
        changed_count=changed_count,
        remaining_count=remaining,
        skipped_count=skipped_count,
        changed_labels=frozenset(changed_labels),
        failures=tuple(failures),
    )


async def process_conversion_review_batch(
    review_id: str,
    *,
    actor: str,
    exception_target_ids: Iterable[str],
) -> ConversionReviewResult:
    async with _review_locks[review_id]:
        return await _process_conversion_review_batch(
            review_id,
            actor=actor,
            exception_target_ids=exception_target_ids,
        )


async def _publish_changed_labels(project_name: str, role: str, labels: set[str]) -> None:
    from .components import OverviewVersionManager
    from .utils import overview_role_update, refresh_overview_pending_labels

    for label in labels:
        OverviewVersionManager.bump(project_name, label)
    refresh_overview_pending_labels(project_name, labels)
    overview_role_update(project_name, role)

