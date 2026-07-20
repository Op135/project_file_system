# -*- encoding: utf-8 -*-
"""样品单执行看板配置加载器。

维护人员通常只需修改项目根目录的 ``sample_order_dashboard_config.json``，修改后重启服务生效。
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SAMPLE_ORDER_CONFIG_PATH = Path(__file__).parent.parent / "sample_order_dashboard_config.json"

_DEFAULT_CONFIG: dict[str, Any] = {
    "public_base_url": "http://192.168.1.102:8080",
    "warning_days": 7,
    "base_editor_roles": ["研发助理", "admin"],
    "delay_editor_roles": ["研发样品组长", "admin"],
    "special_status_editor_roles": ["研发样品组长", "admin"],
    "delay_nature_marker_roles": ["研发经理", "admin"],
    "admin_roles": ["admin"],
    "special_statuses": ["正常", "暂停", "作废"],
    "special_status_reason_required": True,
    "delay_attention_threshold": 2,
    "wecom": {
        "redirect_applicant_notifications_to_manager": True,
        "notify_applicant_on_extension": True,
        "notify_applicant_on_special_status": True,
        "manager_notify_targets": [{"position": "研发经理"}],
    },
}


def _read_config_file(path: Path) -> dict:
    """读取配置文件；读取失败时返回空字典并由各字段使用默认值。"""
    try:
        with path.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return loaded
    except FileNotFoundError:
        logger.warning("样品单看板配置文件不存在：%s，已使用代码默认值", path)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("样品单看板配置文件读取失败，已使用代码默认值")
    return {}


def _string_value(config: dict, key: str, default: str) -> str:
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    logger.warning("样品单看板配置 %s 无效，已使用默认值", key)
    return default


def _nonnegative_int(config: dict, key: str, default: int) -> int:
    value = config.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    logger.warning("样品单看板配置 %s 无效，已使用默认值", key)
    return default


def _bool_value(config: dict, key: str, default: bool) -> bool:
    value = config.get(key)
    if isinstance(value, bool):
        return value
    logger.warning("样品单看板配置 %s 无效，已使用默认值", key)
    return default


def _string_list(config: dict, key: str, default: list[str]) -> list[str]:
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if normalized:
            return normalized
    logger.warning("样品单看板配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _notify_targets(config: dict, key: str, default: list) -> list:
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, (str, dict)) for item in value):
        return copy.deepcopy(value)
    logger.warning("样品单看板企业微信配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def load_sample_order_dashboard_config(path: Optional[Path] = None) -> dict[str, Any]:
    """读取并标准化样品单执行看板配置。"""
    raw = _read_config_file(path or SAMPLE_ORDER_CONFIG_PATH)
    raw_wecom = raw.get("wecom", {}) if isinstance(raw.get("wecom"), dict) else {}
    default_wecom = _DEFAULT_CONFIG["wecom"]
    special_statuses = _string_list(raw, "special_statuses", _DEFAULT_CONFIG["special_statuses"])
    if "正常" not in special_statuses:
        special_statuses.insert(0, "正常")
    return {
        "public_base_url": _string_value(raw, "public_base_url", _DEFAULT_CONFIG["public_base_url"]).rstrip("/"),
        "warning_days": _nonnegative_int(raw, "warning_days", _DEFAULT_CONFIG["warning_days"]),
        "base_editor_roles": _string_list(raw, "base_editor_roles", _DEFAULT_CONFIG["base_editor_roles"]),
        "delay_editor_roles": _string_list(raw, "delay_editor_roles", _DEFAULT_CONFIG["delay_editor_roles"]),
        "special_status_editor_roles": _string_list(
            raw,
            "special_status_editor_roles",
            _DEFAULT_CONFIG["special_status_editor_roles"],
        ),
        "delay_nature_marker_roles": _string_list(
            raw,
            "delay_nature_marker_roles",
            _DEFAULT_CONFIG["delay_nature_marker_roles"],
        ),
        "admin_roles": _string_list(raw, "admin_roles", _DEFAULT_CONFIG["admin_roles"]),
        "special_statuses": special_statuses,
        "special_status_reason_required": _bool_value(
            raw,
            "special_status_reason_required",
            _DEFAULT_CONFIG["special_status_reason_required"],
        ),
        "delay_attention_threshold": _nonnegative_int(
            raw,
            "delay_attention_threshold",
            _DEFAULT_CONFIG["delay_attention_threshold"],
        ),
        "wecom": {
            "redirect_applicant_notifications_to_manager": _bool_value(
                raw_wecom,
                "redirect_applicant_notifications_to_manager",
                default_wecom["redirect_applicant_notifications_to_manager"],
            ),
            "notify_applicant_on_extension": _bool_value(
                raw_wecom,
                "notify_applicant_on_extension",
                default_wecom["notify_applicant_on_extension"],
            ),
            "notify_applicant_on_special_status": _bool_value(
                raw_wecom,
                "notify_applicant_on_special_status",
                default_wecom["notify_applicant_on_special_status"],
            ),
            "manager_notify_targets": _notify_targets(
                raw_wecom,
                "manager_notify_targets",
                default_wecom["manager_notify_targets"],
            ),
        },
    }


SAMPLE_ORDER_CONFIG = load_sample_order_dashboard_config()
SAMPLE_ORDER_PUBLIC_BASE_URL = SAMPLE_ORDER_CONFIG["public_base_url"]
SAMPLE_ORDER_WARNING_DAYS = SAMPLE_ORDER_CONFIG["warning_days"]
SAMPLE_ORDER_BASE_EDITOR_ROLES = SAMPLE_ORDER_CONFIG["base_editor_roles"]
SAMPLE_ORDER_DELAY_EDITOR_ROLES = SAMPLE_ORDER_CONFIG["delay_editor_roles"]
SAMPLE_ORDER_SPECIAL_STATUS_EDITOR_ROLES = SAMPLE_ORDER_CONFIG["special_status_editor_roles"]
SAMPLE_ORDER_DELAY_NATURE_MARKER_ROLES = SAMPLE_ORDER_CONFIG["delay_nature_marker_roles"]
SAMPLE_ORDER_ADMIN_ROLES = SAMPLE_ORDER_CONFIG["admin_roles"]
SAMPLE_ORDER_SPECIAL_STATUSES = SAMPLE_ORDER_CONFIG["special_statuses"]
SAMPLE_ORDER_SPECIAL_STATUS_REASON_REQUIRED = SAMPLE_ORDER_CONFIG["special_status_reason_required"]
SAMPLE_ORDER_DELAY_ATTENTION_THRESHOLD = SAMPLE_ORDER_CONFIG["delay_attention_threshold"]
SAMPLE_ORDER_REDIRECT_APPLICANT_NOTIFICATIONS_TO_MANAGER = SAMPLE_ORDER_CONFIG["wecom"][
    "redirect_applicant_notifications_to_manager"
]
SAMPLE_ORDER_NOTIFY_APPLICANT_ON_EXTENSION = SAMPLE_ORDER_CONFIG["wecom"]["notify_applicant_on_extension"]
SAMPLE_ORDER_NOTIFY_APPLICANT_ON_SPECIAL_STATUS = SAMPLE_ORDER_CONFIG["wecom"][
    "notify_applicant_on_special_status"
]
SAMPLE_ORDER_MANAGER_NOTIFY_TARGETS = SAMPLE_ORDER_CONFIG["wecom"]["manager_notify_targets"]
