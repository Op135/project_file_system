"""转产概述职能确认的右侧快捷处理面板。"""

import asyncio
import logging
from collections.abc import Callable

from nicegui import app, ui

from . import db_storage
from .issue_workflow_utils import schedule_background_task
from .overview_conversion_review import (
    OPEN_REVIEW_STATUSES,
    collect_review_pending_items,
    get_conversion_reviews,
    process_conversion_review_batch,
    process_conversion_review_item,
    reconcile_conversion_reviews,
)
from .overview_corrections import configured_test_display_values

logger = logging.getLogger(__name__)


def open_conversion_review_side_panel(
    review_id: str,
    current_user: str,
    expected_project: str,
    *,
    on_updated: Callable[[], None] | None = None,
) -> bool:
    """在页面右侧打开无背景遮罩的职能汇总面板，保留左侧需求正文可读、可滚动。"""
    reviews = get_conversion_reviews()
    review = reviews.get(review_id)
    if (
        not isinstance(review, dict)
        or review.get("status") not in OPEN_REVIEW_STATUSES
        or review.get("assignee") != current_user
        or review.get("project") != expected_project
    ):
        ui.notify("该职能汇总待办已完成、已转交或不属于当前项目。", type="warning")
        return False

    panel = ui.dialog().props("persistent seamless position=right")

    def notify_updated() -> None:
        if on_updated is not None:
            on_updated()

    def render_panel() -> None:
        latest_review = get_conversion_reviews().get(review_id)
        if (
            not isinstance(latest_review, dict)
            or latest_review.get("status") not in OPEN_REVIEW_STATUSES
            or latest_review.get("assignee") != current_user
            or latest_review.get("project") != expected_project
        ):
            panel.close()
            notify_updated()
            return

        items = collect_review_pending_items(latest_review)
        if not items:

            async def finish_empty_review() -> None:
                await reconcile_conversion_reviews(expected_project)
                panel.close()
                notify_updated()

            schedule_background_task(finish_empty_review(), "完成空转产职能汇总待办")
            return

        role = str(latest_review.get("role") or "")
        exception_values = {
            str(value)
            for value in latest_review.get("exception_target_ids", [])
            if str(value)
        }
        exception_checks: dict[str, object] = {}
        special_count = sum(1 for item in items if item.has_special_reason)
        is_submitting = False
        submit_spinner = None
        submit_status = None
        close_button = None
        submit_button = None

        panel.clear()
        with panel, ui.card().classes(
            "w-[58vw] max-w-[58vw] h-[calc(100vh-1rem)] m-0 p-3 gap-3 overflow-hidden "
            "border-l-2 border-blue-200 shadow-xl"
        ):
            with ui.row().classes("w-full items-start justify-between gap-3 shrink-0"):
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(f"{expected_project}｜{role}职能转产概述确认").classes(
                        "text-xl font-bold text-blue-900"
                    )
                    ui.label(
                        f"待确认范围：项目需求版本 V{latest_review.get('version', '')} · "
                        f"待确认 {len(items)} 个 · 特殊原因 {special_count} 个"
                    ).classes("text-sm text-gray-600")
                status_text = "继续考虑" if latest_review.get("status") == "exceptions" else "待确认"
                ui.badge(
                    status_text,
                    color="orange" if latest_review.get("status") == "exceptions" else "red",
                )

            ui.label(
                "操作提示：请结合左侧项目需求内容判断；提交后，未勾选内容统一激活，"
                "勾选“保留待定”的内容继续留在待办。本面板不设置关联影响；需要评估关联影响的内容，"
                "请先保留待定，关闭面板后在概述区仔细处理。"
            ).classes(
                "text-sm leading-snug font-medium text-blue-900 bg-blue-50 border border-blue-200 "
                "rounded px-2 py-1.5 w-full shrink-0"
            )

            if special_count:
                ui.label(
                    "以下列表已将转产前待定、转产后新增及关联影响等特殊内容置顶，请重点核对。"
                ).classes("text-sm font-bold text-orange-800 shrink-0")

            with ui.scroll_area().classes("w-full grow min-h-0 border rounded p-2"):
                current_section = ""
                for item in items:
                    section = "重点核对" if item.has_special_reason else "常规转产确认"
                    if section != current_section:
                        current_section = section
                        ui.label(section).classes(
                            "w-full text-sm font-bold mt-1 "
                            + ("text-red-700" if section == "重点核对" else "text-blue-700")
                        )
                    border_class = (
                        "border-orange-300 bg-orange-50"
                        if item.has_special_reason
                        else "border-gray-200 bg-white"
                    )
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
                                        [f"{expected_project}_over_data", item.label, item.chip_id],
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
                                        "w-full text-xs rounded px-2 py-1 whitespace-normal break-all "
                                        f"{color_class}"
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
                                processing_notice = ui.notification(
                                    "正在处理该概述，请稍候…",
                                    timeout=None,
                                    spinner=True,
                                )
                                await asyncio.sleep(0)
                                try:
                                    result = await process_conversion_review_item(
                                        review_id,
                                        target_id,
                                        actor=current_user,
                                        target_state=target_state,
                                    )
                                    ui.notify(
                                        result.message,
                                        type="positive" if result.ok else "warning",
                                    )
                                except Exception as exc:
                                    logger.error(
                                        "处理转产职能汇总单项失败: review_id=%s, target_id=%s",
                                        review_id,
                                        target_id,
                                        exc_info=True,
                                    )
                                    ui.notify(f"处理失败：{exc}", type="negative", timeout=0)
                                finally:
                                    processing_notice.dismiss()
                                notify_updated()
                                render_panel()

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
                    notify_updated()
                    panel.close()
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

            with ui.row().classes("w-full justify-end items-center gap-2 shrink-0"):
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
                close_button = ui.button("关闭面板", on_click=panel.close).props("flat color=grey")
                submit_button = ui.button(
                    "确认可激活内容并保留所选待定",
                    icon="done_all",
                    on_click=submit_role_review,
                ).props("color=primary")

    render_panel()
    panel.open()
    return True
