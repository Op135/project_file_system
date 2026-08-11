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
            "医疗器械产品风险管理",
            "SOP/作业指导书",
            "出厂检测报告",
            "工装治具清单",
            "其它",
        ],
        "reasons": ["需求更改", "设计改善", "工艺调整", "物料替换", "资料修正", "产品定标", "其他"],
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
        result["permissions"][key] = _string_list(
            raw_permissions.get(key), default, f"permissions.{key}"
        )

    raw_reminders = raw.get("reminders", {})
    if not isinstance(raw_reminders, dict):
        raw_reminders = {}
    result["reminders"]["impact_followup_states"] = _string_list(
        raw_reminders.get("impact_followup_states"),
        _DEFAULT_CONFIG["reminders"]["impact_followup_states"],
        "reminders.impact_followup_states",
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
ECN_OVERVIEW_CONFLICT_AUTO_CLOSE_SECONDS = ECN_CONFIG["ui"]["overview_conflict_auto_close_seconds"]
ECN_WORKFLOW_ROUTES = ECN_CONFIG["workflow_routes"]


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
    normalized_projects = (
        tuple(str(project) for project in projects)
        if isinstance(projects, (list, tuple))
        else ()
    )
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


def is_ecn_scheme_ready_for_review(ecn_data: Any) -> bool:
    """判断方案是否人员已确认且完整覆盖影响区，可由总控角色发起评审。"""
    if not isinstance(ecn_data, dict):
        return False
    workflow = ecn_data.get("workflow", {})
    if not isinstance(workflow, dict) or workflow.get("current_state") != ECNState.ECN_SCHEMING:
        return False

    participants = workflow.get("scheme_participants", {})
    if not isinstance(participants, dict) or not participants:
        return False
    if not all(status == "confirmed" for status in participants.values()):
        return False

    review_info = ecn_data.get("review_info", {})
    if not isinstance(review_info, dict):
        review_info = {}
    required_docs = {
        name for name, selected in review_info.get("involved_docs", {}).items() if selected
    }
    required_materials = {
        f"{material}-{action}"
        for material, actions in review_info.get("involved_materials", {}).items()
        if isinstance(actions, dict)
        for action, selected in actions.items()
        if selected
    }

    provided_docs = set()
    provided_materials = set()
    change_items = ecn_data.get("change_items", [])
    if not isinstance(change_items, list):
        change_items = []
    for item in change_items:
        if not isinstance(item, dict):
            continue
        linked_docs = item.get("linked_docs", [])
        linked_materials = item.get("linked_materials", [])
        if isinstance(linked_docs, list):
            provided_docs.update(linked_docs)
        if isinstance(linked_materials, list):
            provided_materials.update(linked_materials)

    return required_docs.issubset(provided_docs) and required_materials.issubset(provided_materials)


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

    if (
        is_ecn_scheme_ready_for_review(ecn_data)
        and role_matches_keywords(current_role, ECN_SCHEME_INITIATOR_ROLES)
    ):
        return True

    if current_state not in ECN_IMPACT_FOLLOWUP_STATES:
        return False

    handlers = get_ecn_impact_handlers(ecn_data)
    if handlers:
        return current_user in handlers

    if is_ecn_impact_blank(ecn_data):
        return role_matches_keywords(current_role, ECN_IMPACT_INITIAL_REMINDER_ROLES)

    # 兼容升级前已有的方案编写单：没有 impact_handlers 时，优先使用已有方案参与人。
    participants = workflow.get("scheme_participants", {})
    if isinstance(participants, dict) and participants:
        return current_user in participants

    # 非空但历史数据无法追溯操作者时交给研发助理，避免待办无人负责。
    return role_matches_keywords(current_role, ECN_IMPACT_INITIAL_REMINDER_ROLES)


def get_ecn_dashboard_pending_count(all_ecns: Any, current_user: str, current_role: str) -> int:
    if not isinstance(all_ecns, dict):
        return 0
    return sum(
        1 for ecn_data in all_ecns.values() if is_ecn_pending_for_user(ecn_data, current_user, current_role)
    )
