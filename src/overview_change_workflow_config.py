# -*- encoding: utf-8 -*-
"""概述批量变更与单项纠错的权限、审批流程配置。

维护人员只需修改项目根目录的 ``overview_change_workflow_config.json``；
修改后重启服务即可生效。配置属于权限边界，缺失或格式错误时直接阻止启动，
避免无效配置被静默忽略。
"""

import json
from pathlib import Path
from typing import Any


OVERVIEW_CHANGE_WORKFLOW_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "overview_change_workflow_config.json"
)


class OverviewChangeWorkflowConfigError(ValueError):
    """概述变更权限或审批配置无效。"""


def _string_list(value: Any, field_path: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise OverviewChangeWorkflowConfigError(f"{field_path} 必须是字符串数组")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise OverviewChangeWorkflowConfigError(f"{field_path} 只能包含非空字符串")
        item = item.strip()
        if item not in normalized:
            normalized.append(item)
    if not normalized and not allow_empty:
        raise OverviewChangeWorkflowConfigError(f"{field_path} 不能为空")
    return normalized


def _approval_role_targets(value: Any, field_path: str) -> dict[str, frozenset[str]]:
    if not isinstance(value, dict):
        raise OverviewChangeWorkflowConfigError(f"{field_path} 必须是 JSON 对象")
    normalized: dict[str, frozenset[str]] = {}
    for raw_reviewer_role, raw_target_roles in value.items():
        if not isinstance(raw_reviewer_role, str) or not raw_reviewer_role.strip():
            raise OverviewChangeWorkflowConfigError(f"{field_path} 的审批角色必须是非空字符串")
        reviewer_role = raw_reviewer_role.strip()
        target_roles = _string_list(
            raw_target_roles,
            f"{field_path}.{reviewer_role}",
            allow_empty=True,
        )
        normalized[reviewer_role] = frozenset(target_roles)
    return normalized


def _bool_value(section: dict, key: str, field_path: str) -> bool:
    value = section.get(key)
    if not isinstance(value, bool):
        raise OverviewChangeWorkflowConfigError(f"{field_path}.{key} 必须是 true 或 false")
    return value


def load_overview_change_workflow_config(
    raw_config: dict | None = None,
    *,
    path: Path | None = None,
) -> dict:
    """读取并严格校验概述变更权限和审批配置。"""
    config_path = path or OVERVIEW_CHANGE_WORKFLOW_CONFIG_PATH
    if raw_config is None:
        try:
            with config_path.open("r", encoding="utf-8") as config_file:
                loaded = json.load(config_file)
        except FileNotFoundError as exc:
            raise OverviewChangeWorkflowConfigError(f"配置文件不存在：{config_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise OverviewChangeWorkflowConfigError(f"配置文件读取失败：{exc}") from exc
        raw_config = loaded

    if not isinstance(raw_config, dict):
        raise OverviewChangeWorkflowConfigError("配置根节点必须是 JSON 对象")
    if raw_config.get("schema_version") != 1:
        raise OverviewChangeWorkflowConfigError("schema_version 当前只支持 1")

    raw_batch = raw_config.get("batch_overview")
    raw_correction = raw_config.get("single_correction")
    if not isinstance(raw_batch, dict):
        raise OverviewChangeWorkflowConfigError("batch_overview 必须是 JSON 对象")
    if not isinstance(raw_correction, dict):
        raise OverviewChangeWorkflowConfigError("single_correction 必须是 JSON 对象")

    return {
        "schema_version": 1,
        "batch_overview": {
            "tool_roles": frozenset(
                _string_list(
                    raw_batch.get("tool_roles"),
                    "batch_overview.tool_roles",
                    allow_empty=True,
                )
            ),
            "allowed_project_states": tuple(
                _string_list(
                    raw_batch.get("allowed_project_states"),
                    "batch_overview.allowed_project_states",
                    allow_empty=True,
                )
            ),
            "prevent_self_approval": _bool_value(
                raw_batch,
                "prevent_self_approval",
                "batch_overview",
            ),
            "approval_role_targets": _approval_role_targets(
                raw_batch.get("approval_role_targets"),
                "batch_overview.approval_role_targets",
            ),
        },
        "single_correction": {
            "prevent_self_approval": _bool_value(
                raw_correction,
                "prevent_self_approval",
                "single_correction",
            ),
            "approval_role_targets": _approval_role_targets(
                raw_correction.get("approval_role_targets"),
                "single_correction.approval_role_targets",
            ),
        },
    }


OVERVIEW_CHANGE_WORKFLOW_CONFIG = load_overview_change_workflow_config()
