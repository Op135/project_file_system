# -*- encoding: utf-8 -*-
"""ECN 工程变更模块配置加载器与待办判定规则。

维护人员通常只需要修改项目根目录的 ``ecn_management_config.json``。
配置在模块导入时读取一次，因此修改后需要重启服务。
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ECN_CONFIG_PATH = Path(__file__).parent.parent / "ecn_management_config.json"
ECN_DATA_KEY = "ecn_management_data"
ECN_VERSION_KEY = "ecn_global_version_stamp"
ECN_SCHEME_GROUP_ORDINARY_DOCUMENT = "ordinary_document"
ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT = "overview_document"
ECN_SCHEME_GROUP_MATERIAL = "material"
ECN_SCHEME_GROUP_UNKNOWN = "unknown"


def get_ecn_stage_index(value: object) -> int:
    """将JSON中的负责人节点序号安全收窄为整数，无效值按首节点处理。"""
    if not isinstance(value, (bool, int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def get_ecn_material_code_field_labels(change_type: Any) -> tuple[tuple[str, str], ...]:
    """返回各物料动作需要填写的料号字段及界面名称。"""
    if change_type in {
        ECN_MATERIAL_CHANGE_TYPE_ADD,
        ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE,
        ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY,
    }:
        return (("material_code", "料号"),)
    if change_type == ECN_MATERIAL_CHANGE_TYPE_REPLACE:
        return (("old_material_code", "改前料号"), ("new_material_code", "改后料号"))
    return ()


def _get_ecn_alternative_material_groups(change_type: Any) -> tuple[tuple[str, str], ...]:
    """返回物料动作允许维护的替换料分组字段及显示名称。"""
    if change_type == ECN_MATERIAL_CHANGE_TYPE_ADD:
        return (("alternative_materials", "新增替换料"),)
    if change_type == ECN_MATERIAL_CHANGE_TYPE_REPLACE:
        return (
            ("old_alternative_materials", "更改前替换料"),
            ("new_alternative_materials", "更改后替换料"),
        )
    return ()


def get_ecn_material_code_entries(item: Any) -> list[dict[str, str]]:
    """列出主物料及替换料的全部料号，token 可供专用补录窗口提交。"""
    if not isinstance(item, dict) or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
        return []
    material_change = item.get("material_change", {})
    if not isinstance(material_change, dict):
        return []
    entries = [
        {
            "token": f"main:{key}",
            "label": label,
            "value": str(material_change.get(key) or "").strip(),
        }
        for key, label in get_ecn_material_code_field_labels(item.get("change_type"))
    ]
    for group_key, group_label in _get_ecn_alternative_material_groups(item.get("change_type")):
        group = material_change.get(group_key, [])
        if not isinstance(group, list):
            continue
        for index, alternative in enumerate(group, start=1):
            if not isinstance(alternative, dict):
                continue
            alternative_id = str(alternative.get("alternative_id") or "").strip()
            if not alternative_id:
                continue
            name = str(alternative.get("material_name") or "").strip()
            name_suffix = f"（{name}）" if name else ""
            entries.append(
                {
                    "token": f"{group_key}:{alternative_id}",
                    "label": f"{group_label} {index} 料号{name_suffix}",
                    "value": str(alternative.get("material_code") or "").strip(),
                }
            )
    return entries


def apply_ecn_material_code_values(item: dict, values: dict[str, str]) -> bool:
    """按稳定 token 写入料号；结构变化或未知 token 时拒绝。"""
    material_change = item.get("material_change", {})
    if not isinstance(material_change, dict):
        return False
    remaining = set(values)
    for key, _ in get_ecn_material_code_field_labels(item.get("change_type")):
        token = f"main:{key}"
        if token in values:
            material_change[key] = values[token]
            remaining.discard(token)
    for group_key, _ in _get_ecn_alternative_material_groups(item.get("change_type")):
        group = material_change.get(group_key, [])
        if not isinstance(group, list):
            return False
        for alternative in group:
            if not isinstance(alternative, dict):
                return False
            alternative_id = str(alternative.get("alternative_id") or "").strip()
            token = f"{group_key}:{alternative_id}"
            if alternative_id and token in values:
                alternative["material_code"] = values[token]
                remaining.discard(token)
    return not remaining


def get_ecn_material_code_missing_fields(item: Any) -> list[str]:
    """返回一条物料方案尚未补齐的料号字段名称。"""
    if not isinstance(item, dict) or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
        return []
    return [
        entry["label"]
        for entry in get_ecn_material_code_entries(item)
        if not entry["value"]
    ]


def get_ecn_missing_material_code_items(ecn_data: Any) -> list[dict[str, object]]:
    """按方案显示顺序列出缺少料号的物料方案。"""
    if not isinstance(ecn_data, dict):
        return []
    change_items = ecn_data.get("change_items", [])
    if not isinstance(change_items, list):
        return []
    missing: list[dict[str, object]] = []
    for index, item in enumerate(change_items, start=1):
        fields = get_ecn_material_code_missing_fields(item)
        if fields:
            missing.append(
                {
                    "item_id": str(item.get("item_id") or ""),
                    "scheme_no": f"#{index:02d}",
                    "fields": fields,
                }
            )
    return missing


def get_ecn_material_change_display(item: Any) -> tuple[str, str]:
    """返回结构化物料方案用于表格/快照展示的“变更前、变更后”文本。"""
    if not isinstance(item, dict):
        return "", ""
    change_type = item.get("change_type")
    material_change = item.get("material_change", {})
    if change_type not in ECN_MATERIAL_CHANGE_TYPES or not isinstance(material_change, dict):
        return "", ""

    def alternative_text(group_key: str | None) -> str:
        if not group_key:
            return ""
        group = material_change.get(group_key, [])
        if not isinstance(group, list) or not group:
            return ""
        lines = ["替换料："]
        for index, alternative in enumerate(group, start=1):
            if not isinstance(alternative, dict):
                continue
            code = str(alternative.get("material_code") or "").strip() or "待补充"
            name = str(alternative.get("material_name") or "").strip()
            lines.extend((f"{index}. 料号：{code}", f"   {name}"))
        return "\n".join(lines) if len(lines) > 1 else ""

    def material_text(
        code_key: str,
        name_key: str,
        quantity_key: str,
        unit_key: str,
        alternative_group_key: str | None = None,
    ) -> str:
        code = str(material_change.get(code_key) or "").strip() or "待补充"
        name = str(material_change.get(name_key) or "").strip()
        quantity = material_change.get(quantity_key)
        unit = str(material_change.get(unit_key) or ECN_MATERIAL_DEFAULT_UNIT).strip()
        quantity_text = (
            "" if quantity in [None, ""] else f"{quantity:g}" if isinstance(quantity, (int, float)) else str(quantity)
        )
        lines = [f"料号：{code}", name, f"用量：{quantity_text} {unit}"]
        alternatives = alternative_text(alternative_group_key)
        if alternatives:
            lines.extend(("", alternatives))
        return "\n".join(lines).strip()

    if change_type == ECN_MATERIAL_CHANGE_TYPE_ADD:
        return "无", material_text(
            "material_code", "material_name", "quantity", "unit", "alternative_materials"
        )
    if change_type == ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE:
        return material_text("material_code", "material_name", "quantity", "unit"), str(change_type)
    if change_type == ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY:
        code = str(material_change.get("material_code") or "").strip() or "待补充"
        name = str(material_change.get("material_name") or "").strip()
        unit = str(material_change.get("unit") or ECN_MATERIAL_DEFAULT_UNIT).strip()
        old_quantity = material_change.get("old_quantity")
        new_quantity = material_change.get("new_quantity")
        old_quantity_text = f"{old_quantity:g}" if isinstance(old_quantity, (int, float)) else str(old_quantity)
        new_quantity_text = f"{new_quantity:g}" if isinstance(new_quantity, (int, float)) else str(new_quantity)
        return (
            f"料号：{code}\n{name}\n用量：{old_quantity_text} {unit}",
            f"料号：{code}\n{name}\n用量：{new_quantity_text} {unit}",
        )
    return (
        material_text(
            "old_material_code",
            "old_material_name",
            "old_quantity",
            "old_unit",
            "old_alternative_materials",
        ),
        material_text(
            "new_material_code",
            "new_material_name",
            "new_quantity",
            "new_unit",
            "new_alternative_materials",
        ),
    )


def split_ecn_material_change_display(value: str) -> tuple[str, str]:
    """将物料主信息与替换料附加信息拆开，供界面使用不同视觉层级。"""
    marker = "\n\n替换料：\n"
    primary, separator, alternatives = str(value or "").partition(marker)
    if not separator:
        return primary, ""
    return primary, f"替换料：\n{alternatives}"


def get_ecn_material_change_missing_fields(change_type: Any, material_change: Any) -> list[str]:
    """返回结构化物料方案缺少的必填字段中文名。数量为 0 时仍视为已填写。"""
    if change_type not in ECN_MATERIAL_CHANGE_TYPES or not isinstance(material_change, dict):
        return ["方案分类"]
    required_fields = {
        ECN_MATERIAL_CHANGE_TYPE_ADD: [
            ("material_name", "物料名称"),
            ("quantity", "用量"),
            ("unit", "单位"),
        ],
        ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE: [
            ("material_name", "物料名称"),
            ("quantity", "用量"),
            ("unit", "单位"),
        ],
        ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY: [
            ("material_name", "物料名称"),
            ("old_quantity", "改前用量"),
            ("new_quantity", "改后用量"),
            ("unit", "单位"),
        ],
        ECN_MATERIAL_CHANGE_TYPE_REPLACE: [
            ("old_material_name", "改前物料名称"),
            ("old_quantity", "改前用量"),
            ("old_unit", "改前单位"),
            ("new_material_name", "改后物料名称"),
            ("new_quantity", "改后用量"),
            ("new_unit", "改后单位"),
        ],
    }[change_type]
    missing = [
        label
        for key, label in required_fields
        if material_change.get(key) is None or str(material_change.get(key)).strip() == ""
    ]
    for group_key, group_label in _get_ecn_alternative_material_groups(change_type):
        group = material_change.get(group_key, [])
        if not isinstance(group, list):
            missing.append(group_label)
            continue
        for index, alternative in enumerate(group, start=1):
            if not isinstance(alternative, dict):
                missing.append(f"{group_label} {index} 数据")
                continue
            if not str(alternative.get("alternative_id") or "").strip():
                missing.append(f"{group_label} {index} 数据")
            if not str(alternative.get("material_name") or "").strip():
                missing.append(f"{group_label} {index} 物料名称")
    return missing


class ECNState:
    DRAFT = "草稿"
    ECR_REVIEWING = "ECR 审批中"
    ECN_SCHEMING = "ECN 方案编写与确认中"
    ECN_REVIEWING = "ECN 方案评审中"
    MATERIAL_CODE_PENDING = "ECN 料号补充中"
    ECN_EXECUTING = "ECN 执行确认中"
    CLOSED = "变更已完成"
    CANCEL = "变更已作废"
    REJECTED = "已被驳回"


_DEFAULT_CONFIG: dict[str, Any] = {
    "wecom": {
        "enabled": True,
        "test_mode": True,
        "cc_manager_enabled": True,
        "test_notify_targets": [{"position": "研发经理"}],
        "public_base_url": "",
        "initial_delay_seconds": 30,
        "check_interval_seconds": 60,
        "repeat_hours": 24,
        "retry_seconds": 300,
    },
    "allowed_project_states": ["试产", "量产"],
    "permissions": {
        "scheme_initiator_roles": ["研发经理", "admin"],
        "scheme_writer_roles": ["研发", "工程", "质量"],
        "impact_initial_reminder_roles": ["研发助理"],
        "ordinary_document_file_view_roles_by_type": {
            change_type: ["admin", "研发", "工程", "质量", "销售", "生产", "PMC"]
            for change_type in ["图纸更新", "SOP修改", "测试报告内容格式", "其它"]
        },
    },
    "reminders": {
        "impact_followup_states": [ECNState.ECN_SCHEMING, ECNState.ECN_REVIEWING],
    },
    "scheme_review": {
        "require_rejected_item_selection": True,
        "require_revision_before_reconfirmation": True,
        "participant_statuses": {
            "editing": {"label": "编写中", "color": "orange", "icon": "edit", "remind": True},
            "confirmed": {
                "label": "确认完成方案",
                "color": "green",
                "icon": "check_circle",
                "remind": False,
            },
            "needs_reconfirmation": {
                "label": "待重新确认",
                "color": "red",
                "icon": "published_with_changes",
                "remind": True,
            },
        },
        "item_statuses": {
            "normal": {"label": "正常"},
            "needs_improvement": {"label": "待改进"},
            "revised_pending_confirmation": {"label": "已改进，待重新确认"},
            "revised_confirmed": {"label": "已整改并重新确认"},
        },
        "transitions": {
            "participant_after_edit": "editing",
            "participant_after_confirmation": "confirmed",
            "participant_after_rejection": "needs_reconfirmation",
            "item_after_rejection": "needs_improvement",
            "item_after_revision": "revised_pending_confirmation",
            "item_after_reconfirmation": "revised_confirmed",
        },
    },
    "scheme_tracking": {
        "traceability_levels": [
            "文件",
            "供应商",
            "零件仓",
            "生产在线",
            "半成品仓",
            "成品仓",
            "客户/在途",
        ],
        "disposition_measures": ["不适用", "无条件用完止", "有条件用完止", "返工", "暂存移用", "报废"],
    },
    "scheme_options": {
        "document_change_types": ["图纸更新", "SOP修改", "测试报告内容格式", "其它"],
        "overview_actions": {
            "add": "新增",
            "update": "更换",
            "deactivate": "失效",
        },
        "material_change_types": {
            "add": "新增",
            "adjust_quantity": "调量",
            "discontinue": "仅删除",
            "replace": "更改",
        },
        "material_default_unit": "pcs",
        "material_disposition_required_types": ["discontinue", "replace"],
        "disposition_condition_required_measures": ["有条件用完止"],
    },
    "ui": {
        "overview_conflict_auto_close_seconds": 5.0,
    },
    "schema": {
        "material_categories": [
            "光源",
            "光源基板",
            "光学器件",
            "结构加工件",
            "标签包材",
            "紧固件",
            "外购标准件",
            "电子料",
            "PCB",
            "PCBA",
            "线材",
            "固件",
            "辅料",
        ],
        "material_actions": ["新增", "调量", "仅删除", "更改"],
        "impact_dimensions": [
            "光学部件",
            "内部结构",
            "结构外观",
            "线材",
            "标签包装",
            "硬件易识别",
            "硬件难识别",
            "硬件接口",
            "固件",
            "UI",
            "工艺",
            "工装治具",
            "成本",
            "生产效率",
            "风险等级",
        ],
        "document_types": [
            "光学件图纸",
            "结构件图纸",
            "成品/PCBA图档(3D/2D)",
            "线材图纸",
            "包材图纸",
            "原理图/Layout图/丝印图",
            "其它外购件图纸",
            "产品总BOM",
            "电子BOM",
            "装箱清单",
            "通讯协议/XML协议文档",
            "硬件使用说明书",
            "产品接线说明书",
            "固件使用说明书",
            "产品使用说明书",
            "产品技术规格书",
            "SOP/作业指导书",
            "工装治具清单",
            "出厂测试报告",
            "医疗器械产品风险管理",
            "其它",
        ],
        "reasons": ["需求更改", "设计改善", "工艺调整", "物料替换", "资料修正", "产品定标", "其他"],
        "change_natures": ["永久变更", "临时变更"],
    },
}


def _read_config_file() -> dict:
    try:
        with ECN_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return loaded
    except FileNotFoundError:
        logger.warning("ECN配置文件不存在：%s，已使用代码默认值", ECN_CONFIG_PATH)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("ECN配置文件读取失败，已使用代码默认值")
    return {}


def _string_list(value: Any, default: list[str], field_name: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if normalized:
            return normalized
    logger.warning("ECN配置 %s 无效，已使用默认值", field_name)
    return copy.deepcopy(default)


def _role_map(value: Any, default: dict[str, list[str]], field_name: str) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        logger.warning("ECN配置 %s 无效，已使用默认值", field_name)
        return copy.deepcopy(default)

    result: dict[str, list[str]] = {}
    for change_type, default_roles in default.items():
        roles = value.get(change_type)
        if isinstance(roles, list) and all(isinstance(role, str) and role.strip() for role in roles):
            result[change_type] = list(dict.fromkeys(role.strip() for role in roles))
        else:
            logger.warning(
                "ECN配置 %s.%s 无效，已使用默认值",
                field_name,
                change_type,
            )
            result[change_type] = copy.deepcopy(default_roles)
    for change_type, roles in value.items():
        if change_type in result or not isinstance(change_type, str) or not change_type.strip():
            continue
        if isinstance(roles, list) and all(isinstance(role, str) and role.strip() for role in roles):
            result[change_type.strip()] = list(dict.fromkeys(role.strip() for role in roles))
        else:
            logger.warning("ECN配置 %s.%s 无效，已忽略", field_name, change_type)
    return result


def _positive_number(value: Any, default: float, field_name: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    logger.warning("ECN配置 %s 无效，已使用默认值", field_name)
    return default


def _bool_value(value: Any, default: bool, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    logger.warning("ECN配置 %s 无效，已使用默认值", field_name)
    return default


def _status_config(value: Any, default: dict, field_name: str) -> dict:
    if not isinstance(value, dict):
        logger.warning("ECN配置 %s 无效，已使用默认值", field_name)
        return copy.deepcopy(default)
    result = copy.deepcopy(default)
    for status, default_info in default.items():
        raw_info = value.get(status)
        if not isinstance(raw_info, dict):
            logger.warning("ECN配置 %s.%s 无效，已使用默认值", field_name, status)
            continue
        for key, default_value in default_info.items():
            candidate = raw_info.get(key)
            if isinstance(default_value, bool):
                if isinstance(candidate, bool):
                    result[status][key] = candidate
            elif isinstance(candidate, str) and candidate.strip():
                result[status][key] = candidate.strip()
    return result


def _transition_config(value: Any, default: dict, participant_statuses: dict, item_statuses: dict) -> dict:
    if not isinstance(value, dict):
        logger.warning("ECN配置 scheme_review.transitions 无效，已使用默认值")
        return copy.deepcopy(default)
    result = copy.deepcopy(default)
    for key, default_status in default.items():
        candidate = value.get(key)
        allowed_statuses = participant_statuses if key.startswith("participant_") else item_statuses
        if isinstance(candidate, str) and candidate in allowed_statuses:
            result[key] = candidate
        else:
            logger.warning("ECN配置 scheme_review.transitions.%s 无效，已使用默认值", key)
            result[key] = default_status
    return result


def load_ecn_config(raw_config: dict | None = None) -> dict:
    """读取并逐字段校验 ECN 配置；无效字段独立回退，不影响其他有效配置。"""
    raw = _read_config_file() if raw_config is None else raw_config
    if not isinstance(raw, dict):
        raw = {}

    result = copy.deepcopy(_DEFAULT_CONFIG)
    raw_wecom = raw.get("wecom", {})
    if not isinstance(raw_wecom, dict):
        raw_wecom = {}
    for key in ("enabled", "test_mode", "cc_manager_enabled"):
        result["wecom"][key] = _bool_value(raw_wecom.get(key), _DEFAULT_CONFIG["wecom"][key], f"wecom.{key}")
    for key in ("initial_delay_seconds", "check_interval_seconds", "repeat_hours", "retry_seconds"):
        result["wecom"][key] = _positive_number(raw_wecom.get(key), _DEFAULT_CONFIG["wecom"][key], f"wecom.{key}")
    raw_url = raw_wecom.get("public_base_url")
    if isinstance(raw_url, str):
        result["wecom"]["public_base_url"] = raw_url.strip().rstrip("/")
    targets = raw_wecom.get("test_notify_targets")
    if isinstance(targets, list) and targets and all(isinstance(target, (str, dict)) and target for target in targets):
        result["wecom"]["test_notify_targets"] = copy.deepcopy(targets)
    result["allowed_project_states"] = _string_list(
        raw.get("allowed_project_states"),
        _DEFAULT_CONFIG["allowed_project_states"],
        "allowed_project_states",
    )

    raw_permissions = raw.get("permissions", {})
    if not isinstance(raw_permissions, dict):
        raw_permissions = {}
    for key, default in _DEFAULT_CONFIG["permissions"].items():
        if key == "ordinary_document_file_view_roles_by_type":
            continue
        result["permissions"][key] = _string_list(raw_permissions.get(key), default, f"permissions.{key}")
    result["permissions"]["ordinary_document_file_view_roles_by_type"] = _role_map(
        raw_permissions.get("ordinary_document_file_view_roles_by_type"),
        _DEFAULT_CONFIG["permissions"]["ordinary_document_file_view_roles_by_type"],
        "permissions.ordinary_document_file_view_roles_by_type",
    )

    raw_reminders = raw.get("reminders", {})
    if not isinstance(raw_reminders, dict):
        raw_reminders = {}
    result["reminders"]["impact_followup_states"] = _string_list(
        raw_reminders.get("impact_followup_states"),
        _DEFAULT_CONFIG["reminders"]["impact_followup_states"],
        "reminders.impact_followup_states",
    )
    raw_scheme_review = raw.get("scheme_review", {})
    if not isinstance(raw_scheme_review, dict):
        raw_scheme_review = {}
    result["scheme_review"]["require_rejected_item_selection"] = _bool_value(
        raw_scheme_review.get("require_rejected_item_selection"),
        _DEFAULT_CONFIG["scheme_review"]["require_rejected_item_selection"],
        "scheme_review.require_rejected_item_selection",
    )
    result["scheme_review"]["require_revision_before_reconfirmation"] = _bool_value(
        raw_scheme_review.get("require_revision_before_reconfirmation"),
        _DEFAULT_CONFIG["scheme_review"]["require_revision_before_reconfirmation"],
        "scheme_review.require_revision_before_reconfirmation",
    )
    for status_group in ["participant_statuses", "item_statuses"]:
        result["scheme_review"][status_group] = _status_config(
            raw_scheme_review.get(status_group),
            _DEFAULT_CONFIG["scheme_review"][status_group],
            f"scheme_review.{status_group}",
        )
    result["scheme_review"]["transitions"] = _transition_config(
        raw_scheme_review.get("transitions"),
        _DEFAULT_CONFIG["scheme_review"]["transitions"],
        result["scheme_review"]["participant_statuses"],
        result["scheme_review"]["item_statuses"],
    )

    raw_scheme_tracking = raw.get("scheme_tracking", {})
    if not isinstance(raw_scheme_tracking, dict):
        raw_scheme_tracking = {}
    for key, default in _DEFAULT_CONFIG["scheme_tracking"].items():
        result["scheme_tracking"][key] = _string_list(raw_scheme_tracking.get(key), default, f"scheme_tracking.{key}")

    raw_scheme_options = raw.get("scheme_options", {})
    if not isinstance(raw_scheme_options, dict):
        raw_scheme_options = {}
    for key in [
        "document_change_types",
        "material_disposition_required_types",
        "disposition_condition_required_measures",
    ]:
        default = _DEFAULT_CONFIG["scheme_options"][key]
        result["scheme_options"][key] = _string_list(raw_scheme_options.get(key), default, f"scheme_options.{key}")
    for option_key in ["overview_actions", "material_change_types"]:
        default_labels = _DEFAULT_CONFIG["scheme_options"][option_key]
        raw_labels = raw_scheme_options.get(option_key, {})
        if not isinstance(raw_labels, dict):
            raw_labels = {}
        result["scheme_options"][option_key] = {}
        for semantic_key, default_label in default_labels.items():
            label = raw_labels.get(semantic_key)
            result["scheme_options"][option_key][semantic_key] = (
                label.strip() if isinstance(label, str) and label.strip() else default_label
            )
    default_unit = _DEFAULT_CONFIG["scheme_options"]["material_default_unit"]
    raw_default_unit = raw_scheme_options.get("material_default_unit")
    result["scheme_options"]["material_default_unit"] = (
        raw_default_unit.strip() if isinstance(raw_default_unit, str) and raw_default_unit.strip() else default_unit
    )

    raw_ui = raw.get("ui", {})
    if not isinstance(raw_ui, dict):
        raw_ui = {}
    result["ui"]["overview_conflict_auto_close_seconds"] = _positive_number(
        raw_ui.get("overview_conflict_auto_close_seconds"),
        _DEFAULT_CONFIG["ui"]["overview_conflict_auto_close_seconds"],
        "ui.overview_conflict_auto_close_seconds",
    )

    raw_schema = raw.get("schema", {})
    if not isinstance(raw_schema, dict):
        raw_schema = {}
    for key, default in _DEFAULT_CONFIG["schema"].items():
        result["schema"][key] = _string_list(raw_schema.get(key), default, f"schema.{key}")

    return result


ECN_CONFIG = load_ecn_config()
ECN_WECOM_CONFIG = ECN_CONFIG["wecom"]
ECN_SCHEMA_CONFIG = ECN_CONFIG["schema"]
ECN_ALLOWED_PROJECT_STATES = ECN_CONFIG["allowed_project_states"]
ECN_SCHEME_INITIATOR_ROLES = ECN_CONFIG["permissions"]["scheme_initiator_roles"]
ECN_SCHEME_WRITER_ROLES = ECN_CONFIG["permissions"]["scheme_writer_roles"]
ECN_IMPACT_INITIAL_REMINDER_ROLES = ECN_CONFIG["permissions"]["impact_initial_reminder_roles"]
ECN_ORDINARY_DOCUMENT_FILE_VIEW_ROLES_BY_TYPE = ECN_CONFIG["permissions"]["ordinary_document_file_view_roles_by_type"]
ECN_IMPACT_FOLLOWUP_STATES = ECN_CONFIG["reminders"]["impact_followup_states"]
ECN_REQUIRE_REJECTED_ITEM_SELECTION = ECN_CONFIG["scheme_review"]["require_rejected_item_selection"]
ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION = ECN_CONFIG["scheme_review"]["require_revision_before_reconfirmation"]
ECN_PARTICIPANT_STATUS_CONFIG = ECN_CONFIG["scheme_review"]["participant_statuses"]
ECN_ITEM_STATUS_CONFIG = ECN_CONFIG["scheme_review"]["item_statuses"]
ECN_SCHEME_STATUS_TRANSITIONS = ECN_CONFIG["scheme_review"]["transitions"]
ECN_PARTICIPANT_STATUS_EDITING = ECN_SCHEME_STATUS_TRANSITIONS["participant_after_edit"]
ECN_PARTICIPANT_STATUS_CONFIRMED = ECN_SCHEME_STATUS_TRANSITIONS["participant_after_confirmation"]
ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION = ECN_SCHEME_STATUS_TRANSITIONS["participant_after_rejection"]
ECN_ITEM_STATUS_NORMAL = "normal"
ECN_ITEM_STATUS_NEEDS_IMPROVEMENT = ECN_SCHEME_STATUS_TRANSITIONS["item_after_rejection"]
ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION = ECN_SCHEME_STATUS_TRANSITIONS["item_after_revision"]
ECN_ITEM_STATUS_REVISED_CONFIRMED = ECN_SCHEME_STATUS_TRANSITIONS["item_after_reconfirmation"]
ECN_TRACEABILITY_LEVELS: list[str] = [str(level) for level in ECN_CONFIG["scheme_tracking"]["traceability_levels"]]
ECN_DISPOSITION_MEASURES = ECN_CONFIG["scheme_tracking"]["disposition_measures"]
ECN_DOCUMENT_CHANGE_TYPES = ECN_CONFIG["scheme_options"]["document_change_types"]
ECN_OVERVIEW_ACTION_LABELS = ECN_CONFIG["scheme_options"]["overview_actions"]
ECN_OVERVIEW_ACTION_ADD = "add"
ECN_OVERVIEW_ACTION_UPDATE = "update"
ECN_OVERVIEW_ACTION_DEACTIVATE = "deactivate"
ECN_MATERIAL_CHANGE_TYPE_LABELS = ECN_CONFIG["scheme_options"]["material_change_types"]
ECN_MATERIAL_CHANGE_TYPES = list(ECN_MATERIAL_CHANGE_TYPE_LABELS.values())
ECN_MATERIAL_DEFAULT_UNIT = ECN_CONFIG["scheme_options"]["material_default_unit"]
ECN_MATERIAL_DISPOSITION_REQUIRED_TYPES = set(ECN_CONFIG["scheme_options"]["material_disposition_required_types"])
ECN_DISPOSITION_CONDITION_REQUIRED_MEASURES = set(
    ECN_CONFIG["scheme_options"]["disposition_condition_required_measures"]
)
ECN_EXECUTION_STAGE_ASSISTANT = "assistant_confirmation"
ECN_EXECUTION_STAGE_OVERVIEW_RUNNING = "overview_execution"
ECN_EXECUTION_STAGE_OVERVIEW_FAILED = "overview_failed"
ECN_EXECUTION_STAGE_MATERIAL = "material_confirmation"
ECN_EXECUTION_STAGE_COMPLETED = "completed"
ECN_EXECUTION_RESULT_PENDING = "pending"
ECN_EXECUTION_RESULT_RUNNING = "running"
ECN_EXECUTION_RESULT_SUCCESS = "success"
ECN_EXECUTION_RESULT_FAILED = "failed"
ECN_MATERIAL_CHANGE_TYPE_ADD = ECN_MATERIAL_CHANGE_TYPE_LABELS["add"]
ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY = ECN_MATERIAL_CHANGE_TYPE_LABELS["adjust_quantity"]
ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE = ECN_MATERIAL_CHANGE_TYPE_LABELS["discontinue"]
ECN_MATERIAL_CHANGE_TYPE_REPLACE = ECN_MATERIAL_CHANGE_TYPE_LABELS["replace"]
ECN_OVERVIEW_CONFLICT_AUTO_CLOSE_SECONDS = ECN_CONFIG["ui"]["overview_conflict_auto_close_seconds"]


def ecn_overview_requires_new_content(project_states: Any) -> bool:
    """只要有项目不是纯失效动作，系统内资料方案就必须提供新内容。"""
    if not isinstance(project_states, dict):
        return False
    return any(
        isinstance(state, dict) and state.get("action") != ECN_OVERVIEW_ACTION_DEACTIVATE
        for state in project_states.values()
    )


def get_ecn_overview_project_new_data(new_data: Any, project_state: Any) -> dict[str, Any]:
    """把统一方案内容与单项目校验结果合并，供展示及最终写入概述节点使用。"""
    merged = copy.deepcopy(new_data) if isinstance(new_data, dict) else {}
    project_file_data = project_state.get("new_file_data", {}) if isinstance(project_state, dict) else {}
    if isinstance(project_file_data, dict):
        merged.update(copy.deepcopy(project_file_data))
    return merged


def collect_ecn_pending_overview_overrides(
    change_items: Any,
    primary_project: Any,
    excluded_item_id: Any = None,
) -> dict[str, str]:
    """收集其它未执行概述方案；编辑当前方案时不得把自身草稿当成依赖覆盖。"""
    if not isinstance(change_items, list) or not primary_project:
        return {}
    overrides = {}
    for item in change_items:
        if not isinstance(item, dict) or item.get("type") != "overview_update":
            continue
        if excluded_item_id and item.get("item_id") == excluded_item_id:
            continue
        project_state = item.get("project_states", {}).get(primary_project, {})
        if project_state.get("action") not in {
            ECN_OVERVIEW_ACTION_ADD,
            ECN_OVERVIEW_ACTION_UPDATE,
        }:
            continue
        label = item.get("label")
        content = str(item.get("new_data", {}).get("content") or "").strip()
        if label and content:
            overrides[str(label)] = content
    return overrides


def resolve_ecn_overview_parameter_config(
    flat_configs: Any,
    label: Any,
) -> tuple[dict[str, Any], str]:
    """按具体参数取得当前配置；编辑弹窗初始化不能依赖选择控件补发变化事件。"""
    if not isinstance(flat_configs, dict) or not label:
        return {}, "text"
    raw_config = flat_configs.get(label, {})
    config = copy.deepcopy(raw_config) if isinstance(raw_config, dict) else {}
    processing_type = str(config.get("processing_type") or "text")
    return config, processing_type


def is_ecn_material_disposition_required(change_type: Any) -> bool:
    if change_type not in ECN_MATERIAL_CHANGE_TYPES:
        return True
    semantic_key = next(
        (key for key, label in ECN_MATERIAL_CHANGE_TYPE_LABELS.items() if label == change_type),
        None,
    )
    return semantic_key in ECN_MATERIAL_DISPOSITION_REQUIRED_TYPES


def is_ecn_disposition_condition_required(disposition_measure: Any) -> bool:
    return disposition_measure in ECN_DISPOSITION_CONDITION_REQUIRED_MEASURES


def expand_new_material_traceability_selection(
    selected_levels: Any,
    previous_levels: Any,
) -> list[str]:
    """仅在新勾选等级时向上扩选；取消已有等级时原样保留断层。"""
    selected = (
        {str(level) for level in selected_levels if level not in [None, ""]}
        if isinstance(selected_levels, (list, tuple, set))
        else set()
    )
    previous = (
        {str(level) for level in previous_levels if level not in [None, ""]}
        if isinstance(previous_levels, (list, tuple, set))
        else set()
    )

    newly_selected = selected - previous
    newly_selected_indexes = [index for index, level in enumerate(ECN_TRACEABILITY_LEVELS) if level in newly_selected]
    if newly_selected_indexes:
        selected.update(ECN_TRACEABILITY_LEVELS[: max(newly_selected_indexes) + 1])
    return [level for level in ECN_TRACEABILITY_LEVELS if level in selected]


def role_matches_keywords(current_role: str, role_keywords: list[str]) -> bool:
    role_text = str(current_role or "")
    return any(keyword in role_text for keyword in role_keywords)


def is_ecn_review_info_blank(review_info: Any) -> bool:
    """判断 ECN 影响区是否尚未填写任何有效选择或说明。"""
    if not isinstance(review_info, dict):
        return True
    if review_info.get("expanded_projects_mass") or review_info.get("expanded_projects_non_mass"):
        return False
    if any(bool(value) for value in review_info.get("impacts", {}).values()):
        return False
    if any(bool(value) for value in review_info.get("involved_docs", {}).values()):
        return False
    for actions in review_info.get("involved_materials", {}).values():
        if isinstance(actions, dict) and any(bool(value) for value in actions.values()):
            return False
    if str(review_info.get("other_docs_desc", "")).strip():
        return False
    return True


def get_ecn_scheme_target_projects(ecn_data: Any) -> list[str]:
    """返回方案可关联的完整项目范围：ECR申请项目加影响评审扩大项目。"""
    if not isinstance(ecn_data, dict):
        return []
    review_info = ecn_data.get("review_info", {})
    if not isinstance(review_info, dict):
        review_info = {}
    projects = []
    for values in (
        ecn_data.get("target_projects", []),
        review_info.get("expanded_projects_mass", []),
        review_info.get("expanded_projects_non_mass", []),
    ):
        if not isinstance(values, (list, tuple)):
            continue
        for project in values:
            project_name = str(project or "").strip()
            if project_name and project_name not in projects:
                projects.append(project_name)
    return projects


def is_ecn_impact_blank(ecn_data: Any) -> bool:
    if not isinstance(ecn_data, dict):
        return True
    return is_ecn_review_info_blank(ecn_data.get("review_info", {}))


def get_active_overview_row_contents(raw_data: Any, row_id: Any, req_max_ver: str) -> list[str]:
    """提取概述具体参数在指定基准行、当前需求版本下已经激活的内容。"""
    if not isinstance(raw_data, dict) or row_id in [None, ""]:
        return []

    contents = []
    for chip in raw_data.values():
        if not isinstance(chip, dict) or chip.get("row_id") != row_id:
            continue
        active_versions = chip.get("select_activ_dic", {})
        if not isinstance(active_versions, dict) or active_versions.get(req_max_ver) is not True:
            continue
        content = str(chip.get("content", "")).strip() or "（空内容）"
        if content not in contents:
            contents.append(content)
    return contents


def get_ecn_overview_deactivation_remaining_contents(raw_data: Any, chip_id: Any, req_max_ver: str) -> list[str]:
    """预览本条失效操作后，同一具体参数在当前需求版本下剩余的有效内容。"""
    if not isinstance(raw_data, dict):
        return []
    contents: list[str] = []
    for current_id, chip in raw_data.items():
        if current_id == chip_id or not isinstance(chip, dict):
            continue
        active_versions = chip.get("select_activ_dic", {})
        if not isinstance(active_versions, dict) or active_versions.get(req_max_ver) is not True:
            continue
        content = str(chip.get("content") or "").strip() or "（空内容）"
        if content not in contents:
            contents.append(content)
    return contents


def build_overview_validation_signature(
    processing_type: Any,
    content: Any,
    projects: Any,
    role: Any,
    label: Any,
) -> tuple[str, str, tuple[str, ...], str, str]:
    """生成概述路径类数据的校验签名，防止校验通过后偷换内容或校验上下文。"""
    normalized_projects = tuple(str(project) for project in projects) if isinstance(projects, (list, tuple)) else ()
    return (
        str(processing_type or ""),
        str(content or "").strip(),
        normalized_projects,
        str(role or ""),
        str(label or ""),
    )


def classify_ecn_change_item(item: Any) -> str:
    """按当前方案分类字段归入普通资料、概述资料、物料或未知分组。"""
    if not isinstance(item, dict):
        return ECN_SCHEME_GROUP_UNKNOWN
    scheme_category = item.get("scheme_category")
    if scheme_category == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
        return ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT
    if scheme_category == ECN_SCHEME_GROUP_MATERIAL:
        return ECN_SCHEME_GROUP_MATERIAL
    if scheme_category == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT:
        return ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    return ECN_SCHEME_GROUP_UNKNOWN


def build_ecn_execution_info(change_items: Any) -> dict:
    """生成执行清单骨架；物料责任节点只允许由已发布数据库流程填充。"""
    ordinary_confirmations: dict[str, dict] = {}
    overview_results: dict[str, dict] = {}
    material_confirmations: dict[str, dict] = {}
    if not isinstance(change_items, list):
        change_items = []

    for item in change_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            continue
        scheme_group = classify_ecn_change_item(item)
        if scheme_group == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT:
            ordinary_confirmations[item_id] = {"confirmed": False, "history": []}
        elif scheme_group == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
            overview_results[item_id] = {
                "status": ECN_EXECUTION_RESULT_PENDING,
                "message": "等待研发助理确认第一阶段后执行",
                "projects": {},
            }
        elif scheme_group == ECN_SCHEME_GROUP_MATERIAL:
            material_confirmations[item_id] = {
                "traceability_tasks": {},
                "status": "open",
            }

    return {
        "stage": ECN_EXECUTION_STAGE_ASSISTANT,
        "ordinary_confirmations": ordinary_confirmations,
        "erp_confirmation": {"confirmed": False, "history": []},
        "overview_results": overview_results,
        "material_confirmations": material_confirmations,
    }


def is_ecn_assistant_execution_ready(execution_info: Any) -> bool:
    """未移交的事项及ERP确认后即可执行；移交项独立跟进。"""
    if not isinstance(execution_info, dict):
        return False
    ordinary_confirmations = execution_info.get("ordinary_confirmations", {})
    erp_confirmation = execution_info.get("erp_confirmation", {})
    return (
        isinstance(ordinary_confirmations, dict)
        and all(
            isinstance(confirmation, dict)
            and (confirmation.get("confirmed") is True or bool(confirmation.get("assignee")))
            for confirmation in ordinary_confirmations.values()
        )
        and isinstance(erp_confirmation, dict)
        and (erp_confirmation.get("confirmed") is True or bool(erp_confirmation.get("assignee")))
    )


def get_ecn_special_confirmations(execution_info: Any) -> dict[str, dict]:
    """特定事项包含普通资料及ERP行，返回真实确认对象供事务服务修改。"""
    if not isinstance(execution_info, dict):
        return {}
    result = {
        str(key): value
        for key, value in execution_info.get("ordinary_confirmations", {}).items()
        if isinstance(value, dict)
    }
    erp = execution_info.get("erp_confirmation")
    if isinstance(erp, dict):
        result["__erp__"] = erp
    return result


def is_ecn_special_execution_complete(execution_info: Any) -> bool:
    return all(item.get("confirmed") is True for item in get_ecn_special_confirmations(execution_info).values())


def get_ecn_material_execution_specs(
    item: Any,
    material_entry: Any = None,
) -> list[dict[str, object]]:
    """返回物料追溯执行项；各范围独立，范围内负责人同节点并行、节点间串行。"""
    if not isinstance(item, dict) or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
        return []
    stored_tasks = material_entry.get("traceability_tasks", {}) if isinstance(material_entry, dict) else {}
    tasks = stored_tasks if isinstance(stored_tasks, dict) else {}
    current_stage_by_level: dict[str, int] = {}
    stage_sizes: dict[tuple[str, int], int] = {}
    for task in tasks.values():
        if isinstance(task, dict):
            level = str(task.get("level") or "")
            task_stage = get_ecn_stage_index(task.get("stage_index", 0))
            stage_key = (level, task_stage)
            stage_sizes[stage_key] = stage_sizes.get(stage_key, 0) + 1
            if task.get("confirmed") is not True:
                current_stage_by_level[level] = min(
                    current_stage_by_level.get(level, task_stage),
                    task_stage,
                )

    specs: list[dict[str, object]] = []
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            continue
        level = str(task.get("level") or "")
        task_stage = get_ecn_stage_index(task.get("stage_index", 0))
        manual_assignee = str(task.get("manual_assignee") or "").strip()
        responsible_users = copy.deepcopy(task.get("users", []))
        responsible_type = str(task.get("responsible_type") or "role")
        responsible_key = str(task.get("responsible_key") or "")
        # 已创建任务也按真实回退路线解释，避免开发期数据仍卡在无权限的“项目销售”键上。
        if responsible_type == "project_sales" and not responsible_users:
            responsible_type = "sales_supervisor"
            responsible_key = "销售主管"
        label = str(task.get("label") or task_id)
        if manual_assignee:
            project = str(task.get("project") or "").strip()
            label = f"{project} · {manual_assignee}" if project else manual_assignee
        specs.append(
            {
                "kind": "traceability",
                "key": str(task_id),
                "level": level,
                "responsible_key": responsible_key,
                "responsible_type": "assigned_user"
                if manual_assignee
                else responsible_type,
                "manual_assignment": bool(manual_assignee),
                "label": label,
                "project": str(task.get("project") or ""),
                "roles": [] if manual_assignee else copy.deepcopy(task.get("roles", [])),
                "users": [manual_assignee] if manual_assignee else responsible_users,
                "stage_index": task_stage,
                "parallel": stage_sizes.get((level, task_stage), 0) > 1,
                "available": (task.get("confirmed") is not True and task_stage == current_stage_by_level.get(level)),
                "disposition_instruction": str(task.get("disposition_instruction") or ""),
                "required_permission_code": str(task.get("required_permission_code") or ""),
                "position_ids": copy.deepcopy(task.get("position_ids", [])),
                "workflow_assignment": copy.deepcopy(task.get("workflow_assignment", {})),
            }
        )
    return specs


def is_ecn_material_execution_closed(material_entry: Any) -> bool:
    """判断一条物料方案的全部责任项是否均已确认。"""
    if not isinstance(material_entry, dict):
        return False
    tasks = material_entry.get("traceability_tasks", {})
    if not isinstance(tasks, dict) or not tasks:
        return False
    return all(
        isinstance(confirmation, dict) and confirmation.get("confirmed") is True for confirmation in tasks.values()
    )


def get_ecn_traceability_closure_summary(ecn_data: Any) -> dict[str, str]:
    """按追溯范围汇总一张ECN内所有物料方案责任项的关闭状态。"""
    summary = {level: "—" for level in ECN_TRACEABILITY_LEVELS}
    if not isinstance(ecn_data, dict):
        return summary

    selected_levels: set[str] = set()
    change_items = ecn_data.get("change_items", [])
    if isinstance(change_items, list):
        for item in change_items:
            if not isinstance(item, dict) or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
                continue
            raw_levels = item.get("traceability_levels", [])
            if isinstance(raw_levels, (list, tuple)):
                selected_levels.update(str(level) for level in raw_levels if str(level) in ECN_TRACEABILITY_LEVELS)

    execution_info = ecn_data.get("execution_info", {})
    execution_info = execution_info if isinstance(execution_info, dict) else {}
    stage = str(execution_info.get("stage") or "")
    material_confirmations = execution_info.get("material_confirmations", {})
    material_confirmations = material_confirmations if isinstance(material_confirmations, dict) else {}
    counts = {level: {"confirmed": 0, "total": 0} for level in ECN_TRACEABILITY_LEVELS}
    for material_entry in material_confirmations.values():
        if not isinstance(material_entry, dict):
            continue
        tasks = material_entry.get("traceability_tasks", {})
        if not isinstance(tasks, dict):
            continue
        for task in tasks.values():
            if not isinstance(task, dict):
                continue
            level = str(task.get("level") or "")
            if level not in counts:
                continue
            selected_levels.add(level)
            counts[level]["total"] += 1
            if task.get("confirmed") is True:
                counts[level]["confirmed"] += 1

    for level in ECN_TRACEABILITY_LEVELS:
        if level not in selected_levels:
            continue
        confirmed = counts[level]["confirmed"]
        total = counts[level]["total"]
        if total and confirmed == total:
            summary[level] = "已关闭"
        elif confirmed:
            summary[level] = f"进行中 {confirmed}/{total}"
        elif total and stage == ECN_EXECUTION_STAGE_MATERIAL:
            summary[level] = "待确认"
        else:
            summary[level] = "未开始"
    return summary


def get_ecn_execution_pending_assignees(ecn_data: Any) -> dict[str, list[str]]:
    """返回当前开放执行节点的待办角色和具体用户。"""
    result: dict[str, list[str]] = {"roles": [], "users": []}
    if not isinstance(ecn_data, dict):
        return result
    workflow = ecn_data.get("workflow", {})
    execution_info = ecn_data.get("execution_info", {})
    if (
        not isinstance(workflow, dict)
        or workflow.get("current_state") != ECNState.ECN_EXECUTING
        or not isinstance(execution_info, dict)
    ):
        return result
    result["users"] = list(
        dict.fromkeys(
            str(item["assignee"])
            for item in get_ecn_special_confirmations(execution_info).values()
            if item.get("assignee") and item.get("confirmed") is not True
        )
    )
    stage = execution_info.get("stage")
    if stage in [
        ECN_EXECUTION_STAGE_ASSISTANT,
        ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
        ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    ]:
        assistant_users = execution_info.get("assistant_users", [])
        if isinstance(assistant_users, list):
            result["users"] = list(
                dict.fromkeys(
                    [*result["users"], *(str(value) for value in assistant_users if str(value).strip())]
                )
            )
        return result
    if stage != ECN_EXECUTION_STAGE_MATERIAL:
        return result

    change_items = {
        str(item.get("item_id")): item
        for item in ecn_data.get("change_items", [])
        if isinstance(item, dict) and item.get("item_id")
    }
    material_confirmations = execution_info.get("material_confirmations", {})
    if not isinstance(material_confirmations, dict):
        return result
    for item_id, material_entry in material_confirmations.items():
        if not isinstance(material_entry, dict) or material_entry.get("status") == "closed":
            continue
        item = change_items.get(str(item_id), {})
        tasks = material_entry.get("traceability_tasks", {})
        for spec in get_ecn_material_execution_specs(item, material_entry):
            if spec.get("available") is not True:
                continue
            confirmation = tasks.get(str(spec.get("key")), {}) if isinstance(tasks, dict) else {}
            if isinstance(confirmation, dict) and confirmation.get("confirmed") is True:
                continue
            for target_key, spec_key in [("roles", "roles"), ("users", "users")]:
                values = spec.get(spec_key, [])
                if not isinstance(values, (list, tuple, set)):
                    continue
                for value in values:
                    normalized_value = str(value).strip()
                    if normalized_value and normalized_value not in result[target_key]:
                        result[target_key].append(normalized_value)
    return result


def get_ecn_execution_pending_role_keywords(ecn_data: Any) -> list[str]:
    """返回当前执行阶段仍有确认任务的角色关键字。"""
    return get_ecn_execution_pending_assignees(ecn_data)["roles"]


def get_ecn_execution_pending_usernames(ecn_data: Any) -> list[str]:
    return get_ecn_execution_pending_assignees(ecn_data)["users"]


def is_ecn_execution_pending_for_user(ecn_data: Any, current_user: str, current_role: str) -> bool:
    assignees = get_ecn_execution_pending_assignees(ecn_data)
    return current_user in assignees["users"] or role_matches_keywords(current_role, assignees["roles"])


def can_view_ecn_scheme_non_image_file(
    item: Any,
    current_role: Any,
    overview_config_flat: Any = None,
    ordinary_document_roles_by_type: Any = None,
) -> bool:
    """判断当前角色能否从ECN方案表格查看或下载非图片文件。"""
    role = str(current_role or "")
    category = classify_ecn_change_item(item)
    if category == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
        configs = overview_config_flat if isinstance(overview_config_flat, dict) else {}
        config = configs.get(item.get("label"), {}) if isinstance(item, dict) else {}
        permission = config.get("permission", {}) if isinstance(config, dict) else {}
        if not isinstance(permission, dict):
            return False
        read_roles = permission.get("read_role", [])
        edit_roles = permission.get("edit_role", [])
        allowed_roles = [
            *(read_roles if isinstance(read_roles, list) else []),
            *(edit_roles if isinstance(edit_roles, list) else []),
        ]
        return role in allowed_roles

    if category == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT:
        role_map = (
            ordinary_document_roles_by_type
            if isinstance(ordinary_document_roles_by_type, dict)
            else ECN_ORDINARY_DOCUMENT_FILE_VIEW_ROLES_BY_TYPE
        )
        change_type = str(item.get("change_type") or "") if isinstance(item, dict) else ""
        allowed_keywords = role_map.get(change_type, []) if isinstance(role_map.get(change_type, []), list) else []
        return role_matches_keywords(role, allowed_keywords)

    return False


def get_ecn_impact_handlers(ecn_data: Any) -> list[str]:
    if not isinstance(ecn_data, dict):
        return []
    workflow = ecn_data.get("workflow", {})
    if not isinstance(workflow, dict):
        return []
    handlers = workflow.get("impact_handlers", [])
    if not isinstance(handlers, list):
        return []
    return list(dict.fromkeys(item.strip() for item in handlers if isinstance(item, str) and item.strip()))


def register_ecn_impact_handler(ecn_data: Any, current_user: str, review_info: Any = None) -> bool:
    """在影响区已有内容时登记具体处理人；返回本次是否新增了处理人。"""
    if not isinstance(ecn_data, dict) or not isinstance(current_user, str) or not current_user.strip():
        return False
    effective_review = ecn_data.get("review_info", {}) if review_info is None else review_info
    if is_ecn_review_info_blank(effective_review):
        return False

    workflow = ecn_data.setdefault("workflow", {})
    if not isinstance(workflow, dict):
        workflow = {}
        ecn_data["workflow"] = workflow
    handlers = workflow.get("impact_handlers")
    if not isinstance(handlers, list):
        handlers = []
        workflow["impact_handlers"] = handlers

    normalized_user = current_user.strip()
    if normalized_user in handlers:
        return False
    handlers.append(normalized_user)
    return True


def merge_ecn_impact_audit_log(review_info: Any, incoming_events: Any) -> int:
    """按 event_id 将影响区审计事件追加合并；返回实际新增条数。"""
    if not isinstance(review_info, dict) or not isinstance(incoming_events, list):
        return 0
    audit_log = review_info.setdefault("impact_change_log", [])
    if not isinstance(audit_log, list):
        audit_log = []
        review_info["impact_change_log"] = audit_log
    existing_ids = {event.get("event_id") for event in audit_log if isinstance(event, dict) and event.get("event_id")}
    added = 0
    for event in incoming_events:
        if not isinstance(event, dict):
            continue
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip() or event_id in existing_ids:
            continue
        audit_log.append(copy.deepcopy(event))
        existing_ids.add(event_id)
        added += 1
    return added


def get_ecn_scheme_coverage(ecn_data: Any) -> dict[str, set[str]]:
    """汇总 ECN 要求、资料和物料三类方案关联覆盖情况。"""
    if not isinstance(ecn_data, dict):
        ecn_data = {}
    basic_info = ecn_data.get("basic_info", {})
    if not isinstance(basic_info, dict):
        basic_info = {}
    review_info = ecn_data.get("review_info", {})
    if not isinstance(review_info, dict):
        review_info = {}

    required_requirements = set()
    requirements = basic_info.get("requirements", [])
    if isinstance(requirements, list):
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            requirement_idx = requirement.get("idx")
            if requirement_idx not in [None, ""]:
                required_requirements.add(str(requirement_idx).strip())
    required_docs = {name for name, selected in review_info.get("involved_docs", {}).items() if selected}
    required_materials = {
        f"{material}-{action}"
        for material, actions in review_info.get("involved_materials", {}).items()
        if isinstance(actions, dict)
        for action, selected in actions.items()
        if selected
    }

    provided_requirements = set()
    provided_docs = set()
    provided_materials = set()
    incomplete_material_schemes = set()
    change_items = ecn_data.get("change_items", [])
    if not isinstance(change_items, list):
        change_items = []
    for scheme_index, item in enumerate(change_items, start=1):
        if not isinstance(item, dict):
            continue
        if classify_ecn_change_item(item) == ECN_SCHEME_GROUP_MATERIAL:
            traceability_levels = item.get("traceability_levels", [])
            disposition_measure = item.get("disposition_measure")
            requires_disposition = is_ecn_material_disposition_required(item.get("change_type"))
            disposition_condition = str(item.get("disposition_condition") or "").strip()
            if (
                not (isinstance(traceability_levels, list) and traceability_levels)
                or (requires_disposition and not disposition_measure)
                or (
                    requires_disposition
                    and is_ecn_disposition_condition_required(disposition_measure)
                    and not disposition_condition
                )
            ):
                incomplete_material_schemes.add(f"方案 #{scheme_index:02d}")
        linked_requirements = item.get("req_idxs", [])
        linked_docs = item.get("linked_docs", [])
        linked_materials = item.get("linked_materials", [])
        if isinstance(linked_requirements, list):
            provided_requirements.update(
                str(requirement_idx).strip()
                for requirement_idx in linked_requirements
                if requirement_idx not in [None, ""]
            )
        if isinstance(linked_docs, list):
            provided_docs.update(linked_docs)
        if isinstance(linked_materials, list):
            provided_materials.update(linked_materials)

    return {
        "required_requirements": required_requirements,
        "required_docs": required_docs,
        "required_materials": required_materials,
        "provided_requirements": provided_requirements,
        "provided_docs": provided_docs,
        "provided_materials": provided_materials,
        "missing_requirements": required_requirements - provided_requirements,
        "missing_docs": required_docs - provided_docs,
        "missing_materials": required_materials - provided_materials,
        "incomplete_material_schemes": incomplete_material_schemes,
    }


def is_ecn_scheme_ready_for_review(ecn_data: Any) -> bool:
    """判断人员已确认且要求、资料、物料均被方案覆盖，可由总控角色发起评审。"""
    if not isinstance(ecn_data, dict):
        return False
    workflow = ecn_data.get("workflow", {})
    if not isinstance(workflow, dict) or workflow.get("current_state") != ECNState.ECN_SCHEMING:
        return False

    participants = workflow.get("scheme_participants", {})
    if not isinstance(participants, dict) or not participants:
        return False
    if not all(status == ECN_PARTICIPANT_STATUS_CONFIRMED for status in participants.values()):
        return False

    coverage = get_ecn_scheme_coverage(ecn_data)
    return not any(
        coverage[key]
        for key in [
            "missing_requirements",
            "missing_docs",
            "missing_materials",
            "incomplete_material_schemes",
        ]
    )


_ECN_SCHEME_SNAPSHOT_EXCLUDED_FIELDS = {
    "rejection_history",
    "review_status",
    "execute_status",
}


def build_ecn_scheme_snapshot(item: Any) -> dict:
    """生成可审计的方案业务快照，排除会导致嵌套或属于运行状态的字段。"""
    if not isinstance(item, dict):
        return {}
    return copy.deepcopy({key: value for key, value in item.items() if key not in _ECN_SCHEME_SNAPSHOT_EXCLUDED_FIELDS})


def reject_ecn_scheme_items(
    ecn_data: Any,
    rejected_item_ids: Any,
    reviewer: str,
    reviewer_role: str,
    note: str,
    rejected_at: str,
) -> set[str]:
    """标记被驳回方案并把对应作者切换为待重新确认，返回受影响作者集合。"""
    if not isinstance(ecn_data, dict) or not isinstance(rejected_item_ids, (list, tuple, set)):
        return set()
    normalized_ids = {str(item_id) for item_id in rejected_item_ids if item_id not in [None, ""]}
    if not normalized_ids:
        return set()

    rejected_authors = set()
    change_items = ecn_data.get("change_items", [])
    if not isinstance(change_items, list):
        return set()
    for item in change_items:
        if not isinstance(item, dict) or str(item.get("item_id", "")) not in normalized_ids:
            continue
        before_snapshot = build_ecn_scheme_snapshot(item)
        item["review_status"] = ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
        rejection_record = {
            "reviewer": reviewer,
            "reviewer_role": reviewer_role,
            "note": note,
            "time": rejected_at,
            "before_snapshot": before_snapshot,
        }
        rejection_history = item.setdefault("rejection_history", [])
        if isinstance(rejection_history, list):
            rejection_history.append(copy.deepcopy(rejection_record))
        author = item.get("author")
        if isinstance(author, str) and author.strip():
            rejected_authors.add(author.strip())

    participants = ecn_data.setdefault("workflow", {}).setdefault("scheme_participants", {})
    if isinstance(participants, dict):
        for author in rejected_authors:
            participants[author] = ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
    return rejected_authors


def mark_rejected_scheme_item_revised(item: Any) -> None:
    """作者修改被驳回方案后，保存整改快照并标记为等待作者重新确认。"""
    if not isinstance(item, dict):
        return
    if item.get("review_status") == ECN_ITEM_STATUS_NEEDS_IMPROVEMENT:
        after_snapshot = build_ecn_scheme_snapshot(item)
        rejection_history = item.get("rejection_history", [])
        if isinstance(rejection_history, list) and rejection_history:
            latest_record = rejection_history[-1]
            if isinstance(latest_record, dict):
                latest_record["after_snapshot"] = copy.deepcopy(after_snapshot)
        item["review_status"] = ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION


def confirm_revised_scheme_items(ecn_data: Any, author: str) -> None:
    """作者重新确认时，把已整改方案同步标记为完成重新确认。"""
    if not isinstance(ecn_data, dict):
        return
    for item in ecn_data.get("change_items", []):
        if (
            isinstance(item, dict)
            and item.get("author") == author
            and item.get("review_status") == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
        ):
            item["review_status"] = ECN_ITEM_STATUS_REVISED_CONFIRMED


def has_unrevised_rejected_scheme_items(ecn_data: Any, author: str) -> bool:
    if not isinstance(ecn_data, dict):
        return False
    return any(
        isinstance(item, dict)
        and item.get("author") == author
        and item.get("review_status") == ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
        for item in ecn_data.get("change_items", [])
    )


def get_ecn_pending_approval_roles(workflow: Any) -> list[str]:
    """返回当前节点尚未完成审批的角色，保留节点配置顺序。"""
    if not isinstance(workflow, dict):
        return []
    pending_roles = workflow.get("pending_roles", [])
    step_approvals = workflow.get("step_approvals", {})
    if not isinstance(pending_roles, list):
        return []
    if not isinstance(step_approvals, dict):
        step_approvals = {}
    return [
        str(role) for role in pending_roles if role not in [None, ""] and not bool(step_approvals.get(str(role), False))
    ]


def is_ecn_pending_for_user(ecn_data: Any, current_user: str, current_role: str) -> bool:
    """返回一张 ECN 是否应计入指定用户的主页/列表待办。"""
    if not isinstance(ecn_data, dict):
        return False

    workflow = ecn_data.get("workflow", {})
    basic_info = ecn_data.get("basic_info", {})
    if not isinstance(workflow, dict) or not isinstance(basic_info, dict):
        return False

    current_state = workflow.get("current_state")
    if current_role in get_ecn_pending_approval_roles(workflow):
        return True

    if current_state == ECNState.ECN_EXECUTING:
        return is_ecn_execution_pending_for_user(ecn_data, current_user, current_role)

    if current_state in [ECNState.REJECTED, ECNState.DRAFT] and basic_info.get("applicant") == current_user:
        return True

    if is_ecn_scheme_ready_for_review(ecn_data) and role_matches_keywords(current_role, ECN_SCHEME_INITIATOR_ROLES):
        return True

    if current_state not in ECN_IMPACT_FOLLOWUP_STATES:
        return False

    participants = workflow.get("scheme_participants", {})
    if isinstance(participants, dict) and current_user in participants:
        participant_status = participants.get(current_user)
        status_info = ECN_PARTICIPANT_STATUS_CONFIG.get(participant_status, {})
        return status_info.get("remind") is True
    if isinstance(participants, dict) and participants:
        return False

    if is_ecn_impact_blank(ecn_data):
        return role_matches_keywords(current_role, ECN_IMPACT_INITIAL_REMINDER_ROLES)

    handlers = get_ecn_impact_handlers(ecn_data)
    # 已经写过影响、但尚未提供方案的人仍需提醒；确认完成的参与人不会再提醒。
    return current_user in handlers and current_user not in participants


def get_ecn_dashboard_pending_count(all_ecns: Any, current_user: str, current_role: str) -> int:
    if not isinstance(all_ecns, dict):
        return 0
    return sum(1 for ecn_data in all_ecns.values() if is_ecn_pending_for_user(ecn_data, current_user, current_role))
