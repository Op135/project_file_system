# -*- encoding: utf-8 -*-
"""管理员批量纠正当前项目多版本待定概述的服务。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from . import db_storage
from .overview_batch_operations import update_overview_chip_state
from .overview_operation import get_latest_overview_reason, get_overview_timestamp_items
from .utils import get_overview_latest_responsible


ADMIN_OVERVIEW_MAINTENANCE_USER = "admin"


@dataclass(frozen=True)
class PendingOverviewItem:
    """维护弹窗中展示的一条“概述 × 待定版本”。"""

    role: str
    group_name: str
    label: str
    title: str
    chip_id: str
    version: str
    content: str
    pending_reason: str


@dataclass(frozen=True)
class AdminOverviewMaintenanceResult:
    """管理员批量调用常规状态接口后的汇总结果。"""

    ok: bool
    message: str
    changed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    changed_labels: frozenset[str] = frozenset()
    failures: tuple[str, ...] = ()


def is_admin_overview_maintenance_user(username: object) -> bool:
    """该特殊维护功能只授权给固定的 admin 账户。"""
    return isinstance(username, str) and username == ADMIN_OVERVIEW_MAINTENANCE_USER


def _build_overview_config_index(over_config: Mapping[str, object]) -> dict[str, tuple[str, str, str]]:
    config_index: dict[str, tuple[str, str, str]] = {}
    for role_value, groups_value in over_config.items():
        if not isinstance(groups_value, Mapping):
            continue
        role = str(role_value or "未分类")
        for group_value, configs_value in groups_value.items():
            if not isinstance(configs_value, list):
                continue
            group_name = str(group_value or "未分组")
            for config_value in configs_value:
                if not isinstance(config_value, Mapping):
                    continue
                label_value = config_value.get("label")
                if not isinstance(label_value, str) or not label_value:
                    continue
                title_value = config_value.get("title")
                config_index[label_value] = (role, group_name, str(title_value or label_value))
    return config_index


def _overview_version_sort_key(version: object) -> tuple[int, float, str]:
    text = str(version)
    try:
        return (1, float(text), text)
    except (TypeError, ValueError, OverflowError):
        return (0, 0.0, text)


def _get_pending_reason(chip: Mapping[str, object], version: str) -> str:
    missing = object()
    previous_state: object = missing
    pending_reason = ""
    for _time_text, record in get_overview_timestamp_items(chip):
        record_states = record.get("select_activ_dic")
        if not isinstance(record_states, Mapping) or version not in record_states:
            continue
        current_state = record_states[version]
        if current_state is None and previous_state is not None:
            reason = str(record.get("reason") or "").strip()
            if reason:
                pending_reason = reason
        previous_state = current_state
    return pending_reason or get_latest_overview_reason(chip).strip() or "未记录待定原因"


def collect_pending_overview_items(
    overview_data: Mapping[str, object],
    over_config: Mapping[str, object],
) -> dict[str, list[PendingOverviewItem]]:
    """按职能汇总当前项目全部版本中明确处于待定状态的概述。"""
    config_index = _build_overview_config_index(over_config)
    grouped: dict[str, list[PendingOverviewItem]] = {}
    for label_value, chips_value in overview_data.items():
        if not isinstance(chips_value, Mapping):
            continue
        label = str(label_value)
        role, group_name, title = config_index.get(label, ("未分类", "未分组", label))
        for chip_id_value, chip_value in chips_value.items():
            if not isinstance(chip_value, Mapping):
                continue
            states_value = chip_value.get("select_activ_dic")
            if not isinstance(states_value, Mapping):
                continue
            pending_versions = sorted(
                (str(version) for version, state in states_value.items() if state is None),
                key=_overview_version_sort_key,
            )
            for version in pending_versions:
                item = PendingOverviewItem(
                    role=role,
                    group_name=group_name,
                    label=label,
                    title=title,
                    chip_id=str(chip_id_value),
                    version=version,
                    content=str(chip_value.get("content") or "无内容"),
                    pending_reason=_get_pending_reason(chip_value, version),
                )
                grouped.setdefault(role, []).append(item)

    for items in grouped.values():
        items.sort(
            key=lambda item: (
                item.group_name,
                item.title,
                _overview_version_sort_key(item.version),
                item.content,
                item.chip_id,
            )
        )
    return grouped


async def apply_admin_pending_overview_states(
    *,
    project_name: str,
    targets: Iterable[tuple[str, str, str]],
    target_state: bool,
    actor: object,
    operation_note: str,
) -> AdminOverviewMaintenanceResult:
    """逐项调用常规状态接口，并把原负责人作为最新操作人写入完整记录。"""
    if not is_admin_overview_maintenance_user(actor):
        return AdminOverviewMaintenanceResult(False, "只有 admin 账户可以使用特殊维护功能")
    if not project_name or not isinstance(target_state, bool):
        return AdminOverviewMaintenanceResult(False, "维护参数无效")
    normalized_note = str(operation_note or "").strip()
    if not normalized_note:
        return AdminOverviewMaintenanceResult(False, "请填写操作注释")

    normalized_targets = {
        (str(label), str(chip_id), str(version))
        for label, chip_id, version in targets
        if label and chip_id and version
    }
    if not normalized_targets:
        return AdminOverviewMaintenanceResult(False, "请至少选择一条待定概述版本")

    changed_count = 0
    skipped_count = 0
    changed_labels: set[str] = set()
    failures: list[str] = []
    target_text = "激活" if target_state else "失活"
    reason = f"特殊维护（设为{target_text}）：{normalized_note}"

    for label, chip_id, version in sorted(normalized_targets):
        chip_value = db_storage.get_deep_item([f"{project_name}_over_data", label, chip_id], {})
        if not isinstance(chip_value, dict):
            skipped_count += 1
            continue
        responsible = get_overview_latest_responsible(project_name, label, chip_value)
        changed, message, _updated_chip = await update_overview_chip_state(
            project_name,
            label,
            chip_id,
            version,
            target_state,
            responsible,
            reason,
            expected_state=None,
        )
        if changed:
            changed_count += 1
            changed_labels.add(label)
        elif message in {"概述当前状态已变化", "概述条目已不存在", "目标状态与当前状态相同"}:
            skipped_count += 1
        else:
            failures.append(f"{label}/{chip_id}/V{version}：{message}")

    failed_count = len(failures)
    if changed_count == 0:
        message = "所选概述版本已不再处于待定状态，请重新打开维护窗口"
        if failed_count:
            message = "维护失败，请查看失败明细"
        return AdminOverviewMaintenanceResult(
            False,
            message,
            skipped_count=skipped_count,
            failed_count=failed_count,
            failures=tuple(failures),
        )
    return AdminOverviewMaintenanceResult(
        failed_count == 0,
        "维护完成" if failed_count == 0 else "部分概述版本维护失败",
        changed_count=changed_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        changed_labels=frozenset(changed_labels),
        failures=tuple(failures),
    )
