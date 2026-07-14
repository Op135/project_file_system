# -*- encoding: utf-8 -*-
"""生产异常管理模块的业务配置加载器。

维护人员通常只需要修改项目根目录的 ``error_management_config.json``。
本模块在 Python 导入阶段读取一次 JSON，并把经过校验的值导出为常量供页面和后台任务使用，
因此修改 JSON 后需要重启服务。

设计上采用“逐字段校验和逐字段兜底”：某一项配置错误时只回退该项，不会让整个系统因一处
JSON 内容问题而无法启动。数据库键、路由和内部状态流转等程序协议不放在 JSON 中维护。
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

from .issue_workflow_utils import normalize_time_window

logger = logging.getLogger(__name__)

ERROR_MANAGEMENT_CONFIG_PATH = Path(__file__).parent.parent / "error_management_config.json"

# 这些默认值只用于配置文件缺失或字段无效时保护系统启动。
# 正常业务维护应修改根目录 JSON，而不是直接修改这里。
_DEFAULT_CONFIG = {
    "public_base_url": "http://192.168.1.102:8080",
    "editor_roles": ["研发经理", "admin", "研发助理"],
    "product_states": ["试产", "量产"],
    "filter_states": [
        "全部",
        "异常录入",
        "原因分析中",
        "应急处理中",
        "纠正预防执行中",
        "延期申请中",
        "关闭申请中",
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
        "check_window": {"enabled": True, "start": "08:30", "end": "18:30"},
        "rules": [
            {"key": "due_7_days", "label": "约定完成日期前7天", "days_until_due": 7, "enabled": True},
            {"key": "due_3_days", "label": "约定完成日期前3天", "days_until_due": 3, "enabled": True},
            {"key": "due_today", "label": "约定完成日期当天", "days_until_due": 0, "enabled": True},
            {"key": "overdue", "label": "约定完成日期逾期", "max_days_until_due": -1, "enabled": True},
        ],
    },
}


def _read_config_file() -> dict:
    """读取根目录 JSON；无法读取时返回空字典，让后续每个字段分别使用默认值。"""
    try:
        with ERROR_MANAGEMENT_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return loaded
    except FileNotFoundError:
        logger.warning("生产异常配置文件不存在：%s，已使用代码默认值", ERROR_MANAGEMENT_CONFIG_PATH)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("生产异常配置文件读取失败，已使用代码默认值")
    return {}


def _string_value(config: dict, key: str, default: str) -> str:
    """读取必填的非空字符串，并清除首尾空格。"""
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    logger.warning("生产异常配置 %s 无效，已使用默认值", key)
    return default


def _string_list(config: dict, key: str, default: list[str], *, allow_empty: bool = False) -> list[str]:
    """读取字符串列表，同时去除重复项并保留原有顺序。"""
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if normalized or allow_empty:
            return normalized
    logger.warning("生产异常配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _filter_states(config: dict, default: list[str]) -> list[str]:
    """确保总览页的特殊筛选项“全部”始终存在且位于第一项。"""
    states = _string_list(config, "filter_states", default)
    return ["全部", *(state for state in states if state != "全部")]


def _positive_int(config: dict, key: str, default: int) -> int:
    """读取后台任务时间参数；布尔值虽然属于 int 子类，但不能当作秒数使用。"""
    value = config.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    logger.warning("生产异常配置 %s 无效，已使用默认值", key)
    return default


def _bool_value(config: dict, key: str, default: bool) -> bool:
    """读取严格布尔值，避免字符串 ``"false"`` 被误认为已关闭。"""
    value = config.get(key)
    if isinstance(value, bool):
        return value
    logger.warning("生产异常配置 %s 无效，已使用默认值", key)
    return default


def _notify_targets(config: dict, key: str, default: list) -> list:
    """读取企业微信接收人规则；每项可以是直接账号字符串或成员筛选条件字典。"""
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, (str, dict)) for item in value):
        return copy.deepcopy(value)
    logger.warning("生产异常配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _time_window(config: dict, key: str, default: dict) -> dict:
    """读取后台提醒检查时间窗口；enabled=false 表示不限制检查时间。"""
    normalized = normalize_time_window(config.get(key), default)
    if normalized is not None:
        return normalized
    logger.warning("生产异常配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _reminder_rules(config: dict, default: list[dict]) -> list[dict]:
    """校验并标准化提醒策略。

    ``days_until_due`` 用于精确匹配某一天，``max_days_until_due`` 用于匹配逾期区间。
    两者必须二选一；key 重复、格式错误或明确禁用的规则不会进入运行时规则列表。
    """
    value = config.get("rules")
    if not isinstance(value, list):
        logger.warning("生产异常提醒规则必须是列表，已使用默认值")
        return copy.deepcopy(default)

    normalized_rules = []
    seen_keys = set()
    for index, rule in enumerate(value):
        if not isinstance(rule, dict):
            logger.warning("生产异常提醒规则第 %s 项不是对象，已忽略", index + 1)
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
            logger.warning("生产异常提醒规则第 %s 项格式无效，已忽略", index + 1)
            continue

        normalized_rule = {"key": key.strip(), "label": label.strip()}
        match_key = "days_until_due" if has_exact else "max_days_until_due"
        normalized_rule[match_key] = rule[match_key]
        normalized_rules.append(normalized_rule)
        seen_keys.add(key)
    return normalized_rules


def load_error_management_config() -> dict[str, Any]:
    """组合出页面和后台任务实际使用的完整配置。

    JSON 中的额外说明字段会被自然忽略，所以配置文件可以保留 ``_说明`` 和
    ``_字段说明``，帮助不熟悉代码的维护人员理解字段含义。
    """
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
        "product_states": _string_list(raw_config, "product_states", _DEFAULT_CONFIG["product_states"]),
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
            "check_window": _time_window(
                raw_reminders,
                "check_window",
                default_reminders["check_window"],
            ),
            "rules": _reminder_rules(raw_reminders, default_reminders["rules"]),
        },
    }


# 以下值在模块首次导入时生成一次。页面、后台定时任务和旧 config.py 兼容别名都引用同一份结果。
# 这样可避免同一次运行中不同位置读取到互相矛盾的配置。
ERROR_MANAGEMENT_CONFIG = load_error_management_config()
ERROR_PUBLIC_BASE_URL = ERROR_MANAGEMENT_CONFIG["public_base_url"]
ERROR_EDITOR_ROLES = ERROR_MANAGEMENT_CONFIG["editor_roles"]
ERROR_PRODUCT_STATES = ERROR_MANAGEMENT_CONFIG["product_states"]
ERROR_FILTER_STATES = ERROR_MANAGEMENT_CONFIG["filter_states"]
ERROR_FILTER_ALL_STATE = "全部"
ERROR_FILTER_PENDING_EXTENSION_STATE = "延期申请中"
ERROR_FILTER_PENDING_CLOSE_STATE = "关闭申请中"
ERROR_DEFAULT_NOTIFY_TARGETS = ERROR_MANAGEMENT_CONFIG["wecom"]["default_notify_targets"]
ERROR_EXTENSION_APPROVER_ROLES = ERROR_MANAGEMENT_CONFIG["wecom"]["extension"]["approver_roles"]
ERROR_EXTENSION_NOTIFY_TARGETS = ERROR_MANAGEMENT_CONFIG["wecom"]["extension"]["notify_targets"]
ERROR_EXTENSION_APPROVAL_NOTIFY_TARGETS = ERROR_MANAGEMENT_CONFIG["wecom"]["extension"]["approval_notify_targets"]
ERROR_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL = ERROR_MANAGEMENT_CONFIG["wecom"]["extension"][
    "notify_requester_on_approval"
]
ERROR_BACKGROUND_REMINDER_ENABLED = ERROR_MANAGEMENT_CONFIG["reminders"]["background_enabled"]
ERROR_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS = ERROR_MANAGEMENT_CONFIG["reminders"]["initial_delay_seconds"]
ERROR_BACKGROUND_REMINDER_INTERVAL_SECONDS = ERROR_MANAGEMENT_CONFIG["reminders"]["check_interval_seconds"]
ERROR_REMINDER_CHECK_WINDOW = ERROR_MANAGEMENT_CONFIG["reminders"]["check_window"]
ERROR_REMINDER_RULES = ERROR_MANAGEMENT_CONFIG["reminders"]["rules"]
