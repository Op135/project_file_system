"""项目待办工作台入口与个人概述待办范围判断。"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from nicegui import app

from .access_control import can
from .overview_warning import get_urgent_overview_projects
from .permission_catalog import (
    PROJECT_REQUIREMENT_TODO_VIEW_PERMISSION,
    PROJECT_TODO_VIEW_PERMISSION,
)
from .project_overview_access import can_edit_overview_item


PROJECT_TODO_LEGACY_VIEW_KEYWORDS = ("销售", "研发", "工程", "质量", "boss", "admin")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_view_project_overview_todo(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断用户能否进入项目概述待办。"""
    role = str(current_role or "")
    legacy_allowed = any(keyword in role for keyword in PROJECT_TODO_LEGACY_VIEW_KEYWORDS)
    return can(
        _service(user_service),
        current_user,
        PROJECT_TODO_VIEW_PERMISSION,
        legacy_role=role,
        legacy_allowed_roles=(role,) if legacy_allowed else (),
    )


def can_view_project_requirement_todo(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断用户能否进入项目需求待办。"""
    role = str(current_role or "")
    legacy_allowed = any(keyword in role for keyword in PROJECT_TODO_LEGACY_VIEW_KEYWORDS)
    return can(
        _service(user_service),
        current_user,
        PROJECT_REQUIREMENT_TODO_VIEW_PERMISSION,
        legacy_role=role,
        legacy_allowed_roles=(role,) if legacy_allowed else (),
    )


def can_view_project_todo(current_role: object, current_user: str, *, user_service=None) -> bool:
    """兼容旧调用；原项目待办权限现在仅代表项目概述待办。"""
    return can_view_project_overview_todo(
        current_role,
        current_user,
        user_service=user_service,
    )


def get_requirement_todo_keys_by_state(
    wait_review: object,
    *,
    current_user: str,
    can_edit_requirements: bool,
    can_review_requirement: Callable[[str], bool],
) -> dict[str, set[tuple[str, str]]]:
    """按首页角标口径归类当前用户真正需要处理的需求待办。"""
    result: dict[str, set[tuple[str, str]]] = {"待审": set(), "待修改": set()}
    if not isinstance(wait_review, Mapping):
        return result

    for project_name, versions in wait_review.items():
        if not isinstance(project_name, str) or not isinstance(versions, Mapping):
            continue
        for version, review_data in versions.items():
            if not isinstance(review_data, Mapping):
                continue
            state = review_data.get("state")
            key = (project_name, str(version))
            if state == "待修改":
                if can_edit_requirements and review_data.get("submitter") == current_user:
                    result["待修改"].add(key)
            elif state == "待审" and can_review_requirement(project_name):
                result["待审"].add(key)

    return result


def get_actionable_overview_urgent_projects(
    pending_projects: object,
    overview_config_flat: object,
    project_states: Mapping[str, str],
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> list[tuple[str, int]]:
    """首页概述提醒按本人权限及项目状态计算，不按旧角色隐藏。"""
    actionable = filter_actionable_overview_pending(
        pending_projects,
        overview_config_flat,
        current_role,
        current_user,
        user_service=user_service,
    )
    return get_urgent_overview_projects(actionable, project_states)


def get_default_requirement_review_state(
    requirement_todos: Mapping[str, object],
) -> str:
    """选择需求看板默认展开项；审批待办优先于本人返修待办。"""
    if requirement_todos.get("待审"):
        return "待审"
    if requirement_todos.get("待修改"):
        return "待修改"
    return "待审"


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
