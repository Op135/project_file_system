"""转产项目的必填概述进度统计。"""

import logging
from dataclasses import dataclass
from typing import Mapping

from nicegui import app

from .access_control import can
from . import db_storage
from .permission_catalog import PROJECT_TRIAL_PROGRESS_VIEW_PERMISSION


BLOCKING_STATES = {"缺必填", "有待定"}
IMPORTANCE_STORAGE_KEY = "trial_progress_importance"
logger = logging.getLogger(__name__)


def can_view_trial_progress(username: str, *, user_service=None) -> bool:
    """只有被授权的用户才能查看跨项目转产进度。"""
    service = user_service or getattr(app.state, "user_service", None)
    return can(service, username, PROJECT_TRIAL_PROGRESS_VIEW_PERMISSION)


def normalize_importance(raw: object) -> dict[str, int]:
    """忽略损坏或过期的标记值，避免影响进度页。"""
    if not isinstance(raw, dict):
        return {}
    return {
        project: level
        for project, level in raw.items()
        if isinstance(project, str) and type(level) is int and 1 <= level <= 5
    }


async def get_latest_importance() -> dict[str, int]:
    return normalize_importance(await db_storage.get_fresh_item(IMPORTANCE_STORAGE_KEY, {}))


async def set_project_importance(project: str, level: int, *, actor: str) -> bool:
    """在最新 SQLite 值上原子更新单项目标记，避免多人覆盖其它项目。"""
    if not can_view_trial_progress(actor) or type(level) is not int or not 0 <= level <= 5:
        return False
    applied = False

    def update(raw: object) -> object:
        nonlocal applied
        summary = app.storage.general.get("project_summary", {}).get(project)
        recorded = app.storage.general.get("project_req_max_ver", {})
        if not isinstance(summary, dict) or summary.get("state") != "转产" or recorded.get(project) is None:
            return db_storage.ATOMIC_NO_UPDATE
        values = normalize_importance(raw)
        if level:
            values[project] = level
        else:
            values.pop(project, None)
        applied = True
        return values

    return await db_storage.atomic_deep_update([IMPORTANCE_STORAGE_KEY], update) and applied


async def clear_project_importance(project: str) -> bool:
    """项目离开转产后清理其持久化标记。"""
    def remove(raw: object) -> object:
        values = normalize_importance(raw)
        if project not in values:
            return db_storage.ATOMIC_NO_UPDATE
        values.pop(project)
        return values

    return await db_storage.atomic_deep_update([IMPORTANCE_STORAGE_KEY], remove)


async def prune_inactive_importance(project_summary: Mapping[str, object]) -> bool:
    """清理不再处于转产状态的旧标记，覆盖外部状态变更或之前清理失败。"""
    def is_known_inactive(project: str) -> bool:
        summary = project_summary.get(project)
        return isinstance(summary, dict) and summary.get("state") != "转产"

    current = await get_latest_importance()
    if not any(is_known_inactive(project) for project in current):
        return True

    def remove_inactive(raw: object) -> object:
        values = normalize_importance(raw)
        kept = {
            project: level
            for project, level in values.items()
            if not is_known_inactive(project)
        }
        return kept if kept != values else db_storage.ATOMIC_NO_UPDATE

    success = await db_storage.atomic_deep_update([IMPORTANCE_STORAGE_KEY], remove_inactive)
    if not success:
        logger.error("清理已离开转产状态的项目重要程度失败")
    return success


@dataclass(frozen=True)
class RoleProgress:
    role: str
    assignee: str
    missing: tuple[tuple[str, str], ...]
    skipped: bool = False


@dataclass(frozen=True)
class ProjectProgress:
    project: str
    completed: int
    total: int
    roles: tuple[RoleProgress, ...]

    @property
    def percentage(self) -> int:
        if not self.total:
            return 100
        if self.completed == self.total:
            return 100
        return min(99, round(100 * self.completed / self.total))


def build_trial_progress(
    project_summary: Mapping[str, object],
    recorded_projects: Mapping[str, object],
    overview_roles: Mapping[str, object],
    overview_config: Mapping[str, object],
    pending_by_user: Mapping[str, object],
) -> list[ProjectProgress]:
    """仅按必填概述的待办状态计算准入进度。"""
    results: list[ProjectProgress] = []
    for project, summary in sorted(project_summary.items()):
        if not isinstance(summary, dict) or summary.get("state") != "转产":
            continue
        if recorded_projects.get(project) is None:
            continue
        project_roles = overview_roles.get(project)
        if not isinstance(project_roles, dict):
            continue
        role_rows: list[RoleProgress] = []
        total = 0
        missing_count = 0
        for role, assignment in project_roles.items():
            if not isinstance(assignment, dict):
                continue
            raw_assignee = str(assignment.get("latest_user") or "").split("：")[-1].strip()
            if raw_assignee == "——":
                role_rows.append(RoleProgress(str(role), "——", (), True))
                continue
            assignee = raw_assignee or "待定"
            role_config = overview_config.get(role)
            required: dict[str, str] = {}
            if isinstance(role_config, dict):
                for entries in role_config.values():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        label = entry.get("label")
                        if isinstance(label, str) and entry.get("nature") == "必填":
                            required[label] = str(entry.get("title") or label)
            user_pending = pending_by_user.get(raw_assignee or "待定负责人", {})
            project_pending = user_pending.get(project, {}) if isinstance(user_pending, dict) else {}
            if not isinstance(project_pending, dict):
                project_pending = {}
            missing: list[tuple[str, str]] = []
            for label, title in required.items():
                state = project_pending.get(label)
                if assignee == "待定" or state in BLOCKING_STATES:
                    missing.append((title, str(state or "缺必填")))
            total += len(required)
            missing_count += len(missing)
            role_rows.append(RoleProgress(str(role), assignee, tuple(missing)))
        results.append(ProjectProgress(str(project), total - missing_count, total, tuple(role_rows)))
    return results
