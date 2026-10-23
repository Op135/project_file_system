"""已录需求的转产项目概述完成进度。"""

import logging
from functools import partial
from urllib.parse import urlencode

from nicegui import app, ui
from nicegui.events import ValueChangeEventArguments

from .. import db_storage
from ..config import IMG_DIR, PRESET_AVATARS
from ..overview_trial_progress import (
    IMPORTANCE_STORAGE_KEY,
    ProjectProgress,
    build_trial_progress,
    can_view_trial_progress,
    get_latest_importance,
    normalize_importance,
    prune_inactive_importance,
    set_project_importance,
)
from ..utils import (
    get_cache_busted_path,
    logout,
    setup_global_activity_tracking,
    sync_current_user_role,
)

logger = logging.getLogger(__name__)


@ui.page("/trial_progress")
def trial_progress_page() -> None:
    current_user = app.storage.user.get("current_user")
    if not isinstance(current_user, str) or not current_user:
        ui.navigate.to("/login")
        return
    sync_current_user_role()
    if not can_view_trial_progress(current_user):
        logger.warning("用户 %s 无权查看转产进度页面", current_user)
        ui.notify("当前账号没有查看转产进度的权限", type="negative")
        ui.navigate.to("/main")
        return

    setup_global_activity_tracking("trial_progress")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    avatar = get_cache_busted_path(user_prefs.get("avatar", PRESET_AVATARS[0]))
    importance = normalize_importance(db_storage.get_item(IMPORTANCE_STORAGE_KEY, {}))
    shown_rows: tuple[ProjectProgress, ...] = ()

    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4 z-50"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("转产进度").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(avatar)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {current_user}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=logout)

    with ui.dialog() as details_dialog, ui.card().classes("w-full max-w-2xl max-h-[85vh] overflow-y-auto p-5"):
        details_content = ui.column().classes("w-full")

    def open_project_overview(project: str) -> None:
        if not can_view_trial_progress(current_user):
            ui.notify("当前账号没有查看转产进度的权限", type="negative")
            return
        query = urlencode({"type": "requirement", "project_name": project})
        ui.navigate.to(f"/main/requirement?{query}")

    def open_details(progress: ProjectProgress) -> None:
        if not can_view_trial_progress(current_user):
            ui.notify("当前账号没有查看转产进度的权限", type="negative")
            return
        details_content.clear()
        with details_content:
            ui.label(f"{progress.project} · 转产概述进度 {progress.percentage}%").classes("text-lg font-bold")
            for role in progress.roles:
                with ui.column().classes("w-full gap-1 border-b border-slate-100 py-2"):
                    ui.label(f"{role.role} · {role.assignee}").classes("font-medium")
                    if role.skipped:
                        ui.label("整个职能不需录入").classes("text-xs text-slate-500")
                    elif role.missing:
                        for title, state in role.missing:
                            ui.label(f"• {title}（{state}）").classes("text-sm text-red-700")
                    else:
                        ui.label("必填概述已完成").classes("text-sm text-green-700")
            if progress.percentage == 100:
                ui.label("概述进度已完成，请前往项目需求概述页面将状态修改为试产。").classes("text-sm text-green-800")
                ui.button(
                    "前往项目需求概述",
                    on_click=partial(open_project_overview, progress.project),
                    icon="open_in_new",
                )
            ui.button("关闭", on_click=details_dialog.close).props("flat")
        details_dialog.open()

    async def change_importance(project: str, event: ValueChangeEventArguments) -> None:
        value = event.value
        if not isinstance(value, (int, float)) or isinstance(value, bool) or int(value) != value:
            ui.notify("重要程度无效，请刷新后重试", type="negative")
            await refresh_progress(force=True)
            return
        level = int(value)
        if not await set_project_importance(project, level, actor=current_user):
            ui.notify("重要程度保存失败，请刷新后重试", type="negative")
        await refresh_progress(force=True)

    def current_progress_rows() -> list[ProjectProgress]:
        rows = build_trial_progress(
            app.storage.general.get("project_summary", {}),
            app.storage.general.get("project_req_max_ver", {}),
            app.storage.general.get("overview_role", {}),
            app.storage.general.get("over_config_data", {}),
            app.storage.general.get("overview_charge_pending", {}),
        )
        rows.sort(key=lambda row: (-importance.get(row.project, 0), row.project))
        return rows

    async def refresh_progress(*, force: bool = False) -> None:
        if not can_view_trial_progress(current_user):
            ui.navigate.to("/main")
            return
        summary = app.storage.general.get("project_summary", {})
        if isinstance(summary, dict) and summary:
            await prune_inactive_importance(summary)
        latest = await get_latest_importance()
        previous_importance = importance.copy()
        importance.clear()
        importance.update(latest)
        if not force and previous_importance == latest and tuple(current_progress_rows()) == shown_rows:
            return
        try:
            raw_scroll_top = await ui.run_javascript(
                "const card = document.getElementById('trial-progress-card-scroll'); "
                "return card ? card.scrollTop : 0;"
            )
            scroll_top = float(raw_scroll_top or 0)
        except Exception:
            scroll_top = 0.0
        render_progress.refresh()
        if scroll_top:
            try:
                await ui.run_javascript(
                    f"""
                    const restoreTrialProgressScroll = () => {{
                        const card = document.getElementById('trial-progress-card-scroll');
                        if (card) card.scrollTop = {scroll_top};
                    }};
                    requestAnimationFrame(() => requestAnimationFrame(restoreTrialProgressScroll));
                    setTimeout(restoreTrialProgressScroll, 80);
                    """
                )
            except Exception:
                logger.debug("恢复转产进度滚动位置失败", exc_info=True)

    @ui.refreshable
    def render_progress() -> None:
        nonlocal shown_rows
        if not can_view_trial_progress(current_user):
            ui.navigate.to("/main")
            return
        progress_rows = current_progress_rows()
        shown_rows = tuple(progress_rows)
        with ui.element("div").classes("grid w-full grid-cols-1 lg:grid-cols-2 gap-4"):
            with (
                ui.card()
                .classes(
                    "w-full min-w-0 !p-0 !gap-0 overflow-x-hidden overflow-y-auto overscroll-contain "
                    "rounded-xl border border-slate-200 shadow-sm"
                )
                .style("max-height: min(720px, calc(100vh - 6rem));")
                .props("id=trial-progress-card-scroll")
            ):
                with ui.row().classes(
                    "sticky top-0 z-10 w-full shrink-0 items-center justify-between gap-2 "
                    "px-4 py-3 bg-white border-b border-slate-200"
                ):
                    ui.label("转产项目概述完成进度").classes("text-lg font-bold text-slate-800")
                    ui.label(f"{len(progress_rows)} 个已录需求项目").classes("text-xs text-slate-500")
                if not progress_rows:
                    ui.label("暂无已录需求的转产项目").classes("p-5 text-sm text-slate-500")
                for progress in progress_rows:
                    level = importance.get(progress.project, 0)
                    with ui.column().classes(
                        "w-full min-w-0 shrink-0 gap-2 px-4 py-3 border-b border-slate-100 last:border-b-0"
                    ):
                        with ui.row().classes("w-full min-w-0 items-center justify-between gap-2"):
                            ui.label(progress.project).classes("font-semibold text-slate-800 break-all")
                            ui.label(f"{progress.percentage}% · {progress.completed}/{progress.total}").classes(
                                "shrink-0 text-sm font-medium text-slate-600"
                            )
                        with (
                            ui.element("div")
                            .classes("w-full py-1 cursor-pointer")
                            .on("click", lambda _, row=progress: open_details(row))
                            .tooltip("点击查看职能负责人及未完成概述")
                        ):
                            ui.linear_progress(
                                value=progress.percentage / 100,
                                size="10px",
                                show_value=False,
                                color="positive" if progress.percentage == 100 else "primary",
                            ).classes("w-full rounded-full")
                        with ui.row().classes("w-full items-center justify-between gap-2"):
                            ui.rating(
                                value=level,
                                max=5,
                                color="grey-5",
                                size="20px",
                                on_change=partial(change_importance, progress.project),
                            ).props("color-selected=amber-6")
                            if progress.percentage == 100:
                                ui.button(
                                    "前往切换试产", on_click=partial(open_project_overview, progress.project)
                                ).props("flat dense no-caps color=positive size=sm")
                            else:
                                ui.button("查看明细", on_click=partial(open_details, progress)).props(
                                    "flat dense no-caps size=sm"
                                ).classes("text-slate-600")

    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-y-auto bg-gray-50 p-4 md:p-6"):
        render_progress()
    ui.timer(0.1, refresh_progress, once=True)
    ui.timer(10, refresh_progress)
