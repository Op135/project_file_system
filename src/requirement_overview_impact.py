# -*- encoding: utf-8 -*-
"""需求节点变动与项目概述项之间的影响关系配置。"""

import copy
import json
import os
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, MutableMapping
from uuid import uuid4


REQUIREMENT_OVERVIEW_IMPACT_CONFIG_PATH = Path(__file__).parent.parent / "requirement_overview_impact.json"
REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY = "requirement_overview_impact_config"
SUPPORTED_UNMAPPED_POLICIES = {"all_overviews", "block"}
_CONFIG_WRITE_LOCK = threading.Lock()


class RequirementOverviewImpactConfigError(ValueError):
    """需求与概述影响配置无效。"""


def _normalize_node_id(value: Any) -> str:
    node_id = str(value).strip()
    if not node_id:
        raise RequirementOverviewImpactConfigError("node_id 不能为空")
    return node_id


def _node_id_sort_key(node_id: str) -> tuple[int, Decimal, str]:
    """按 node_id 数值排序；兼容历史非数字键并将其稳定排在数字键之后。"""
    try:
        numeric_value = Decimal(node_id)
        if numeric_value.is_finite():
            return 0, numeric_value, node_id
    except InvalidOperation:
        pass
    return 1, Decimal(0), node_id


def load_requirement_overview_impact_config(
    raw_config: dict | None = None,
    *,
    valid_overview_labels: Iterable[str] | None = None,
) -> dict:
    """读取、规范化并校验需求节点影响配置。"""
    if raw_config is None:
        try:
            with REQUIREMENT_OVERVIEW_IMPACT_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
                raw_config = json.load(config_file)
        except FileNotFoundError as exc:
            raise RequirementOverviewImpactConfigError(
                f"配置文件不存在：{REQUIREMENT_OVERVIEW_IMPACT_CONFIG_PATH}"
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RequirementOverviewImpactConfigError(f"配置文件读取失败：{exc}") from exc

    if not isinstance(raw_config, dict):
        raise RequirementOverviewImpactConfigError("配置根节点必须是 JSON 对象")
    if raw_config.get("schema_version") != 1:
        raise RequirementOverviewImpactConfigError("schema_version 当前只支持 1")

    unmapped_policy = raw_config.get("unmapped_policy", "all_overviews")
    if unmapped_policy not in SUPPORTED_UNMAPPED_POLICIES:
        raise RequirementOverviewImpactConfigError(
            f"unmapped_policy 必须是 {sorted(SUPPORTED_UNMAPPED_POLICIES)} 之一"
        )

    raw_node_impacts = raw_config.get("node_impacts", {})
    if not isinstance(raw_node_impacts, dict):
        raise RequirementOverviewImpactConfigError("node_impacts 必须是 JSON 对象")

    valid_labels = None if valid_overview_labels is None else {str(label) for label in valid_overview_labels}
    normalized_impacts = {}
    invalid_labels = set()
    for raw_node_id, raw_labels in raw_node_impacts.items():
        node_id = _normalize_node_id(raw_node_id)
        if not isinstance(raw_labels, list):
            raise RequirementOverviewImpactConfigError(f"node_id={node_id} 的配置值必须是 label 数组")

        labels = []
        for raw_label in raw_labels:
            if not isinstance(raw_label, str) or not raw_label.strip():
                raise RequirementOverviewImpactConfigError(f"node_id={node_id} 包含无效的概述 label")
            label = raw_label.strip()
            if label not in labels:
                labels.append(label)
            if valid_labels is not None and label not in valid_labels:
                invalid_labels.add(label)
        normalized_impacts[node_id] = labels

    if invalid_labels:
        raise RequirementOverviewImpactConfigError(
            f"配置引用了 overview_config.json 中不存在的 label：{', '.join(sorted(invalid_labels))}"
        )

    return {
        "schema_version": 1,
        "unmapped_policy": unmapped_policy,
        "node_impacts": normalized_impacts,
        "valid": True,
        "error": "",
    }


def save_requirement_overview_impact_config(
    raw_config: dict,
    *,
    valid_overview_labels: Iterable[str],
    storage: MutableMapping[str, object],
    config_path: str | Path = REQUIREMENT_OVERVIEW_IMPACT_CONFIG_PATH,
) -> dict:
    """校验并事务式更新影响配置文件与服务端内存。

    配置先完成全量校验，再通过同目录临时文件原子替换正式文件。若内存同步失败，
    会恢复原文件与原内存值，避免审批进程读取到文件、内存不一致的配置。
    """
    normalized_config = load_requirement_overview_impact_config(
        raw_config,
        valid_overview_labels=valid_overview_labels,
    )
    normalized_config["node_impacts"] = {
        node_id: normalized_config["node_impacts"][node_id]
        for node_id in sorted(normalized_config["node_impacts"], key=_node_id_sort_key)
    }
    file_config = {
        "schema_version": normalized_config["schema_version"],
        "unmapped_policy": normalized_config["unmapped_policy"],
        "node_impacts": normalized_config["node_impacts"],
    }
    target_path = Path(config_path)

    with _CONFIG_WRITE_LOCK:
        try:
            file_existed = target_path.exists()
            previous_file_content = target_path.read_bytes() if file_existed else b""
        except OSError as exc:
            raise RequirementOverviewImpactConfigError(f"保存影响配置失败：无法读取原配置：{exc}") from exc
        storage_had_key = REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY in storage
        previous_storage_value = copy.deepcopy(storage.get(REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY))

        try:
            _write_json_atomic(target_path, file_config)
            storage[REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY] = copy.deepcopy(normalized_config)
        except Exception as exc:
            rollback_errors = []
            try:
                if file_existed:
                    _write_bytes_atomic(target_path, previous_file_content)
                elif target_path.exists():
                    target_path.unlink()
            except OSError as rollback_exc:
                rollback_errors.append(f"文件回滚失败：{rollback_exc}")

            try:
                if storage_had_key:
                    storage[REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY] = previous_storage_value
                else:
                    storage.pop(REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY, None)
            except Exception as rollback_exc:
                rollback_errors.append(f"内存回滚失败：{rollback_exc}")

            detail = f"；{'；'.join(rollback_errors)}" if rollback_errors else ""
            raise RequirementOverviewImpactConfigError(f"保存影响配置失败：{exc}{detail}") from exc

    return normalized_config


def _write_json_atomic(file_path: Path, data: dict[str, object]) -> None:
    encoded = json.dumps(data, ensure_ascii=False, indent=4).encode("utf-8")
    _write_bytes_atomic(file_path, encoded)


def _write_bytes_atomic(file_path: Path, content: bytes) -> None:
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_name(f".{file_path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, file_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def collect_requirement_change_node_ids(overview_data: dict, version: str) -> dict[str, set[str]]:
    """从概述整理文件的指定版本块提取增、删、改节点 ID。"""
    version_key = f"{int(float(version))}.0"
    version_data = overview_data.get(version_key, {})
    result = {"added": set(), "deleted": set(), "modified": set()}

    for change_type in ("added", "deleted"):
        items = version_data.get(change_type, {})
        if not isinstance(items, dict):
            continue
        for item in items.values():
            if isinstance(item, dict) and item.get("node_id") not in {None, ""}:
                result[change_type].add(_normalize_node_id(item["node_id"]))

    modified_items = version_data.get("modified", {})
    if isinstance(modified_items, dict):
        for item in modified_items.values():
            if not isinstance(item, dict):
                continue
            node_data = item.get("new_data") or item.get("old_data") or {}
            if isinstance(node_data, dict) and node_data.get("node_id") not in {None, ""}:
                result["modified"].add(_normalize_node_id(node_data["node_id"]))

    return result


def resolve_requirement_overview_impacts(
    change_node_ids: dict[str, set[str]] | Iterable[str],
    config: dict,
    all_overview_labels: Iterable[str],
) -> tuple[set[str], set[str]]:
    """根据内存配置解析受影响概述；返回（受影响 labels，未配置 node_ids）。"""
    if not config or not config.get("valid"):
        error = config.get("error", "配置尚未加载") if isinstance(config, dict) else "配置尚未加载"
        raise RequirementOverviewImpactConfigError(error)

    if isinstance(change_node_ids, dict):
        changed_ids = {
            _normalize_node_id(node_id)
            for node_ids in change_node_ids.values()
            for node_id in node_ids
        }
    else:
        changed_ids = {_normalize_node_id(node_id) for node_id in change_node_ids}

    node_impacts = config.get("node_impacts", {})
    missing_node_ids = changed_ids - set(node_impacts)
    if missing_node_ids and config.get("unmapped_policy") == "block":
        raise RequirementOverviewImpactConfigError(
            f"以下变动需求节点尚未配置概述影响关系：{', '.join(sorted(missing_node_ids))}"
        )

    affected_labels = {
        label
        for node_id in changed_ids & set(node_impacts)
        for label in node_impacts.get(node_id, [])
    }
    if missing_node_ids and config.get("unmapped_policy") == "all_overviews":
        affected_labels.update(str(label) for label in all_overview_labels)

    return affected_labels, missing_node_ids
