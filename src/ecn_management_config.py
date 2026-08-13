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


def get_ecn_material_change_display(item: Any) -> tuple[str, str]:
    """返回物料方案用于表格/快照展示的“变更前、变更后”文本，并兼容旧文本方案。"""
    if not isinstance(item, dict):
        return "", ""
    change_type = item.get("change_type")
    material_change = item.get("material_change", {})
    if change_type not in ECN_MATERIAL_CHANGE_TYPES or not isinstance(material_change, dict):
        return str(item.get("old_content") or ""), str(item.get("new_content") or "")

    def material_text(name_key: str, quantity_key: str, unit_key: str) -> str:
        name = str(material_change.get(name_key) or "").strip()
        quantity = material_change.get(quantity_key)
        unit = str(material_change.get(unit_key) or ECN_MATERIAL_DEFAULT_UNIT).strip()
        quantity_text = (
            ""
            if quantity in [None, ""]
            else f"{quantity:g}"
            if isinstance(quantity, (int, float))
            else str(quantity)
        )
        return f"{name}\n用量：{quantity_text} {unit}".strip()

    if change_type == ECN_MATERIAL_CHANGE_TYPE_ADD:
        return "无", material_text("material_name", "quantity", "unit")
    if change_type == ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE:
        return material_text("material_name", "quantity", "unit"), "弃用"
    if change_type == ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY:
        name = str(material_change.get("material_name") or "").strip()
        unit = str(material_change.get("unit") or ECN_MATERIAL_DEFAULT_UNIT).strip()
        old_quantity = material_change.get("old_quantity")
        new_quantity = material_change.get("new_quantity")
        old_quantity_text = f"{old_quantity:g}" if isinstance(old_quantity, (int, float)) else str(old_quantity)
        new_quantity_text = f"{new_quantity:g}" if isinstance(new_quantity, (int, float)) else str(new_quantity)
        return (f"{name}\n用量：{old_quantity_text} {unit}", f"{name}\n用量：{new_quantity_text} {unit}")
    return (
        material_text("old_material_name", "old_quantity", "old_unit"),
        material_text("new_material_name", "new_quantity", "new_unit"),
    )


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
    return [
        label
        for key, label in required_fields
        if material_change.get(key) is None or str(material_change.get(key)).strip() == ""
    ]


class ECNState:
    DRAFT = "草稿"
    ECR_REVIEWING = "ECR 审批中"
    ECN_SCHEMING = "ECN 方案编写与确认中"
    ECN_REVIEWING = "ECN 方案评审中"
    ECN_EXECUTING = "ECN 等待各部执行确认"
    PENDING_FINAL_EXECUTE = "等待最终数据变更"
    CLOSED = "变更已完成"
    CANCEL = "变更已作废"
    REJECTED = "已被驳回"


_DEFAULT_CONFIG = {
    "allowed_project_states": ["试产", "量产"],
    "permissions": {
        "scheme_initiator_roles": ["研发经理", "admin"],
        "scheme_writer_roles": ["研发", "工程", "质量"],
        "impact_initial_reminder_roles": ["研发助理"],
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
            "无影响",
            "追溯至文件",
            "追溯至供应商存量",
            "追溯至零件/返修/在线",
            "追溯至半成品/返修/在线",
            "追溯至成品/返修/在线",
            "追溯至在途/客户",
        ],
        "disposition_measures": ["报废", "返工"],
    },
    "scheme_options": {
        "document_change_types": ["图纸更新", "SOP修改", "测试报告内容格式", "其它"],
        "material_change_types": {
            "add": "新增",
            "adjust_quantity": "调量",
            "discontinue": "弃用",
            "replace": "更换",
        },
        "material_default_unit": "pcs",
        "material_disposition_required_types": ["discontinue", "replace"],
        "disposition_condition_required_measures": ["有条件用完止"],
    },
    "ui": {
        "overview_conflict_auto_close_seconds": 5.0,
    },
    "workflow_routes": {
        "ECR_PHASE": {
            "SALES_INITIATED": [["销售总监"], ["研发经理"]],
            "RD_INITIATED": [["研发经理"], ["销售总监"]],
        },
        "ECN_SCHEME_REVIEW_PHASE": [["研发经理"], ["销售总监"], ["工程", "质量", "PMC"]],
        "ECN_EXECUTION_PHASE": [["工程", "生产", "PMC", "质量"], ["研发经理_EXECUTE"]],
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
        "material_actions": ["新增", "调量", "弃用", "返工使用", "弃用更换"],
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
        "execution_handling_measures": ["报废", "返工"],
        "trial_conclusions": [
            "无需试产,变更完成",
            "试产通过,变更完成",
            "试产条件通过,变更内容再完善",
            "试产不通过,重新试产",
        ],
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


def _approval_steps(value: Any, default: list[list[str]], field_name: str) -> list[list[str]]:
    if (
        isinstance(value, list)
        and value
        and all(isinstance(step, list) and step for step in value)
        and all(isinstance(role, str) and role.strip() for step in value for role in step)
    ):
        return [list(dict.fromkeys(role.strip() for role in step)) for step in value]
    logger.warning("ECN配置 %s 无效，已使用默认值", field_name)
    return copy.deepcopy(default)


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
    result["allowed_project_states"] = _string_list(
        raw.get("allowed_project_states"),
        _DEFAULT_CONFIG["allowed_project_states"],
        "allowed_project_states",
    )

    raw_permissions = raw.get("permissions", {})
    if not isinstance(raw_permissions, dict):
        raw_permissions = {}
    for key, default in _DEFAULT_CONFIG["permissions"].items():
        result["permissions"][key] = _string_list(raw_permissions.get(key), default, f"permissions.{key}")

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

    raw_scheme_tracking = raw.get("scheme_tracking", raw.get("material_scheme", {}))
    if not isinstance(raw_scheme_tracking, dict):
        raw_scheme_tracking = {}
    for key, default in _DEFAULT_CONFIG["scheme_tracking"].items():
        result["scheme_tracking"][key] = _string_list(
            raw_scheme_tracking.get(key), default, f"scheme_tracking.{key}"
        )

    raw_scheme_options = raw.get("scheme_options", {})
    if not isinstance(raw_scheme_options, dict):
        raw_scheme_options = {}
    for key in [
        "document_change_types",
        "material_disposition_required_types",
        "disposition_condition_required_measures",
    ]:
        default = _DEFAULT_CONFIG["scheme_options"][key]
        result["scheme_options"][key] = _string_list(
            raw_scheme_options.get(key), default, f"scheme_options.{key}"
        )
    default_material_change_types = _DEFAULT_CONFIG["scheme_options"]["material_change_types"]
    raw_material_change_types = raw_scheme_options.get("material_change_types", {})
    if not isinstance(raw_material_change_types, dict):
        raw_material_change_types = {}
    result["scheme_options"]["material_change_types"] = {}
    for semantic_key, default_label in default_material_change_types.items():
        label = raw_material_change_types.get(semantic_key)
        result["scheme_options"]["material_change_types"][semantic_key] = (
            label.strip() if isinstance(label, str) and label.strip() else default_label
        )
    default_unit = _DEFAULT_CONFIG["scheme_options"]["material_default_unit"]
    raw_default_unit = raw_scheme_options.get("material_default_unit")
    result["scheme_options"]["material_default_unit"] = (
        raw_default_unit.strip()
        if isinstance(raw_default_unit, str) and raw_default_unit.strip()
        else default_unit
    )

    raw_ui = raw.get("ui", {})
    if not isinstance(raw_ui, dict):
        raw_ui = {}
    result["ui"]["overview_conflict_auto_close_seconds"] = _positive_number(
        raw_ui.get("overview_conflict_auto_close_seconds"),
        _DEFAULT_CONFIG["ui"]["overview_conflict_auto_close_seconds"],
        "ui.overview_conflict_auto_close_seconds",
    )

    raw_routes = raw.get("workflow_routes", {})
    if not isinstance(raw_routes, dict):
        raw_routes = {}
    raw_ecr_routes = raw_routes.get("ECR_PHASE", {})
    if not isinstance(raw_ecr_routes, dict):
        raw_ecr_routes = {}
    for route_type, default in _DEFAULT_CONFIG["workflow_routes"]["ECR_PHASE"].items():
        result["workflow_routes"]["ECR_PHASE"][route_type] = _approval_steps(
            raw_ecr_routes.get(route_type), default, f"workflow_routes.ECR_PHASE.{route_type}"
        )
    for phase in ["ECN_SCHEME_REVIEW_PHASE", "ECN_EXECUTION_PHASE"]:
        result["workflow_routes"][phase] = _approval_steps(
            raw_routes.get(phase), _DEFAULT_CONFIG["workflow_routes"][phase], f"workflow_routes.{phase}"
        )

    raw_schema = raw.get("schema", {})
    if not isinstance(raw_schema, dict):
        raw_schema = {}
    for key, default in _DEFAULT_CONFIG["schema"].items():
        result["schema"][key] = _string_list(raw_schema.get(key), default, f"schema.{key}")

    return result


ECN_CONFIG = load_ecn_config()
ECN_SCHEMA_CONFIG = ECN_CONFIG["schema"]
ECN_ALLOWED_PROJECT_STATES = ECN_CONFIG["allowed_project_states"]
ECN_SCHEME_INITIATOR_ROLES = ECN_CONFIG["permissions"]["scheme_initiator_roles"]
ECN_SCHEME_WRITER_ROLES = ECN_CONFIG["permissions"]["scheme_writer_roles"]
ECN_IMPACT_INITIAL_REMINDER_ROLES = ECN_CONFIG["permissions"]["impact_initial_reminder_roles"]
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
ECN_TRACEABILITY_LEVELS = ECN_CONFIG["scheme_tracking"]["traceability_levels"]
ECN_DISPOSITION_MEASURES = ECN_CONFIG["scheme_tracking"]["disposition_measures"]
ECN_DOCUMENT_CHANGE_TYPES = ECN_CONFIG["scheme_options"]["document_change_types"]
ECN_MATERIAL_CHANGE_TYPE_LABELS = ECN_CONFIG["scheme_options"]["material_change_types"]
ECN_MATERIAL_CHANGE_TYPES = list(ECN_MATERIAL_CHANGE_TYPE_LABELS.values())
ECN_MATERIAL_DEFAULT_UNIT = ECN_CONFIG["scheme_options"]["material_default_unit"]
ECN_MATERIAL_DISPOSITION_REQUIRED_TYPES = set(
    ECN_CONFIG["scheme_options"]["material_disposition_required_types"]
)
ECN_DISPOSITION_CONDITION_REQUIRED_MEASURES = set(
    ECN_CONFIG["scheme_options"]["disposition_condition_required_measures"]
)
ECN_MATERIAL_CHANGE_TYPE_ADD = ECN_MATERIAL_CHANGE_TYPE_LABELS["add"]
ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY = ECN_MATERIAL_CHANGE_TYPE_LABELS["adjust_quantity"]
ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE = ECN_MATERIAL_CHANGE_TYPE_LABELS["discontinue"]
ECN_MATERIAL_CHANGE_TYPE_REPLACE = ECN_MATERIAL_CHANGE_TYPE_LABELS["replace"]
ECN_OVERVIEW_CONFLICT_AUTO_CLOSE_SECONDS = ECN_CONFIG["ui"]["overview_conflict_auto_close_seconds"]
ECN_WORKFLOW_ROUTES = ECN_CONFIG["workflow_routes"]


def is_ecn_material_disposition_required(change_type: Any) -> bool:
    if change_type not in ECN_MATERIAL_CHANGE_TYPES:
        return True
    semantic_key = next(
        (
            key
            for key, label in ECN_MATERIAL_CHANGE_TYPE_LABELS.items()
            if label == change_type
        ),
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
    selected = {
        str(level)
        for level in selected_levels
        if level not in [None, ""]
    } if isinstance(selected_levels, (list, tuple, set)) else set()
    previous = {
        str(level)
        for level in previous_levels
        if level not in [None, ""]
    } if isinstance(previous_levels, (list, tuple, set)) else set()

    newly_selected = selected - previous
    newly_selected_indexes = [
        index
        for index, level in enumerate(ECN_TRACEABILITY_LEVELS)
        if level in newly_selected
    ]
    if newly_selected_indexes:
        selected.update(
            ECN_TRACEABILITY_LEVELS[: max(newly_selected_indexes) + 1]
        )
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
    """把新旧方案条目统一归入普通资料、概述资料、物料或未知分组。"""
    if not isinstance(item, dict):
        return ECN_SCHEME_GROUP_UNKNOWN
    if item.get("type") == "overview_update":
        return ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT

    scheme_category = item.get("scheme_category")
    if scheme_category == ECN_SCHEME_GROUP_MATERIAL:
        return ECN_SCHEME_GROUP_MATERIAL
    if scheme_category in {"document", ECN_SCHEME_GROUP_ORDINARY_DOCUMENT}:
        return ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    if item.get("type") == "text_desc" and not scheme_category:
        return ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    return ECN_SCHEME_GROUP_UNKNOWN


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
        for fallback_idx, requirement in enumerate(requirements, start=1):
            if not isinstance(requirement, dict):
                continue
            requirement_idx = requirement.get("idx", fallback_idx)
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
            if not traceability_levels and item.get("traceability_level"):
                traceability_levels = [item.get("traceability_level")]
            disposition_measure = item.get("disposition_measure")
            if not disposition_measure:
                legacy_measures = item.get("disposition_measures", [])
                if isinstance(legacy_measures, list) and legacy_measures:
                    disposition_measure = legacy_measures[0]
            requires_disposition = is_ecn_material_disposition_required(item.get("change_type"))
            disposition_condition = str(item.get("disposition_condition") or "").strip()
            if not (isinstance(traceability_levels, list) and traceability_levels) or (
                requires_disposition and not disposition_measure
            ) or (
                requires_disposition
                and is_ecn_disposition_condition_required(disposition_measure)
                and not disposition_condition
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
    "rejection_info",
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
        rejection_info = {
            "reviewer": reviewer,
            "reviewer_role": reviewer_role,
            "note": note,
            "time": rejected_at,
            "before_snapshot": before_snapshot,
        }
        item["rejection_info"] = copy.deepcopy(rejection_info)
        rejection_history = item.setdefault("rejection_history", [])
        if isinstance(rejection_history, list):
            rejection_history.append(copy.deepcopy(rejection_info))
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
        rejection_info = item.get("rejection_info", {})
        if isinstance(rejection_info, dict):
            rejection_info["after_snapshot"] = copy.deepcopy(after_snapshot)
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


def is_ecn_pending_for_user(ecn_data: Any, current_user: str, current_role: str) -> bool:
    """返回一张 ECN 是否应计入指定用户的主页/列表待办。"""
    if not isinstance(ecn_data, dict):
        return False

    workflow = ecn_data.get("workflow", {})
    basic_info = ecn_data.get("basic_info", {})
    if not isinstance(workflow, dict) or not isinstance(basic_info, dict):
        return False

    current_state = workflow.get("current_state")
    pending_roles = workflow.get("pending_roles", [])
    if isinstance(pending_roles, list) and current_role in pending_roles:
        return True

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
        # 已存在明确参与人时，不再把历史单兜底分派给研发助理；已确认参与人也不提醒。
        return False

    if is_ecn_impact_blank(ecn_data):
        return role_matches_keywords(current_role, ECN_IMPACT_INITIAL_REMINDER_ROLES)

    handlers = get_ecn_impact_handlers(ecn_data)
    if handlers:
        # 已经写过影响、但尚未提供方案的人仍需提醒；确认完成的参与人不会再提醒。
        return current_user in handlers and current_user not in participants

    # 非空但历史数据无法追溯操作者时交给研发助理，避免待办无人负责。
    return role_matches_keywords(current_role, ECN_IMPACT_INITIAL_REMINDER_ROLES)


def get_ecn_dashboard_pending_count(all_ecns: Any, current_user: str, current_role: str) -> int:
    if not isinstance(all_ecns, dict):
        return 0
    return sum(1 for ecn_data in all_ecns.values() if is_ecn_pending_for_user(ecn_data, current_user, current_role))
