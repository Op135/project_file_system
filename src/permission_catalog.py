"""稳定权限编码及其兼容默认配置。

业务模块应依赖权限编码，而不是面向员工展示的角色名称。旧角色名称映射集中隔离在
本模块中，只用于业务模块分阶段迁移期间保持原有行为。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PermissionDefinition:
    code: str
    name: str
    module: str
    description: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "name": self.name,
            "module": self.module,
            "description": self.description,
        }


CORE_PERMISSIONS = (
    PermissionDefinition("system.manage", "进入系统管理", "系统管理", "进入系统管理页面并维护系统配置"),
)


PROJECT_VIEW_PERMISSION = "project.view"
PROJECT_ALL_STATES_VIEW_PERMISSION = "project.all_states.view"
PROJECT_BASE_EDIT_PERMISSION = "project.base.edit"
PROJECT_STATUS_EDIT_PERMISSION = "project.status.edit"
PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION = "project.engineer.assign_all"
PROJECT_LEGACY_RECORD_MANAGE_PERMISSION = "project.record.manage"


PROJECT_PERMISSIONS = (
    PermissionDefinition(
        PROJECT_VIEW_PERMISSION,
        "查看 — 项目资料总表",
        "项目资料",
        "允许从主页进入并查看项目资料总表；未同时授权全部状态时只显示试产、量产项目",
    ),
    PermissionDefinition(
        PROJECT_ALL_STATES_VIEW_PERMISSION,
        "查看 — 全部项目状态",
        "项目资料",
        "允许在项目资料总表中查看待定、作废等全部状态的项目",
    ),
    PermissionDefinition(
        PROJECT_BASE_EDIT_PERMISSION,
        "维护 — 项目基础资料",
        "项目资料",
        "允许新增项目，并修改立项日期、简介、备注和客户简称；不包含项目状态和项目工程师",
    ),
    PermissionDefinition(
        PROJECT_STATUS_EDIT_PERMISSION,
        "维护 — 项目状态",
        "项目资料",
        "允许独立修改项目状态；新建项目时未获此权限则固定使用默认研发状态",
    ),
    PermissionDefinition(
        PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
        "指定 — 全部项目的项目工程师",
        "项目资料",
        "允许为任意项目指定或更换项目工程师负责人",
    ),
)


PROJECT_REQUIREMENT_VIEW_PERMISSION = "project_requirement.view"
PROJECT_REQUIREMENT_EDIT_PERMISSION = "project_requirement.edit"
PROJECT_REQUIREMENT_REVIEW_ASSIGNED_PERMISSION = "project_requirement.review.assigned"
PROJECT_REQUIREMENT_REVIEW_ALL_PERMISSION = "project_requirement.review.all"
PROJECT_REQUIREMENT_REVOKE_PERMISSION = "project_requirement.approval.revoke"
PROJECT_REQUIREMENT_DRAFT_MANAGE_ALL_PERMISSION = "project_requirement.draft.manage_all"


PROJECT_REQUIREMENT_PERMISSIONS = (
    PermissionDefinition(
        PROJECT_REQUIREMENT_VIEW_PERMISSION,
        "查看 — 项目需求配置正文",
        "项目需求配置",
        "允许从项目资料总表打开并查看项目需求配置正文",
    ),
    PermissionDefinition(
        PROJECT_REQUIREMENT_EDIT_PERMISSION,
        "维护 — 项目需求配置正文",
        "项目需求配置",
        "允许新建、暂存、自动保存和提交项目需求配置",
    ),
    PermissionDefinition(
        PROJECT_REQUIREMENT_REVIEW_ASSIGNED_PERMISSION,
        "审批 — 本人（项目工程师）负责项目需求配置",
        "项目需求配置",
        "允许审批本人被指定为项目工程师的项目需求配置",
    ),
    PermissionDefinition(
        PROJECT_REQUIREMENT_REVIEW_ALL_PERMISSION,
        "审批 — 全部项目需求配置",
        "项目需求配置",
        "允许审批全部项目需求配置，不受项目工程师分配限制",
    ),
    PermissionDefinition(
        PROJECT_REQUIREMENT_REVOKE_PERMISSION,
        "撤销 — 已通过需求审批",
        "项目需求配置",
        "允许撤销已经通过的需求版本并回退项目最高需求版本",
    ),
    PermissionDefinition(
        PROJECT_REQUIREMENT_DRAFT_MANAGE_ALL_PERMISSION,
        "查看 — 全部需求草稿",
        "项目需求配置",
        "允许在项目待办页查看其他用户的需求草稿；草稿仍只能由创建人删除",
    ),
)


PROJECT_TODO_VIEW_PERMISSION = "project_todo.view"


PROJECT_TODO_PERMISSIONS = (
    PermissionDefinition(
        PROJECT_TODO_VIEW_PERMISSION,
        "查看 — 项目待办工作台",
        "项目待办",
        "允许从主页进入项目待办工作台；具体需求、概述和审批内容仍按各自业务权限过滤",
    ),
)


QUESTION_TREE_VIEW_PERMISSION = "question_tree.view"


QUESTION_TREE_PERMISSIONS = (
    PermissionDefinition(
        QUESTION_TREE_VIEW_PERMISSION,
        "查看 — 需求项结构",
        "需求项结构",
        "允许进入需求项结构页面、搜索问题节点并查看或打印完整问题清单",
    ),
)


PROJECT_TEST_SUMMARY_VIEW_PERMISSION = "project_test_summary.view"


PROJECT_TEST_SUMMARY_PERMISSIONS = (
    PermissionDefinition(
        PROJECT_TEST_SUMMARY_VIEW_PERMISSION,
        "查看 — 生产测试项汇总表",
        "生产测试项汇总",
        "允许从项目资料或项目概述打开、查看并打印指定项目的生产测试项汇总表",
    ),
)


PROJECT_OVERVIEW_DIMENSIONS = (
    ("optical", "光学"),
    ("structure", "结构"),
    ("hardware", "硬件"),
    ("software", "软件"),
    ("ui", "UI"),
    ("process", "工艺"),
)
PROJECT_OVERVIEW_INACTIVE_VIEW_PERMISSION = "project_overview.inactive.view"
PROJECT_OVERVIEW_CONTENT_MANAGE_ALL_PERMISSION = "project_overview.content.manage_all"
PROJECT_OVERVIEW_BATCH_SUBMIT_PERMISSION = "project_overview.batch.submit"
PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION = "project_overview.batch.review"
PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION = "project_overview.correction.review"


def project_overview_dimension_permission(dimension: str, action: str) -> str:
    """生成上一版专业维度权限编码，仅用于迁移已有授权。"""
    return f"project_overview.{str(dimension).strip().lower()}.{str(action).strip().lower()}"


def project_overview_item_permission(label: str, action: str) -> str:
    """按概述配置的稳定 label 生成查看或维护权限编码。"""
    return f"project_overview.item.{str(label).strip().lower()}.{str(action).strip().lower()}"


PROJECT_OVERVIEW_PERMISSIONS = (
    PermissionDefinition(
        PROJECT_OVERVIEW_INACTIVE_VIEW_PERMISSION,
        "查看 — 失活项目概述",
        "项目概述",
        "允许在概述页面切换并查阅已经失活的历史概述内容",
    ),
    PermissionDefinition(
        PROJECT_OVERVIEW_CONTENT_MANAGE_ALL_PERMISSION,
        "修改 — 全部概述原始内容（无痕迹记录）",
        "项目概述",
        "允许使用管理工具直接修正全部专业的概述原始内容，不产生记录",
    ),
    PermissionDefinition(
        PROJECT_OVERVIEW_BATCH_SUBMIT_PERMISSION,
        "申请 — 批量概述变更",
        "项目概述",
        "允许跨项目批量新增概述或申请修改概述激活状态",
    ),
    PermissionDefinition(
        PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
        "审批 — 批量概述变更",
        "项目概述",
        "允许审批批量概述变更申请",
    ),
    PermissionDefinition(
        PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
        "审批 — 概述原记录纠错",
        "项目概述",
        "允许审批单条概述原记录纠错或删除申请",
    ),
)


class ProjectOverviewPermissionCatalogError(ValueError):
    """概述配置无法生成稳定权限目录。"""


def project_overview_permission_definitions(
    overview_config: dict[str, Any] | None = None,
    *,
    path: Path | str | None = None,
) -> tuple[PermissionDefinition, ...]:
    """从概述 JSON 动态生成 label 级权限，并拒绝空值、非法编码和重复 label。"""
    if overview_config is None:
        source = Path(path) if path else Path(__file__).resolve().parents[1] / "overview_config.json"
        try:
            overview_config = json.loads(source.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ProjectOverviewPermissionCatalogError(f"读取概述配置失败：{exc}") from exc
    if not isinstance(overview_config, dict):
        raise ProjectOverviewPermissionCatalogError("overview_config.json 根节点必须是对象")

    definitions: list[PermissionDefinition] = []
    seen: dict[str, str] = {}
    for raw_dimension, raw_groups in overview_config.items():
        dimension = str(raw_dimension).strip()
        if not dimension or not isinstance(raw_groups, dict):
            raise ProjectOverviewPermissionCatalogError(f"概述专业 {dimension or '<空>'} 的分组必须是对象")
        for raw_group, raw_items in raw_groups.items():
            group = str(raw_group).strip()
            if not group or not isinstance(raw_items, list):
                raise ProjectOverviewPermissionCatalogError(f"{dimension} / {group or '<空>'} 必须是列表")
            for index, item in enumerate(raw_items, start=1):
                if not isinstance(item, dict):
                    raise ProjectOverviewPermissionCatalogError(f"{dimension} / {group} 第 {index} 项必须是对象")
                label = str(item.get("label") or "").strip()
                title = str(item.get("title") or label).strip()
                location = f"{dimension} / {group} / {title or '<未命名>'}"
                if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", label):
                    raise ProjectOverviewPermissionCatalogError(
                        f"{location} 的 label“{label}”格式无效；必须以小写字母开头，"
                        "仅使用小写字母、数字和下划线，长度 2–40 位"
                    )
                if label in seen:
                    raise ProjectOverviewPermissionCatalogError(f"概述 label“{label}”重复：{seen[label]}；{location}")
                seen[label] = location
                module = f"项目概述 · {dimension} · {group}"
                for action, action_name in (("view", "查看"), ("edit", "维护")):
                    definitions.append(
                        PermissionDefinition(
                            project_overview_item_permission(label, action),
                            f"{action_name} — {title}",
                            module,
                            f"允许{action_name}{dimension} / {group}中的“{title}”；稳定标识：{label}",
                        )
                    )
    return tuple(definitions)


SAMPLE_ORDER_BASE_EDIT_PERMISSION = "sample_order.base.edit"
SAMPLE_ORDER_VIEW_PERMISSION = "sample_order.view"
SAMPLE_ORDER_DELAY_EDIT_PERMISSION = "sample_order.delay.edit"
SAMPLE_ORDER_SPECIAL_STATUS_EDIT_PERMISSION = "sample_order.special_status.edit"
SAMPLE_ORDER_DELAY_NATURE_EDIT_PERMISSION = "sample_order.delay_nature.edit"
SAMPLE_ORDER_DELETE_PERMISSION = "sample_order.delete"
SAMPLE_ORDER_AVERAGE_SCORE_VIEW_PERMISSION = "sample_order.average_score.view"


SAMPLE_ORDER_PERMISSIONS = (
    PermissionDefinition(
        SAMPLE_ORDER_VIEW_PERMISSION,
        "查看 — 执行看板",
        "样品单执行看板",
        "允许从主页进入并查看样品单执行看板",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_BASE_EDIT_PERMISSION,
        "维护 — 基础与执行信息",
        "样品单执行看板",
        "允许新建、导入并维护样品单基础信息和执行信息",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_DELAY_EDIT_PERMISSION,
        "维护 — 延期信息",
        "样品单执行看板",
        "允许新增和维护样品单延期记录",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_SPECIAL_STATUS_EDIT_PERMISSION,
        "维护 — 特殊状态",
        "样品单执行看板",
        "允许设置样品单暂停、作废等特殊状态",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_DELAY_NATURE_EDIT_PERMISSION,
        "标记 — 延期性质",
        "样品单执行看板",
        "允许为已完成的延期样品单标记延期性质",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_DELETE_PERMISSION,
        "删除 — 样品单",
        "样品单执行看板",
        "允许永久删除样品单记录",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_AVERAGE_SCORE_VIEW_PERMISSION,
        "查看 — 平均考核分",
        "样品单执行看板",
        "允许查看样品单看板中的平均考核分统计",
    ),
)


ERROR_VIEW_PERMISSION = "error.view"
ERROR_RECORD_EDIT_PERMISSION = "error.record.edit"
ERROR_REQUEST_APPROVE_PERMISSION = "error.request.approve"
ERROR_RECORD_RENAME_PERMISSION = "error.record.rename"
ERROR_RECORD_DELETE_PERMISSION = "error.record.delete"
ERROR_REMINDER_CHECK_PERMISSION = "error.reminder.check"


ERROR_PERMISSIONS = (
    PermissionDefinition(
        ERROR_VIEW_PERMISSION,
        "查看 — 异常单",
        "异常单跟进",
        "允许从主页进入并查看生产异常单",
    ),
    PermissionDefinition(
        ERROR_RECORD_EDIT_PERMISSION,
        "维护 — 整单信息",
        "异常单跟进",
        "允许新建并维护异常单整单内容",
    ),
    PermissionDefinition(
        ERROR_REQUEST_APPROVE_PERMISSION,
        "审批 — 延期/关闭申请",
        "异常单跟进",
        "允许审批纠正预防措施的延期申请和关闭申请",
    ),
    PermissionDefinition(
        ERROR_RECORD_RENAME_PERMISSION,
        "修改 — 异常单号",
        "异常单跟进",
        "允许修改已有异常单的单号",
    ),
    PermissionDefinition(
        ERROR_RECORD_DELETE_PERMISSION,
        "删除 — 异常单",
        "异常单跟进",
        "允许永久删除整张异常单",
    ),
    PermissionDefinition(
        ERROR_REMINDER_CHECK_PERMISSION,
        "触发 — 人工检查提醒",
        "异常单跟进",
        "允许手动触发异常措施到期提醒检查",
    ),
)


SAMPLE_ISSUE_VIEW_PERMISSION = "sample_issue.view"
SAMPLE_ISSUE_CREATE_PERMISSION = "sample_issue.record.create"
SAMPLE_ISSUE_EDIT_ALL_PERMISSION = "sample_issue.record.edit_all"
SAMPLE_ISSUE_EXTENSION_APPROVE_PERMISSION = "sample_issue.extension.approve"
SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION = "sample_issue.close.approve"
SAMPLE_ISSUE_LEGACY_CLOSE_DEFAULT_APPROVE_PERMISSION = "sample_issue.close.approve.default"
SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION = "sample_issue.close.approve.electron_to_electron"
SAMPLE_ISSUE_REMINDER_CHECK_PERMISSION = "sample_issue.reminder.check"
SAMPLE_ISSUE_DELETE_PERMISSION = "sample_issue.delete"


SAMPLE_ISSUE_PERMISSIONS = (
    PermissionDefinition(SAMPLE_ISSUE_VIEW_PERMISSION, "查看 — 样品问题", "样品问题跟进"),
    PermissionDefinition(SAMPLE_ISSUE_CREATE_PERMISSION, "录入 — 样品问题", "样品问题跟进"),
    PermissionDefinition(
        SAMPLE_ISSUE_EDIT_ALL_PERMISSION,
        "维护 — 非本人录入/对策区块",
        "样品问题跟进",
        "允许维护非本人创建的录入区块，并协助维护非本人负责的对策区块",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_EXTENSION_APPROVE_PERMISSION,
        "审批 — 延期申请",
        "样品问题跟进",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
        "审批 — 关闭申请",
        "样品问题跟进",
        "作为审批流程候选人处理样品问题关闭申请；实际可审批单据由具体流程待办决定",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_REMINDER_CHECK_PERMISSION,
        "触发 — 人工检查提醒",
        "样品问题跟进",
    ),
    PermissionDefinition(SAMPLE_ISSUE_DELETE_PERMISSION, "删除 — 样品问题", "样品问题跟进"),
)


DESIGN_KNOWLEDGE_VIEW_PERMISSION = "design_knowledge.view"
DESIGN_KNOWLEDGE_CREATE_PERMISSION = "design_knowledge.record.create"
DESIGN_KNOWLEDGE_EDIT_PERMISSION = "design_knowledge.record.edit"
DESIGN_KNOWLEDGE_REVIEW_PERMISSION = "design_knowledge.record.review"
DESIGN_KNOWLEDGE_TAG_MANAGE_PERMISSION = "design_knowledge.tag.manage"
DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION = "design_knowledge.tag.review"
DESIGN_KNOWLEDGE_DELETE_PERMISSION = "design_knowledge.delete"


DESIGN_KNOWLEDGE_PERMISSIONS = (
    PermissionDefinition(
        DESIGN_KNOWLEDGE_VIEW_PERMISSION,
        "查看 — 设计知识库",
        "设计知识库",
        "允许从主页进入并查看已发布的设计知识",
    ),
    PermissionDefinition(
        DESIGN_KNOWLEDGE_CREATE_PERMISSION,
        "录入 — 设计知识",
        "设计知识库",
        "允许新建设计知识草稿",
    ),
    PermissionDefinition(
        DESIGN_KNOWLEDGE_EDIT_PERMISSION,
        "维护 — 本人设计知识",
        "设计知识库",
        "允许维护本人创建且尚未停用的设计知识，并提交审核",
    ),
    PermissionDefinition(
        DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
        "审批 — 设计知识发布",
        "设计知识库",
        "作为审批流程候选人审核知识发布申请，并维护已发布知识的适用状态",
    ),
    PermissionDefinition(
        DESIGN_KNOWLEDGE_TAG_MANAGE_PERMISSION,
        "维护 — 受控标签库",
        "设计知识库",
        "允许直接新增和维护受控标签",
    ),
    PermissionDefinition(
        DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
        "审批 — 新标签申请",
        "设计知识库",
        "作为审批流程候选人审批新标签申请",
    ),
    PermissionDefinition(
        DESIGN_KNOWLEDGE_DELETE_PERMISSION,
        "删除 — 设计知识",
        "设计知识库",
        "允许永久删除设计知识记录",
    ),
)


STATISTICS_VIEW_PERMISSION = "statistics.view"
STATISTICS_OVERVIEW_VIEW_PERMISSION = "statistics.overview.view"
STATISTICS_OVERVIEW_OWNER_MANAGE_PERMISSION = "statistics.overview_owner.manage"


STATISTICS_PERMISSIONS = (
    PermissionDefinition(
        STATISTICS_VIEW_PERMISSION,
        "查看 — 统计信息",
        "统计信息",
        "允许从主页进入统计信息页面并查看通用统计区块",
    ),
    PermissionDefinition(
        STATISTICS_OVERVIEW_VIEW_PERMISSION,
        "查看 — 全部概述待办与负责人统计",
        "统计信息",
        "允许查看全体概述负责人的待办、完成度和历史趋势统计",
    ),
    PermissionDefinition(
        STATISTICS_OVERVIEW_OWNER_MANAGE_PERMISSION,
        "维护 — 项目概述负责人",
        "统计信息",
        "允许跨项目配置光学、结构、硬件、软件、UI 和工艺等概述负责人",
    ),
)


# ECN 工程变更权限按业务动作拆分，审批权限只表示候选资格，不能代替具体流程待办。
ECN_VIEW_PERMISSION = "ecn.view"
ECN_CREATE_PERMISSION = "ecn.request.create"
ECN_IMPACT_EDIT_PERMISSION = "ecn.impact.edit"
ECN_SCHEME_EDIT_PERMISSION = "ecn.scheme.edit"
ECN_SCHEME_REVIEW_SUBMIT_PERMISSION = "ecn.scheme.review.submit"
ECN_IMPACT_INITIAL_REMINDER_PERMISSION = "ecn.impact.initial_reminder"
ECN_ECR_APPROVE_PERMISSION = "ecn.ecr.approve"
ECN_SCHEME_APPROVE_PERMISSION = "ecn.scheme.approve"
ECN_EXECUTION_ASSISTANT_PERMISSION = "ecn.execution.assistant"
ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION = "ecn.execution.material.confirm"
ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION = "ecn.execution.purchase.confirm"
ECN_EXECUTION_PMC_CONFIRM_PERMISSION = "ecn.execution.pmc.confirm"
ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION = "ecn.execution.production.confirm"
ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION = "ecn.execution.sales_supervisor.confirm"
ECN_DELETE_PERMISSION = "ecn.delete"

ECN_ORDINARY_FILE_CHANGE_TYPE_KEYS = {
    "图纸更新": "drawing",
    "SOP修改": "sop",
    "测试报告内容格式": "test_report",
    "其它": "other",
}


def ecn_ordinary_file_view_permission(change_type: object) -> str:
    """返回特定事项/资料分类对应的 ECN 非图片附件查看权限。"""
    key = ECN_ORDINARY_FILE_CHANGE_TYPE_KEYS.get(str(change_type or "").strip(), "")
    return f"ecn.file.ordinary.{key}.view" if key else ""


ECN_PERMISSIONS = (
    PermissionDefinition(
        ECN_VIEW_PERMISSION,
        "查看 — ECN工程变更",
        "工程变更 · 入口与申请",
        "允许从主页进入并查看ECN工程变更列表及单据详情",
    ),
    PermissionDefinition(
        ECN_CREATE_PERMISSION,
        "申请 — 新建/提交ECR",
        "工程变更 · 入口与申请",
        "允许新建ECR、保存本人草稿、提交、撤回和作废本人尚未完成的申请",
    ),
    PermissionDefinition(
        ECN_IMPACT_EDIT_PERMISSION,
        "维护 — ECN影响评估",
        "工程变更 · 影响与方案",
        "允许在方案编写阶段维护扩大影响范围、涉及资料和物料等影响信息",
    ),
    PermissionDefinition(
        ECN_SCHEME_EDIT_PERMISSION,
        "编写 — ECN变更方案",
        "工程变更 · 影响与方案",
        "允许在方案编写阶段新增、维护并确认本人编写的变更方案",
    ),
    PermissionDefinition(
        ECN_SCHEME_REVIEW_SUBMIT_PERMISSION,
        "发起 — ECN方案评审",
        "工程变更 · 影响与方案",
        "允许在所有方案参与人确认完成后发起ECN方案评审",
    ),
    PermissionDefinition(
        ECN_IMPACT_INITIAL_REMINDER_PERMISSION,
        "提醒 — ECN影响评估尚未认领",
        "工程变更 · 影响与方案",
        "ECR通过后影响评估仍为空且无人认领时，在主页接收全局兜底待办提醒",
    ),
    PermissionDefinition(
        ECN_ECR_APPROVE_PERMISSION,
        "审批 — ECR申请",
        "工程变更 · 审批",
        "作为ECR审批候选人；实际可审批单据由已发布流程产生的具体待办决定",
    ),
    PermissionDefinition(
        ECN_SCHEME_APPROVE_PERMISSION,
        "审批 — ECN方案",
        "工程变更 · 审批",
        "作为ECN方案评审候选人；实际可审批单据由已发布流程产生的具体待办决定",
    ),
    PermissionDefinition(
        ECN_EXECUTION_ASSISTANT_PERMISSION,
        "执行 — 资料准备与系统内资料落盘",
        "工程变更 · 执行",
        "允许确认第一阶段资料和ERP状态，并触发或恢复系统内资料执行",
    ),
    PermissionDefinition(
        ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
        "执行 — 本人负责项目的销售追溯确认",
        "工程变更 · 执行",
        "允许项目销售确认单据中明确分配给本人的客户/在途追溯责任项",
    ),
    PermissionDefinition(
        ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
        "执行 — 采购追溯责任项",
        "工程变更 · 执行",
        "允许确认ECN物料执行清单中的采购责任节点",
    ),
    PermissionDefinition(
        ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
        "执行 — PMC追溯责任项",
        "工程变更 · 执行",
        "允许确认ECN物料执行清单中的PMC责任节点",
    ),
    PermissionDefinition(
        ECN_EXECUTION_PRODUCTION_CONFIRM_PERMISSION,
        "执行 — 生产追溯责任项",
        "工程变更 · 执行",
        "允许确认ECN物料执行清单中的生产管理责任节点",
    ),
    PermissionDefinition(
        ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
        "执行 — 销售管理追溯责任项",
        "工程变更 · 执行",
        "允许确认客户/在途范围的销售管理责任节点，并在项目销售未识别时兜底确认",
    ),
    *(
        PermissionDefinition(
            f"ecn.file.ordinary.{permission_key}.view",
            f"查看附件 — {change_type}",
            "工程变更 · 附件",
            f"允许查看或下载ECN中“{change_type}”类特定事项/资料方案的非图片附件",
        )
        for change_type, permission_key in ECN_ORDINARY_FILE_CHANGE_TYPE_KEYS.items()
    ),
    PermissionDefinition(
        ECN_DELETE_PERMISSION,
        "删除 — ECN单据",
        "工程变更 · 高风险操作",
        "允许永久删除ECN单据及其流程记录；此权限不会自动授予普通岗位",
    ),
)


SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION = "notifications.sample_order.extension.receive"
SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION = "notifications.sample_order.special_status.receive"
ERROR_EXTENSION_REQUEST_NOTIFY_PERMISSION = "notifications.error.extension.request.receive"
ERROR_EXTENSION_RESULT_NOTIFY_PERMISSION = "notifications.error.extension.result.receive"
ERROR_EXTENSION_APPROVED_NOTIFY_PERMISSION = "notifications.error.extension.approved.receive"
ERROR_CLOSE_REQUEST_NOTIFY_PERMISSION = "notifications.error.close.request.receive"
ERROR_CLOSE_RESULT_NOTIFY_PERMISSION = "notifications.error.close.result.receive"
ERROR_CLOSE_APPROVED_NOTIFY_PERMISSION = "notifications.error.close.approved.receive"
ERROR_OWNER_MISSING_REMINDER_PERMISSION = "notifications.error.owner_missing_reminder.receive"
SAMPLE_ISSUE_EXTENSION_REQUEST_NOTIFY_PERMISSION = "notifications.sample_issue.extension.request.receive"
SAMPLE_ISSUE_EXTENSION_RESULT_NOTIFY_PERMISSION = "notifications.sample_issue.extension.result.receive"
SAMPLE_ISSUE_EXTENSION_APPROVED_NOTIFY_PERMISSION = "notifications.sample_issue.extension.approved.receive"
SAMPLE_ISSUE_CLOSE_DEFAULT_REQUEST_NOTIFY_PERMISSION = "notifications.sample_issue.close.default.request.receive"
SAMPLE_ISSUE_CLOSE_DEFAULT_RESULT_NOTIFY_PERMISSION = "notifications.sample_issue.close.default.result.receive"
SAMPLE_ISSUE_CLOSE_DEFAULT_APPROVED_NOTIFY_PERMISSION = "notifications.sample_issue.close.default.approved.receive"
SAMPLE_ISSUE_CLOSE_ELECTRON_REQUEST_NOTIFY_PERMISSION = "notifications.sample_issue.close.electron.request.receive"
SAMPLE_ISSUE_CLOSE_ELECTRON_RESULT_NOTIFY_PERMISSION = "notifications.sample_issue.close.electron.result.receive"
SAMPLE_ISSUE_CLOSE_ELECTRON_APPROVED_NOTIFY_PERMISSION = "notifications.sample_issue.close.electron.approved.receive"
SAMPLE_ISSUE_FALLBACK_REMINDER_PERMISSION = "notifications.sample_issue.fallback_reminder.receive"
SAMPLE_ORDER_NOTIFICATION_MODULE = "样品单执行看板 · 通知接收"
ERROR_NOTIFICATION_MODULE = "异常单跟进 · 通知接收"
SAMPLE_ISSUE_NOTIFICATION_MODULE = "样品问题跟进 · 通知接收"


NOTIFICATION_PERMISSIONS = (
    PermissionDefinition(
        SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
        "告知 — 样品单 — 延期关注",
        SAMPLE_ORDER_NOTIFICATION_MODULE,
        "接收样品单超过延期次数阈值的关注通知；调试转发开启时也接收申请人延期通知",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION,
        "告知 — 样品单 — 特殊状态",
        SAMPLE_ORDER_NOTIFICATION_MODULE,
        "接收样品单暂停、作废等特殊状态变更通知",
    ),
    PermissionDefinition(
        ERROR_EXTENSION_REQUEST_NOTIFY_PERMISSION,
        "审批 — 异常措施 — 延期申请",
        ERROR_NOTIFICATION_MODULE,
        "负责人提交纠正预防措施延期申请时接收通知",
    ),
    PermissionDefinition(
        ERROR_EXTENSION_RESULT_NOTIFY_PERMISSION,
        "告知 — 异常措施 — 全局延期审批结果（通/驳）",
        ERROR_NOTIFICATION_MODULE,
        "纠正预防措施延期申请通过或驳回后接收审批结果通知",
    ),
    PermissionDefinition(
        ERROR_EXTENSION_APPROVED_NOTIFY_PERMISSION,
        "抄送 — 异常措施 — 全局延期（通过）",
        ERROR_NOTIFICATION_MODULE,
        "仅在纠正预防措施延期申请审批通过后除申请人外增加的通知",
    ),
    PermissionDefinition(
        ERROR_CLOSE_REQUEST_NOTIFY_PERMISSION,
        "审批 — 异常措施 — 关闭申请",
        ERROR_NOTIFICATION_MODULE,
        "负责人提交纠正预防措施关闭申请时接收通知",
    ),
    PermissionDefinition(
        ERROR_CLOSE_RESULT_NOTIFY_PERMISSION,
        "告知 — 异常措施 — 全局关闭审批结果（通/驳）",
        ERROR_NOTIFICATION_MODULE,
        "纠正预防措施关闭申请通过或驳回后接收审批结果通知",
    ),
    PermissionDefinition(
        ERROR_CLOSE_APPROVED_NOTIFY_PERMISSION,
        "抄送 — 异常措施 — 全局关闭（通过）",
        ERROR_NOTIFICATION_MODULE,
        "仅在纠正预防措施关闭申请审批通过后除申请人外增加的通知",
    ),
    PermissionDefinition(
        ERROR_OWNER_MISSING_REMINDER_PERMISSION,
        "提醒 — 异常措施 — 无负责人兜底",
        ERROR_NOTIFICATION_MODULE,
        "异常措施未填写负责人时，作为到期提醒的兜底接收人",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_EXTENSION_REQUEST_NOTIFY_PERMISSION,
        "审批 — 样品问题 — 延期申请",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "负责人提交样品问题延期申请时接收通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_EXTENSION_RESULT_NOTIFY_PERMISSION,
        "告知 — 样品问题 — 全局延期审批结果（通/驳）",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "样品问题延期申请通过或驳回后接收审批结果通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_EXTENSION_APPROVED_NOTIFY_PERMISSION,
        "抄送 — 样品问题 — 全局延期（通过）",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "仅在样品问题延期申请审批通过后除申请人外增加的通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_DEFAULT_REQUEST_NOTIFY_PERMISSION,
        "审批 — 样品问题 — 非特殊组别关闭申请",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "未命中特殊路由的关闭申请审批通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_DEFAULT_RESULT_NOTIFY_PERMISSION,
        "告知 — 样品问题 — 非特殊组别全局关闭审批结果（通/驳）",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "样品问题关闭申请通过或驳回后接收审批结果通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_DEFAULT_APPROVED_NOTIFY_PERMISSION,
        "抄送 — 样品问题 — 非特殊组别全局关闭（通过）",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "仅在样品问题关闭申请审批通过后除申请人外增加的通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_ELECTRON_REQUEST_NOTIFY_PERMISSION,
        "审批 — 样品问题 — 电子组关闭申请",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "电子组岗位人员发起的关闭申请审批通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_ELECTRON_RESULT_NOTIFY_PERMISSION,
        "告知 — 样品问题 — 电子组全局关闭审批结果（通/驳）",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "电子组样品问题关闭申请通过或驳回后接收审批结果通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_ELECTRON_APPROVED_NOTIFY_PERMISSION,
        "抄送 — 样品问题 — 电子组全局关闭（通过）",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "仅在电子组样品问题关闭申请审批通过后除申请人外增加的通知",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_FALLBACK_REMINDER_PERMISSION,
        "提醒 — 样品问题 — 无人员兜底",
        SAMPLE_ISSUE_NOTIFICATION_MODULE,
        "样品问题未填写负责人时，作为到期提醒的兜底接收人",
    ),
)


# 上一版宽粒度通知权限只用于一次性迁移已有授权，不再显示在权限目录中。
DEPRECATED_PERMISSION_REPLACEMENTS = {
    PROJECT_LEGACY_RECORD_MANAGE_PERMISSION: (
        PROJECT_BASE_EDIT_PERMISSION,
        PROJECT_STATUS_EDIT_PERMISSION,
        PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
    ),
    SAMPLE_ISSUE_LEGACY_CLOSE_DEFAULT_APPROVE_PERMISSION: (SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,),
    SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION: (SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,),
    "notifications.sample_order.attention.receive": (
        SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
        SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION,
    ),
    "notifications.error.workflow.receive": (
        ERROR_EXTENSION_REQUEST_NOTIFY_PERMISSION,
        ERROR_EXTENSION_RESULT_NOTIFY_PERMISSION,
        ERROR_CLOSE_REQUEST_NOTIFY_PERMISSION,
        ERROR_CLOSE_RESULT_NOTIFY_PERMISSION,
    ),
    "notifications.error.approval.receive": (
        ERROR_EXTENSION_APPROVED_NOTIFY_PERMISSION,
        ERROR_CLOSE_APPROVED_NOTIFY_PERMISSION,
    ),
    "notifications.error.fallback.receive": (ERROR_OWNER_MISSING_REMINDER_PERMISSION,),
}


_TOOL_NAMES = {
    "etendue_calc": "光学扩展量极限计算",
    "simple_coupling_calc": "简单透镜组耦合效率",
    "microlens_calc": "复眼透镜耦合效率",
    "mode_calc": "激光横模分析",
    "spherical_calc": "球面透镜面型分析",
    "material_matcher": "智能物料请购核算",
    "optical_curve_manager": "研发光学曲线资料库",
    "spectral_analyzer": "光谱色度与显色分析",
    "operand_lookup": "Zemax 操作数查询",
    "pixel_statistics": "光斑均匀性计算",
}


def tool_permission_code(tool_key: str) -> str:
    return f"tools.{str(tool_key).strip()}.use"


TOOL_PERMISSIONS = tuple(
    PermissionDefinition(
        tool_permission_code(tool_key),
        f"使用{name}",
        "分析工具",
        f"允许查看并打开{name}",
    )
    for tool_key, name in _TOOL_NAMES.items()
)

STATIC_PERMISSION_CATALOG = (
    CORE_PERMISSIONS
    + TOOL_PERMISSIONS
    + PROJECT_PERMISSIONS
    + PROJECT_REQUIREMENT_PERMISSIONS
    + PROJECT_TODO_PERMISSIONS
    + QUESTION_TREE_PERMISSIONS
    + PROJECT_TEST_SUMMARY_PERMISSIONS
    + PROJECT_OVERVIEW_PERMISSIONS
    + SAMPLE_ORDER_PERMISSIONS
    + ERROR_PERMISSIONS
    + SAMPLE_ISSUE_PERMISSIONS
    + DESIGN_KNOWLEDGE_PERMISSIONS
    + STATISTICS_PERMISSIONS
    + ECN_PERMISSIONS
    + NOTIFICATION_PERMISSIONS
)
try:
    PROJECT_OVERVIEW_ITEM_PERMISSIONS = project_overview_permission_definitions()
except ProjectOverviewPermissionCatalogError:
    # 配置错误不能阻断整个系统启动；进入权限管理或手动刷新概述配置时会严格校验并提示。
    logger.exception("概述 label 权限目录初始化失败，本次启动暂不注册概述项权限")
    PROJECT_OVERVIEW_ITEM_PERMISSIONS = ()
PERMISSION_CATALOG = STATIC_PERMISSION_CATALOG + PROJECT_OVERVIEW_ITEM_PERMISSIONS
PERMISSION_CODES = frozenset(item.code for item in PERMISSION_CATALOG)

# 把上一版专业级授权一次性展开到该专业当前已有的全部 label，随后删除专业级权限。
for dimension_code, dimension_name in PROJECT_OVERVIEW_DIMENSIONS:
    dimension_permissions = tuple(
        item for item in PROJECT_OVERVIEW_ITEM_PERMISSIONS if item.module.startswith(f"项目概述 · {dimension_name} · ")
    )
    for action in ("view", "edit"):
        replacement_codes = tuple(item.code for item in dimension_permissions if item.code.endswith(f".{action}"))
        if replacement_codes:
            DEPRECATED_PERMISSION_REPLACEMENTS[project_overview_dimension_permission(dimension_code, action)] = (
                replacement_codes
            )


def ignores_legacy_role_grants(permission_code: str) -> bool:
    """判断权限是否已经正式停止读取旧角色过渡授权。"""
    normalized = str(permission_code or "").strip().lower()
    return (
        normalized == "system.manage"
        or (normalized.startswith("tools.") and normalized.endswith(".use"))
        or normalized.startswith("project.")
        or normalized.startswith("project_requirement.")
        or normalized.startswith("project_todo.")
        or normalized.startswith("question_tree.")
        or normalized.startswith("project_test_summary.")
        or normalized.startswith("project_overview.")
        or normalized.startswith("sample_order.")
        or normalized.startswith("error.")
        or normalized.startswith("sample_issue.")
        or normalized.startswith("design_knowledge.")
        or normalized.startswith("statistics.")
        or normalized.startswith("ecn.")
        or normalized.startswith("notifications.")
    )


def load_tool_role_mapping(path: Path | str) -> dict[str, list[str]] | None:
    """读取旧工具角色文件，用于单向初始化兼容授权。

    返回 ``None`` 表示文件不存在，沿用原有语义：所有兼容角色均可使用全部工具。
    文件内容无效时采用安全关闭策略，返回空映射。
    """
    source = Path(path)
    if not source.exists():
        return None
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("读取旧工具权限配置失败：%s", source)
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[str]] = {}
    for tool_key, roles in raw.items():
        if tool_key not in _TOOL_NAMES or not isinstance(roles, list):
            continue
        result[tool_key] = list(dict.fromkeys(str(role).strip() for role in roles if str(role).strip()))
    return result


def build_legacy_default_grants(
    tool_role_mapping: dict[str, list[str]] | None,
    *,
    known_role_names: list[str] | None = None,
) -> dict[str, set[str]]:
    """按旧显示角色名称生成初始授权。

    每一组角色与权限关系只初始化一次。该映射不是运行时授权规则，因此不会在已经
    迁移的模块中重新引入角色关键词匹配。
    """
    grants: dict[str, set[str]] = {"admin": {item.code for item in CORE_PERMISSIONS}}
    if tool_role_mapping is None:
        for role_name in known_role_names or []:
            grants.setdefault(role_name, set()).update(item.code for item in TOOL_PERMISSIONS)
        return grants
    for tool_key, role_names in tool_role_mapping.items():
        permission_code = tool_permission_code(tool_key)
        if permission_code not in PERMISSION_CODES:
            continue
        for role_name in role_names:
            grants.setdefault(role_name, set()).add(permission_code)
    return grants


def permission_catalog_rows(
    *,
    overview_config: dict[str, Any] | None = None,
    overview_config_path: Path | str | None = None,
    strict_overview: bool = True,
) -> list[dict[str, Any]]:
    """返回当前权限目录；概述 label 每次调用都从最新 JSON 动态生成。"""
    try:
        overview_permissions = project_overview_permission_definitions(
            overview_config,
            path=overview_config_path,
        )
    except ProjectOverviewPermissionCatalogError:
        if strict_overview:
            raise
        logger.exception("概述 label 权限目录同步失败，本次仅同步静态权限")
        overview_permissions = ()
    return [item.to_dict() for item in STATIC_PERMISSION_CATALOG + overview_permissions]
