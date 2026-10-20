"""项目状态统一迁移、阶段校验和企业微信通知服务。"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from nicegui import app

from . import db_storage
from . import overview_storage
from .config import BASE_DIR, OVERVIEW_EXEMPTION_CHECK_LIST, PROJECT_STATE_LIST
from .notification_recipients import resolve_permission_wecom_recipients
from .notification_content import build_compact_text_notification
from .overview_conversion_review import (
    build_conversion_preview,
    conversion_target_id,
    reconcile_conversion_reviews,
)
from .overview_operation import append_overview_timestamp, get_automatic_overview_reason
from .permission_catalog import (
    PROJECT_STATUS_RD_TO_TRANSFER_NOTIFY_PERMISSION,
    PROJECT_STATUS_TRANSFER_TO_TRIAL_NOTIFY_PERMISSION,
    PROJECT_STATUS_TRIAL_TO_MASS_NOTIFY_PERMISSION,
)
from .project_access import can_transition_project_status
from .wecom_service import send_wecom_text_message

logger = logging.getLogger(__name__)

PROJECT_SUMMARY_PATH = Path(BASE_DIR) / "data" / "project_summary.json"
PROJECT_STATE_NOTIFICATION_PERMISSIONS = {
    ("研发", "转产"): PROJECT_STATUS_RD_TO_TRANSFER_NOTIFY_PERMISSION,
    ("转产", "试产"): PROJECT_STATUS_TRANSFER_TO_TRIAL_NOTIFY_PERMISSION,
    ("试产", "量产"): PROJECT_STATUS_TRIAL_TO_MASS_NOTIFY_PERMISSION,
}

_project_transition_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(frozen=True)
class ProjectStateTransitionCheck:
    ok: bool
    message: str = ""
    previous_state: str = ""
    target_state: str = ""
    missing_roles: tuple[str, ...] = ()
    permission_issues: tuple[str, ...] = ()
    trial_blockers: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectStateTransitionResult:
    ok: bool
    message: str
    previous_state: str = ""
    target_state: str = ""
    changed: bool = False
    notification_warning: str = ""


def is_valid_project_state_path(previous_state: object, target_state: object) -> bool:
    """复用既有状态机：禁止跨阶段和生产阶段逆向切换。"""
    previous = str(previous_state or "")
    target = str(target_state or "")
    if not previous or target not in PROJECT_STATE_LIST or previous == target:
        return False
    forbidden = (
        (previous in {"作废", "待定"} and target in {"转产", "试产", "量产"})
        or (previous == "研发" and target in {"试产", "量产"})
        or (previous == "转产" and target == "量产")
        or (previous in {"试产", "量产"} and target == "转产")
        or (previous in {"转产", "试产", "量产"} and target in {"研发", "待定", "作废"})
    )
    return not forbidden


def collect_trial_transition_blockers(project_name: str) -> dict[str, tuple[str, ...]]:
    """返回阻止转产进入试产的负责人及概述 label。"""
    pending_value = app.storage.general.get("overview_charge_pending", {})
    if not isinstance(pending_value, dict):
        return {}
    blockers: dict[str, tuple[str, ...]] = {}
    for responsible, projects in pending_value.items():
        if not isinstance(projects, dict):
            continue
        states = projects.get(project_name)
        if not isinstance(states, dict):
            continue
        labels = tuple(
            str(label)
            for label, state in states.items()
            if state in {"缺必填", "有待定"} and label not in OVERVIEW_EXEMPTION_CHECK_LIST
        )
        if labels:
            blockers[str(responsible)] = labels
    return blockers


def check_project_state_transition(
    project_name: str,
    target_state: str,
    *,
    actor: str,
    current_role: object = "",
    user_service=None,
) -> ProjectStateTransitionCheck:
    """在最新缓存状态上检查权限、合法路径和阶段业务前置条件。"""
    project_value = app.storage.general.get("project_summary", {}).get(project_name)
    if not isinstance(project_value, dict):
        return ProjectStateTransitionCheck(False, "项目不存在或项目资料尚未加载")
    previous_state = str(project_value.get("state") or "")
    target = str(target_state or "")
    if previous_state == target:
        return ProjectStateTransitionCheck(
            True,
            "项目状态没有变化",
            previous_state,
            target,
        )
    if not can_transition_project_status(
        current_role,
        actor,
        previous_state,
        target,
        user_service=user_service,
    ):
        return ProjectStateTransitionCheck(
            False,
            "当前账号没有执行该状态切换的权限",
            previous_state,
            target,
        )
    if not is_valid_project_state_path(previous_state, target):
        return ProjectStateTransitionCheck(
            False,
            f"禁止从{previous_state}直接切换为{target}",
            previous_state,
            target,
        )
    if (previous_state, target) == ("研发", "转产"):
        version_value = app.storage.general.get("project_req_max_ver", {}).get(project_name)
        if version_value is None:
            return ProjectStateTransitionCheck(
                False,
                "当前项目没有可用的最高需求版本，不能生成转产确认待办",
                previous_state,
                target,
            )
        previews = build_conversion_preview(project_name, str(version_value))
        missing_roles = tuple(preview.role for preview in previews if not preview.assignee)
        permission_issues = tuple(
            f"{preview.role}（{'、'.join(preview.permission_missing_titles)}）"
            for preview in previews
            if preview.permission_missing_titles
        )
        if missing_roles:
            return ProjectStateTransitionCheck(
                False,
                f"以下职能没有负责人：{'、'.join(missing_roles)}",
                previous_state,
                target,
                missing_roles=missing_roles,
            )
        if permission_issues:
            return ProjectStateTransitionCheck(
                False,
                f"以下负责人缺少对应概述维护权限：{'；'.join(permission_issues)}",
                previous_state,
                target,
                permission_issues=permission_issues,
            )
    if (previous_state, target) == ("转产", "试产"):
        blockers = collect_trial_transition_blockers(project_name)
        if blockers:
            return ProjectStateTransitionCheck(
                False,
                "项目仍有缺少必填或尚未确认的概述，不能进入试产",
                previous_state,
                target,
                trial_blockers=blockers,
            )
    return ProjectStateTransitionCheck(True, "可以切换", previous_state, target)


def _write_project_summary(summary: dict[str, Any]) -> None:
    PROJECT_SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = PROJECT_SUMMARY_PATH.with_name(
        f".{PROJECT_SUMMARY_PATH.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=4),
            encoding="utf-8",
        )
        os.replace(temporary_path, PROJECT_SUMMARY_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def _build_conversion_mutator(
    project_name: str,
    version: str,
    changed_labels: set[str],
    origins: dict[str, str],
):
    def set_pending(over_data):
        if not isinstance(over_data, dict):
            return db_storage.ATOMIC_NO_UPDATE
        changed_labels.clear()
        origins.clear()
        for label, label_value in over_data.items():
            if not isinstance(label_value, dict):
                continue
            label_changed = False
            for chip_id, chip in label_value.items():
                if not isinstance(chip, dict):
                    continue
                activation = chip.get("select_activ_dic")
                if not isinstance(activation, dict) or version not in activation:
                    continue
                current_state = activation.get(version)
                target_id = conversion_target_id(str(label), str(chip_id), version)
                if current_state is None:
                    origins[target_id] = "preexisting"
                should_convert = (
                    chip.get("type") == "svn"
                    and chip.get("warehouse") != "Product"
                    and current_state in {True, None}
                ) or (chip.get("type") != "svn" and current_state is True)
                if not should_convert:
                    continue
                if current_state is True:
                    origins[target_id] = "conversion"
                activation[version] = None
                chip["icon"] = "question_mark"
                chip["enabled"] = None
                chip["bg_color"] = "bg-amber-5"
                if chip.get("type") == "svn":
                    chip["warehouse"] = "Product"
                if current_state is True:
                    from .utils import get_overview_latest_responsible

                    append_overview_timestamp(
                        chip,
                        creator=get_overview_latest_responsible(project_name, str(label), chip),
                        reason=get_automatic_overview_reason("conversion_pending"),
                    )
                label_changed = True
            if label_changed:
                changed_labels.add(str(label))
        return over_data

    return set_pending


async def _publish_conversion_changes(
    project_name: str,
    changed_labels: set[str],
    origins: dict[str, str],
    actor: str,
) -> bool:
    from .components import OverviewVersionManager
    from .utils import overview_role_update, refresh_overview_pending_labels

    for label in changed_labels:
        OverviewVersionManager.bump(project_name, label)
    overview_role_update(project_name, "all_update")
    refresh_overview_pending_labels(project_name, changed_labels)
    return await reconcile_conversion_reviews(
        project_name,
        initial_origins=origins,
        transition_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        actor=actor,
    )


async def _send_transition_notification(
    project_name: str,
    previous_state: str,
    target_state: str,
    actor: str,
    changed_at: str,
) -> str:
    permission_code = PROJECT_STATE_NOTIFICATION_PERMISSIONS.get((previous_state, target_state))
    if not permission_code:
        return ""
    recipients = await resolve_permission_wecom_recipients(permission_code, fallback_touser="")
    if not recipients:
        warning = f"通知权限 {permission_code} 未匹配到已绑定企业微信成员"
        logger.warning(warning)
        return warning
    content = build_compact_text_notification(
        f"【项目】已进入{target_state}阶段",
        f"项目：{project_name}",
        f"状态：{previous_state} → {target_state}",
    )
    success, message = await send_wecom_text_message(
        content,
        recipients,
        module="project_status",
        business_key=f"{project_name}|{previous_state}|{target_state}|{changed_at}",
        message_type=f"{previous_state}_to_{target_state}",
    )
    if success:
        return ""
    logger.error(
        "项目状态变更通知发送失败: project=%s, transition=%s->%s, error=%s",
        project_name,
        previous_state,
        target_state,
        message,
    )
    return f"企业微信通知发送失败：{message}"


async def transition_project_state(
    project_name: str,
    target_state: str,
    *,
    actor: str,
    current_role: object = "",
    user_service=None,
    project_updates: dict[str, object] | None = None,
) -> ProjectStateTransitionResult:
    """执行唯一的项目状态迁移路径，并在成功后发送对应订阅通知。"""
    async with _project_transition_locks[project_name]:
        check = check_project_state_transition(
            project_name,
            target_state,
            actor=actor,
            current_role=current_role,
            user_service=user_service,
        )
        if not check.ok:
            return ProjectStateTransitionResult(
                False,
                check.message,
                check.previous_state,
                check.target_state,
            )
        if check.previous_state == check.target_state:
            return ProjectStateTransitionResult(
                True,
                check.message,
                check.previous_state,
                check.target_state,
                changed=False,
            )

        try:
            raw_summary = json.loads(PROJECT_SUMMARY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("读取项目汇总失败", exc_info=True)
            return ProjectStateTransitionResult(False, f"读取项目汇总失败：{exc}")
        if not isinstance(raw_summary, dict):
            return ProjectStateTransitionResult(False, "项目汇总数据格式错误")
        current_entry = raw_summary.get(project_name)
        if not isinstance(current_entry, dict):
            return ProjectStateTransitionResult(False, "项目不存在")
        live_previous_state = str(current_entry.get("state") or "")
        if live_previous_state != check.previous_state:
            return ProjectStateTransitionResult(False, "项目状态已被其他用户修改，请刷新后重试")

        old_entry = copy.deepcopy(current_entry)
        if project_updates:
            for key, value in project_updates.items():
                if key not in {"state", "project_name", "sub_project", "project"}:
                    current_entry[key] = copy.deepcopy(value)
        current_entry["state"] = check.target_state
        try:
            _write_project_summary(raw_summary)
        except (OSError, TypeError) as exc:
            logger.error("写入项目状态失败", exc_info=True)
            return ProjectStateTransitionResult(False, f"写入项目状态失败：{exc}")
        cached_entry_value = app.storage.general.setdefault("project_summary", {}).get(project_name)
        old_cached_entry = copy.deepcopy(cached_entry_value) if isinstance(cached_entry_value, dict) else {}
        cached_entry = copy.deepcopy(old_cached_entry)
        cached_entry.update(copy.deepcopy(current_entry))
        app.storage.general["project_summary"][project_name] = cached_entry

        review_success = True
        if (check.previous_state, check.target_state) == ("研发", "转产"):
            version_value = app.storage.general.get("project_req_max_ver", {}).get(project_name)
            if version_value is None:
                raw_summary[project_name] = old_entry
                _write_project_summary(raw_summary)
                app.storage.general["project_summary"][project_name] = old_cached_entry
                return ProjectStateTransitionResult(False, "当前项目没有可用的最高需求版本")
            changed_labels: set[str] = set()
            origins: dict[str, str] = {}
            try:
                changed = await overview_storage.atomic_deep_update(
                    [f"{project_name}_over_data"],
                    _build_conversion_mutator(
                        project_name,
                        str(version_value),
                        changed_labels,
                        origins,
                    ),
                )
            except Exception:
                logger.error("转产概述状态更新异常: project=%s", project_name, exc_info=True)
                changed = False
            if not changed:
                raw_summary[project_name] = old_entry
                _write_project_summary(raw_summary)
                app.storage.general["project_summary"][project_name] = old_cached_entry
                return ProjectStateTransitionResult(False, "转产概述状态更新失败，项目状态已恢复")
            try:
                review_success = await _publish_conversion_changes(
                    project_name,
                    changed_labels,
                    origins,
                    actor,
                )
            except Exception:
                logger.error("转产职能汇总待办生成异常: project=%s", project_name, exc_info=True)
                review_success = False
            if not review_success:
                logger.error("项目已转产但职能汇总待办生成失败: project=%s", project_name)

        changed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            notification_warning = await _send_transition_notification(
                project_name,
                check.previous_state,
                check.target_state,
                actor,
                changed_at,
            )
        except Exception as exc:
            logger.error("项目状态变更通知异常: project=%s", project_name, exc_info=True)
            notification_warning = f"企业微信通知异常：{exc}"
        if (
            (check.previous_state, check.target_state) == ("研发", "转产")
            and not review_success
        ):
            workflow_warning = "转产职能汇总待办生成失败，请联系管理员处理"
            notification_warning = "；".join(
                value for value in (workflow_warning, notification_warning) if value
            )
        message = f"项目状态已从{check.previous_state}切换为{check.target_state}"
        if notification_warning:
            message += "；状态已生效，但存在后续处理警告"
        return ProjectStateTransitionResult(
            True,
            message,
            check.previous_state,
            check.target_state,
            changed=True,
            notification_warning=notification_warning,
        )


async def update_project_details(
    project_name: str,
    updates: dict[str, object],
) -> tuple[bool, str]:
    """与状态迁移共用项目锁更新基础资料，始终保留文件中的最新状态。"""
    async with _project_transition_locks[project_name]:
        try:
            raw_summary = json.loads(PROJECT_SUMMARY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("读取项目汇总失败", exc_info=True)
            return False, f"读取项目汇总失败：{exc}"
        if not isinstance(raw_summary, dict):
            return False, "项目汇总数据格式错误"
        current_entry = raw_summary.get(project_name)
        if not isinstance(current_entry, dict):
            return False, f"项目 {project_name} 不存在，无法修改"
        for key, value in updates.items():
            if key not in {"state", "project_name", "sub_project", "project"}:
                current_entry[key] = copy.deepcopy(value)
        try:
            _write_project_summary(raw_summary)
        except (OSError, TypeError) as exc:
            logger.error("写入项目基础资料失败", exc_info=True)
            return False, f"写入项目基础资料失败：{exc}"
        cached_value = app.storage.general.setdefault("project_summary", {}).get(project_name)
        cached_entry = copy.deepcopy(cached_value) if isinstance(cached_value, dict) else {}
        cached_entry.update(copy.deepcopy(current_entry))
        app.storage.general["project_summary"][project_name] = cached_entry
        return True, project_name
