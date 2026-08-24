# -*- encoding: utf-8 -*-
"""概述操作原因配置及轻量历史记录辅助函数。"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Mapping, Optional

from .config import BASE_DIR


OVERVIEW_OPERATION_CONFIG_PATH = Path(BASE_DIR) / "overview_operation_config.json"


def _load_operation_config() -> dict:
    with OVERVIEW_OPERATION_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)
    if not isinstance(config.get("reason_options"), dict):
        raise ValueError("overview_operation_config.json 缺少 reason_options 配置")
    if not isinstance(config.get("automatic_reasons"), dict):
        raise ValueError("overview_operation_config.json 缺少 automatic_reasons 配置")
    return config


OVERVIEW_OPERATION_CONFIG = _load_operation_config()
OVERVIEW_REASON_OPTIONS = OVERVIEW_OPERATION_CONFIG["reason_options"]
OVERVIEW_AUTOMATIC_REASONS = OVERVIEW_OPERATION_CONFIG["automatic_reasons"]


def get_overview_reason_labels(operation: str) -> list[str]:
    """返回指定操作的原因文案；code 仅供配置识别，不进入业务数据。"""
    return [
        str(item.get("label") or "").strip()
        for item in OVERVIEW_REASON_OPTIONS.get(operation, [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]


def resolve_overview_reason(selected: object, other_text: object = "") -> str:
    """把单选项和“其他”文本整理为最终落盘的人类可读原因。"""
    selected_text = str(selected or "").strip()
    if selected_text != "其他":
        return selected_text
    detail = str(other_text or "").strip()
    return f"其他：{detail}" if detail else ""


def get_automatic_overview_reason(key: str, default: str = "") -> str:
    return str(OVERVIEW_AUTOMATIC_REASONS.get(key) or default).strip()


def get_overview_timestamp_items(chip: Mapping) -> list[tuple[str, dict]]:
    """按可解析时间排序历史；无法解析的旧键保持原插入顺序并排在前面。"""
    timestamp = chip.get("timestamp", {}) if isinstance(chip, Mapping) else {}
    if not isinstance(timestamp, Mapping):
        return []
    indexed_items = [
        (index, str(key), value if isinstance(value, dict) else {})
        for index, (key, value) in enumerate(timestamp.items())
    ]

    def sort_key(item: tuple[int, str, dict]):
        index, time_text, _record = item
        try:
            return (1, datetime.fromisoformat(time_text), index)
        except (TypeError, ValueError):
            return (0, datetime.min, index)

    return [(time_text, record) for _index, time_text, record in sorted(indexed_items, key=sort_key)]


def get_latest_overview_record(chip: Mapping) -> tuple[str, dict]:
    items = get_overview_timestamp_items(chip)
    return items[-1] if items else ("", {})


def get_first_overview_record(chip: Mapping) -> tuple[str, dict]:
    items = get_overview_timestamp_items(chip)
    return items[0] if items else ("", {})


def get_latest_overview_reason(chip: Mapping, *, legacy_fallback: bool = True) -> str:
    """读取最新注释；旧概述没有 reason 时才回退顶层 notes。"""
    _time_text, record = get_latest_overview_record(chip)
    reason = str(record.get("reason") or "").strip()
    if reason or not legacy_fallback:
        return reason
    return str(chip.get("notes") or "").strip()


def get_latest_overview_operator(chip: Mapping) -> str:
    _time_text, record = get_latest_overview_record(chip)
    return str(record.get("creator") or chip.get("creator") or "未知").strip()


def append_overview_timestamp(
    chip: dict,
    *,
    creator: str,
    reason: str,
    operation_time: Optional[str] = None,
    source_id: str = "",
) -> str:
    """在状态已经修改后，追加一条秒级概述状态快照。"""
    time_text = operation_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = {
        "creator": str(creator or "待定负责人"),
        "reason": str(reason or "").strip(),
        "select_activ_dic": copy.deepcopy(chip.get("select_activ_dic", {})),
    }
    if source_id:
        record["source_id"] = str(source_id)
    chip.setdefault("timestamp", {})[time_text] = record
    return time_text
