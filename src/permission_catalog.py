"""稳定权限编码及其兼容默认配置。

业务模块应依赖权限编码，而不是面向员工展示的角色名称。旧角色名称映射集中隔离在
本模块中，只用于业务模块分阶段迁移期间保持原有行为。
"""

from __future__ import annotations

import json
import logging
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
SAMPLE_ISSUE_CLOSE_DEFAULT_APPROVE_PERMISSION = "sample_issue.close.approve.default"
SAMPLE_ISSUE_CLOSE_ELECTRON_APPROVE_PERMISSION = "sample_issue.close.approve.electron_to_electron"
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
        SAMPLE_ISSUE_CLOSE_DEFAULT_APPROVE_PERMISSION,
        "审批 — 非特殊组别关闭申请",
        "样品问题跟进",
        "审批未命中特殊路由的关闭申请",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_CLOSE_ELECTRON_APPROVE_PERMISSION,
        "审批 — 电子组关闭申请",
        "样品问题跟进",
        "审批研发电子组岗位人员发起的关闭申请",
    ),
    PermissionDefinition(
        SAMPLE_ISSUE_REMINDER_CHECK_PERMISSION,
        "触发 — 人工检查提醒",
        "样品问题跟进",
    ),
    PermissionDefinition(SAMPLE_ISSUE_DELETE_PERMISSION, "删除 — 样品问题", "样品问题跟进"),
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

PERMISSION_CATALOG = (
    CORE_PERMISSIONS
    + TOOL_PERMISSIONS
    + SAMPLE_ORDER_PERMISSIONS
    + ERROR_PERMISSIONS
    + SAMPLE_ISSUE_PERMISSIONS
    + NOTIFICATION_PERMISSIONS
)
PERMISSION_CODES = frozenset(item.code for item in PERMISSION_CATALOG)


def ignores_legacy_role_grants(permission_code: str) -> bool:
    """判断权限是否已经正式停止读取旧角色过渡授权。"""
    normalized = str(permission_code or "").strip().lower()
    return (
        normalized == "system.manage"
        or (normalized.startswith("tools.") and normalized.endswith(".use"))
        or normalized.startswith("sample_order.")
        or normalized.startswith("error.")
        or normalized.startswith("sample_issue.")
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


def permission_catalog_rows() -> list[dict[str, Any]]:
    return [item.to_dict() for item in PERMISSION_CATALOG]
