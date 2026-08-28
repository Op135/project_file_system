"""项目待办工作台入口与个人概述待办范围判断。"""

from __future__ import annotations

from collections.abc import Mapping

from nicegui import app

from .access_control import can
from .permission_catalog import PROJECT_TODO_VIEW_PERMISSION
from .project_overview_access import can_edit_overview_item


PROJECT_TODO_LEGACY_VIEW_KEYWORDS = ("销售", "研发", "工程", "质量", "boss", "admin")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_view_project_todo(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断用户能否进入项目待办工作台。"""
    role = str(current_role or "")
    legacy_allowed = any(keyword in role for keyword in PROJECT_TODO_LEGACY_VIEW_KEYWORDS)
    return can(
        _service(user_service),
        current_user,
        PROJECT_TODO_VIEW_PERMISSION,
        legacy_role=role,
        legacy_allowed_roles=(role,) if legacy_allowed else (),
    )


def filter_actionable_overview_pending(
    pending_projects: object,
    overview_config_flat: object,
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> dict[str, dict]:
    """只保留当前用户拥有对应 label 维护权限的个人概述待办。"""
    if not isinstance(pending_projects, Mapping) or not isinstance(overview_config_flat, Mapping):
        return {}

    result: dict[str, dict] = {}
    for project_name, states in pending_projects.items():
        if not isinstance(states, Mapping):
            continue
        actionable_states = {}
        for label, state in states.items():
            config = overview_config_flat.get(label)
            if not isinstance(config, dict):
                continue
            if can_edit_overview_item(
                config,
                current_role,
                current_user,
                user_service=_service(user_service),
            ):
                actionable_states[str(label)] = state
        if actionable_states:
            result[str(project_name)] = actionable_states
    return result
