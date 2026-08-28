# -*- encoding: utf-8 -*-
import asyncio
import copy
import json
import logging
import mimetypes
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from nicegui import app, ui

from .. import db_storage
from ..components import FileThumbnail, OverviewReasonSelector
from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR, REQ_REMOVE_DIR
from ..overview_batch_operations import (
    BATCH_OVERVIEW_REQUESTS_KEY,
    BATCH_OVERVIEW_STAGING_DIR,
    can_review_batch_overview_request,
    execute_batch_overview_request,
    get_batch_overview_pending_count,
    update_batch_overview_request,
)
from ..overview_corrections import (
    OVERVIEW_CORRECTION_REQUESTS_KEY,
    archive_correction_request,
    build_correction_changes,
    can_review_correction_request,
    cleanup_correction_staged_files,
    execute_correction_request,
    get_correction_pending_count,
    update_correction_request,
    validate_staged_path,
)
from ..overview_warning import get_overview_counts, get_overview_warning, sort_overview_pending_items
from ..project_requirement_access import (
    can_edit_project_requirement,
    can_manage_all_project_requirement_drafts,
    can_review_all_project_requirements,
    can_review_project_requirement,
    can_revoke_project_requirement_approval,
    has_assigned_requirement_review_permission,
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

# 获取 logger
logger = logging.getLogger(__name__)

# 同一项目的审批必须串行，避免重复点击或不同客户端同时通过同一版本。
_requirement_review_locks = defaultdict(asyncio.Lock)
_batch_overview_review_locks = defaultdict(asyncio.Lock)
_overview_correction_review_locks = defaultdict(asyncio.Lock)
_correction_preview_routes: set[str] = set()


def _reload_after_dialog_transition(delay: float = 0.4) -> None:
    """给弹窗关闭动画留出时间，再刷新待办页。"""
    ui.timer(delay, ui.navigate.reload, once=True)


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
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")
        return

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()
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
    current_user = app.storage.user.get("current_user", "匿名用户")
    current_role = sync_current_user_role()

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

    # 读取配置文件
    try:
        with open(f"{BASE_DIR}/module_show_role.json", "r", encoding="utf-8") as f:
            module_show_data = json.load(f)
    except Exception as e:
        logger.error(f"无法读取权限配置: {e}")
        module_show_data = {}  # 防止报错

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

            # 刷新UI行
            refresh_review_row(container_row, p_name, v)
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
        if not can_review_requirement(p_name):
            ui.notify("当前账号无权移除该需求记录", type="negative")
            return
        move_file_with_timestamp_pathlib(f"{REQ_DIR}/{p_name}_需求配置_V{v}.json", REQ_REMOVE_DIR)
        delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")
        app.storage.general["wait_review"][p_name].pop(v, None)
        container_row.delete()  # 删除整行UI
        dialog.close()

    def remove_requirement_dialog(container_row, p_name, v):
        if not can_review_requirement(p_name):
            ui.notify("当前账号无权移除该需求记录", type="negative")
            return
        dialog.clear()
        with dialog, ui.card():
            ui.label("⚠️ 危险操作").classes("text-lg font-bold text-red-600")
            ui.label(f"确认移除 {p_name}_V{v} 吗？移除后需联系管理员恢复。")
            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("确认移除", color="red", on_click=lambda: remove_requirement_file(container_row, p_name, v))
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
        # 全局审批人仍可看到待修改记录；仅负责具体项目的审批人在退回后不再保留待办。
        if review_state == "已审" or review_state == "待修改" and not can_review_all and can_review:
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

                            ui.button(
                                icon="check",
                                color="green",
                                on_click=lambda: set_review_pass_dialog(container, project_name, ver),
                            ).props("flat round dense").tooltip("审核通过")

                            ui.button(
                                icon="edit_note", color="orange", on_click=lambda: set_review_revise(project_name, ver)
                            ).on("click", lambda: refresh_review_row(container, project_name, ver)).props(
                                "flat round dense"
                            ).tooltip("退回修改")

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
                            ui.button(
                                icon="edit_note", color="blue", on_click=lambda: get_requirement_page(project_name, ver)
                            ).props("flat round dense").tooltip("配置需求")

                            ui.button(
                                icon="replay", color="orange", on_click=lambda: set_review_revise(project_name, ver)
                            ).on("click", lambda: refresh_review_row(container, project_name, ver)).props(
                                "flat round dense"
                            ).tooltip("申请修改")

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
        app.storage.general["overview_change_requests"][req_id]["status"] = "withdrawn"
        ui.notify("申请已撤回")
        ui.navigate.reload()

    # 2. 审批通过 (执行物理动作 + 数据更新 + 归档)
    async def handle_approve(req_id, req_data):
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
            ui.navigate.reload()
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

    def open_reject_modal(req_id):
        """弹出驳回理由填写对话框"""
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
                    ui.navigate.reload()  # 刷新页面以更新列表
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
        ui.navigate.reload()

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
                _reload_after_dialog_transition()
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
                    _reload_after_dialog_transition()
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
            except Exception as exc:
                logger.error("执行单项概述纠错失败: request_id=%s", request_id, exc_info=True)
                await update_correction_request(
                    request_id,
                    {"status": "failed", "result": {"ok": False, "message": str(exc)}},
                )
                ui.notify(f"纠错审批异常：{exc}", type="negative", timeout=0)
            finally:
                processing_notice.dismiss()
            _reload_after_dialog_transition()

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
                ui.navigate.reload()

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
        ui.navigate.reload()

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
                _reload_after_dialog_transition()
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
            _reload_after_dialog_transition()

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
                ui.navigate.reload()

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
                ui.navigate.reload()

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
                    # A. 待判断概述 (Priority Task)
                    if current_role in module_show_data.get("overview_charge_pending_module", []):
                        my_pending = app.storage.general["overview_charge_pending"].get(current_user, {})
                        if my_pending:
                            with ui.card().classes("w-full rounded-xl shadow-sm border border-red-100 bg-white"):
                                ui_card_header("待处理：项目概述", "edit_document", "red-600")
                                with ui.column().classes(
                                    "w-full gap-2 px-1 pr-2 max-h-[60vh] overflow-y-auto overflow-x-hidden"
                                ):
                                    over_flat = app.storage.general.get("over_config_data_flat", {})
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
                                    sorted_pending_items = sort_overview_pending_items(
                                        visible_pending_items, project_states
                                    )

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
                                                [
                                                    f"• {over_flat.get(item, {}).get('title', '未知概述项')}"
                                                    for item in false_items
                                                ]
                                            )
                                        if need_items:
                                            if tooltip_html:
                                                tooltip_html += "<br><br>"
                                            tooltip_html += "<b>【需填无内容】</b><br>" + "<br>".join(
                                                [
                                                    f"• {over_flat.get(item, {}).get('title', '未知概述项')}"
                                                    for item in need_items
                                                ]
                                            )
                                        if none_items:
                                            if tooltip_html:
                                                tooltip_html += "<br><br>"
                                            tooltip_html += "<b>【待确认】</b><br>" + "<br>".join(
                                                [
                                                    f"• {over_flat.get(item, {}).get('title', '未知概述项')}"
                                                    for item in none_items
                                                ]
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
                                                        num_html = f'<span class="font-black text-lg text-{base_color}-600">{num}</span>'
                                                else:
                                                    num_html = str(num)
                                                parts_html.append(f"{num_html}{text}")

                                            title_html = f'<span class="font-medium text-gray-800">{project_name}（{"，".join(parts_html)}）</span>'

                                            # ui.element: 创建基础 DOM 元素作为包裹层，避开 v-html 的内部覆盖效应
                                            title_wrapper = ui.element("div").classes(
                                                "cursor-help flex items-center gap-2"
                                            )

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

                    # B. 需求评审队列 (Review Queue)
                    has_assigned_projects = bool(project_engineer_dic.get(current_user)) and (
                        has_assigned_requirement_review_permission(current_role, current_user)
                    )
                    if can_edit_requirements() or can_review_all_requirements() or has_assigned_projects:
                        with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white"):
                            ui_card_header("需求评审看板", "rate_review", "blue-600")

                            review_container = ui.column().classes("w-full gap-3")
                            has_review_data = False

                            with review_container:
                                if app.storage.general.get("wait_review", {}):
                                    for project_name, ver_dic in app.storage.general["wait_review"].items():
                                        for ver, dic in ver_dic.items():
                                            # 过滤显示逻辑
                                            is_reviewer = can_review_requirement(project_name)
                                            is_global_reviewer = can_review_all_requirements()
                                            is_submitter = dic.get("submitter") == current_user

                                            should_show = False
                                            # 全局审批人或提交人查看尚未完成的需求记录。
                                            if (is_global_reviewer or is_submitter) and dic.get("state") != "已审":
                                                should_show = True
                                            # 具体项目审批人只查看分配给自己的待审记录。
                                            elif is_reviewer and dic.get("state") == "待审":
                                                should_show = True

                                            if should_show:
                                                has_review_data = True
                                                # 创建行容器
                                                row = ui.row().classes("w-full p-0 gap-0")
                                                refresh_review_row(row, project_name, ver)

                            if not has_review_data:
                                with ui.column().classes("w-full items-center py-8 text-gray-400"):
                                    ui.icon("task_alt", size="4em").classes("mb-2 opacity-50")
                                    ui.label("当前没有待评审的需求").classes("text-sm")

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
                            has_drafts = False

                            with ui.scroll_area().classes("h-64 w-full pr-2"):
                                for user, project_dic in temp_req_dic.items():
                                    if user == current_user or can_manage_all_drafts:
                                        for project_name, version_li in project_dic.items():
                                            for version in version_li:
                                                has_drafts = True
                                                row = ui.row().classes(
                                                    "w-full items-center justify-between py-2 border-b border-gray-100 last:border-0"
                                                )
                                                with row:
                                                    with ui.column().classes("gap-0"):
                                                        ui.label(project_name).classes(
                                                            "font-medium text-sm text-gray-700"
                                                        )
                                                        ui.label(f"V{version} • {user}").classes(
                                                            "text-xs text-gray-400"
                                                        )

                                                    with ui.row().classes("gap-1"):
                                                        # 跨用户草稿管理权限只允许查看，草稿本人仍可继续编辑。
                                                        btn_icon = "visibility" if user != current_user else "edit"
                                                        ui.button(
                                                            icon=btn_icon,
                                                            on_click=lambda pn=project_name, v=version, owner=user: (
                                                                get_req_page(pn, v, owner)
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

                            if not has_drafts:
                                ui.label("暂无草稿记录").classes("text-sm text-gray-400 p-2")
                    # 概述修改申请审批（单项目内容修改 + 跨项目批量变更）
                    all_requests = app.storage.general.get("overview_change_requests", {})
                    correction_requests = db_storage.get_item(OVERVIEW_CORRECTION_REQUESTS_KEY, {}) or {}
                    visible_correction_requests = {
                        rid: request
                        for rid, request in correction_requests.items()
                        if (
                            request.get("submitter") == current_user
                            and request.get("status") in {"pending", "rejected", "failed"}
                        )
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
                    can_show_single_requests = current_role in module_show_data.get("overview_change_requests", [])
                    visible_single_requests = {
                        rid: request
                        for rid, request in all_requests.items()
                        if (current_role == "研发经理" and request.get("status") == "pending")
                        or request.get("submitter") == current_user
                    }
                    if can_show_single_requests or visible_correction_requests or visible_batch_requests:
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
                            with ui.column().classes("w-full gap-2"):
                                if (
                                    not visible_single_requests
                                    and not visible_correction_requests
                                    and not visible_batch_requests
                                ):
                                    with ui.column().classes("w-full items-center py-8 text-gray-400"):
                                        ui.icon("task_alt", size="4em").classes("mb-2 opacity-50")
                                        ui.label("当前没有待处理的概述变更申请").classes("text-sm")

                                if visible_single_requests:
                                    ui.label("旧版单项目概述变更申请").classes("font-bold text-gray-800")
                                    for rid, req in visible_single_requests.items():
                                        is_manager = current_role == "研发经理"
                                        is_mine = req.get("submitter") == current_user

                                        with ui.row().classes(
                                            "w-full items-center justify-between p-3 bg-gray-50 rounded border"
                                        ):
                                            with ui.column().classes("gap-1"):
                                                ui.label(
                                                    f"{req.get('project_name', '')} | {req.get('action', '')}"
                                                ).classes("font-bold")
                                                ui.label(
                                                    f"{req.get('old_content', '')} → {req.get('new_content', '')}"
                                                ).classes("text-sm text-gray-600")
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
                                                    f"申请人：{request.get('submitter', '')} ｜ "
                                                    f"{request.get('updated_at', '')}"
                                                ).classes("text-xs text-gray-600")
                                                status_badge(str(request.get("status") or ""))
                                                if request.get("status") in {"rejected", "failed"}:
                                                    ui.label(
                                                        f"处理信息：{request.get('reject_reason') or request.get('result', {}).get('message', '')}"
                                                    ).classes("text-xs font-bold text-red-700")
                                            ui.button(
                                                "查看详情",
                                                icon="open_in_new",
                                                on_click=lambda _=None, request_id=rid: open_correction_request_detail(
                                                    request_id
                                                ),
                                            ).props("flat dense color=purple size=sm")

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
                                                if (
                                                    request.get("status") == "rejected"
                                                    and request.get("submitter") == current_user
                                                ):
                                                    ui.label(f"驳回理由：{request.get('reject_reason', '')}").classes(
                                                        "text-xs font-bold text-red-700"
                                                    )
                                            ui.button(
                                                "查看详情",
                                                icon="open_in_new",
                                                on_click=lambda _=None, request_id=rid: open_batch_request_detail(
                                                    request_id
                                                ),
                                            ).props("flat dense color=primary size=sm")
                    else:
                        ui.label("暂无概述变更申请").classes("text-sm text-gray-400 p-2")
