# -*- encoding: utf-8 -*-
import asyncio
import copy
import json
import logging
import mimetypes
import os
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from nicegui import app, ui

from .. import db_storage
from ..components import FileThumbnail, OverviewReasonSelector
from ..config import (
    BASE_DIR,
    IMG_DIR,
    OVER_DIR,
    PRESET_AVATARS,
    REQ_DIR,
    REQ_REMOVE_DIR,
    SYSTEM_PUBLIC_BASE_URL,
)
from ..issue_workflow_utils import schedule_background_task
from ..overview_batch_operations import (
    BATCH_OVERVIEW_REQUESTS_KEY,
    BATCH_OVERVIEW_STAGING_DIR,
    can_review_batch_overview_request,
    execute_batch_overview_request,
    get_batch_overview_pending_count,
    update_batch_overview_request,
)
from ..overview_add_approval import (
    OVERVIEW_ADD_REQUESTS_KEY,
    approve_overview_add_request,
    can_review_overview_add_request,
    get_overview_add_pending_count,
    reject_overview_add_request,
    withdraw_overview_add_request,
)
from ..overview_corrections import (
    OVERVIEW_CORRECTION_REQUESTS_KEY,
    archive_correction_request,
    build_correction_changes,
    can_review_correction_request,
    cleanup_correction_staged_files,
    configured_test_field_definitions,
    configured_test_display_values,
    execute_correction_request,
    get_correction_pending_count,
    update_correction_request,
    validate_staged_path,
)
from ..overview_conversion_review import (
    OPEN_REVIEW_STATUSES,
    collect_review_pending_items,
    get_actionable_conversion_reviews,
    get_conversion_covered_labels,
    get_conversion_reviews,
    process_conversion_review_batch,
    process_conversion_review_item,
    reconcile_conversion_reviews,
)
from ..overview_warning import get_overview_counts, get_overview_warning, sort_overview_pending_items
from ..project_overview_access import can_review_overview_correction
from ..project_requirement_access import (
    can_edit_project_requirement,
    can_manage_all_project_requirement_drafts,
    can_review_all_project_requirements,
    can_review_project_requirement,
    can_revoke_project_requirement_approval,
    has_assigned_requirement_review_permission,
)
from ..project_todo_access import (
    can_view_project_todo,
    filter_actionable_overview_pending,
    get_default_requirement_review_state,
    get_requirement_todo_keys_by_state,
)
from ..requirement_overview_impact import RequirementOverviewImpactConfigError
from ..utils import (
    delete_file,
    get_cache_busted_path,
    get_overviow_page,
    get_project_engineer_project_list_dic,
    get_requirement_overview_impacts,
    logout,
    move_file_with_timestamp_pathlib,
    prepare_requirement_version_tidy,
    project_summary_update,
    refresh_overview_pending_labels,
    restore_file_bytes,
    restore_overview_active_state,
    set_overview_active_state,
    set_project_custom_labels,
    setup_global_activity_tracking,
    snapshot_file_bytes,
    sync_current_user_role,
    validate_search_path,
    validate_svn_url,
)
from ..workflow_notifications import send_workflow_completion_cc

# 获取 logger
logger = logging.getLogger(__name__)

# 同一项目的审批必须串行，避免重复点击或不同客户端同时通过同一版本。
_requirement_review_locks = defaultdict(asyncio.Lock)
_batch_overview_review_locks = defaultdict(asyncio.Lock)
_overview_correction_review_locks = defaultdict(asyncio.Lock)
_correction_preview_routes: set[str] = set()


def _group_visible_requirement_reviews(
    wait_review: object,
    *,
    current_user: str,
    can_review_requirement: Callable[[str], bool],
    can_review_all: bool,
) -> dict[str, list[tuple[str, str]]]:
    """按状态归类当前用户可见的未完成需求评审记录。"""
    groups: dict[str, list[tuple[str, str]]] = {"待审": [], "待修改": []}
    if not isinstance(wait_review, dict):
        return groups

    for project_name, versions in wait_review.items():
        if not isinstance(project_name, str) or not isinstance(versions, dict):
            continue
        for version, review_data in versions.items():
            if not isinstance(review_data, dict):
                continue
            state = review_data.get("state")
            if state not in groups:
                continue

            is_submitter = review_data.get("submitter") == current_user
            is_project_reviewer = can_review_requirement(project_name)
            if can_review_all or is_submitter or (is_project_reviewer and state == "待审"):
                groups[state].append((project_name, str(version)))

    return groups


def _can_remove_requirement_review(
    review_record: object,
    *,
    current_user: str,
    can_edit_requirements: bool,
    reviewer_allowed: bool,
) -> bool:
    """提交人可撤销自己的未完成需求，审核人保留原移除权限。"""
    if not isinstance(review_record, dict) or review_record.get("state") not in {"待审", "待修改"}:
        return False
    return reviewer_allowed or (
        can_edit_requirements and review_record.get("submitter") == current_user
    )


def _schedule_overview_completion_cc(
    request: dict,
    *,
    subject: str,
    business_key: str,
    reviewer: str,
) -> None:
    assignment = request.get("workflow_assignment", {})
    if not isinstance(assignment, dict):
        return
    project = str(request.get("project") or request.get("project_name") or "—")
    schedule_background_task(
        send_workflow_completion_cc(
            assignment,
            title=f"【项目概述】{subject}已通过",
            lines=(
                f"项目：{project}",
                "结果：全部审批节点已通过",
                f"审批人：{reviewer}",
            ),
            link_url=f"{SYSTEM_PUBLIC_BASE_URL}/information",
            module="project_overview",
            business_key=f"{business_key}:completion_cc",
        ),
        f"{subject}完成抄送",
    )


def _get_draft_saved_timestamp(project_name, version, owner) -> float:
    """读取草稿保存时间；旧草稿没有时间戳时使用文件修改时间。"""
    file_path = Path(REQ_DIR) / "temp" / owner / f"{project_name}_需求配置_V{version}.json"
    try:
        with file_path.open(encoding="utf-8") as draft_file:
            draft = json.load(draft_file)
        saved_at = draft.get("req_timestamp") if isinstance(draft, dict) else None
        if saved_at:
            return datetime.fromisoformat(str(saved_at)).timestamp()
    except (OSError, ValueError, OverflowError):
        pass
    try:
        return file_path.stat().st_mtime
    except OSError:
        return float("-inf")


def _get_correction_media_preview(
    request_id: str,
    payload: dict,
    snapshot: dict,
    side: str,
) -> tuple[str, str, str] | None:
    """解析并注册纠错审批媒体预览，返回 URL、MIME 和真实本地路径。"""
    filename = Path(str(snapshot.get("content") or "")).name
    if not filename:
        return None

    staged_path: Path | None = None
    if side == "after" and payload.get("staged_file_path"):
        staged_ok, staged_path = validate_staged_path(str(payload.get("staged_file_path") or ""))
        if not staged_ok or staged_path is None:
            return None

    if staged_path is not None:
        local_path = staged_path
        audit_token = str(payload.get("staged_file_sha256") or "")
    else:
        config = payload.get("config") or {}
        upload_path_value = str(config.get("upload_path") or "").strip()
        if not upload_path_value:
            return None
        local_path = Path(upload_path_value) / filename
        audit_token = str(payload.get("original_file_sha256") or "") if side == "before" else ""

    if not local_path.is_file():
        return None
    if not audit_token:
        stat = local_path.stat()
        audit_token = f"{stat.st_mtime_ns:x}-{stat.st_size:x}"

    safe_request_id = re.sub(r"[^0-9A-Za-z_-]", "_", request_id)
    safe_token = re.sub(r"[^0-9A-Za-z_-]", "_", audit_token[:20])
    preview_url = f"/correction-preview/{safe_request_id}/{side}-{safe_token}{local_path.suffix.lower()}"
    if preview_url not in _correction_preview_routes:
        try:
            app.add_static_file(local_file=str(local_path), url_path=preview_url)
        except Exception:
            # 热重载或其它客户端可能已经注册了相同路径；此时继续复用既有路由。
            logger.debug("注册纠错文件预览路由失败或路由已存在: %s", preview_url, exc_info=True)
        _correction_preview_routes.add(preview_url)

    mime_type = str(
        (
            payload.get("uploaded_file_type")
            if side == "after" and staged_path is not None
            else snapshot.get("file_type")
        )
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    return preview_url, mime_type, str(local_path)


def _render_correction_media_previews(request_id: str, request: dict, payload: dict) -> None:
    """使用概述/需求共用的 FileThumbnail 呈现纠错前后的真实文件。"""
    before = payload.get("before_snapshot") or {}
    after = payload.get("after_snapshot") or {}
    preview_items = [("原文件", "before", before)]
    if request.get("action") == "correct" and after:
        preview_items.append(("纠错后文件", "after", after))

    ui.label("文件预览与下载").classes("font-bold text-gray-800")
    with ui.row().classes("w-full items-stretch gap-3 flex-wrap"):
        for title, side, snapshot in preview_items:
            with ui.card().classes("min-w-[260px] flex-1 p-3 shadow-sm border border-blue-100"):
                ui.label(title).classes("font-bold text-blue-900")
                preview = _get_correction_media_preview(request_id, payload, snapshot, side)
                if preview is None:
                    ui.label(f"{snapshot.get('content', '文件')} 不存在或暂存路径已失效").classes(
                        "text-sm text-red-700"
                    )
                    continue
                preview_url, mime_type, local_path = preview
                FileThumbnail(
                    file_url=preview_url,
                    file_type=mime_type,
                    file_name_suffix=str(snapshot.get("content") or Path(local_path).name),
                    file_lab="原" if side == "before" else "新",
                    display_lab="原" if side == "before" else "新",
                    parents_h=12,
                    delet_lab=False,
                    local_file_path=local_path,
                )


# --- UI 辅助组件 ---
def ui_card_header(title, icon="assignment", color="blue-500"):
    """统一的卡片标题样式"""
    with ui.row().classes("w-full items-center gap-2 pb-3 border-b border-gray-100 mb-3"):
        ui.icon(icon, color=color.replace("text-", "")).classes("text-xl")
        ui.label(title).classes("text-lg font-bold text-gray-800")


def status_badge(text: str | None, color_name: str = "gray"):
    """状态小标签"""
    normalized_text = str(text or "")
    # 简单的颜色映射
    colors = {
        "待审": ("orange-100", "orange-800"),
        "已审": ("green-100", "green-800"),
        "待修改": ("red-100", "red-800"),
        "研发": ("blue-100", "blue-800"),
        "pending": ("orange-100", "orange-800"),
        "rejected": ("red-100", "red-800"),
        "withdrawn": ("gray-100", "gray-800"),
        "approved": ("green-100", "green-800"),
        "approved_with_warnings": ("amber-100", "amber-800"),
        "processing": ("blue-100", "blue-800"),
        "failed": ("red-100", "red-800"),
    }
    display_text = {
        "pending": "待审批",
        "rejected": "已驳回",
        "withdrawn": "已撤回",
        "approved": "已通过",
        "approved_with_warnings": "已通过（部分异常）",
        "processing": "审批执行中",
        "failed": "执行失败",
    }.get(normalized_text, normalized_text)
    bg, fg = colors.get(normalized_text, (f"{color_name}-100", f"{color_name}-800"))
    ui.label(display_text).classes(f"text-xs px-2 py-0.5 rounded bg-{bg} text-{fg} font-medium")


@ui.page("/information")
def information_page():
    # 1. 权限与基础数据获取
    stored_current_user = app.storage.user.get("current_user")
    if not isinstance(stored_current_user, str) or not stored_current_user:
        ui.navigate.to("/login")
        return

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking("information")
    ui.add_css("""
        @keyframes overview-warning-shake {
            0%, 100% { transform: translateX(0); }
            5%, 35%, 55%, 85% { transform: translateX(-2px); }
            15%, 45%, 65%, 95% { transform: translateX(2px); }
        }

        @keyframes overview-warning-flash {
            0%, 100% {
                opacity: 1;
                transform: scale(1);
                text-shadow: 0 0 2px currentColor;
            }
            50% {
                opacity: 0.25;
                transform: scale(1.2);
                text-shadow: 0 0 8px currentColor;
            }
        }

        .overview-warning-shake {
            animation: overview-warning-shake 1.0s ease-in-out infinite;
            transform-origin: center;
            will-change: transform;
        }

        .overview-warning-flash {
            animation: overview-warning-flash 1.6s ease-in-out infinite;
            will-change: opacity, transform;
        }

        .overview-warning-number {
            display: inline-block;
        }

        @media (prefers-reduced-motion: reduce) {
            .overview-warning-shake,
            .overview-warning-flash {
                animation: none;
            }
        }
    """)

    dialog = ui.dialog().props("persistent").classes("")
    conversion_review_dialog = ui.dialog().props("persistent")
    current_user = stored_current_user
    current_role = sync_current_user_role()
    if not can_view_project_todo(current_role, current_user):
        ui.notify("当前账号没有查看项目待办工作台的权限", type="negative")
        ui.navigate.to("/main")
        return

    def current_project_engineers() -> dict:
        return app.storage.general.get("project_engineer", {}) or {}

    def can_review_requirement(project_name: str) -> bool:
        """复核稳定审批资格及当前项目工程师的具体责任。"""
        return can_review_project_requirement(
            current_role,
            current_user,
            project_name,
            current_project_engineers(),
        )

    def can_review_all_requirements() -> bool:
        return can_review_all_project_requirements(current_role, current_user)

    def can_edit_requirements() -> bool:
        return can_edit_project_requirement(current_role, current_user)

    def can_remove_requirement(p_name, v) -> bool:
        review_record = app.storage.general.get("wait_review", {}).get(p_name, {}).get(v)
        return _can_remove_requirement_review(
            review_record,
            current_user=current_user,
            can_edit_requirements=can_edit_requirements(),
            reviewer_allowed=can_review_requirement(p_name),
        )

    # 头像处理
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])
    current_display_path = get_cache_busted_path(current_avatar_path)

    # -------------------------------------------------------------------------
    # 业务逻辑函数 (保持原有逻辑核心，适配新UI容器)
    # -------------------------------------------------------------------------

    def set_review_revise(p_name, v):
        review_record = app.storage.general.get("wait_review", {}).get(p_name, {}).get(v, {})
        is_submitter = review_record.get("submitter") == current_user and can_edit_requirements()
        if not is_submitter and not can_review_requirement(p_name):
            ui.notify("当前账号无权退回或申请修改该需求", type="negative")
            return
        app.storage.general["wait_review"][p_name][v]["state"] = "待修改"
        render_requirement_review_board.refresh()

    async def set_review_pass(container_row, p_name, v):
        """审核通过逻辑"""
        if not can_review_requirement(p_name):
            ui.notify("当前账号不是该项目需求配置的审批人", type="negative")
            return
        async with _requirement_review_locks[p_name]:
            current_record = app.storage.general.get("wait_review", {}).get(p_name, {}).get(v, {})
            if current_record.get("state") != "待审":
                ui.notify("需求非待审状态，无法通过，已刷新列表", type="warning")
                refresh_review_row(container_row, p_name, v)
                return

            candidate_path = ""
            official_path = Path(OVER_DIR) / f"{p_name}_概述整理.json"
            try:
                # 先整理成不可见候选文件；正式概述文件此时完全不动。
                candidate_path, candidate_data = await prepare_requirement_version_tidy(p_name, v)
                if not candidate_path:
                    raise RuntimeError("需求概述候选文件整理失败")

                affected_labels, missing_node_ids, change_node_ids = get_requirement_overview_impacts(
                    candidate_data,
                    v,
                    p_name,
                )
                logger.info(
                    "需求审批影响范围已解析: project=%s, version=%s, changes=%s, affected_labels=%s, "
                    "unmapped_node_ids=%s",
                    p_name,
                    v,
                    {key: sorted(value) for key, value in change_node_ids.items()},
                    sorted(affected_labels),
                    sorted(missing_node_ids),
                )
            except (RequirementOverviewImpactConfigError, RuntimeError, OSError, ValueError) as exc:
                if candidate_path:
                    Path(candidate_path).unlink(missing_ok=True)
                logger.error(
                    "审核通过前整理需求概述或解析影响配置失败: project=%s, version=%s", p_name, v, exc_info=True
                )
                ui.notify(
                    f"需求概述整理失败，已中止审批通过：{exc}",
                    type="negative",
                    position="center",
                    timeout=0,
                    close_button="✖",
                )
                return

            try:
                official_existed, official_content = snapshot_file_bytes(official_path)
            except OSError as exc:
                Path(candidate_path).unlink(missing_ok=True)
                logger.error("读取正式概述文件快照失败: %s", official_path, exc_info=True)
                ui.notify(f"无法建立正式概述文件回滚点，审批已中止：{exc}", type="negative", position="center")
                return

            overview_rollback_context = {}
            try:
                overview_success, changed_labels = await set_overview_active_state(
                    p_name,
                    v,
                    affected_labels,
                    rollback_context=overview_rollback_context,
                )
            except Exception as exc:
                Path(candidate_path).unlink(missing_ok=True)
                logger.error("审核通过前更新概述激活状态异常: project=%s, version=%s", p_name, v, exc_info=True)
                ui.notify(f"概述状态更新异常，审批未生效：{exc}", type="negative", position="center")
                return
            if not overview_success:
                Path(candidate_path).unlink(missing_ok=True)
                logger.error("审核通过前更新概述激活状态失败: project=%s, version=%s", p_name, v)
                ui.notify(
                    "概述状态更新失败，候选概述文件已丢弃，审批未生效。",
                    type="negative",
                    position="center",
                    timeout=0,
                    close_button="✖",
                )
                return

            overview_before = overview_rollback_context.get("before", {})
            overview_after = overview_rollback_context.get("after", overview_before)
            review_record_before = copy.deepcopy(current_record)
            max_versions = app.storage.general.setdefault("project_req_max_ver", {})
            max_version_existed = p_name in max_versions
            max_version_before = max_versions.get(p_name)
            checked_versions = app.storage.general.setdefault("overview_active_state_checked_versions", {})
            checked_version_existed = p_name in checked_versions
            checked_version_before = checked_versions.get(p_name)

            try:
                # 候选文件与正式文件位于同一目录，替换动作在文件系统层面是原子的。
                os.replace(candidate_path, official_path)
                app.storage.general["wait_review"][p_name][v]["state"] = "已审"
                app.storage.general["wait_review"][p_name][v]["pass_time"] = datetime.now().isoformat()
                max_versions[p_name] = v
                checked_versions[p_name] = v
            except Exception as exc:
                logger.error("发布审批结果失败，开始补偿回滚: project=%s, version=%s", p_name, v, exc_info=True)
                file_rollback_success = True
                try:
                    restore_file_bytes(official_path, official_existed, official_content)
                except Exception:
                    file_rollback_success = False
                    logger.critical("正式概述文件补偿回滚失败: %s", official_path, exc_info=True)

                state_rollback_success = True
                try:
                    app.storage.general["wait_review"][p_name][v] = review_record_before
                    if max_version_existed:
                        max_versions[p_name] = max_version_before
                    else:
                        max_versions.pop(p_name, None)
                    if checked_version_existed:
                        checked_versions[p_name] = checked_version_before
                    else:
                        checked_versions.pop(p_name, None)
                except Exception:
                    state_rollback_success = False
                    logger.critical(
                        "需求审批状态补偿回滚失败: project=%s, version=%s",
                        p_name,
                        v,
                        exc_info=True,
                    )

                overview_rollback_success = await restore_overview_active_state(
                    p_name,
                    overview_before,
                    overview_after,
                )
                Path(candidate_path).unlink(missing_ok=True)
                rollback_message = "审批发布失败，所有已完成步骤均已回滚。"
                if not file_rollback_success or not state_rollback_success or not overview_rollback_success:
                    rollback_message = "审批发布失败，且自动回滚不完整，请立即联系管理员检查。"
                ui.notify(
                    f"{rollback_message}错误：{exc}",
                    type="negative",
                    position="center",
                    timeout=0,
                    close_button="✖",
                )
                return

            # 下面都是已提交后的派生缓存刷新，不再参与审批事务。
            try:
                if changed_labels:
                    from ..components import OverviewVersionManager

                    for label in changed_labels:
                        OverviewVersionManager.bump(p_name, label)
                refresh_overview_pending_labels(p_name, affected_labels)
                Path(OVER_DIR, f"{p_name}_概述整理_temp.json").unlink(missing_ok=True)
                set_project_custom_labels(p_name)
            except Exception:
                logger.error(
                    "需求审批已成功，但派生缓存刷新失败: project=%s, version=%s",
                    p_name,
                    v,
                    exc_info=True,
                )

            # 刷新整个看板，使记录按最新状态重新归类并同步空状态。
            render_requirement_review_board.refresh()
            dialog.close()

    async def set_temporary_project_review_pass(container_row, p_name, v, data):
        if not can_review_requirement(p_name):
            ui.notify("当前账号不是该项目需求配置的审批人", type="negative")
            return
        if data.get("introduction").strip() and data.get("customer").strip():
            temp_data = {
                p_name: {
                    "state": "研发",
                    "model_notes": data.get("notes").strip(),
                    "creation_date": datetime.now().strftime("%Y-%m-%d"),
                    "introduction": data.get("introduction").strip(),
                    "customer": data.get("customer").strip(),
                }
            }
            # 更新 project_summary
            project_data = {}
            try:
                with open(f"{BASE_DIR}/data/project_summary.json", "r", encoding="utf-8") as f:
                    project_data = json.load(f)
            except FileNotFoundError:
                pass
            project_data.update(temp_data)
            with open(f"{BASE_DIR}/data/project_summary.json", "w", encoding="utf-8") as f:
                json.dump(project_data, f, indent=4, ensure_ascii=False)

            project_summary_update()
            await set_review_pass(container_row, p_name, v)
        else:
            ui.notify("项目简介与客户简称必须填写!", type="warning", position="bottom", close_button="✖")

    async def set_temporary_project_dialog(container_row, p_name, v):
        if not can_review_requirement(p_name):
            ui.notify("当前账号不是该项目需求配置的审批人", type="negative")
            return
        if "RFTS" in p_name and p_name not in app.storage.general["project_summary"]:
            dialog.clear()
            with dialog, ui.card().classes("w-full max-w-lg"):
                ui.label("🆕 新建项目补全信息").classes("text-lg font-bold mb-2")
                pro_data = {"notes": "", "introduction": "", "customer": ""}

                ui.input(label="项目备注", placeholder="选填").bind_value(pro_data, "notes").classes("w-full")
                ui.textarea(label="项目简介", placeholder="必填").bind_value(pro_data, "introduction").classes("w-full")
                ui.input(label="项目客户", placeholder="必填").bind_value(pro_data, "customer").classes("w-full")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button("取消", on_click=dialog.close).props("flat color=grey")
                    ui.button(
                        "确认创建",
                        color="primary",
                        on_click=lambda: set_temporary_project_review_pass(container_row, p_name, v, pro_data),
                    )
            dialog.open()
        else:
            await set_review_pass(container_row, p_name, v)

    async def set_review_pass_dialog(container_row, p_name, v):
        """点击审核通过的入口"""
        if not can_review_requirement(p_name):
            ui.notify("当前账号不是该项目需求配置的审批人", type="negative")
            return
        current_state = app.storage.general["wait_review"][p_name].get(v, {}).get("state")

        if current_state == "待审":
            old_v = "1.0" if v == "1.0" else f"{int(float(v)) - 1}.0"
            new_submitter = app.storage.general["wait_review"][p_name].get(v, {}).get("submitter")
            old_submitter = app.storage.general["wait_review"][p_name].get(old_v, {}).get("submitter")

            if new_submitter != old_submitter:
                dialog.clear()
                with dialog, ui.card():
                    ui.label("⚠️ 提交人变更提醒").classes("text-lg font-bold text-orange-600")
                    ui.label(f"提交人从 {old_submitter} 变更为 {new_submitter}，是否继续？")
                    with ui.row().classes("w-full justify-end mt-4"):
                        ui.button("取消", on_click=dialog.close).props("flat")
                        ui.button(
                            "继续通过",
                            color="red",
                            on_click=lambda: set_temporary_project_dialog(container_row, p_name, v),
                        )
                dialog.open()
            else:
                dialog.clear()
                with dialog, ui.card():
                    ui.label("⚠️ 确认通过该项目需求内容的评审吗？").classes("text-lg font-bold text-orange-600")
                    with ui.row().classes("w-full justify-end mt-4"):
                        ui.button("取消", on_click=dialog.close).props("flat")
                        ui.button(
                            "继续通过",
                            color="red",
                            on_click=lambda: set_temporary_project_dialog(container_row, p_name, v),
                        )
                dialog.open()
        else:
            ui.notify("需求非待审状态，无法通过，已刷新列表", type="warning")
            refresh_review_row(container_row, p_name, v)

    def remove_requirement_file(container_row, p_name, v):
        if not can_remove_requirement(p_name, v):
            ui.notify("当前账号无权移除该需求记录，或记录状态已经变化", type="negative")
            return
        move_file_with_timestamp_pathlib(f"{REQ_DIR}/{p_name}_需求配置_V{v}.json", REQ_REMOVE_DIR)
        delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")
        app.storage.general["wait_review"][p_name].pop(v, None)
        render_requirement_review_board.refresh()
        dialog.close()

    def remove_requirement_dialog(container_row, p_name, v):
        if not can_remove_requirement(p_name, v):
            ui.notify("当前账号无权移除该需求记录，或记录状态已经变化", type="negative")
            return
        review_record = app.storage.general.get("wait_review", {}).get(p_name, {}).get(v, {})
        is_own_submission = review_record.get("submitter") == current_user
        action_text = "撤销并移除" if is_own_submission else "移除"
        dialog.clear()
        with dialog, ui.card():
            ui.label("⚠️ 危险操作").classes("text-lg font-bold text-red-600")
            ui.label(f"确认{action_text} {p_name}_V{v} 吗？移除后需联系管理员恢复。")
            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button(
                    f"确认{action_text}",
                    color="red",
                    on_click=lambda: remove_requirement_file(container_row, p_name, v),
                )
        dialog.open()

    def get_requirement_page(project_name, ver):
        file_path = os.path.join(REQ_DIR, f"{project_name}_需求配置_V{ver}.json")
        ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    def get_req_page(project_name, version, owner_username=None):
        """打开草稿；查看他人草稿时强制使用只读模式。"""
        owner = str(owner_username or current_user)
        file_path = os.path.join(REQ_DIR, f"temp/{owner}/{project_name}_需求配置_V{version}.json")
        readonly_query = "&readonly=1" if owner != current_user else ""
        ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}{readonly_query}")

    def dele_temp_req_row(container_row, project_name, version):
        """删除暂存记录的行"""
        if not can_edit_requirements():
            ui.notify("当前账号无权删除需求草稿", type="negative")
            return
        try:
            app.storage.general["temp_req"][current_user][project_name].remove(version)
            file_path = Path(os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_需求配置_V{version}.json"))
            file_path.unlink(missing_ok=True)
            container_row.delete()
            ui.notify("已移除暂存记录", type="positive")
        except Exception as e:
            logger.error(f"删除失败: {e}")
            ui.notify("删除失败", type="negative")

    # --- 核心UI渲染逻辑：单行刷新 ---

    def refresh_review_row(container, project_name, ver):
        """
        刷新单个评审条目的UI。
        如果状态变为'已审'，则删除该行；否则重新渲染按钮。
        """
        # 1. 获取最新状态
        try:
            review_data = app.storage.general["wait_review"][project_name][ver]
            review_state = review_data.get("state")
            submitter = review_data.get("submitter")
        except (KeyError, TypeError):
            container.delete()  # 数据丢失，删除UI
            return

        can_review = can_review_requirement(project_name)
        can_review_all = can_review_all_requirements()
        is_submitter = submitter == current_user
        should_show = (can_review_all or is_submitter) and review_state in {"待审", "待修改"}
        should_show = should_show or (can_review and review_state == "待审")
        if not should_show:
            container.delete()
            return

        # 3. 重新渲染内容
        container.clear()
        with container:
            # --- 卡片布局 ---
            with ui.card().classes(
                "w-full p-3 border-l-4 border-l-blue-500 shadow-sm hover:shadow-md transition-shadow duration-300 bg-white"
            ):
                with ui.row().classes("w-full justify-between items-center wrap gap-2"):
                    # 左侧：信息展示
                    with ui.column().classes("gap-1"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(project_name).classes("font-bold text-gray-800 text-base")
                            project_engineer = app.storage.general.get("project_engineer", {}).get(
                                project_name, "未指定"
                            )
                            status_badge(f"V{ver}", "blue")
                            status_badge(review_state)
                        ui.label(f"提交人: {submitter}，项目工程师：{project_engineer}").classes(
                            "text-xs text-gray-500"
                        )

                    # 右侧：操作按钮组
                    with ui.row().classes("items-center gap-2"):
                        # 权限判断
                        if can_review:
                            # 审核者视角
                            ui.button(icon="visibility", on_click=lambda: get_overviow_page(project_name, True)).props(
                                "flat round dense text-color=grey-7"
                            ).tooltip("查看需求详情")

                            if review_state == "待审":
                                ui.button(
                                    icon="check",
                                    color="green",
                                    on_click=lambda: set_review_pass_dialog(container, project_name, ver),
                                ).props("flat round dense").tooltip("审核通过")

                                ui.button(
                                    icon="edit_note",
                                    color="orange",
                                    on_click=lambda: set_review_revise(project_name, ver),
                                ).props("flat round dense").tooltip("退回修改")

                            ui.button(
                                icon="delete",
                                color="red",
                                on_click=lambda: remove_requirement_dialog(container, project_name, ver),
                            ).props("flat round dense").tooltip("移除记录")
                        else:
                            # 普通提交者视角
                            ui.button(icon="visibility", on_click=lambda: get_overviow_page(project_name, True)).props(
                                "flat round dense text-color=grey-7"
                            ).tooltip("查看需求详情")
                            if is_submitter and review_state == "待修改":
                                ui.button(
                                    icon="edit_note",
                                    color="blue",
                                    on_click=lambda: get_requirement_page(project_name, ver),
                                ).props("flat round dense").tooltip("配置需求")
                            elif is_submitter and review_state == "待审":
                                ui.button(
                                    icon="replay",
                                    color="orange",
                                    on_click=lambda: set_review_revise(project_name, ver),
                                ).props("flat round dense").tooltip("申请修改")

                            if is_submitter and can_edit_requirements():
                                ui.button(
                                    icon="delete",
                                    color="red",
                                    on_click=lambda: remove_requirement_dialog(container, project_name, ver),
                                ).props("flat round dense").tooltip("撤销并移除需求")

    @ui.refreshable
    def render_requirement_review_board() -> None:
        review_groups = _group_visible_requirement_reviews(
            app.storage.general.get("wait_review", {}),
            current_user=current_user,
            can_review_requirement=can_review_requirement,
            can_review_all=can_review_all_requirements(),
        )
        if not any(review_groups.values()):
            with ui.column().classes("w-full items-center py-8 text-gray-400"):
                ui.icon("task_alt", size="4em").classes("mb-2 opacity-50")
                ui.label("当前没有待评审的需求").classes("text-sm")
            return

        requirement_todos = get_requirement_todo_keys_by_state(
            app.storage.general.get("wait_review", {}),
            current_user=current_user,
            can_edit_requirements=can_edit_requirements(),
            can_review_requirement=can_review_requirement,
        )
        default_open_state = get_default_requirement_review_state(requirement_todos)
        group_options = (
            (
                "待审",
                "pending_actions",
                default_open_state == "待审",
                "text-blue-700 border-blue-300",
            ),
            (
                "待修改",
                "edit_note",
                default_open_state == "待修改",
                "text-orange-700 border-orange-300",
            ),
        )
        with ui.column().classes("w-full gap-3"):
            for state, icon, default_open, style_classes in group_options:
                review_items = review_groups[state]
                if not review_items:
                    continue
                with ui.expansion(
                    f"{state}（{len(review_items)}）",
                    icon=icon,
                    value=default_open,
                ).classes(f"w-full rounded-lg border {style_classes}"):
                    with ui.column().classes("w-full gap-3 pb-2"):
                        for project_name, ver in review_items:
                            row = ui.row().classes("w-full p-0 gap-0")
                            refresh_review_row(row, project_name, ver)

    def create_revoke_dialog():
        """构建需要独立稳定权限的撤销审批对话框。"""
        if not can_revoke_project_requirement_approval(current_role, current_user):
            ui.notify("当前账号没有撤销需求审批的权限", type="negative")
            return
        dialog.clear()
        # 运行时状态初始化，避免使用全局变量造成的静态类型冲突或闭包引用问题
        revoke_state = {
            "project_name": "",
            "action_type": "",  # "none", "delete", "revert"
            "target_ver": "",
            "prev_ver": "",
        }

        # ui.card(): NiceGUI 卡片容器组件，提供背景和阴影包裹。
        with dialog, ui.card().classes("w-full p-4"):
            # ui.label(): NiceGUI 文本标签组件。
            ui.label("撤销需求审批").classes("text-lg font-bold text-red-600 mb-4")

            # ui.input(): NiceGUI 文本输入框组件，用于接收用户输入内容。
            model_input = ui.input(label="请输入要撤销审批的项目型号", placeholder="严格区分大小写...").classes(
                "w-full mb-2"
            )

            feedback_label = ui.label("").classes("w-full mt-2 text-sm font-medium whitespace-pre-wrap")

            # ui.row(): NiceGUI 行容器组件，用于水平排列内部元素。
            action_row = ui.row().classes("w-full justify-end mt-4")

            def check_status():
                """检查项目状态并给出将要执行的预判提示"""
                p_name = model_input.value.strip()
                if not p_name:
                    feedback_label.set_text("请输入有效的型号名称。")
                    feedback_label.classes(replace="text-gray-500")
                    confirm_btn.set_visibility(False)
                    return

                # app.storage.general: NiceGUI 全局通用存储字典，常用于持久化后端运行时的跨用户共享数据。
                wait_review_data = app.storage.general.get("wait_review", {}).get(p_name, {})
                max_ver = app.storage.general.get("project_req_max_ver", {}).get(p_name)

                if not max_ver or max_ver not in wait_review_data:
                    feedback_label.set_text(f"【{p_name}】当前无待处理或已审记录，无法回退。")
                    feedback_label.classes(replace="text-gray-500")
                    confirm_btn.set_visibility(False)
                    return

                current_state = wait_review_data[max_ver].get("state")
                if current_state != "已审":
                    feedback_label.set_text(f"【{p_name}】当前V{max_ver}版本状态为“{current_state}”，无需撤销。")
                    feedback_label.classes(replace="text-gray-500")
                    confirm_btn.set_visibility(False)
                    return

                # 记录有效状态准备执行
                revoke_state["project_name"] = p_name
                revoke_state["target_ver"] = max_ver

                if max_ver == "1.0":
                    revoke_state["action_type"] = "delete"
                    feedback_label.set_text(
                        f"⚠️ 打算执行：\n1.复原型号【{p_name}】1.0 版本的审批状态为“待审”，该项目最高需求版本记录删除。\n2.删除概述整理文件，迫使下次访问概述页面时重新生成，确保版本回退后概述内容与需求状态一致。"
                    )
                    feedback_label.classes(replace="text-red-600")
                else:
                    revoke_state["action_type"] = "revert"
                    prev_ver = f"{int(float(max_ver)) - 1}.0"
                    revoke_state["prev_ver"] = prev_ver
                    feedback_label.set_text(
                        f"⚠️ 打算执行：\n1.将型号【{p_name}】V{max_ver} 版本的状态强行恢复为‘待审’，系统最高版本号回退至 V{prev_ver}。\n2.删除概述整理文件，迫使下次访问概述页面时重新生成，确保版本回退后概述内容与需求状态一致。"
                    )
                    feedback_label.classes(replace="text-orange-600")

                # set_visibility(): NiceGUI 元素的显隐控制函数。
                confirm_btn.set_visibility(True)

            def execute_revoke():
                """确认执行撤销操作"""
                if not can_revoke_project_requirement_approval(current_role, current_user):
                    ui.notify("当前账号没有撤销需求审批的权限", type="negative")
                    return
                p_name = revoke_state["project_name"]
                t_ver = revoke_state["target_ver"]

                if revoke_state["action_type"] == "delete":
                    app.storage.general["wait_review"][p_name][t_ver]["state"] = "待审"
                    app.storage.general["wait_review"][p_name][t_ver].pop("pass_time", None)
                    app.storage.general["project_req_max_ver"].pop(p_name, None)
                elif revoke_state["action_type"] == "revert":
                    app.storage.general["wait_review"][p_name][t_ver]["state"] = "待审"
                    # 清理该版本的审批通过时间
                    app.storage.general["wait_review"][p_name][t_ver].pop("pass_time", None)
                    app.storage.general["project_req_max_ver"][p_name] = revoke_state["prev_ver"]
                # 删除概述整理文件，迫使下次访问概述页面时重新生成，确保版本回退后概述内容与需求状态一致
                # 优化后的写法：直接使用 Path 对象与 / 运算符进行路径拼接
                overview_file_path = Path(OVER_DIR) / f"{p_name}_概述整理.json"
                overview_file_path.unlink(missing_ok=True)

                # ui.notify(): NiceGUI 页面消息通知组件，用于屏幕上方/边缘弹出轻量反馈。
                ui.notify(f"【{p_name}】审批已成功撤销，页面即将刷新", type="positive")
                dialog.close()
                model_input.value = ""
                feedback_label.set_text("")
                confirm_btn.set_visibility(False)

                # ui.timer(): NiceGUI 延时执行函数； ui.navigate.reload(): 刷新当前页面。
                ui.timer(1.0, lambda: ui.navigate.reload())

            # ui.button(): NiceGUI 按钮组件。
            ui.button("检索/预判", on_click=check_status).props("outline color=primary").classes("w-full mt-2")

            with action_row:
                ui.button("取消", on_click=dialog.close).props("flat text-color=grey")
                confirm_btn = ui.button("确认撤销", color="red", on_click=execute_revoke)
                confirm_btn.set_visibility(False)

        dialog.open()

    # 1. 撤回逻辑 (不归档，转为 withdrawn 状态留给用户修改)
    async def handle_withdraw(req_id):
        request = app.storage.general.get("overview_change_requests", {}).get(req_id, {})
        if request.get("submitter") != current_user:
            ui.notify("只有申请人本人可以撤回概述变更申请", type="negative")
            return
        app.storage.general["overview_change_requests"][req_id]["status"] = "withdrawn"
        ui.notify("申请已撤回")
        refresh_overview_request_sections()

    # 2. 审批通过 (执行物理动作 + 数据更新 + 归档)
    async def handle_approve(req_id, req_data):
        live_request = app.storage.general.get("overview_change_requests", {}).get(req_id, {})
        if not live_request or not can_review_overview_correction(live_request, current_role, current_user):
            ui.notify("当前账号没有审批该概述变更申请的权限", type="negative")
            return
        req_data = live_request
        # --- 业务校验逻辑 ---
        config = req_data["config"]
        project = req_data["project_name"]
        new_val = req_data["new_content"]

        is_valid = True
        msg = ""
        if req_data["chip_type"] == "search":
            is_valid, _, _, _, msg = await validate_search_path(new_val, config, [project])
        elif req_data["chip_type"] == "svn":
            is_valid, _, _, msg = await validate_svn_url(new_val, config, [project])

        if not is_valid:
            ui.notify(f"业务校验未通过：{msg}", type="negative")
            return

        # --- 执行修改 ---
        try:
            # 移动物理文件
            if req_data.get("temp_file_path"):
                import shutil

                shutil.move(req_data["temp_file_path"], Path(config["upload_path"]) / new_val)

            # 更新数据库
            base_path = [f"{project}_over_data", req_data["label"], req_data["chip_id"]]
            if req_data["action"] == "modify":
                await db_storage.set_deep_item(base_path + ["content"], new_val)
                if req_data["chip_type"] == "test":
                    await db_storage.set_deep_item(base_path + ["test_select_data"], req_data["new_test_data"])
            elif req_data["action"] == "delete":
                await db_storage.del_deep_item(base_path)

            # 核心：触发即时刷新
            from ..components import OverviewVersionManager

            OverviewVersionManager.bump(project, req_data["label"])

            # 归档
            await handle_archive(req_id, "approved")
            ui.notify("审批通过并已同步刷新")
        except Exception as e:
            ui.notify(f"错误: {e}", type="negative")

    # 3. 归档逻辑 (清理 app.storage.general，写入数据库持久化)
    async def handle_archive(req_id, final_status):
        archive_data = app.storage.general["overview_change_requests"].pop(req_id)
        archive_data["status"] = final_status
        archive_data["finish_time"] = datetime.now().isoformat()

        # 写入专门的数据库归档节点，防止 general 膨胀
        await db_storage.atomic_deep_update(
            ["overview_change_archives"], lambda old: {**(old or {}), req_id: archive_data}
        )
        refresh_overview_request_sections()

    def open_reject_modal(req_id):
        """弹出驳回理由填写对话框"""
        request = app.storage.general.get("overview_change_requests", {}).get(req_id, {})
        if not request or not can_review_overview_correction(request, current_role, current_user):
            ui.notify("当前账号没有审批该概述变更申请的权限", type="negative")
            return
        dialog.clear()
        with dialog, ui.card().classes("w-[400px]"):
            ui.label("驳回变更申请").classes("text-lg font-bold text-red-600 mb-2")

            # 驳回理由输入框
            reason_input = (
                ui.textarea("驳回理由 (必填)", placeholder="请写明为什么驳回该修改...")
                .classes("w-full")
                .props("outlined autofocus")
            )

            async def confirm_reject():
                live_request = app.storage.general.get("overview_change_requests", {}).get(req_id, {})
                if not live_request or not can_review_overview_correction(live_request, current_role, current_user):
                    ui.notify("当前账号没有审批该概述变更申请的权限", type="negative")
                    return
                reason = reason_input.value
                if not reason or not reason.strip():
                    ui.notify("请填写驳回理由！", type="warning", position="top")
                    return

                try:
                    # 1. 更新申请状态与驳回理由
                    app.storage.general["overview_change_requests"][req_id]["status"] = "rejected"
                    app.storage.general["overview_change_requests"][req_id]["reject_reason"] = reason.strip()

                    ui.notify("已驳回该申请", type="positive")
                    dialog.close()
                    refresh_overview_request_sections()
                except Exception as e:
                    ui.notify(f"操作失败: {e}", type="negative")

            with ui.row().classes("w-full justify-end mt-4 gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat text-color=grey")
                ui.button("确认驳回", color="red", on_click=confirm_reject)

        dialog.open()

    def trigger_edit(req_data):
        """引导申请人跳转回项目概述页面进行修改"""
        project_name = req_data.get("project_name")

        # 弹出提示，告知用户接下来的操作
        ui.notify(
            f"正在跳转至【{project_name}】项目概述页面...\n请在对应项上再次点击【申请变更】按钮即可继续修改。",
            type="info",
            position="center",
            timeout=3000,
            multi_line=True,
        )

        # 延迟 1.5 秒后，利用现有的 utils 函数跳转到该项目的概述页面
        ui.timer(1.5, lambda: get_overviow_page(project_name, False), once=True)

    correction_request_dialog = ui.dialog().props("persistent")

    async def delete_correction_request(request_id: str) -> None:
        request = db_storage.get_deep_item([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id], {})
        if request.get("submitter") != current_user or request.get("status") not in {
            "pending",
            "rejected",
            "failed",
        }:
            ui.notify("申请状态已变化，当前无法撤销删除。", type="warning")
            return
        deleted = await db_storage.del_deep_item([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        if not deleted:
            ui.notify("纠错申请删除失败，请刷新后重试。", type="negative")
            return
        payload = request.get("payload") or {}
        cleanup_correction_staged_files([str(payload.get("staged_file_path") or "")])
        correction_request_dialog.close()
        ui.notify("纠错申请已撤销删除。", type="positive")
        refresh_overview_request_sections()

    async def approve_correction_request(request_id: str) -> None:
        # 先让详情弹窗完成关闭动画；审批中的文件、数据库和归档操作随后继续执行。
        correction_request_dialog.close()
        await asyncio.sleep(0.1)
        async with _overview_correction_review_locks[request_id]:
            claimed: dict[str, object] = {"value": False, "request": None}

            def claim(request):
                if (
                    not request
                    or request.get("status") != "pending"
                    or not can_review_correction_request(request, current_user, str(current_role or ""))
                ):
                    return db_storage.ATOMIC_NO_UPDATE
                request["status"] = "processing"
                request["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                claimed["value"] = True
                claimed["request"] = copy.deepcopy(request)
                return request

            await db_storage.atomic_deep_update([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id], claim)
            request = claimed["request"]
            if claimed["value"] is not True or not isinstance(request, dict):
                ui.notify("申请已被处理或当前账号无审批权限。", type="warning")
                refresh_overview_request_sections()
                return
            processing_notice = ui.notification("正在执行单项概述纠错审批…", timeout=None, spinner=True)
            try:
                result = await execute_correction_request(request)
                if not result.get("ok"):
                    await update_correction_request(
                        request_id,
                        {
                            "status": "failed",
                            "result": result,
                        },
                    )
                    ui.notify(f"纠错执行失败：{result.get('message', '未知原因')}", type="negative", timeout=0)
                    refresh_overview_request_sections()
                    return
                reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                review_log = list(request.get("review_log") or [])
                review_log.append(
                    {
                        "action": "approve",
                        "user": current_user,
                        "role": current_role,
                        "time": reviewed_at,
                    }
                )
                archived = await archive_correction_request(
                    request_id,
                    request,
                    "approved",
                    {
                        "reviewer": current_user,
                        "reviewer_role": current_role,
                        "reviewed_at": reviewed_at,
                        "review_log": review_log,
                        "result": result,
                    },
                )
                if not archived:
                    await update_correction_request(
                        request_id,
                        {
                            "status": "approved",
                            "reviewer": current_user,
                            "reviewed_at": reviewed_at,
                            "result": result,
                        },
                    )
                    ui.notify("纠错已执行，但活动申请清理失败，请联系管理员检查归档。", type="warning", timeout=0)
                else:
                    ui.notify(result.get("message", "纠错审批已通过。"), type="positive")
                completion_scheduler = globals().get("_schedule_overview_completion_cc")
                if callable(completion_scheduler):
                    completion_scheduler(
                        request,
                        subject="概述原记录纠错",
                        business_key=request_id,
                        reviewer=current_user,
                    )
            except Exception as exc:
                logger.error("执行单项概述纠错失败: request_id=%s", request_id, exc_info=True)
                await update_correction_request(
                    request_id,
                    {"status": "failed", "result": {"ok": False, "message": str(exc)}},
                )
                ui.notify(f"纠错审批异常：{exc}", type="negative", timeout=0)
            finally:
                processing_notice.dismiss()
            refresh_overview_request_sections()

    def reject_correction_request(request_id: str) -> None:
        reject_dialog = ui.dialog().props("persistent")
        with reject_dialog, ui.card().classes("w-[440px] max-w-[94vw]"):
            ui.label("驳回原记录纠错申请").classes("text-lg font-bold text-red-700")
            reason_input = ui.textarea("驳回理由（必填）").props("outlined autofocus auto-grow").classes("w-full")

            async def confirm_reject():
                reason = str(reason_input.value or "").strip()
                if not reason:
                    ui.notify("请填写驳回理由。", type="warning")
                    return
                rejected = {"value": False}

                def reject(request):
                    if (
                        not request
                        or request.get("status") != "pending"
                        or not can_review_correction_request(request, current_user, str(current_role or ""))
                    ):
                        return db_storage.ATOMIC_NO_UPDATE
                    request["status"] = "rejected"
                    request["reject_reason"] = reason
                    request["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    request.setdefault("review_log", []).append(
                        {
                            "action": "reject",
                            "user": current_user,
                            "role": current_role,
                            "note": reason,
                            "time": request["updated_at"],
                        }
                    )
                    rejected["value"] = True
                    return request

                await db_storage.atomic_deep_update([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id], reject)
                if rejected["value"] is not True:
                    ui.notify("申请已被处理或当前账号无审批权限。", type="warning")
                    return
                reject_dialog.close()
                correction_request_dialog.close()
                ui.notify("纠错申请已驳回。", type="positive")
                refresh_overview_request_sections()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=reject_dialog.close).props("flat color=grey")
                ui.button("确认驳回", on_click=confirm_reject).props("color=negative")
        reject_dialog.open()

    def open_correction_request_detail(request_id: str) -> None:
        request = db_storage.get_deep_item([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id], {})
        if not request:
            ui.notify("纠错申请已不存在。", type="warning")
            return
        is_mine = request.get("submitter") == current_user
        can_review = can_review_correction_request(request, current_user, str(current_role or ""))
        if not is_mine and not can_review:
            ui.notify("当前账号无权查阅该纠错申请。", type="negative")
            return
        payload = request.get("payload") or {}
        before = payload.get("before_snapshot") or {}
        after = payload.get("after_snapshot") or None
        config = payload.get("config") or {}
        changes = build_correction_changes(before, after, config, str(request.get("action") or "correct"))

        correction_request_dialog.clear()
        with correction_request_dialog, ui.card().classes("w-[900px] max-w-[96vw] h-[88vh] max-h-[92vh] p-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("原记录纠错申请详情").classes("text-xl font-bold text-blue-900")
                ui.button(icon="close", on_click=correction_request_dialog.close).props("flat round dense")
            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full gap-3 pr-2"):
                    with ui.grid(columns=2).classes("w-full gap-2 text-sm"):
                        ui.label(f"项目：{request.get('project', '')}")
                        ui.label(f"概述项：{request.get('title', request.get('label', ''))}")
                        ui.label(f"申请人：{request.get('submitter', '')}（{request.get('submitter_role', '')}）")
                        ui.label(f"申请时间：{request.get('created_at', '')}")
                    ui.label(f"纠错理由：{request.get('reason', '')}").classes(
                        "w-full p-2 rounded bg-blue-50 text-blue-900 font-medium"
                    )
                    if request.get("reject_reason"):
                        ui.label(f"驳回理由：{request.get('reject_reason')}").classes(
                            "w-full p-2 rounded bg-red-50 text-red-700 font-bold"
                        )
                    ui.label(
                        "处理方式：纠正原记录" if request.get("action") == "correct" else "处理方式：删除错误记录"
                    ).classes("font-bold")
                    ui.separator()
                    ui.label("修改内容对照").classes("font-bold text-gray-800")
                    if request.get("action") == "correct" and request.get("chip_type") in {
                        "file",
                        "image",
                        "video",
                    }:
                        with ui.card().classes("w-full p-3 shadow-none border border-blue-200 bg-blue-50/40"):
                            ui.label("文件校验信息").classes("font-bold text-blue-900")
                            ui.label(
                                f"原文件 SHA256：{payload.get('original_file_sha256') or '提交时未找到原文件'}"
                            ).classes("text-xs font-mono break-all text-gray-600")
                            if payload.get("staged_file_path"):
                                ui.badge("已上传替换文件", color="blue").props("outline")
                                ui.label(
                                    f"送审文件 SHA256：{payload.get('staged_file_sha256') or '旧申请未记录'}"
                                ).classes("text-xs font-mono break-all text-gray-700")
                            else:
                                ui.label("未上传替换文件；审批时将引用正式目录中的目标文件名。").classes(
                                    "text-xs text-gray-600"
                                )
                    if request.get("chip_type") in {"file", "image", "video"}:
                        _render_correction_media_previews(request_id, request, payload)
                    for change in changes:
                        changed = change.get("changed") is True
                        with ui.card().classes(
                            "w-full p-3 shadow-none border "
                            + ("border-amber-300 bg-amber-50/40" if changed else "border-gray-200 bg-gray-50")
                        ):
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(str(change.get("title") or change.get("key") or "字段")).classes("font-bold")
                                ui.badge("已变化" if changed else "未变化", color="orange" if changed else "grey")
                            if "before_select" in change:
                                ui.label(
                                    f"原选择：{change.get('before_select') or '未选择'}"
                                    + (f"；补充：{change.get('before_other')}" if change.get("before_other") else "")
                                ).classes("text-sm text-gray-600")
                                ui.label(
                                    f"纠正后：{change.get('after_select') or '未选择'}"
                                    + (f"；补充：{change.get('after_other')}" if change.get("after_other") else "")
                                ).classes("text-sm text-gray-800")
                            else:
                                ui.label(f"原值：{change.get('before', '')}").classes("text-sm text-gray-600")
                                ui.label(f"纠正后：{change.get('after', '')}").classes("text-sm text-gray-800")
                    delete_targets = payload.get("delete_targets") or []
                    if request.get("action") == "delete" and len(delete_targets) > 1:
                        ui.label(f"表格整行删除范围（{len(delete_targets)} 个单元格）").classes(
                            "font-bold text-red-700"
                        )
                        with ui.row().classes("w-full gap-2 flex-wrap"):
                            for target in delete_targets:
                                snapshot = target.get("snapshot") or {}
                                ui.chip(
                                    f"{target.get('label', '')}｜{snapshot.get('content', '')}",
                                    icon="delete",
                                ).props("outline color=negative")
                    if request.get("result"):
                        ui.label(f"执行信息：{request.get('result', {}).get('message', '')}").classes(
                            "text-sm text-red-700"
                        )

            with ui.row().classes("w-full justify-end gap-2 pt-2 border-t"):
                if can_review and request.get("status") == "pending":
                    ui.button("驳回", on_click=lambda: reject_correction_request(request_id)).props(
                        "outline color=negative"
                    )
                    ui.button("审批通过并纠错", on_click=lambda: approve_correction_request(request_id)).props(
                        "color=positive"
                    )
                if is_mine and request.get("status") in {"pending", "rejected", "failed"}:
                    ui.button("撤回并删除", on_click=lambda: delete_correction_request(request_id)).props(
                        "outline color=orange"
                    )
                if is_mine and request.get("status") in {"rejected", "failed"}:
                    ui.button(
                        "返回项目修改",
                        on_click=lambda: get_overviow_page(
                            str(request.get("project") or ""),
                            False,
                            correction_label=str(request.get("label") or ""),
                            correction_chip_id=str(request.get("chip_id") or ""),
                        ),
                    ).props("color=primary")
                ui.button("关闭", on_click=correction_request_dialog.close).props("flat color=grey")
        correction_request_dialog.open()

    batch_request_dialog = ui.dialog().props("persistent")
    overview_add_request_dialog = ui.dialog().props("persistent")

    async def approve_overview_add(request_id: str) -> None:
        result = await approve_overview_add_request(request_id, current_user)
        status = str(result.get("status") or "")
        ui.notify(
            str(result.get("message") or "审批处理完成"),
            type="positive" if status in {"approved", "advanced", "node_pending"} else "negative",
            multi_line=True,
        )
        if status == "approved":
            request = db_storage.get_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id], {})
            if isinstance(request, dict):
                _schedule_overview_completion_cc(
                    request,
                    subject="关键概述新增",
                    business_key=request_id,
                    reviewer=current_user,
                )
        overview_add_request_dialog.close()
        refresh_overview_request_sections()

    def reject_overview_add(request_id: str) -> None:
        reject_dialog = ui.dialog().props("persistent")
        with reject_dialog, ui.card().classes("w-[440px] max-w-[94vw]"):
            ui.label("驳回关键概述新增申请").classes("text-lg font-bold text-red-700")
            reason_input = ui.textarea("驳回理由（必填）").props("outlined autofocus auto-grow").classes("w-full")

            async def confirm_reject() -> None:
                reason = str(reason_input.value or "").strip()
                if not reason:
                    ui.notify("请填写驳回理由。", type="warning")
                    return
                result = await reject_overview_add_request(
                    request_id,
                    current_user,
                    reason,
                )
                ui.notify(
                    result["message"],
                    type="positive" if result["status"] == "rejected" else "negative",
                )
                reject_dialog.close()
                overview_add_request_dialog.close()
                refresh_overview_request_sections()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=reject_dialog.close).props("flat")
                ui.button("确认驳回", on_click=confirm_reject).props("color=negative")
        reject_dialog.open()

    async def withdraw_overview_add(request_id: str) -> None:
        result = await withdraw_overview_add_request(request_id, current_user)
        ui.notify(
            result["message"],
            type="positive" if result["status"] == "withdrawn" else "warning",
        )
        overview_add_request_dialog.close()
        refresh_overview_request_sections()

    def open_overview_add_request_detail(request_id: str) -> None:
        """展示关键概述新增候选内容；审批人先查看完整内容再处理。"""
        request = db_storage.get_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id], {})
        if not isinstance(request, dict) or not request:
            ui.notify("申请已不存在。", type="warning")
            return
        is_mine = request.get("submitter") == current_user
        can_review = can_review_overview_add_request(request, current_user)
        if not is_mine and not can_review:
            ui.notify("当前账号无权查阅该申请。", type="negative")
            return

        payload = request.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        chip = payload.get("chip")
        chip = chip if isinstance(chip, dict) else {}
        config = payload.get("config")
        config = config if isinstance(config, dict) else {}
        processing_type = str(chip.get("type") or config.get("processing_type") or "text")
        type_name = {
            "text": "文本",
            "test": "测试参数",
            "file": "文件",
            "image": "图片",
            "video": "视频",
            "svn": "SVN 文件",
            "search": "检索内容",
        }.get(processing_type, processing_type or "文本")

        operation_reason = ""
        timestamps = chip.get("timestamp")
        if isinstance(timestamps, dict) and timestamps:
            latest_key = max(str(key) for key in timestamps)
            latest_record = timestamps.get(latest_key)
            if isinstance(latest_record, dict):
                operation_reason = str(latest_record.get("reason") or "")

        overview_add_request_dialog.clear()
        with overview_add_request_dialog, ui.card().classes(
            "w-[900px] max-w-[96vw] h-[80vh] max-h-[92vh] p-4"
        ):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("关键概述新增申请详情").classes("text-xl font-bold text-amber-900")
                ui.button(icon="close", on_click=overview_add_request_dialog.close).props("flat round dense")

            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full gap-3 pr-2"):
                    with ui.grid(columns=2).classes("w-full gap-2 text-sm"):
                        ui.label(f"项目：{request.get('project', '')}")
                        ui.label(f"概述项：{request.get('title', request.get('label', ''))}")
                        ui.label(
                            f"申请人：{request.get('submitter', '')}（{request.get('submitter_role', '')}）"
                        )
                        ui.label(f"状态：{batch_status_text(str(request.get('status') or ''))}")
                        ui.label(f"申请时间：{request.get('created_at', '')}")
                        ui.label(f"最近更新：{request.get('updated_at', '')}")
                        ui.label(f"所属职能：{config.get('role') or '未配置'}")
                        ui.label(f"所属分组：{config.get('group_name') or '未配置'}")
                        ui.label(f"内容类型：{type_name}")
                        ui.label(f"需求版本：{chip.get('req_ver') or '未记录'}")

                    if request.get("reject_reason"):
                        ui.label(f"驳回理由：{request.get('reject_reason')}").classes(
                            "w-full p-2 rounded bg-red-50 text-red-700 font-bold"
                        )

                    ui.separator()
                    ui.label("送审内容").classes("font-bold text-gray-800")
                    ui.label(str(chip.get("content") or "未填写")).classes(
                        "w-full whitespace-pre-wrap break-all rounded border border-amber-100 "
                        "bg-amber-50/40 p-3 text-sm text-gray-800"
                    )
                    if operation_reason:
                        ui.label(f"操作原因：{operation_reason}").classes("text-sm text-gray-600")

                    configured_impacts = [
                        str(label) for label in config.get("impact_list", []) if str(label)
                    ]
                    if configured_impacts:
                        selected_impacts = {
                            str(label) for label in payload.get("related_labels", []) if str(label)
                        }
                        impact_mode = str(payload.get("impact_mode") or "none")
                        effective_impacts = (
                            configured_impacts
                            if impact_mode == "all"
                            else [label for label in configured_impacts if label in selected_impacts]
                            if impact_mode == "selected"
                            else []
                        )
                        ui.label("关联影响范围").classes("font-bold text-gray-800")
                        if effective_impacts:
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                for related_label in effective_impacts:
                                    title = (
                                        app.storage.general.get("over_config_data_flat", {})
                                        .get(related_label, {})
                                        .get("title", related_label)
                                    )
                                    ui.chip(title, icon="pending_actions").props(
                                        "dense outline color=amber-9"
                                    )
                            ui.label("以上关联项将在本申请全部审批通过后转为待确认。\n").classes(
                                "text-xs text-amber-700"
                            )
                        else:
                            ui.label("本次明确选择不影响其它概述项。\n").classes(
                                "text-sm text-gray-600"
                            )

                    if processing_type == "test":
                        test_data = chip.get("test_select_data")
                        test_data = test_data if isinstance(test_data, dict) else {}
                        configured_fields = configured_test_field_definitions(config)
                        if configured_fields:
                            ui.label("测试参数明细").classes("font-bold text-gray-800")
                        with ui.grid(columns=2).classes("w-full gap-3"):
                            for prefix, title, _ in configured_fields:
                                selected = str(test_data.get(f"{prefix}_select") or "").strip()
                                other_text = str(test_data.get(f"{prefix}_other_text") or "").strip()
                                value = " / ".join(part for part in (selected, other_text) if part) or "未填写"
                                with ui.column().classes("min-w-0 gap-1 rounded bg-gray-50 p-2"):
                                    ui.label(title).classes("text-sm font-medium text-gray-700")
                                    ui.label(value).classes("whitespace-pre-wrap break-all text-sm text-gray-600")

                    if processing_type in {"file", "image", "video", "svn"}:
                        ui.label("文件信息").classes("font-bold text-gray-800")
                        with ui.grid(columns=2).classes("w-full gap-2 text-sm"):
                            ui.label(f"文件名：{Path(str(chip.get('content') or '')).name or '未记录'}")
                            ui.label(f"文件类型：{chip.get('file_type') or type_name}")
                            if processing_type == "svn":
                                ui.label(f"仓库：{chip.get('warehouse') or '未记录'}")

                    activation = chip.get("select_activ_dic")
                    if isinstance(activation, dict) and activation:
                        ui.label("版本激活状态").classes("font-bold text-gray-800")
                        with ui.row().classes("w-full gap-2 flex-wrap"):
                            for version, active in activation.items():
                                state_text = "激活" if active is True else "失活" if active is False else "待定"
                                state_color = "green" if active is True else "grey" if active is False else "orange"
                                ui.badge(f"V{version}：{state_text}", color=state_color).props("outline")

                    review_log = request.get("review_log")
                    if isinstance(review_log, list) and review_log:
                        ui.separator()
                        ui.label("审批记录").classes("font-bold text-gray-800")
                        for record in review_log:
                            if not isinstance(record, dict):
                                continue
                            action_text = {
                                "approve": "通过",
                                "reject": "驳回",
                                "resubmit": "修改后重新提交",
                            }.get(str(record.get("action") or ""), "处理")
                            note = str(record.get("note") or "").strip()
                            ui.label(
                                f"{record.get('time', '')} ｜ {record.get('user', '')} ｜ {action_text}"
                                + (f" ｜ {note}" if note else "")
                            ).classes("text-sm text-gray-600")

                    result = request.get("result")
                    if isinstance(result, dict) and result:
                        ui.label(f"执行结果：{result.get('message', '')}").classes(
                            "w-full rounded bg-red-50 p-2 text-sm text-red-700"
                            if result.get("ok") is not True
                            else "w-full rounded bg-green-50 p-2 text-sm text-green-700"
                        )

            with ui.row().classes("w-full justify-end gap-2 pt-2 border-t"):
                if can_review and request.get("status") == "pending":
                    ui.button(
                        "驳回",
                        on_click=lambda: reject_overview_add(request_id),
                    ).props("outline color=negative")
                    ui.button(
                        "审批通过并生效",
                        on_click=lambda: approve_overview_add(request_id),
                    ).props("color=positive")
                if is_mine and request.get("status") in {"pending", "rejected", "failed"}:
                    ui.button(
                        "撤回并删除",
                        on_click=lambda: withdraw_overview_add(request_id),
                    ).props("outline color=orange")
                if is_mine and request.get("status") == "rejected":
                    ui.button(
                        "返回项目修改",
                        on_click=lambda: get_overviow_page(
                            str(request.get("project") or ""),
                            False,
                            add_request_id=request_id,
                        ),
                    ).props("color=primary")
                ui.button("关闭", on_click=overview_add_request_dialog.close).props("flat color=grey")
        overview_add_request_dialog.open()

    def batch_status_text(status: str) -> str:
        return {
            "pending": "待审批",
            "processing": "审批执行中",
            "rejected": "已驳回",
            "withdrawn": "已撤回",
            "approved": "已通过",
            "approved_with_warnings": "已通过（部分异常）",
            "failed": "执行失败",
        }.get(status, status or "未知")

    async def withdraw_batch_request(request_id: str) -> None:
        request = db_storage.get_deep_item([BATCH_OVERVIEW_REQUESTS_KEY, request_id], {})
        if request.get("submitter") != current_user or request.get("status") not in {
            "pending",
            "rejected",
            "failed",
            "withdrawn",
        }:
            ui.notify("申请状态已变化，当前无法撤销删除。", type="warning")
            return
        payload = request.get("payload") or {}
        staged_path = Path(str(payload.get("staged_file_path") or ""))
        deleted = await db_storage.del_deep_item([BATCH_OVERVIEW_REQUESTS_KEY, request_id])
        if not deleted:
            ui.notify("申请删除失败，请刷新后重试。", type="negative")
            return
        staged_path_is_safe = bool(
            str(payload.get("staged_file_path") or "").strip()
            and staged_path.resolve().is_relative_to(BATCH_OVERVIEW_STAGING_DIR.resolve())
        )
        if staged_path_is_safe and staged_path.is_file():
            staged_path.unlink(missing_ok=True)
            try:
                staged_path.parent.rmdir()
            except OSError:
                pass
        batch_request_dialog.close()
        ui.notify("批量概述申请已撤销删除。", type="positive")
        refresh_overview_request_sections()

    async def approve_batch_request(request_id: str) -> None:
        # 批量执行可能包含多个项目和文件操作，先收起弹窗避免界面等待执行完成。
        batch_request_dialog.close()
        await asyncio.sleep(0.1)
        async with _batch_overview_review_locks[request_id]:
            claimed = {"value": False, "request": None}

            def claim(request):
                if (
                    not request
                    or request.get("status") != "pending"
                    or not can_review_batch_overview_request(request, current_user, str(current_role or ""))
                ):
                    return db_storage.ATOMIC_NO_UPDATE
                request["status"] = "processing"
                request["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                claimed["value"] = True
                claimed["request"] = copy.deepcopy(request)
                return request

            await db_storage.atomic_deep_update([BATCH_OVERVIEW_REQUESTS_KEY, request_id], claim)
            request = claimed["request"]
            if claimed["value"] is not True or not isinstance(request, dict):
                ui.notify("申请已被处理或当前账号无审核权限。", type="warning")
                refresh_overview_request_sections()
                return

            processing_notice = ui.notification("正在执行批量概述审批…", timeout=None, spinner=True)
            try:
                result = await execute_batch_overview_request(request)
                success_count = len(result.get("successes", []))
                failed_items = result.get("failed", [])
                if success_count and failed_items:
                    final_status = "approved_with_warnings"
                elif success_count:
                    final_status = "approved"
                else:
                    final_status = "failed"
                review_log = list(request.get("review_log") or [])
                review_log.append(
                    {
                        "action": "approve",
                        "user": current_user,
                        "role": current_role,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                await update_batch_overview_request(
                    request_id,
                    {
                        "status": final_status,
                        "reviewer": current_user,
                        "reviewer_role": current_role,
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "result": result,
                        "review_log": review_log,
                    },
                )
                if success_count:
                    completion_scheduler = globals().get("_schedule_overview_completion_cc")
                    if callable(completion_scheduler):
                        completion_scheduler(
                            request,
                            subject="批量概述变更",
                            business_key=request_id,
                            reviewer=current_user,
                        )
                notification_type = "positive" if final_status == "approved" else "warning"
                ui.notify(result.get("message", "批量申请已处理。"), type=notification_type, timeout=0)
            except Exception as exc:
                logger.error("执行批量概述审批申请失败: request_id=%s", request_id, exc_info=True)
                await update_batch_overview_request(
                    request_id,
                    {
                        "status": "failed",
                        "result": {"message": str(exc), "successes": [], "skipped": [], "failed": [str(exc)]},
                    },
                )
                ui.notify(f"审批执行失败：{exc}", type="negative", timeout=0)
            finally:
                processing_notice.dismiss()
            refresh_overview_request_sections()

    def reject_batch_request(request_id: str) -> None:
        reject_dialog = ui.dialog().props("persistent")
        with reject_dialog, ui.card().classes("w-[440px] max-w-[94vw]"):
            ui.label("驳回批量概述申请").classes("text-lg font-bold text-red-700")
            reason_input = ui.textarea("驳回理由（必填）").props("outlined autofocus auto-grow").classes("w-full")

            async def confirm_reject_batch():
                reason = str(reason_input.value or "").strip()
                if not reason:
                    ui.notify("请填写驳回理由。", type="warning")
                    return
                rejected = {"value": False}

                def reject(request):
                    if (
                        not request
                        or request.get("status") != "pending"
                        or not can_review_batch_overview_request(request, current_user, str(current_role or ""))
                    ):
                        return db_storage.ATOMIC_NO_UPDATE
                    request["status"] = "rejected"
                    request["reject_reason"] = reason
                    request["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    request.setdefault("review_log", []).append(
                        {
                            "action": "reject",
                            "user": current_user,
                            "role": current_role,
                            "note": reason,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    rejected["value"] = True
                    return request

                await db_storage.atomic_deep_update([BATCH_OVERVIEW_REQUESTS_KEY, request_id], reject)
                if rejected["value"] is not True:
                    ui.notify("申请已被处理或当前账号无审核权限。", type="warning")
                    return
                reject_dialog.close()
                batch_request_dialog.close()
                ui.notify("批量概述申请已驳回，申请人将在待办中收到提示。", type="positive")
                refresh_overview_request_sections()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=reject_dialog.close).props("flat color=grey")
                ui.button("确认驳回", on_click=confirm_reject_batch).props("color=negative")
        reject_dialog.open()

    def open_batch_request_detail(request_id: str) -> None:
        request = db_storage.get_deep_item([BATCH_OVERVIEW_REQUESTS_KEY, request_id], {})
        if not request:
            ui.notify("申请已不存在。", type="warning")
            return
        is_mine = request.get("submitter") == current_user
        can_review = can_review_batch_overview_request(request, current_user, str(current_role or ""))
        if not is_mine and not can_review:
            ui.notify("当前账号无权查阅该申请。", type="negative")
            return
        payload = copy.deepcopy(request.get("payload") or {})
        action = payload.get("action")
        editable = is_mine and request.get("status") in {"rejected", "failed"}
        editor = {
            "projects": list(payload.get("projects") or []),
            "content": str(payload.get("content") or ""),
            "reason": str(payload.get("reason") or payload.get("notes") or ""),
            "target_state": {True: "active", None: "pending", False: "inactive"}.get(
                payload.get("target_state"), "pending"
            ),
            "impact_mode": payload.get("impact_mode", "none"),
            "impact_selected": {
                label: label in payload.get("related_labels", [])
                for label in payload.get("config", {}).get("impact_list", [])
                if label
            },
        }

        batch_request_dialog.clear()
        with batch_request_dialog, ui.card().classes("w-[900px] max-w-[96vw] h-[80vh] max-h-[92vh] p-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("批量概述变更申请详情").classes("text-xl font-bold text-blue-900")
                ui.button(icon="close", on_click=batch_request_dialog.close).props("flat round dense")
            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full gap-3 pr-2"):
                    with ui.grid(columns=2).classes("w-full gap-2 text-sm"):
                        ui.label(f"申请人：{request.get('submitter')}（{request.get('submitter_role')}）")
                        ui.label(f"状态：{batch_status_text(str(request.get('status') or ''))}")
                        ui.label(f"申请时间：{request.get('created_at', '')}")
                        ui.label(f"最近更新：{request.get('updated_at', '')}")
                    ui.label(
                        f"操作：{'批量新增概述' if action == 'add' else '批量修改激活状态'} ｜ "
                        f"{payload.get('role', '')} / {payload.get('group_name', '')} / {payload.get('title', payload.get('label', ''))}"
                    ).classes("font-bold text-gray-800")
                    if request.get("reject_reason"):
                        ui.label(f"驳回理由：{request['reject_reason']}").classes(
                            "w-full p-2 rounded bg-red-50 text-red-700 font-bold"
                        )

                    project_names = [str(project) for project in payload.get("projects", [])]
                    project_options = {project: project for project in project_names}
                    if editable:
                        ui.select(project_options, label="目标项目", multiple=True).bind_value(
                            editor, "projects"
                        ).props("outlined use-chips options-dense").classes("w-full")
                    else:
                        with ui.column().classes("w-full gap-1"):
                            ui.label(f"目标项目（{len(project_names)}）").classes("text-sm font-medium text-gray-700")
                            with ui.row().classes("w-full gap-2 flex-wrap"):
                                for target_project in project_names:
                                    ui.chip(target_project).props("dense color=blue-1 text-color=blue-9")

                    reason_selector = None
                    if action == "add":
                        is_staged_media = bool(payload.get("staged_file_path"))
                        content_input = (
                            ui.input("新增内容").bind_value(editor, "content").props("outlined").classes("w-full")
                        )
                        if is_staged_media:
                            content_input.disable()
                            content_input.tooltip("已上传文件名不能在审批单中修改；可撤回后重新发起。")
                        if not editable:
                            content_input.disable()
                        config = payload.get("config") or {}
                        if (payload.get("actual_type") or config.get("processing_type")) == "test":
                            extra_data = payload.get("extra_data")
                            test_data = extra_data.get("test_select_data") if isinstance(extra_data, dict) else None
                            if not isinstance(test_data, dict):
                                test_data = {}
                            with ui.grid(columns=2).classes("w-full gap-3"):
                                for prefix, title, _ in configured_test_field_definitions(config):
                                    selected = str(test_data.get(f"{prefix}_select") or "").strip()
                                    other_text = str(test_data.get(f"{prefix}_other_text") or "").strip()
                                    value = " / ".join(part for part in (selected, other_text) if part) or "未填写"
                                    with ui.column().classes("min-w-0 gap-1"):
                                        ui.label(title).classes("text-sm font-medium text-gray-700")
                                        ui.label(value).classes("whitespace-pre-wrap break-all text-sm text-gray-600")
                    else:
                        ui.label(f"已选概述条目：{len(payload.get('chip_targets', []))} 条").classes("text-sm")
                        target_radio = (
                            ui.radio({"active": "设为激活", "pending": "设为待定", "inactive": "设为失活"})
                            .bind_value(editor, "target_state")
                            .props("inline")
                        )
                        if not editable:
                            target_radio.disable()

                    if editable:
                        reason_selector = OverviewReasonSelector(
                            "create" if action == "add" else "state_change",
                            "操作原因（必选）",
                        )
                        if editor["reason"]:
                            reason_selector.value = editor["reason"]
                    else:
                        ui.label(f"操作原因：{editor['reason'] or '旧申请未记录'}").classes("text-sm text-gray-600")

                    configured_related = list(
                        dict.fromkeys(label for label in payload.get("config", {}).get("impact_list", []) if label)
                    )
                    if configured_related:
                        ui.separator()
                        impact_radio = (
                            ui.radio({"none": "本次不影响其它项", "selected": "勾选的受影响", "all": "全部受影响"})
                            .bind_value(editor, "impact_mode")
                            .props("inline")
                        )
                        if not editable:
                            impact_radio.disable()
                        for related_label in configured_related:
                            title = (
                                app.storage.general.get("over_config_data_flat", {})
                                .get(related_label, {})
                                .get("title", related_label)
                            )
                            checkbox = ui.checkbox(title).bind_value(editor["impact_selected"], related_label)
                            checkbox.bind_visibility_from(
                                editor, "impact_mode", backward=lambda value: value == "selected"
                            )
                            if not editable:
                                checkbox.disable()

                    result = request.get("result") or {}
                    if result:
                        ui.separator()
                        ui.label("执行结果").classes("font-bold")
                        ui.label(str(result.get("message") or "")).classes("text-sm")
                        for item in result.get("skipped", []):
                            ui.label(f"跳过｜{item}").classes("text-xs text-amber-700")
                        for item in result.get("failed", []):
                            ui.label(f"失败｜{item}").classes("text-xs text-red-700")

            async def resubmit_batch_request():
                projects = list(editor["projects"] or [])
                if not projects:
                    ui.notify("请至少保留一个目标项目。", type="warning")
                    return
                new_payload = copy.deepcopy(payload)
                new_payload["projects"] = projects
                new_payload["content"] = str(editor["content"] or "").strip()
                new_payload["reason"] = reason_selector.value.strip() if reason_selector else editor["reason"]
                new_payload.pop("notes", None)
                if not new_payload["reason"]:
                    ui.notify("请选择操作原因；选择“其他”时需填写具体原因。", type="warning")
                    return
                if action == "state":
                    new_payload["target_state"] = {"active": True, "pending": None, "inactive": False}[
                        editor["target_state"]
                    ]
                    new_payload["chip_targets"] = [
                        target for target in new_payload.get("chip_targets", []) if target.get("project") in projects
                    ]
                    if not new_payload["chip_targets"]:
                        ui.notify("所选项目中没有可处理的目标概述条目。", type="warning")
                        return
                configured = list(
                    dict.fromkeys(label for label in new_payload.get("config", {}).get("impact_list", []) if label)
                )
                if editor["impact_mode"] == "all":
                    related = configured
                elif editor["impact_mode"] == "selected":
                    related = [label for label in configured if editor["impact_selected"].get(label) is True]
                    if not related:
                        ui.notify("请至少勾选一个确实受影响的概述项。", type="warning")
                        return
                else:
                    related = []
                new_payload["impact_mode"] = editor["impact_mode"]
                new_payload["related_labels"] = related
                review_log = list(request.get("review_log") or [])
                review_log.append(
                    {
                        "action": "resubmit",
                        "user": current_user,
                        "role": current_role,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                await update_batch_overview_request(
                    request_id,
                    {
                        "status": "pending",
                        "reject_reason": "",
                        "payload": new_payload,
                        "review_log": review_log,
                    },
                )
                batch_request_dialog.close()
                ui.notify("申请已修改并重新提交审批。", type="positive")
                refresh_overview_request_sections()

            with ui.row().classes("w-full justify-end gap-2 pt-2 border-t"):
                if can_review and request.get("status") == "pending":
                    ui.button("驳回", on_click=lambda: reject_batch_request(request_id)).props("outline color=negative")
                    ui.button("审批通过并执行", on_click=lambda: approve_batch_request(request_id)).props(
                        "color=positive"
                    )
                if is_mine and request.get("status") in {"pending", "rejected", "failed"}:
                    ui.button("撤回并删除", on_click=lambda: withdraw_batch_request(request_id)).props(
                        "outline color=orange"
                    )
                if is_mine and request.get("status") == "withdrawn":
                    ui.button("删除记录", on_click=lambda: withdraw_batch_request(request_id)).props(
                        "outline color=negative"
                    )
                if editable:
                    ui.button("修改并重新提交", on_click=resubmit_batch_request).props("color=primary")
                ui.button("关闭", on_click=batch_request_dialog.close).props("flat color=grey")
        batch_request_dialog.open()

    def _refresh_conversion_sections() -> None:
        render_conversion_reviews.refresh()
        render_overview_pending.refresh()

    def open_conversion_review_detail(review_id: str) -> None:
        reviews = get_conversion_reviews()
        review = reviews.get(review_id)
        if (
            not isinstance(review, dict)
            or review.get("status") not in OPEN_REVIEW_STATUSES
            or review.get("assignee") != current_user
        ):
            ui.notify("该职能汇总待办已完成、已转交或不存在。", type="warning")
            _refresh_conversion_sections()
            return
        items = collect_review_pending_items(review)
        if not items:
            project_name = str(review.get("project") or "")

            async def finish_empty_review() -> None:
                await reconcile_conversion_reviews(project_name)
                conversion_review_dialog.close()
                _refresh_conversion_sections()

            schedule_background_task(finish_empty_review(), "完成空转产职能汇总待办")
            return

        project_name = str(review.get("project") or "")
        role = str(review.get("role") or "")
        exception_values = {
            str(value) for value in review.get("exception_target_ids", []) if str(value)
        }
        exception_checks: dict[str, object] = {}
        special_count = sum(1 for item in items if item.has_special_reason)

        conversion_review_dialog.clear()
        with conversion_review_dialog, ui.card().classes("w-[1050px] max-w-[96vw] max-h-[92vh] gap-3"):
            with ui.row().classes("w-full items-start justify-between gap-3"):
                with ui.column().classes("gap-0"):
                    ui.label(f"{project_name}｜{role}职能转产概述确认").classes(
                        "text-xl font-bold text-blue-900"
                    )
                    ui.label(
                        f"待确认范围：项目需求版本 V{review.get('version', '')} · "
                        f"待确认 {len(items)} 个 · "
                        f"特殊原因 {special_count} 个"
                    ).classes("text-sm text-gray-600")
                status_text = "继续考虑" if review.get("status") == "exceptions" else "待确认"
                ui.badge(status_text, color="orange" if review.get("status") == "exceptions" else "red")

            ui.label(
                "“保留待定”默认不勾选；提交后未勾选内容统一激活，勾选内容继续留在本汇总待办。"
            ).classes("text-sm text-blue-800 bg-blue-50 border border-blue-100 rounded p-2 w-full")
            ui.label(
                "本页面用于快捷确认，不设置关联影响。若某个内容还需要评估或设置关联影响，"
                "请先勾选“保留待定”，再前往项目需求概述页面仔细处理。"
            ).classes(
                "text-sm font-bold text-amber-900 bg-amber-50 border border-amber-200 rounded p-2 w-full"
            )

            if special_count:
                ui.label(
                    "以下列表已将转产前待定、转产后新增及关联影响等特殊项置顶，请重点核对。"
                ).classes("text-sm font-bold text-orange-800")

            with ui.scroll_area().classes("w-full h-[58vh] border rounded p-2"):
                current_section = ""
                for item in items:
                    section = "重点核对" if item.has_special_reason else "常规转产确认"
                    if section != current_section:
                        current_section = section
                        ui.label(section).classes(
                            "w-full text-sm font-bold mt-1 "
                            + ("text-red-700" if section == "重点核对" else "text-blue-700")
                        )
                    border_class = "border-orange-300 bg-orange-50" if item.has_special_reason else "border-gray-200 bg-white"
                    with ui.card().classes(f"w-full shadow-none border {border_class} p-3 gap-2"):
                        with ui.row().classes("w-full items-start justify-between flex-nowrap gap-3"):
                            with ui.column().classes("min-w-0 gap-1 grow"):
                                with ui.row().classes("items-center gap-1 flex-wrap"):
                                    ui.label(f"{item.title} · {item.group_name}").classes(
                                        "text-sm font-bold text-gray-900"
                                    )
                                    ui.badge(f"需求版本 V{item.version}", color="blue").props(
                                        "outline"
                                    ).tooltip(
                                        "表示该概述在这个项目需求版本下的激活状态，不是概述内容版本"
                                    )
                                    if item.origin == "preexisting":
                                        ui.badge("转产前已待定", color="orange")
                                    elif item.origin == "post_conversion":
                                        ui.badge("转产后新增", color="red")
                                    else:
                                        ui.badge("转产待确认", color="blue")
                                ui.label(item.content).classes(
                                    "text-sm text-gray-700 whitespace-normal break-all"
                                )
                                item_config = app.storage.general.get("over_config_data_flat", {}).get(
                                    item.label,
                                    {},
                                )
                                if (
                                    isinstance(item_config, dict)
                                    and item_config.get("processing_type") == "test"
                                ):
                                    chip_value = db_storage.get_deep_item(
                                        [f"{project_name}_over_data", item.label, item.chip_id],
                                        {},
                                    )
                                    if isinstance(chip_value, dict):
                                        with ui.row().classes("w-full gap-2 flex-wrap"):
                                            for field_title, field_value in configured_test_display_values(
                                                chip_value,
                                                item_config,
                                            ):
                                                ui.badge(
                                                    f"{field_title}：{field_value}",
                                                    color="grey-7",
                                                ).props("outline")
                                for reason in item.reasons:
                                    color_class = {
                                        "conversion": "text-blue-700 bg-blue-50",
                                        "related": "text-red-700 bg-red-50",
                                        "requirement": "text-orange-800 bg-orange-50",
                                        "manual": "text-purple-700 bg-purple-50",
                                    }.get(reason.category, "text-gray-700 bg-gray-50")
                                    detail = f"待定原因：{reason.text}"
                                    metadata = " ｜ ".join(
                                        value
                                        for value in [reason.operator, reason.time, reason.source]
                                        if value
                                    )
                                    if metadata:
                                        detail += f" ｜ {metadata}"
                                    ui.label(detail).classes(
                                        f"w-full text-xs rounded px-2 py-1 whitespace-normal break-all {color_class}"
                                    )
                            check = ui.checkbox(
                                "保留待定",
                                value=item.target_id in exception_values,
                            ).props("dense color=orange")
                            exception_checks[item.target_id] = check
                        with ui.row().classes("w-full justify-end gap-1"):

                            async def handle_single(
                                target_state: bool,
                                *,
                                target_id: str = item.target_id,
                            ) -> None:
                                result = await process_conversion_review_item(
                                    review_id,
                                    target_id,
                                    actor=current_user,
                                    target_state=target_state,
                                )
                                if result.ok:
                                    ui.notify(result.message, type="positive")
                                else:
                                    ui.notify(result.message, type="warning")
                                latest_review = get_conversion_reviews().get(review_id)
                                _refresh_conversion_sections()
                                if (
                                    isinstance(latest_review, dict)
                                    and latest_review.get("status") in OPEN_REVIEW_STATUSES
                                ):
                                    open_conversion_review_detail(review_id)
                                else:
                                    conversion_review_dialog.close()

                            ui.button(
                                "激活",
                                icon="check_circle",
                                on_click=lambda _=None, handler=handle_single: handler(True),
                            ).props("flat dense color=positive size=sm")
                            ui.button(
                                "失活",
                                icon="block",
                                on_click=lambda _=None, handler=handle_single: handler(False),
                            ).props("flat dense color=grey-7 size=sm")

            special_ack = None
            if special_count:
                special_ack = ui.checkbox(
                    f"我已核对上方 {special_count} 个特殊待定原因",
                    value=False,
                ).props("dense color=orange")

            is_submitting = False
            submit_spinner = None
            submit_status = None
            close_button = None
            submit_button = None

            async def submit_role_review() -> None:
                nonlocal is_submitting
                if is_submitting:
                    return
                if special_ack is not None and special_ack.value is not True:
                    ui.notify("请先确认已经核对特殊待定原因。", type="warning")
                    return
                exceptions = {
                    target_id
                    for target_id, checkbox in exception_checks.items()
                    if getattr(checkbox, "value", False) is True
                }
                is_submitting = True
                if submit_button is not None:
                    submit_button.disable()
                if close_button is not None:
                    close_button.disable()
                if special_ack is not None:
                    special_ack.disable()
                for checkbox in exception_checks.values():
                    disable_checkbox = getattr(checkbox, "disable", None)
                    if callable(disable_checkbox):
                        disable_checkbox()
                if submit_spinner is not None:
                    submit_spinner.set_visibility(True)
                if submit_status is not None:
                    submit_status.set_visibility(True)
                processing_notice = ui.notification(
                    "正在批量处理职能概述，请稍候…",
                    timeout=None,
                    spinner=True,
                )
                # 先把禁用、加载状态推送到浏览器，再开始可能耗时的逐项写入。
                await asyncio.sleep(0)
                try:
                    result = await process_conversion_review_batch(
                        review_id,
                        actor=current_user,
                        exception_target_ids=exceptions,
                    )
                    detail = f"；{result.failures[0]}" if result.failures else ""
                    ui.notify(
                        f"{result.message}{detail}",
                        type="positive" if result.ok else "warning",
                        multi_line=True,
                        timeout=0 if result.failures else 5000,
                        close_button="✖" if result.failures else False,
                    )
                    conversion_review_dialog.close()
                    _refresh_conversion_sections()
                except Exception as exc:
                    logger.error("批量处理转产职能概述失败: review_id=%s", review_id, exc_info=True)
                    ui.notify(
                        f"处理失败：{exc}",
                        type="negative",
                        timeout=0,
                        close_button="✖",
                    )
                    is_submitting = False
                    if submit_button is not None:
                        submit_button.enable()
                    if close_button is not None:
                        close_button.enable()
                    if special_ack is not None:
                        special_ack.enable()
                    for checkbox in exception_checks.values():
                        enable_checkbox = getattr(checkbox, "enable", None)
                        if callable(enable_checkbox):
                            enable_checkbox()
                    if submit_spinner is not None:
                        submit_spinner.set_visibility(False)
                    if submit_status is not None:
                        submit_status.set_visibility(False)
                finally:
                    processing_notice.dismiss()

            with ui.row().classes("w-full justify-between items-center"):
                ui.button(
                    "前往项目概述页",
                    icon="open_in_new",
                    on_click=lambda: get_overviow_page(project_name, False),
                ).props("flat color=primary")
                with ui.row().classes("gap-2 items-center"):
                    submit_spinner = ui.spinner(
                        type="hourglass",
                        size="sm",
                        color="amber-8",
                        thickness=8.0,
                    )
                    submit_spinner.set_visibility(False)
                    submit_status = ui.label("正在处理，请稍候…").classes(
                        "text-sm font-medium text-amber-9"
                    )
                    submit_status.set_visibility(False)
                    close_button = ui.button(
                        "关闭",
                        on_click=conversion_review_dialog.close,
                    ).props("flat color=grey")
                    submit_button = ui.button(
                        "确认可激活内容并保留所选待定",
                        icon="done_all",
                        on_click=submit_role_review,
                    ).props("color=primary")
        conversion_review_dialog.open()

    @ui.refreshable
    def render_conversion_reviews() -> None:
        reviews = get_actionable_conversion_reviews(current_user)
        if not reviews:
            return
        with ui.card().classes("w-full rounded-xl shadow-sm border border-blue-100 bg-white mb-3"):
            ui_card_header("转产概述职能确认", "fact_check", "blue-700")
            with ui.column().classes("w-full gap-2 px-1 pr-2 max-h-[48vh] overflow-y-auto"):
                for review in reviews:
                    project_name = str(review.get("project") or "")
                    role = str(review.get("role") or "")
                    current_count = int(review.get("current_count") or 0)
                    special_count = int(review.get("special_count") or 0)
                    status_text = "继续考虑" if review.get("status") == "exceptions" else "待确认"
                    with ui.row().classes(
                        "w-full items-center justify-between p-3 rounded-lg border border-blue-200 bg-blue-50"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label(f"{project_name}｜{role}职能").classes("font-bold text-gray-900")
                            ui.label(
                                f"{status_text} · 剩余 {current_count} 个 · 特殊原因 {special_count} 个"
                            ).classes(
                                "text-xs font-medium " + ("text-red-700" if special_count else "text-blue-700")
                            )
                        ui.button(
                            "查看并处理",
                            icon="arrow_forward",
                            on_click=lambda _=None, rid=str(review.get("id") or ""), pn=project_name: get_overviow_page(
                                pn,
                                False,
                                conversion_review_id=rid,
                            ),
                        ).props("flat dense color=primary size=sm")

    @ui.refreshable
    def render_overview_pending() -> None:
        # A. 待判断概述 (Priority Task)
        over_flat = app.storage.general.get("over_config_data_flat", {})
        my_pending = filter_actionable_overview_pending(
            app.storage.general["overview_charge_pending"].get(current_user, {}),
            over_flat,
            current_role,
            current_user,
        )
        covered_labels = get_conversion_covered_labels(current_user)
        for covered_project, labels in covered_labels.items():
            project_states = my_pending.get(covered_project)
            if not isinstance(project_states, dict):
                continue
            for label in labels:
                if project_states.get(label) == "有待定":
                    project_states.pop(label, None)
            if not project_states:
                my_pending.pop(covered_project, None)
        if my_pending:
            # 待办已按具体用户名分配，不再额外依赖旧角色名单控制显示。
            if isinstance(my_pending, dict):
                with ui.card().classes("w-full rounded-xl shadow-sm border border-red-100 bg-white"):
                    ui_card_header("待处理：项目概述", "edit_document", "red-600")
                    with ui.column().classes("w-full gap-2 px-1 pr-2 max-h-[60vh] overflow-y-auto overflow-x-hidden"):
                        project_summary = app.storage.general.get("project_summary", {})

                        project_states = {
                            project_name: project_summary.get(project_name, {}).get("state", "未知")
                            for project_name in my_pending
                        }
                        visible_pending_items = [
                            (project_name, state_dic)
                            for project_name, state_dic in my_pending.items()
                            if project_states[project_name] not in ["作废", "待定"]
                        ]
                        sorted_pending_items = sort_overview_pending_items(visible_pending_items, project_states)

                        for project_name, state_dic in sorted_pending_items:
                            # 1. 获取已经过滤过的当前项目状态
                            proj_state = project_states[project_name]
                            counts = get_overview_counts(state_dic)
                            labels_map = {
                                "false": "项必填概述无内容",
                                "none": "项概述待确认",
                                "need": "项需填概述无内容",
                            }

                            # 2. 综合项目阶段与概述问题，选出当前最高警示项
                            warning = get_overview_warning(proj_state, counts)
                            if warning is None:
                                continue  # 没有任何积压，跳过渲染

                            active_key, warning_level = warning

                            # 3. 根据综合警示级别决定当前行的视觉色彩与动画表现
                            if warning_level == 4:
                                # 4级警示：紫色行，数字闪烁
                                row_bg = "bg-violet-200 border-violet-400 hover:bg-violet-300"
                                base_color = "violet"
                                is_flash = True
                            elif warning_level == 3:
                                # 3级警示：红色行，数字闪烁
                                row_bg = "bg-red-50 border-red-200 hover:bg-red-100"
                                base_color = "red"
                                is_flash = True
                            elif warning_level == 2:
                                # 2级警示：橙色行，数字闪烁
                                row_bg = "bg-orange-50 border-orange-200 hover:bg-orange-100"
                                base_color = "orange"
                                is_flash = True
                            elif warning_level == 1:
                                # 1级警示：黄色行，数字闪烁
                                row_bg = "bg-amber-50 border-amber-200 hover:bg-amber-100"
                                base_color = "amber"
                                is_flash = False
                            else:
                                # 0级示：蓝色行，仅加粗不闪烁
                                row_bg = "bg-blue-50 border-blue-200 hover:bg-blue-100"
                                base_color = "blue"
                                is_flash = False

                            # --- 提取并构建 HTML 格式的 Tooltip 内容 ---
                            false_items = [k for k, v in state_dic.items() if v == "缺必填"]
                            need_items = [k for k, v in state_dic.items() if v == "缺需填"]
                            none_items = [k for k, v in state_dic.items() if v == "有待定"]

                            tooltip_html = ""
                            if false_items:
                                tooltip_html += "<b>【必填无内容】</b><br>" + "<br>".join(
                                    [f"• {over_flat.get(item, {}).get('title', '未知概述项')}" for item in false_items]
                                )
                            if need_items:
                                if tooltip_html:
                                    tooltip_html += "<br><br>"
                                tooltip_html += "<b>【需填无内容】</b><br>" + "<br>".join(
                                    [f"• {over_flat.get(item, {}).get('title', '未知概述项')}" for item in need_items]
                                )
                            if none_items:
                                if tooltip_html:
                                    tooltip_html += "<br><br>"
                                tooltip_html += "<b>【待确认】</b><br>" + "<br>".join(
                                    [f"• {over_flat.get(item, {}).get('title', '未知概述项')}" for item in none_items]
                                )
                            # ------------------------------------

                            # 4. 渲染最终容器
                            row_animation = "overview-warning-shake" if warning_level >= 3 else ""
                            row_container = ui.row().classes(
                                f"w-full items-center justify-between p-3 rounded-lg border "
                                f"transition-colors {row_bg} {row_animation}"
                            )

                            with row_container:
                                # 构造富文本标题（保持原有的中文阅读排版顺序：必填 -> 需填 -> 待确认）
                                parts_html = []
                                display_order = ["false", "need", "none"]

                                for k in display_order:
                                    num = counts[k]
                                    text = labels_map[k]
                                    # 如果当前项正是触发最高优先级的项，实施视觉凸显
                                    if k == active_key:
                                        if is_flash:
                                            # 缩放、明暗和发光同时变化，使数字警示更醒目
                                            num_html = (
                                                '<span class="overview-warning-flash overview-warning-number '
                                                f'font-black text-lg text-{base_color}-600">{num}</span>'
                                            )
                                        else:
                                            # 仅加粗高亮
                                            num_html = (
                                                f'<span class="font-black text-lg text-{base_color}-600">{num}</span>'
                                            )
                                    else:
                                        num_html = str(num)
                                    parts_html.append(f"{num_html}{text}")

                                title_html = f'<span class="font-medium text-gray-800">{project_name}（{"，".join(parts_html)}）</span>'

                                # ui.element: 创建基础 DOM 元素作为包裹层，避开 v-html 的内部覆盖效应
                                title_wrapper = ui.element("div").classes("cursor-help flex items-center gap-2")

                                with title_wrapper:
                                    ui.html(title_html, sanitize=False)
                                    if warning_level >= 3:
                                        ui.badge("尽快处理", color=base_color).classes(
                                            "overview-warning-flash font-bold"
                                        )
                                    with ui.tooltip().classes("text-xs bg-gray-600/90 text-white p-2"):
                                        ui.html(tooltip_html, sanitize=False)

                                # 侧边按钮的颜色与该行代表的优先级颜色基调保持一致
                                ui.button(
                                    "去处理",
                                    icon="arrow_forward",
                                    on_click=lambda _=None, pn=project_name: get_overviow_page(pn, False),
                                ).props(f"flat dense color={base_color} size=sm")

    @ui.refreshable
    def render_overview_requests() -> None:
        # 概述修改申请审批（单项目内容修改 + 跨项目批量变更）
        all_requests = app.storage.general.get("overview_change_requests", {})
        correction_requests = db_storage.get_item(OVERVIEW_CORRECTION_REQUESTS_KEY, {}) or {}
        visible_correction_requests = {
            rid: request
            for rid, request in correction_requests.items()
            if (request.get("submitter") == current_user and request.get("status") in {"pending", "rejected", "failed"})
            or (
                request.get("status") == "pending"
                and can_review_correction_request(request, current_user, str(current_role or ""))
            )
        }
        batch_requests = db_storage.get_item(BATCH_OVERVIEW_REQUESTS_KEY, {}) or {}
        visible_batch_requests = {
            rid: request
            for rid, request in batch_requests.items()
            if (
                request.get("submitter") == current_user
                and request.get("status") in {"pending", "rejected", "failed", "withdrawn"}
            )
            or (
                request.get("status") == "pending"
                and can_review_batch_overview_request(request, current_user, str(current_role or ""))
            )
        }
        add_requests = db_storage.get_item(OVERVIEW_ADD_REQUESTS_KEY, {}) or {}
        visible_add_requests = {
            rid: request
            for rid, request in add_requests.items()
            if (
                isinstance(request, dict)
                and (
                    (
                        request.get("submitter") == current_user
                        and request.get("status") in {"pending", "rejected", "failed"}
                    )
                    or can_review_overview_add_request(request, current_user)
                )
            )
        }
        visible_single_requests = {
            rid: request
            for rid, request in all_requests.items()
            if (
                request.get("status") == "pending"
                and can_review_overview_correction(request, current_role, current_user)
            )
            or request.get("submitter") == current_user
        }
        if visible_single_requests or visible_correction_requests or visible_batch_requests or visible_add_requests:
            with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white"):
                ui_card_header("概述变更审批", "fact_check", "orange-600")
                batch_todo_count = get_batch_overview_pending_count(
                    batch_requests,
                    current_user,
                    str(current_role or ""),
                )
                if batch_todo_count:
                    ui.badge(f"批量申请待办 {batch_todo_count}", color="red").classes("mb-2")
                correction_todo_count = get_correction_pending_count(
                    correction_requests,
                    current_user,
                    str(current_role or ""),
                )
                if correction_todo_count:
                    ui.badge(f"原记录纠错待办 {correction_todo_count}", color="red").classes("mb-2")
                add_todo_count = get_overview_add_pending_count(add_requests, current_user)
                if add_todo_count:
                    ui.badge(f"关键概述新增待办 {add_todo_count}", color="red").classes("mb-2")
                with ui.column().classes("w-full gap-2"):
                    if (
                        not visible_single_requests
                        and not visible_correction_requests
                        and not visible_batch_requests
                        and not visible_add_requests
                    ):
                        with ui.column().classes("w-full items-center py-8 text-gray-400"):
                            ui.icon("task_alt", size="4em").classes("mb-2 opacity-50")
                            ui.label("当前没有待处理的概述变更申请").classes("text-sm")

                    if visible_single_requests:
                        ui.label("旧版单项目概述变更申请").classes("font-bold text-gray-800")
                        for rid, req in visible_single_requests.items():
                            is_manager = can_review_overview_correction(req, current_role, current_user)
                            is_mine = req.get("submitter") == current_user

                            with ui.row().classes("w-full items-center justify-between p-3 bg-gray-50 rounded border"):
                                with ui.column().classes("gap-1"):
                                    ui.label(f"{req.get('project_name', '')} | {req.get('action', '')}").classes(
                                        "font-bold"
                                    )
                                    ui.label(f"{req.get('old_content', '')} → {req.get('new_content', '')}").classes(
                                        "text-sm text-gray-600"
                                    )
                                    status_badge(req.get("status", ""))

                                with ui.row().classes("gap-2"):
                                    if is_manager and req.get("status") == "pending":
                                        ui.button(
                                            "通过",
                                            color="green",
                                            on_click=lambda r=rid, d=req: handle_approve(r, d),
                                        ).props("dense size=sm")
                                        ui.button(
                                            "驳回",
                                            color="red",
                                            on_click=lambda r=rid: open_reject_modal(r),
                                        ).props("dense size=sm")

                                    if is_mine:
                                        if req.get("status") in ["rejected", "withdrawn"]:
                                            ui.button(
                                                "修改再提",
                                                color="blue",
                                                on_click=lambda d=req: trigger_edit(d),
                                            ).props("dense size=sm")
                                            ui.button(
                                                "放弃申请",
                                                color="grey",
                                                on_click=lambda r=rid: handle_archive(r, "cancelled"),
                                            ).props("dense size=sm")
                                        if req.get("status") == "pending":
                                            ui.button(
                                                "撤销",
                                                color="orange",
                                                on_click=lambda r=rid: handle_withdraw(r),
                                            ).props("dense size=sm")

                    if visible_correction_requests:
                        ui.separator().classes("my-1")
                        ui.label("原记录纠错申请").classes("font-bold text-purple-900")
                        for rid, request in sorted(
                            visible_correction_requests.items(),
                            key=lambda item: item[1].get("updated_at", ""),
                            reverse=True,
                        ):
                            with ui.row().classes(
                                "w-full items-center justify-between p-3 bg-purple-50/40 "
                                "rounded border border-purple-100"
                            ):
                                with ui.column().classes("gap-1 min-w-0"):
                                    ui.label(
                                        f"{request.get('project', '')} ｜ "
                                        f"{request.get('title', request.get('label', '未命名'))} ｜ "
                                        f"{'纠正原记录' if request.get('action') == 'correct' else '删除错误记录'}"
                                    ).classes("font-bold")
                                    ui.label(
                                        f"申请人：{request.get('submitter', '')} ｜ {request.get('updated_at', '')}"
                                    ).classes("text-xs text-gray-600")
                                    status_badge(str(request.get("status") or ""))
                                    if request.get("status") in {"rejected", "failed"}:
                                        ui.label(
                                            f"处理信息：{request.get('reject_reason') or request.get('result', {}).get('message', '')}"
                                        ).classes("text-xs font-bold text-red-700")
                                ui.button(
                                    "查看详情",
                                    icon="open_in_new",
                                    on_click=lambda _=None, request_id=rid: open_correction_request_detail(request_id),
                                ).props("flat dense color=purple size=sm")

                    if visible_add_requests:
                        ui.separator().classes("my-1")
                        ui.label("关键概述新增申请").classes("font-bold text-amber-900")
                        for rid, request in sorted(
                            visible_add_requests.items(),
                            key=lambda item: item[1].get("updated_at", ""),
                            reverse=True,
                        ):
                            payload = request.get("payload") or {}
                            chip = payload.get("chip") or {}
                            with ui.row().classes(
                                "w-full items-center justify-between p-3 bg-amber-50/50 "
                                "rounded border border-amber-100"
                            ):
                                with ui.column().classes("gap-1 min-w-0"):
                                    ui.label(
                                        f"{request.get('project', '')} ｜ "
                                        f"{request.get('title', request.get('label', '未命名'))}"
                                    ).classes("font-bold")
                                    ui.label(
                                        f"申请人：{request.get('submitter', '')} ｜ "
                                        f"内容：{chip.get('content', '')} ｜ "
                                        f"{request.get('updated_at', '')}"
                                    ).classes("text-xs text-gray-600")
                                    status_badge(str(request.get("status") or ""))
                                    if request.get("reject_reason"):
                                        ui.label(f"驳回理由：{request.get('reject_reason')}").classes(
                                            "text-xs font-bold text-red-700"
                                        )
                                    result = request.get("result")
                                    if request.get("status") == "failed" and isinstance(result, dict):
                                        ui.label(f"执行失败：{result.get('message', '')}").classes(
                                            "text-xs font-bold text-red-700"
                                        )
                                with ui.row().classes("gap-2"):
                                    ui.button(
                                        "查看详情",
                                        icon="open_in_new",
                                        on_click=lambda _=None, request_id=rid: open_overview_add_request_detail(
                                            request_id
                                        ),
                                    ).props("flat dense color=amber-9 size=sm")

                    if visible_batch_requests:
                        ui.separator().classes("my-1")
                        ui.label("跨项目批量概述申请").classes("font-bold text-blue-900")
                        for rid, request in sorted(
                            visible_batch_requests.items(),
                            key=lambda item: item[1].get("updated_at", ""),
                            reverse=True,
                        ):
                            payload = request.get("payload") or {}
                            with ui.row().classes(
                                "w-full items-center justify-between p-3 bg-blue-50/40 rounded border border-blue-100"
                            ):
                                with ui.column().classes("gap-1 min-w-0"):
                                    ui.label(
                                        f"{payload.get('title', payload.get('label', '未命名'))} ｜ "
                                        f"{'批量新增' if payload.get('action') == 'add' else '批量改状态'}"
                                    ).classes("font-bold")
                                    ui.label(
                                        f"申请人：{request.get('submitter', '')} ｜ "
                                        f"目标项目：{len(payload.get('projects', []))} 个 ｜ "
                                        f"{request.get('updated_at', '')}"
                                    ).classes("text-xs text-gray-600")
                                    status_badge(str(request.get("status") or ""))
                                    if request.get("status") == "rejected" and request.get("submitter") == current_user:
                                        ui.label(f"驳回理由：{request.get('reject_reason', '')}").classes(
                                            "text-xs font-bold text-red-700"
                                        )
                                ui.button(
                                    "查看详情",
                                    icon="open_in_new",
                                    on_click=lambda _=None, request_id=rid: open_batch_request_detail(request_id),
                                ).props("flat dense color=primary size=sm")
        else:
            ui.label("暂无概述变更申请").classes("text-sm text-gray-400 p-2")

    def refresh_overview_request_sections() -> None:
        """概述变更申请变化后仅更新审批列表和受影响的概述待办。"""
        render_overview_requests.refresh()
        render_overview_pending.refresh()

    # -------------------------------------------------------------------------
    # 页面整体布局
    # -------------------------------------------------------------------------
    # 1. 顶部导航栏 (深色主题)
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("项目待办项").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                if can_revoke_project_requirement_approval(current_role, current_user):
                    ui.menu_item("撤销需求审批", on_click=lambda: create_revoke_dialog()).classes(
                        "text-red-600 font-bold"
                    )
                    ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # 2. 主内容区域 (Grid布局)
    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.element("div").classes("w-full h-full overflow-y-auto overflow-x-hidden p-4 md:p-6"):
            project_engineer_dic = get_project_engineer_project_list_dic()

            # Grid: 大屏12列，左8右4；小屏自动换行
            with ui.grid(columns=12).classes("w-full gap-4"):
                # =========================================================
                # 左侧列 (主要工作流)
                # =========================================================
                with ui.column().classes("col-span-12 lg:col-span-6 gap-4"):
                    with ui.column().classes("w-full gap-0"):
                        render_conversion_reviews()
                        render_overview_pending()

                    # B. 需求评审队列 (Review Queue)
                    has_assigned_projects = bool(project_engineer_dic.get(current_user)) and (
                        has_assigned_requirement_review_permission(current_role, current_user)
                    )
                    if can_edit_requirements() or can_review_all_requirements() or has_assigned_projects:
                        with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white"):
                            ui_card_header("需求评审看板", "rate_review", "blue-600")
                            render_requirement_review_board()

                # =========================================================
                # 右侧列
                # =========================================================
                with ui.column().classes("col-span-12 lg:col-span-6 gap-4"):
                    # D. 草稿箱 (Drafts)
                    can_manage_all_drafts = can_manage_all_project_requirement_drafts(
                        current_role,
                        current_user,
                    )
                    if can_edit_requirements() or can_manage_all_drafts:
                        with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white"):
                            ui_card_header("需求草稿箱", "save_as", "amber-600")

                            temp_req_dic = app.storage.general.get("temp_req", {})
                            drafts = []
                            for user, project_dic in temp_req_dic.items():
                                if user == current_user or can_manage_all_drafts:
                                    for project_name, version_li in project_dic.items():
                                        for version in version_li:
                                            saved_timestamp = _get_draft_saved_timestamp(project_name, version, user)
                                            drafts.append((project_name, version, user, saved_timestamp))
                            drafts.sort(key=lambda draft: (draft[0], draft[3]), reverse=True)

                            with ui.scroll_area().classes("h-90 w-full pr-2"):
                                for project_name, version, user, saved_timestamp in drafts:
                                    row = ui.element("div").classes(
                                        "w-full grid grid-cols-[minmax(0,1fr)_auto] items-start sm:items-center "
                                        "gap-x-3 gap-y-1 py-2 border-b border-gray-100 last:border-0"
                                    )
                                    with row:
                                        saved_time = (
                                            datetime.fromtimestamp(saved_timestamp).strftime("%Y-%m-%d %H:%M:%S")
                                            if saved_timestamp != float("-inf")
                                            else "未知"
                                        )
                                        with ui.element("div").classes(
                                            "min-w-0 grid grid-cols-1 sm:grid-cols-[minmax(0,1fr)_48px_72px_minmax(0,1.25fr)] "
                                            "sm:items-center gap-x-3 gap-y-1"
                                        ):
                                            ui.label(project_name).classes(
                                                "min-w-0 truncate font-medium text-sm text-gray-700"
                                            ).tooltip(project_name)
                                            ui.label(f"V{version}").classes(
                                                "min-w-0 truncate text-xs text-gray-400"
                                            ).tooltip(f"V{version}")
                                            ui.label(user).classes("min-w-0 truncate text-xs text-gray-400").tooltip(
                                                user
                                            )
                                            ui.label(f"保存时间：{saved_time}").classes(
                                                "min-w-0 truncate tabular-nums text-xs text-gray-400"
                                            ).tooltip(f"保存时间：{saved_time}")

                                        with ui.row().classes("shrink-0 self-center gap-1 flex-nowrap justify-end"):
                                            # 跨用户草稿管理权限只允许查看，草稿本人仍可继续编辑。
                                            btn_icon = "visibility" if user != current_user else "edit"
                                            ui.button(
                                                icon=btn_icon,
                                                on_click=lambda pn=project_name, v=version, owner=user: get_req_page(
                                                    pn, v, owner
                                                ),
                                            ).props("flat dense size=sm color=amber").tooltip("查看/编辑")

                                            if user == current_user:
                                                ui.button(
                                                    icon="close",
                                                    color="red",
                                                    on_click=lambda r=row, pn=project_name, v=version: (
                                                        dele_temp_req_row(r, pn, v)
                                                    ),
                                                ).props("flat dense size=sm").tooltip("丢弃草稿")

                            if not drafts:
                                ui.label("暂无草稿记录").classes("text-sm text-gray-400 p-2")
                    with ui.column().classes("w-full gap-0"):
                        render_overview_requests()

    async def reconcile_visible_conversion_reviews() -> None:
        project_summary = app.storage.general.get("project_summary", {})
        if isinstance(project_summary, dict):
            for project_name, project_value in project_summary.items():
                if isinstance(project_value, dict) and project_value.get("state") == "转产":
                    await reconcile_conversion_reviews(str(project_name))
        _refresh_conversion_sections()

    schedule_background_task(
        reconcile_visible_conversion_reviews(),
        "校准转产概述职能汇总待办",
    )
