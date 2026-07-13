# -*- encoding: utf-8 -*-
"""设计知识库模块的业务配置加载器。

维护人员通常只需要修改项目根目录的 ``design_knowledge_config.json``。
本模块在导入阶段读取一次 JSON，并把经过校验的值导出为常量供页面使用，修改 JSON 后需要重启服务。
"""

import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DESIGN_KNOWLEDGE_CONFIG_PATH = Path(__file__).parent.parent / "design_knowledge_config.json"

_SUPPORTED_CONTENT_TYPES = ["设计规范", "错误案例", "优秀案例"]
_CONTENT_COPY_KEYS = [
    "title_hint",
    "summary_label",
    "summary_placeholder",
    "scene_label",
    "scene_placeholder",
    "analysis_label",
    "analysis_placeholder",
    "suggestion_label",
    "suggestion_placeholder",
]

# 这些默认值只用于配置文件缺失或字段无效时保护系统启动。
# 正常业务维护应修改根目录 JSON，而不是直接修改这里。
_DEFAULT_CONFIG = {
    "content_types": _SUPPORTED_CONTENT_TYPES,
    "design_domains": ["光学", "结构", "散热", "硬件", "软件", "工艺"],
    "rule_levels": ["规定", "推荐", "提示"],
    "error_severity_levels": ["致命", "严重", "中等", "轻度"],
    "practice_value_levels": ["强推荐", "可参考", "特定场景适用"],
    "project_categories": [
        "通用",
        "整机",
        "光学模组",
        "结构件",
        "散热系统",
        "电子硬件",
        "软件",
        "工艺制程",
        "包装/辅料",
    ],
    "applicable_phases": [
        "需求评审",
        "方案设计",
        "详细设计",
        "设计评审",
        "样机打样",
        "设计验证",
        "试产导入",
        "量产维护",
    ],
    "knowledge_editor_role_keywords": ["研发", "工程", "质量", "boss", "admin"],
    "tag_manager_role_keywords": ["经理", "主管", "总监", "boss", "admin"],
    "review_routing_rules": [
        {
            "key": "rd_electronics",
            "label": "研发电子组审核",
            "submitter_role_keywords": ["研发硬件", "研发软件"],
            "approver_roles": ["研发电子主管"],
        },
        {
            "key": "rd_structure",
            "label": "研发结构组审核",
            "submitter_role_keywords": ["研发结构"],
            "approver_roles": ["研发结构组长"],
        },
        {
            "key": "rd_other",
            "label": "研发其它审核",
            "submitter_role_keywords": ["研发"],
            "approver_roles": ["研发经理"],
        },
    ],
    "review_fallback_approver_roles": ["经理", "主管", "总监", "boss", "admin"],
    "attachment": {
        "dir_name": "design_knowledge",
        "parents_h": 12,
    },
    "default_tag_catalog": {
        "光学": ["杂散光", "MTF", "照度均匀性", "镜头装配", "公差敏感", "光斑", "镀膜"],
        "结构": ["防水", "跌落", "卡扣", "螺丝柱", "装配干涉", "公差链", "防呆"],
        "散热": ["热阻", "导热垫", "风道", "散热片", "温升", "热仿真", "接触热阻"],
        "硬件": ["EMC", "ESD", "电源纹波", "接口防护", "接地", "降额", "线束"],
        "软件": ["通信协议", "异常恢复", "升级", "日志", "参数校验", "版本兼容", "看门狗"],
        "工艺": ["点胶", "焊接", "灌封", "治具", "可制造性", "扭矩", "来料检验"],
    },
    "content_type_copy": {
        "设计规范": {
            "title_hint": "建议格式如：结构防水 - 壳体密封筋最小压缩量要求",
            "summary_label": "规范摘要",
            "summary_placeholder": "一句话说明这条规范要求什么，便于列表快速浏览。",
            "scene_label": "适用范围/约束条件",
            "scene_placeholder": "说明适用于哪些产品、部件、环境、参数范围或设计前提。",
            "analysis_label": "设计依据/风险说明",
            "analysis_placeholder": "说明为什么要这样规定，可填写验证依据、风险后果或历史来源。",
            "suggestion_label": "规定内容/推荐做法",
            "suggestion_placeholder": "写清楚必须遵守或推荐采用的具体做法、数值范围、检查点。",
        },
        "错误案例": {
            "title_hint": "建议格式如：RFFM-1519 - 镜头偏心导致光斑不均",
            "summary_label": "问题摘要",
            "summary_placeholder": "一句话概括错误案例：发生了什么问题，造成了什么影响。",
            "scene_label": "问题现象/发生场景",
            "scene_placeholder": "说明在哪个项目、阶段、条件下发现，现场表现是什么。",
            "analysis_label": "原因分析",
            "analysis_placeholder": "说明直接原因、根因、遗漏的检查项或错误决策点。",
            "suggestion_label": "整改措施/预防建议",
            "suggestion_placeholder": "说明已采取的整改措施，以及以后如何避免重复发生。",
        },
        "优秀案例": {
            "title_hint": "建议格式如：RFFM-1519 - 导热路径复用降低温升",
            "summary_label": "案例摘要",
            "summary_placeholder": "一句话说明这个案例好在哪里，值得后续借鉴什么。",
            "scene_label": "适用场景",
            "scene_placeholder": "说明适合在哪类产品、约束条件或设计目标下复用。",
            "analysis_label": "设计亮点/方案依据",
            "analysis_placeholder": "说明关键设计思路、验证结果、性能收益或取舍理由。",
            "suggestion_label": "可借鉴点/复用建议",
            "suggestion_placeholder": "说明后续项目可以怎样复用，复用时需要注意什么边界条件。",
        },
    },
}


def _read_config_file() -> dict:
    """读取根目录 JSON；无法读取时返回空字典，让后续每个字段分别使用默认值。"""
    try:
        with DESIGN_KNOWLEDGE_CONFIG_PATH.open("r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
        if not isinstance(loaded, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        return loaded
    except FileNotFoundError:
        logger.warning("设计知识库配置文件不存在：%s，已使用代码默认值", DESIGN_KNOWLEDGE_CONFIG_PATH)
    except (OSError, json.JSONDecodeError, ValueError):
        logger.exception("设计知识库配置文件读取失败，已使用代码默认值")
    return {}


def _string_list(config: dict, key: str, default: list[str], *, allow_empty: bool = False) -> list[str]:
    """读取字符串列表，同时去除重复项并保留原有顺序。"""
    value = config.get(key)
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        normalized = list(dict.fromkeys(item.strip() for item in value))
        if normalized or allow_empty:
            return normalized
    logger.warning("设计知识库配置 %s 无效，已使用默认值", key)
    return copy.deepcopy(default)


def _content_types(config: dict) -> list[str]:
    """内容类型影响页面逻辑；允许调整顺序，但不接收未支持的新类型。"""
    configured = _string_list(config, "content_types", _DEFAULT_CONFIG["content_types"])
    supported = [item for item in configured if item in _SUPPORTED_CONTENT_TYPES]
    normalized = [*supported, *(item for item in _SUPPORTED_CONTENT_TYPES if item not in supported)]
    ignored = [item for item in configured if item not in _SUPPORTED_CONTENT_TYPES]
    if ignored:
        logger.warning("设计知识库暂不支持内容类型 %s，已忽略", ignored)
    return normalized


def _positive_int(config: dict, key: str, default: int) -> int:
    """读取正整数配置；布尔值虽然属于 int 子类，但不能当作数值使用。"""
    value = config.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    logger.warning("设计知识库配置 %s 无效，已使用默认值", key)
    return default


def _string_value(config: dict, key: str, default: str) -> str:
    value = config.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    logger.warning("设计知识库配置 %s 无效，已使用默认值", key)
    return default


def _attachment_config(config: dict) -> dict[str, Any]:
    default_attachment = _DEFAULT_CONFIG["attachment"]
    raw_attachment = config.get("attachment", {})
    if not isinstance(raw_attachment, dict):
        logger.warning("设计知识库配置 attachment 无效，已使用默认值")
        raw_attachment = {}
    return {
        "dir_name": _string_value(raw_attachment, "dir_name", default_attachment["dir_name"]),
        "parents_h": _positive_int(raw_attachment, "parents_h", default_attachment["parents_h"]),
    }


def _tag_catalog(config: dict, domains: list[str]) -> dict[str, list[str]]:
    raw_catalog = config.get("default_tag_catalog", {})
    if not isinstance(raw_catalog, dict):
        logger.warning("设计知识库配置 default_tag_catalog 无效，已使用默认值")
        raw_catalog = {}

    default_catalog = _DEFAULT_CONFIG["default_tag_catalog"]
    normalized: dict[str, list[str]] = {}
    for domain in domains:
        default_tags = default_catalog.get(domain, [])
        normalized[domain] = _string_list(raw_catalog, domain, default_tags, allow_empty=True)
    return normalized


def _review_routing_rules(config: dict) -> list[dict[str, Any]]:
    """读取按提交者角色关键字匹配的审核路由；规则按配置顺序优先匹配。"""
    value = config.get("review_routing_rules", _DEFAULT_CONFIG["review_routing_rules"])
    if not isinstance(value, list):
        logger.warning("设计知识库配置 review_routing_rules 必须是列表，已使用默认值")
        value = _DEFAULT_CONFIG["review_routing_rules"]

    normalized_rules: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, rule in enumerate(value):
        if not isinstance(rule, dict) or rule.get("enabled", True) is False:
            continue
        role_keywords = _string_list(
            rule,
            "submitter_role_keywords",
            [],
            allow_empty=True,
        )
        approver_roles = _string_list(rule, "approver_roles", [], allow_empty=True)
        key = str(rule.get("key") or f"review_route_{index + 1}").strip()
        if not key or key in seen_keys or not role_keywords or not approver_roles:
            logger.warning("设计知识库审核路由第 %s 项无效或重复，已忽略", index + 1)
            continue
        normalized_rules.append(
            {
                "key": key,
                "label": str(rule.get("label") or key).strip(),
                "submitter_role_keywords": role_keywords,
                "approver_roles": approver_roles,
            }
        )
        seen_keys.add(key)
    return normalized_rules


def _content_type_copy(config: dict, content_types: list[str]) -> dict[str, dict[str, str]]:
    raw_copy = config.get("content_type_copy", {})
    if not isinstance(raw_copy, dict):
        logger.warning("设计知识库配置 content_type_copy 无效，已使用默认值")
        raw_copy = {}

    default_copy = _DEFAULT_CONFIG["content_type_copy"]
    normalized: dict[str, dict[str, str]] = {}
    for content_type in content_types:
        default_item = copy.deepcopy(default_copy.get(content_type, default_copy[_SUPPORTED_CONTENT_TYPES[0]]))
        raw_item = raw_copy.get(content_type, {})
        if not isinstance(raw_item, dict):
            logger.warning("设计知识库配置 content_type_copy.%s 无效，已使用默认值", content_type)
            raw_item = {}

        for key in _CONTENT_COPY_KEYS:
            value = raw_item.get(key)
            if isinstance(value, str) and value.strip():
                default_item[key] = value.strip()
        normalized[content_type] = default_item
    return normalized


def load_design_knowledge_config() -> dict[str, Any]:
    """组合出设计知识库页面实际使用的完整配置。"""
    raw_config = _read_config_file()
    content_types = _content_types(raw_config)
    domains = _string_list(raw_config, "design_domains", _DEFAULT_CONFIG["design_domains"])
    attachment = _attachment_config(raw_config)

    return {
        "content_types": content_types,
        "design_domains": domains,
        "rule_levels": _string_list(raw_config, "rule_levels", _DEFAULT_CONFIG["rule_levels"]),
        "error_severity_levels": _string_list(
            raw_config,
            "error_severity_levels",
            _DEFAULT_CONFIG["error_severity_levels"],
        ),
        "practice_value_levels": _string_list(
            raw_config,
            "practice_value_levels",
            _DEFAULT_CONFIG["practice_value_levels"],
        ),
        "project_categories": _string_list(raw_config, "project_categories", _DEFAULT_CONFIG["project_categories"]),
        "applicable_phases": _string_list(raw_config, "applicable_phases", _DEFAULT_CONFIG["applicable_phases"]),
        "knowledge_editor_role_keywords": _string_list(
            raw_config,
            "knowledge_editor_role_keywords",
            _DEFAULT_CONFIG["knowledge_editor_role_keywords"],
        ),
        "tag_manager_role_keywords": _string_list(
            raw_config,
            "tag_manager_role_keywords",
            _DEFAULT_CONFIG["tag_manager_role_keywords"],
        ),
        "review_routing_rules": _review_routing_rules(raw_config),
        "review_fallback_approver_roles": _string_list(
            raw_config,
            "review_fallback_approver_roles",
            _DEFAULT_CONFIG["review_fallback_approver_roles"],
        ),
        "attachment": attachment,
        "default_tag_catalog": _tag_catalog(raw_config, domains),
        "content_type_copy": _content_type_copy(raw_config, content_types),
    }


def resolve_design_knowledge_review_route(submitter_role: str) -> dict[str, Any]:
    """按配置顺序解析审核路由；未命中时返回兜底角色。"""
    role_text = str(submitter_role or "")
    for rule in DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES:
        if any(keyword in role_text for keyword in rule.get("submitter_role_keywords", [])):
            return copy.deepcopy(rule)
    return {
        "key": "fallback",
        "label": "默认审核",
        "submitter_role_keywords": [],
        "approver_roles": copy.deepcopy(DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES),
    }


def resolve_design_knowledge_submission_review_route(submission: Any) -> dict[str, Any]:
    """优先读取提交时固化的路由；旧记录按创建者角色重新匹配。"""
    if not isinstance(submission, dict):
        return resolve_design_knowledge_review_route("")

    route = resolve_design_knowledge_review_route(submission.get("created_role", ""))
    stored_roles = submission.get("approver_roles")
    if isinstance(stored_roles, list):
        normalized_roles = list(
            dict.fromkeys(str(role).strip() for role in stored_roles if isinstance(role, str) and role.strip())
        )
        if normalized_roles:
            route["approver_roles"] = normalized_roles
            stored_key = str(submission.get("review_route_key") or "").strip()
            stored_label = str(submission.get("review_route_label") or "").strip()
            route["key"] = stored_key or route["key"]
            route["label"] = stored_label or route["label"]
    return route


def is_design_knowledge_review_approver_role(current_role: str) -> bool:
    """判断角色是否出现在任一审核路由中，用于显示审核入口。"""
    approver_roles = list(DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES)
    for rule in DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES:
        approver_roles.extend(rule.get("approver_roles", []))
    return any(role_key in str(current_role or "") for role_key in dict.fromkeys(approver_roles))


def can_review_design_knowledge_submission(submission: Any, current_user: str, current_role: str) -> bool:
    """判断用户能否审核指定知识或标签申请；admin 可兜底，普通用户不可自审。"""
    if str(current_user or "").strip().lower() == "admin" or str(current_role or "").strip().lower() == "admin":
        return True
    if not isinstance(submission, dict) or submission.get("created_by") == current_user:
        return False
    route = resolve_design_knowledge_submission_review_route(submission)
    return any(role_key in str(current_role or "") for role_key in route.get("approver_roles", []))


DESIGN_KNOWLEDGE_CONFIG = load_design_knowledge_config()
CONTENT_TYPES = DESIGN_KNOWLEDGE_CONFIG["content_types"]
DESIGN_DOMAINS = DESIGN_KNOWLEDGE_CONFIG["design_domains"]
RULE_LEVELS = DESIGN_KNOWLEDGE_CONFIG["rule_levels"]
ERROR_SEVERITY_LEVELS = DESIGN_KNOWLEDGE_CONFIG["error_severity_levels"]
PRACTICE_VALUE_LEVELS = DESIGN_KNOWLEDGE_CONFIG["practice_value_levels"]
PROJECT_CATEGORIES = DESIGN_KNOWLEDGE_CONFIG["project_categories"]
APPLICABLE_PHASES = DESIGN_KNOWLEDGE_CONFIG["applicable_phases"]
DESIGN_KNOWLEDGE_EDITOR_ROLE_KEYWORDS = DESIGN_KNOWLEDGE_CONFIG["knowledge_editor_role_keywords"]
DESIGN_KNOWLEDGE_TAG_MANAGER_ROLE_KEYWORDS = DESIGN_KNOWLEDGE_CONFIG["tag_manager_role_keywords"]
DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES = DESIGN_KNOWLEDGE_CONFIG["review_routing_rules"]
DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES = DESIGN_KNOWLEDGE_CONFIG["review_fallback_approver_roles"]
DESIGN_ATTACHMENT_DIR_NAME = DESIGN_KNOWLEDGE_CONFIG["attachment"]["dir_name"]
DESIGN_ATTACHMENT_PARENTS_H = DESIGN_KNOWLEDGE_CONFIG["attachment"]["parents_h"]
DEFAULT_TAG_CATALOG = DESIGN_KNOWLEDGE_CONFIG["default_tag_catalog"]
CONTENT_TYPE_COPY = DESIGN_KNOWLEDGE_CONFIG["content_type_copy"]
