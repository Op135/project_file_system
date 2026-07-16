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

from .issue_workflow_utils import normalize_time_window

logger = logging.getLogger(__name__)

SAMPLE_ISSUE_CONFIG_PATH = Path(__file__).parent.parent / "sample_issue_collection_config.json"

SAMPLE_STATUS_ISSUE_RECORDED = "问题录入完毕"
SAMPLE_STATUS_TEMPORARY_ACTION_DONE = "临时对策填写完毕"
SAMPLE_STATUS_CORRECTIVE_ACTION_DONE = "纠正预防措施填写完毕"
SAMPLE_STATUS_SPECIAL_PREPARATION = "试产前特殊准备"

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
        SAMPLE_STATUS_SPECIAL_PREPARATION,
        "关闭申请中",
        "延期申请中",
        "已关闭",
    ],
    "special_preparation": {
        "owner_role": "NPI工程师",
        "owner_role_keywords": ["NPI工程", "NPI工程师"],
        "default_owner_name": "杨铁华",
        "default_owner_userid": "YangTieHua",
        "default_actions": ["试产前落实工装治具", "试产前落实到SOP"],
    },
    "wecom": {
        "default_notify_targets": [{"position": "研发经理"}],
        "extension": {
            "approver_roles": ["研发经理", "admin"],
            "notify_targets": [{"position": "研发经理"}],
            "approval_notify_targets": [{"position": "研发经理"}, {"position": "研发助理"}],
            "notify_requester_on_approval": True,
        },
        "close": {
            "approver_roles": ["研发经理", "admin"],
            "notify_targets": [{"position": "研发经理"}],
            "approval_notify_targets": [{"position": "研发经理"}, {"position": "研发助理"}],
            "notify_requester_on_approval": True,
            "routing_rules": [],
        },
    },
    "reminders": {
        "background_enabled": True,
        "initial_delay_seconds": 60,
        "check_interval_seconds": 3600,
        "check_window": {"enabled": True, "start": "08:30", "end": "18:30"},
        "rules": [
            {"key": "due_7_days", "label": "预计完成日期前7天", "days_until_due": 7, "enabled": True},
            {"key": "due_3_days", "label": "预计完成日期前3天", "days_until_due": 3, "enabled": True},
            {"key": "due_today", "label": "预计完成日期当天", "days_until_due": 0, "enabled": True},
            {"key": "overdue", "label": "预计完成日期逾期", "max_days_until_due": -1, "enabled": True},
        ],
        "incomplete_rules": [
            {"key": "incomplete_1_day", "label": "问题录入后1天未完善对策", "days_since_record": 1, "enabled": True},
            {"key": "incomplete_3_days", "label": "问题录入后3天未完善对策", "days_since_record": 3, "enabled": True},
            {
                "key": "incomplete_over_3_days",
                "label": "问题录入超过3天仍未完善对策",
                "min_days_since_record": 4,
                "enabled": True,
            },
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
    for required_state in [SAMPLE_STATUS_SPECIAL_PREPARATION, "延期申请中", "关闭申请中", "已关闭"]:
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


def _string_values(value) -> list[str]:
    """标准化配置中的字符串或字符串列表。"""
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return list(dict.fromkeys(item.strip() for item in value))
    return []


def _notify_targets_from_roles(roles: list[str]) -> list[dict]:
    """把审批角色按企业微信职位规则转换为通知对象；admin 不作为职位推送。"""
    return [{"position": role} for role in roles if role.lower() != "admin"]


def _time_window(config: dict, key: str, default: dict) -> dict:
    """读取后台提醒检查时间窗口；enabled=false 表示不限制检查时间。"""
    normalized = normalize_time_window(config.get(key), default)
    if normalized is not None:
        return normalized
    logger.warning("样品问题配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _close_routing_rules(config: dict, default_close: dict) -> list[dict]:
    """读取按申请人角色关键词路由的关闭审批规则。"""
    value = config.get("routing_rules", [])
    if not isinstance(value, list):
        logger.warning("样品问题关闭审批路由 routing_rules 必须是列表，已忽略")
        return []

    normalized_rules = []
    seen_keys = set()
    for index, rule in enumerate(value):
        if not isinstance(rule, dict):
            logger.warning("样品问题关闭审批路由第 %s 项不是对象，已忽略", index + 1)
            continue
        if rule.get("enabled", True) is False:
            continue

        role_keywords = _string_values(
            rule.get("requester_role_keywords")
            or rule.get("requester_role_contains")
            or rule.get("role_keywords")
        )
        approver_roles = _string_values(rule.get("approver_roles"))
        if not role_keywords or not approver_roles:
            logger.warning("样品问题关闭审批路由第 %s 项缺少角色关键词或审批角色，已忽略", index + 1)
            continue

        key = str(rule.get("key") or f"close_route_{index + 1}").strip()
        if not key or key in seen_keys:
            logger.warning("样品问题关闭审批路由第 %s 项 key 无效或重复，已忽略", index + 1)
            continue

        default_notify_targets = _notify_targets_from_roles(approver_roles) or default_close["notify_targets"]
        normalized_rules.append(
            {
                "key": key,
                "label": str(rule.get("label") or key).strip(),
                "requester_role_keywords": role_keywords,
                "approver_roles": approver_roles,
                "notify_targets": _notify_targets(rule, "notify_targets", default_notify_targets),
                "approval_notify_targets": _notify_targets(
                    rule,
                    "approval_notify_targets",
                    default_close["approval_notify_targets"],
                ),
                "notify_requester_on_approval": _bool_value(
                    rule,
                    "notify_requester_on_approval",
                    default_close["notify_requester_on_approval"],
                ),
            }
        )
        seen_keys.add(key)
    return normalized_rules


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


def _incomplete_reminder_rules(config: dict, default: list[dict]) -> list[dict]:
    """校验并标准化对策未完善提醒策略。"""
    value = config.get("incomplete_rules")
    if not isinstance(value, list):
        logger.warning("样品问题未完善对策提醒规则必须是列表，已使用默认值")
        return copy.deepcopy(default)

    normalized_rules = []
    seen_keys = set()
    for index, rule in enumerate(value):
        if not isinstance(rule, dict):
            logger.warning("样品问题未完善对策提醒规则第 %s 项不是对象，已忽略", index + 1)
            continue
        if rule.get("enabled", True) is False:
            continue

        key = rule.get("key")
        label = rule.get("label")
        has_exact = isinstance(rule.get("days_since_record"), int) and not isinstance(
            rule.get("days_since_record"), bool
        )
        has_min = isinstance(rule.get("min_days_since_record"), int) and not isinstance(
            rule.get("min_days_since_record"), bool
        )
        if (
            not isinstance(key, str)
            or not key.strip()
            or key in seen_keys
            or not isinstance(label, str)
            or not label.strip()
            or has_exact == has_min
        ):
            logger.warning("样品问题未完善对策提醒规则第 %s 项格式无效，已忽略", index + 1)
            continue

        match_key = "days_since_record" if has_exact else "min_days_since_record"
        match_value = rule[match_key]
        if match_value < 0:
            logger.warning("样品问题未完善对策提醒规则第 %s 项天数无效，已忽略", index + 1)
            continue

        normalized_rule = {"key": key.strip(), "label": label.strip(), match_key: match_value}
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
    raw_close = raw_wecom.get("close", {}) if isinstance(raw_wecom.get("close"), dict) else {}
    raw_reminders = raw_config.get("reminders", {}) if isinstance(raw_config.get("reminders"), dict) else {}
    raw_special_preparation = (
        raw_config.get("special_preparation", {})
        if isinstance(raw_config.get("special_preparation"), dict)
        else {}
    )
    default_close = default_wecom["close"]
    default_special_preparation = _DEFAULT_CONFIG["special_preparation"]

    close_config = {
        "approver_roles": _string_list(
            raw_close,
            "approver_roles",
            default_close["approver_roles"],
        ),
        "notify_targets": _notify_targets(
            raw_close,
            "notify_targets",
            default_close["notify_targets"],
        ),
        "approval_notify_targets": _notify_targets(
            raw_close,
            "approval_notify_targets",
            default_close["approval_notify_targets"],
        ),
        "notify_requester_on_approval": _bool_value(
            raw_close,
            "notify_requester_on_approval",
            default_close["notify_requester_on_approval"],
        ),
    }
    close_config["routing_rules"] = _close_routing_rules(raw_close, close_config)

    return {
        "public_base_url": _string_value(raw_config, "public_base_url", _DEFAULT_CONFIG["public_base_url"]).rstrip("/"),
        "editor_roles": _string_list(raw_config, "editor_roles", _DEFAULT_CONFIG["editor_roles"]),
        "filter_states": _filter_states(raw_config, _DEFAULT_CONFIG["filter_states"]),
        "special_preparation": {
            "owner_role": _string_value(
                raw_special_preparation,
                "owner_role",
                default_special_preparation["owner_role"],
            ),
            "owner_role_keywords": _string_list(
                raw_special_preparation,
                "owner_role_keywords",
                default_special_preparation["owner_role_keywords"],
            ),
            "default_owner_name": _string_value(
                raw_special_preparation,
                "default_owner_name",
                default_special_preparation["default_owner_name"],
            ),
            "default_owner_userid": _string_value(
                raw_special_preparation,
                "default_owner_userid",
                default_special_preparation["default_owner_userid"],
            ),
            "default_actions": _string_list(
                raw_special_preparation,
                "default_actions",
                default_special_preparation["default_actions"],
            ),
        },
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
            "close": close_config,
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
            "check_window": _time_window(
                raw_reminders,
                "check_window",
                default_reminders["check_window"],
            ),
            "rules": _reminder_rules(raw_reminders, default_reminders["rules"]),
            "incomplete_rules": _incomplete_reminder_rules(
                raw_reminders,
                default_reminders["incomplete_rules"],
            ),
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
SAMPLE_SPECIAL_PREPARATION_OWNER_ROLE = SAMPLE_ISSUE_CONFIG["special_preparation"]["owner_role"]
SAMPLE_SPECIAL_PREPARATION_OWNER_ROLE_KEYWORDS = SAMPLE_ISSUE_CONFIG["special_preparation"][
    "owner_role_keywords"
]
SAMPLE_SPECIAL_PREPARATION_DEFAULT_OWNER_NAME = SAMPLE_ISSUE_CONFIG["special_preparation"][
    "default_owner_name"
]
SAMPLE_SPECIAL_PREPARATION_DEFAULT_OWNER_USERID = SAMPLE_ISSUE_CONFIG["special_preparation"][
    "default_owner_userid"
]
SAMPLE_SPECIAL_PREPARATION_DEFAULT_ACTIONS = SAMPLE_ISSUE_CONFIG["special_preparation"]["default_actions"]
SAMPLE_DEFAULT_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["default_notify_targets"]
SAMPLE_EXTENSION_APPROVER_ROLES = SAMPLE_ISSUE_CONFIG["wecom"]["extension"]["approver_roles"]
SAMPLE_EXTENSION_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["extension"]["notify_targets"]
SAMPLE_EXTENSION_APPROVAL_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["extension"]["approval_notify_targets"]
SAMPLE_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL = SAMPLE_ISSUE_CONFIG["wecom"]["extension"][
    "notify_requester_on_approval"
]
SAMPLE_CLOSE_APPROVER_ROLES = SAMPLE_ISSUE_CONFIG["wecom"]["close"]["approver_roles"]
SAMPLE_CLOSE_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["close"]["notify_targets"]
SAMPLE_CLOSE_APPROVAL_NOTIFY_TARGETS = SAMPLE_ISSUE_CONFIG["wecom"]["close"]["approval_notify_targets"]
SAMPLE_CLOSE_NOTIFY_REQUESTER_ON_APPROVAL = SAMPLE_ISSUE_CONFIG["wecom"]["close"]["notify_requester_on_approval"]
SAMPLE_CLOSE_ROUTING_RULES = SAMPLE_ISSUE_CONFIG["wecom"]["close"]["routing_rules"]
SAMPLE_BACKGROUND_REMINDER_ENABLED = SAMPLE_ISSUE_CONFIG["reminders"]["background_enabled"]
SAMPLE_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS = SAMPLE_ISSUE_CONFIG["reminders"]["initial_delay_seconds"]
SAMPLE_BACKGROUND_REMINDER_INTERVAL_SECONDS = SAMPLE_ISSUE_CONFIG["reminders"]["check_interval_seconds"]
SAMPLE_REMINDER_CHECK_WINDOW = SAMPLE_ISSUE_CONFIG["reminders"]["check_window"]
SAMPLE_REMINDER_RULES = SAMPLE_ISSUE_CONFIG["reminders"]["rules"]
SAMPLE_INCOMPLETE_REMINDER_RULES = SAMPLE_ISSUE_CONFIG["reminders"]["incomplete_rules"]
