"""把旧概述 JSON 角色规则批量换算为岗位的 label 级权限。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .permission_catalog import (
    project_overview_item_permission,
    project_overview_permission_definitions,
)


OVERVIEW_ROLE_POSITION_MAPPING_META_KEY = "project_overview_role_position_mapping"
OVERVIEW_ROLE_POSITION_MANAGED_META_KEY = "project_overview_role_position_managed_permissions"
OVERVIEW_ITEM_PERMISSION_PREFIX = "project_overview.item."


def load_overview_permission_source(path: Path | str) -> dict[str, Any]:
    """读取并严格校验用于批量映射的概述配置。"""
    source = Path(path)
    try:
        config = json.loads(source.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"读取概述配置失败：{exc}") from exc
    project_overview_permission_definitions(config)
    return config


def collect_legacy_overview_role_usage(config: dict[str, Any]) -> list[dict[str, Any]]:
    """统计旧角色在概述查看和维护规则中分别覆盖多少个 label。"""
    project_overview_permission_definitions(config)
    usage: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"view_labels": set(), "edit_labels": set()}
    )
    for groups in config.values():
        for items in groups.values():
            for item in items:
                label = str(item.get("label") or "").strip()
                permission = item.get("permission", {})
                if not isinstance(permission, dict):
                    continue
                for action, source_key in (("view", "read_role"), ("edit", "edit_role")):
                    roles = permission.get(source_key, [])
                    if not isinstance(roles, list):
                        continue
                    for raw_role in roles:
                        role = str(raw_role or "").strip()
                        if role:
                            usage[role][f"{action}_labels"].add(label)
    return [
        {
            "role": role,
            "view_count": len(values["view_labels"]),
            "edit_count": len(values["edit_labels"]),
        }
        for role, values in sorted(usage.items(), key=lambda item: item[0].casefold())
    ]


def normalize_overview_role_position_mapping(
    raw_mapping: object,
    *,
    valid_roles: set[str] | None = None,
    valid_position_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """清理映射中的空值、重复岗位及已经失效的角色或岗位。"""
    if not isinstance(raw_mapping, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for raw_role, raw_position_ids in raw_mapping.items():
        role = str(raw_role or "").strip()
        if not role or (valid_roles is not None and role not in valid_roles):
            continue
        if not isinstance(raw_position_ids, (list, tuple, set)):
            continue
        position_ids = list(
            dict.fromkeys(
                str(position_id).strip()
                for position_id in raw_position_ids
                if str(position_id).strip()
                and (
                    valid_position_ids is None
                    or str(position_id).strip() in valid_position_ids
                )
            )
        )
        if position_ids:
            normalized[role] = position_ids
    return normalized


def build_overview_position_permission_plan(
    config: dict[str, Any],
    role_position_mapping: dict[str, list[str]],
) -> dict[str, set[str]]:
    """按旧角色读写规则计算每个目标岗位应拥有的完整概述项权限集合。"""
    project_overview_permission_definitions(config)
    plan: dict[str, set[str]] = defaultdict(set)
    for groups in config.values():
        for items in groups.values():
            for item in items:
                label = str(item.get("label") or "").strip()
                permission = item.get("permission", {})
                if not isinstance(permission, dict):
                    continue
                for action, source_key in (("view", "read_role"), ("edit", "edit_role")):
                    roles = permission.get(source_key, [])
                    if not isinstance(roles, list):
                        continue
                    permission_code = project_overview_item_permission(label, action)
                    for raw_role in roles:
                        role = str(raw_role or "").strip()
                        for position_id in role_position_mapping.get(role, []):
                            plan[str(position_id)].add(permission_code)
    return dict(plan)
