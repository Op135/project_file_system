# -*- encoding: utf-8 -*-
"""样品问题收集模块的业务配置加载器。

维护人员通常只需要修改项目根目录的 ``sample_issue_collection_config.json``。
本模块在导入阶段读取一次 JSON，并把经过校验的值导出为常量供页面使用，修改 JSON 后需要重启服务。
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SAMPLE_ISSUE_CONFIG_PATH = Path(__file__).parent.parent / "sample_issue_collection_config.json"

SAMPLE_STATUS_ISSUE_RECORDED = "问题录入完毕"
SAMPLE_STATUS_TEMPORARY_ACTION_DONE = "临时对策填写完毕"
SAMPLE_STATUS_CORRECTIVE_ACTION_DONE = "纠正预防措施填写完毕"

_LEGACY_FILTER_STATE_RENAMES = {
    "问题录入": SAMPLE_STATUS_ISSUE_RECORDED,
    "对策填写中": SAMPLE_STATUS_TEMPORARY_ACTION_DONE,
    "措施执行中": SAMPLE_STATUS_CORRECTIVE_ACTION_DONE,
}

_DEFAULT_CONFIG = {
    "public_base_url": "http://192.168.1.102:8080",
    "editor_roles": ["研发经理", "admin", "研发助理"],
    "filter_states": [
        "全部",
        SAMPLE_STATUS_ISSUE_RECORDED,
        SAMPLE_STATUS_TEMPORARY_ACTION_DONE,
        SAMPLE_STATUS_CORRECTIVE_ACTION_DONE,
        "关闭申请中",
        "延期申请中",
        "已关闭",
    ],
    "wecom": {
        "default_notify_targets": [{"position": "研发经理"}],
        "extension": {
            "approver_roles": ["研发经理", "admin"],
            "notify_targets": [{"position": "研发经理"}],
            "approval_notify_targets": [{"position": "研发经理"}, {"position": "研发助理"}],
            "notify_requester_on_approval": True,
        },
    },
    "reminders": {
        "background_enabled": True,
        "initial_delay_seconds": 60,
        "check_interval_seconds": 3600,
        "rules": [
            {"key": "due_7_days", "label": "预计完成日期前7天", "days_until_due": 7, "enabled": True},
            {"key": "due_3_days", "label": "预计完成日期前3天", "days_until_due": 3, "enabled": True},
            {"key": "due_today", "label": "预计完成日期当天", "days_until_due": 0, "enabled": True},
            {"key": "overdue", "label": "预计完成日期逾期", "max_days_until_due": -1, "enabled": True},
        ],
    },
}


def _read_config_file() -> dict:
    """读取根目录 JSON；无法读取时返回空字典，让后续每个字段分别使用默认值。"""
    try:
        with SAMPLE_ISSUE_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return loaded
    except FileNotFoundError:
        logger.warning("样品问题配置文件不存在：%s，已使用代码默认值", SAMPLE_ISSUE_CONFIG_PATH)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("样品问题配置文件读取失败，已使用代码默认值")
    return {}


def _string_value(config: dict, key: str, default: str) -> str:
    """读取必填的非空字符串，并清除首尾空格。"""
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    logger.warning("样品问题配置 %s 无效，已使用默认值", key)
    return default


def _string_list(config: dict, key: str, default: list[str]) -> list[str]:
    """读取字符串列表，同时去除重复项并保留原有顺序。"""
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if normalized:
            return normalized
    logger.warning("样品问题配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _filter_states(config: dict, default: list[str]) -> list[str]:
    """确保总览页的特殊筛选项始终存在。"""
    states = [
        _LEGACY_FILTER_STATE_RENAMES.get(state, state)
        for state in _string_list(config, "filter_states", default)
    ]
    states = list(dict.fromkeys(states))
    normalized = ["全部", *(state for state in states if state != "全部")]
    for required_state in ["延期申请中", "关闭申请中", "已关闭"]:
        if required_state not in normalized:
            normalized.append(required_state)
    return normalized


def _bool_value(config: dict, key: str, default: bool) -> bool:
    """读取严格布尔值，避免字符串 ``"false"`` 被误认为已关闭。"""
    value = config.get(key)
    if isinstance(value, bool):
        return value
    logger.warning("样品问题配置 %s 无效，已使用默认值", key)
    return default


def _positive_int(config: dict, key: str, default: int) -> int:
    """读取后台任务时间参数。"""
    value = config.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    logger.warning("样品问题配置 %s 无效，已使用默认值", key)
    return default


def _notify_targets(config: dict, key: str, default: list) -> list:
    """读取企业微信接收人规则；每项可以是直接账号字符串或成员筛选条件字典。"""
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, (str, dict)) for item in value):
        return copy.deepcopy(value)
    logger.warning("样品问题配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _reminder_rules(config: dict, default: list[dict]) -> list[dict]:
    """校验并标准化提醒策略。"""
    value = config.get("rules")
    if not isinstance(value, list):
        logger.warning("样品问题提醒规则必须是列表，已使用默认值")
        return copy.deepcopy(default)

    normalized_rules = []
    seen_keys = set()
    for index, rule in enumerate(value):
        if not isinstance(rule, dict):
            logger.warning("样品问题提醒规则第 %s 项不是对象，已忽略", index + 1)
            continue
        if rule.get("enabled", True) is False:
            continue

        key = rule.get("key")
        label = rule.get("label")
        has_exact = isinstance(rule.get("days_until_due"), int) and not isinstance(rule.get("days_until_due"), bool)
        has_max = isinstance(rule.get("max_days_until_due"), int) and not isinstance(
            rule.get("max_days_until_due"), bool
        )
        if (
            not isinstance(key, str)
            or not key.strip()
            or key in seen_keys
            or not isinstance(label, str)
            or not label.strip()
            or has_exact == has_max
        ):
            logger.warning("样品问题提醒规则第 %s 项格式无效，已忽略", index + 1)
            continue

        normalized_rule = {"key": key.strip(), "label": label.strip()}
        match_key = "days_until_due" if has_exact else "max_days_until_due"
        normalized_rule[match_key] = rule[match_key]
        normalized_rules.append(normalized_rule)
        seen_keys.add(key)
    return normalized_rules


def load_sample_issue_config() -> dict[str, Any]:
    """组合出样品问题页面实际使用的完整配置。"""
    raw_config = _read_config_file()
    default_wecom = _DEFAULT_CONFIG["wecom"]
    default_extension = default_wecom["extension"]
    default_reminders = _DEFAULT_CONFIG["reminders"]

    raw_wecom = raw_config.get("wecom", {}) if isinstance(raw_config.get("wecom"), dict) else {}
    raw_extension = raw_wecom.get("extension", {}) if isinstance(raw_wecom.get("extension"), dict) else {}
    raw_reminders = raw_config.get("reminders", {}) if isinstance(raw_config.get("reminders"), dict) else {}

    return {
        "public_base_url": _string_value(raw_config, "public_base_url", _DEFAULT_CONFIG["public_base_url"]).rstrip("/"),
        "editor_roles": _string_list(raw_config, "editor_roles", _DEFAULT_CONFIG["editor_roles"]),
        "filter_states": _filter_states(raw_config, _DEFAULT_CONFIG["filter_states"]),
        "wecom": {
            "default_notify_targets": _notify_targets(
                raw_wecom,
                "default_notify_targets",
                default_wecom["default_notify_targets"],
            ),
            "extension": {
                "approver_roles": _string_list(
                    raw_extension,
                    "approver_roles",
                    default_extension["approver_roles"],
                ),
                "notify_targets": _notify_targets(
                    raw_extension,
                    "notify_targets",
                    default_extension["notify_targets"],
                ),
                "approval_notify_targets": _notify_targets(
                    raw_extension,
                    "approval_notify_targets",
                    default_extension["approval_notify_targets"],
                ),
                "notify_requester_on_approval": _bool_value(
                    raw_extension,
                    "notify_requester_on_approval",
                    default_extension["notify_requester_on_approval"],
                ),
            },
        },
        "reminders": {
            "background_enabled": _bool_value(
                raw_reminders,
                "background_enabled",
                default_reminders["background_enabled"],
            ),
            "initial_delay_seconds": _positive_int(
                raw_reminders,
                "initial_delay_seconds",
                default_reminders["initial_delay_seconds"],
            ),
            "check_interval_seconds": _positive_int(
                raw_reminders,
                "check_interval_seconds",
                default_reminders["check_interval_seconds"],
            ),
            "rules": _reminder_rules(raw_reminders, default_reminders["rules"]),
        },
    }


SAMPLE_ISSUE_CONFIG = load_sample_issue_config()
SAMPLE_PUBLIC_BASE_URL = SAMPLE_ISSUE_CONFIG["public_base_url"]
SAMPLE_EDITOR_ROLES = SAMPLE_ISSUE_CONFIG["editor_roles"]
SAMPLE_FILTER_STATES = SAMPLE_ISSUE_CONFIG["filter_states"]
SAMPLE_FILTER_ALL_STATE = "全部"
SAMPLE_FILTER_PENDING_EXTENSION_STATE = "延期申请中"
SAMPLE_FILTER_PENDING_CLOSE_STATE = "关闭申请中"
SAMPLE_FILTER_CLOSED_STATE = "已关闭"
SAMPLE_DEFAULT_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["default_notify_targets"]
SAMPLE_EXTENSION_APPROVER_ROLES = SAMPLE_ISSUE_CONFIG["wecom"]["extension"]["approver_roles"]
SAMPLE_EXTENSION_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["extension"]["notify_targets"]
SAMPLE_EXTENSION_APPROVAL_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["extension"]["approval_notify_targets"]
SAMPLE_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL = SAMPLE_ISSUE_CONFIG["wecom"]["extension"][
    "notify_requester_on_approval"
]
SAMPLE_BACKGROUND_REMINDER_ENABLED = SAMPLE_ISSUE_CONFIG["reminders"]["background_enabled"]
SAMPLE_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS = SAMPLE_ISSUE_CONFIG["reminders"]["initial_delay_seconds"]
SAMPLE_BACKGROUND_REMINDER_INTERVAL_SECONDS = SAMPLE_ISSUE_CONFIG["reminders"]["check_interval_seconds"]
SAMPLE_REMINDER_RULES = SAMPLE_ISSUE_CONFIG["reminders"]["rules"]
