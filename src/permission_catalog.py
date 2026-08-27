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
        "查看样品单执行看板",
        "样品单执行看板",
        "允许从主页进入并查看样品单执行看板",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_BASE_EDIT_PERMISSION,
        "维护样品单基础与执行信息",
        "样品单执行看板",
        "允许新建、导入并维护样品单基础信息和执行信息",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_DELAY_EDIT_PERMISSION,
        "维护样品单延期信息",
        "样品单执行看板",
        "允许新增和维护样品单延期记录",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_SPECIAL_STATUS_EDIT_PERMISSION,
        "维护样品单特殊状态",
        "样品单执行看板",
        "允许设置样品单暂停、作废等特殊状态",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_DELAY_NATURE_EDIT_PERMISSION,
        "标记样品单延期性质",
        "样品单执行看板",
        "允许为已完成的延期样品单标记延期性质",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_DELETE_PERMISSION,
        "删除样品单",
        "样品单执行看板",
        "允许永久删除样品单记录",
    ),
    PermissionDefinition(
        SAMPLE_ORDER_AVERAGE_SCORE_VIEW_PERMISSION,
        "查看样品单平均考核分",
        "样品单执行看板",
        "允许查看样品单看板中的平均考核分统计",
    ),
)


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

PERMISSION_CATALOG = CORE_PERMISSIONS + TOOL_PERMISSIONS + SAMPLE_ORDER_PERMISSIONS
PERMISSION_CODES = frozenset(item.code for item in PERMISSION_CATALOG)


def ignores_legacy_role_grants(permission_code: str) -> bool:
    """判断权限是否已经正式停止读取旧角色过渡授权。"""
    normalized = str(permission_code or "").strip().lower()
    return (
        normalized == "system.manage"
        or (normalized.startswith("tools.") and normalized.endswith(".use"))
        or normalized.startswith("sample_order.")
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
