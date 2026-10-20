"""ECN 通用附件清单与自定义上传控件。"""

from __future__ import annotations

import json
from typing import Any, Callable

from nicegui import app, ui

from ...custom_ui import CompactUpload, CustomUploadRemovedEventArguments, custom_upload
from ...ecn_access import get_active_ecn_actor_role
from ...ecn_management_config import ECN_ATTACHMENT_CONFIG
from ...image_preview import show_interactive_image_preview
from .attachment_preview import (
    attachment_kind,
    issue_attachment_preview_url,
    issue_overview_scheme_preview_url,
    issue_staged_attachment_preview_url,
)
from .attachments import (
    attach_existing,
    cleanup_staged,
    resolve_attachment,
    remove_existing,
    resolve_staged_attachment,
    stage_upload,
    visible_attachments,
)


def _show_image_preview(url: str) -> None:
    show_interactive_image_preview(url)


def _open_pdf_in_new_tab(url: str) -> None:
    """沿用普通概述页的浏览器原生打开方式，避免导航组件的新标签页被拦截。"""
    ui.run_javascript(f"window.open({json.dumps(url)}, '_blank');")


def open_ecn_attachment_file(
    ecn_id: str, scope: str, item_id: str, task_key: str, file_id: str, user: str, role: str,
) -> None:
    service = getattr(app.state, "user_service", None)
    fresh = visible_attachments(ecn_id, scope, item_id, task_key, user, role, service)
    current = next((entry for entry in fresh if str(entry.get("id")) == file_id), None)
    if current is None:
        ui.notify("附件已变化或无权查看", type="warning")
        return
    path = resolve_attachment(current)
    if not path.is_file():
        ui.notify("附件文件不存在", type="warning")
        return
    name = str(current.get("name") or "附件")
    kind = attachment_kind(name)
    if kind == "image":
        _show_image_preview(issue_attachment_preview_url(
            ecn_id, scope, item_id, task_key, file_id, user, role,
        ))
    elif kind == "pdf":
        _open_pdf_in_new_tab(issue_attachment_preview_url(
            ecn_id, scope, item_id, task_key, file_id, user, role,
        ))
    else:
        ui.download(path, name)


def open_staged_ecn_attachment_file(attachment: dict, user: str, role: str) -> None:
    service = getattr(app.state, "user_service", None)
    if attachment.get("uploaded_by") != user or get_active_ecn_actor_role(
        user, role, user_service=service,
    ) is None:
        ui.notify("当前无法查看此暂存附件", type="warning")
        return
    path = resolve_staged_attachment(attachment)
    if not path.is_file():
        ui.notify("暂存附件不存在", type="warning")
        return
    name = str(attachment.get("name") or "附件")
    kind = attachment_kind(name)
    if kind == "image":
        _show_image_preview(issue_staged_attachment_preview_url(attachment, user, role))
    elif kind == "pdf":
        _open_pdf_in_new_tab(issue_staged_attachment_preview_url(attachment, user, role))
    else:
        ui.download(path, name)


def open_overview_scheme_upload(
    ecn_id: str,
    item_id: str,
    attachment: dict,
    user: str,
    role: str,
) -> None:
    """按文件类型打开系统内资料方案中已经归档的原文件。"""
    name = str(attachment.get("name") or "附件")
    file_id = str(attachment.get("id") or "")
    url = issue_overview_scheme_preview_url(ecn_id, item_id, file_id, user, role)
    kind = attachment_kind(name)
    if kind == "image":
        _show_image_preview(url)
    elif kind == "pdf":
        _open_pdf_in_new_tab(url)
    else:
        ui.download(url, name)


def render_ecn_ecr_attachments(ecn_id: str, user: str, role: str, *, can_upload: bool) -> None:
    """在 ECR 表单内直接展示附件；仅申请人编辑时提供上传控件。"""
    service = getattr(app.state, "user_service", None)
    content = ui.element("div").classes(
        "w-full grid grid-cols-1 md:grid-cols-2 gap-5 items-start" if can_upload else "w-full"
    )
    with content, ui.column().classes("w-full min-w-0 gap-2"):
        ui.label("已上传附件（点击文件名预览或下载）").classes("text-xs font-semibold text-slate-600")
        list_host = ui.column().classes("w-full gap-3 max-h-[45vh] overflow-y-auto")

    def render_list() -> None:
        list_host.clear()
        files = visible_attachments(ecn_id, "ecr", "", "", user, role, service)
        with list_host:
            if not files:
                ui.label("暂无附件").classes("text-xs text-slate-400 py-2")
            for attachment in files:
                file_id = str(attachment.get("id") or "")
                name = str(attachment.get("name") or "附件")
                kind = attachment_kind(name)

                def open_file(fid=file_id) -> None:
                    open_ecn_attachment_file(ecn_id, "ecr", "", "", fid, user, role)

                with ui.row().classes(
                    "w-full min-h-[56px] items-center gap-2 rounded-lg "
                    "border border-slate-200 bg-white px-3 py-2"
                ):
                    if kind == "image":
                        ui.image(issue_attachment_preview_url(
                            ecn_id, "ecr", "", "", file_id, user, role,
                        )).classes("w-10 h-10 object-cover rounded cursor-pointer shrink-0").on(
                            "click", open_file,
                        )
                    else:
                        ui.icon("picture_as_pdf" if kind == "pdf" else "attach_file", size="xs").classes(
                            "text-slate-500 shrink-0"
                        )
                    ui.label(name).classes(
                        "flex-1 min-w-0 text-sm break-all cursor-pointer hover:underline text-indigo-700"
                    ).on("click", open_file)
                    action_icon = "open_in_new" if kind == "pdf" else "zoom_in" if kind == "image" else "download"
                    ui.button(icon=action_icon, on_click=open_file).props("flat round dense size=sm")
                    if can_upload and attachment.get("uploaded_by") == user:
                        async def remove_file(fid=file_id) -> None:
                            result = await remove_existing(ecn_id, "ecr", "", "", fid, user, role, service)
                            if not result.ok:
                                ui.notify(result.message, type="warning")
                                return
                            ui.notify("附件已删除", type="positive")
                            render_list()

                        ui.button(icon="delete_outline", on_click=remove_file).props(
                            "flat round dense size=sm color=red-5"
                        ).tooltip("删除我上传的附件")

    render_list()
    if not can_upload:
        return

    async def handle_upload(event: Any) -> None:
        staged: dict | None = None
        try:
            staged = await stage_upload(event.file, user)
            result = await attach_existing(ecn_id, "ecr", "", "", staged, user, role, service)
            if not result.ok:
                event.sender.reset()
                ui.notify(result.message, type="warning")
                return
            event.sender.reset()
            ui.notify("附件上传成功", type="positive")
            render_list()
        except Exception as exc:
            event.sender.reset()
            ui.notify(f"上传失败：{exc}", type="negative")
        finally:
            if staged is not None:
                cleanup_staged([staged])

    with content, ui.column().classes("w-full min-w-0 gap-2"):
        ui.label("添加申请附件（可选）").classes("text-xs font-semibold text-slate-600")
        custom_upload(
            multiple=True,
            max_file_size=int(ECN_ATTACHMENT_CONFIG["max_file_size_mb"]) * 1024 * 1024,
            on_upload=handle_upload,
        ).props("accept=*/*")


def open_ecn_attachment_dialog(
    ecn_id: str,
    scope: str,
    item_id: str,
    task_key: str,
    title: str,
    user: str,
    role: str,
    *,
    can_upload: bool,
    on_updated: Callable[[], Any] | None = None,
) -> None:
    dialog = ui.dialog()
    service = getattr(app.state, "user_service", None)
    with dialog, ui.card().classes("w-[940px] max-w-[95vw] p-4 gap-3"):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label(title).classes("text-base font-bold text-slate-800")
            ui.button(icon="close", on_click=dialog.close).props("flat round dense")
        if can_upload:
            upload_control: CompactUpload | None = None

            def component_files() -> list[dict[str, Any]]:
                attachments = visible_attachments(
                    ecn_id, scope, item_id, task_key, user, role, service
                )
                files: list[dict[str, Any]] = []
                for attachment in attachments:
                    entry = dict(attachment)
                    file_id = str(entry.get("id") or "")
                    name = str(entry.get("name") or "附件")
                    entry.update(
                        key=file_id,
                        name=name,
                        description=(
                            f"{entry.get('uploaded_by', '')} · {entry.get('uploaded_at', '')}"
                        ).strip(" ·"),
                        removable=entry.get("uploaded_by") == user,
                        on_open=lambda fid=file_id: open_ecn_attachment_file(
                            ecn_id, scope, item_id, task_key, fid, user, role
                        ),
                    )
                    if attachment_kind(name) == "image":
                        entry["preview_url"] = issue_attachment_preview_url(
                            ecn_id, scope, item_id, task_key, file_id, user, role
                        )
                    files.append(entry)
                return files

            def refresh_component(*, clear_selected: bool = False) -> None:
                if upload_control is not None:
                    upload_control.set_existing_files(
                        component_files(), clear_selected=clear_selected
                    )

            async def handle_upload(event: Any) -> None:
                staged: dict | None = None
                try:
                    staged = await stage_upload(event.file, user)
                    result = await attach_existing(
                        ecn_id, scope, item_id, task_key, staged, user, role, service
                    )
                    if not result.ok:
                        refresh_component(clear_selected=True)
                        ui.notify(result.message, type="warning")
                        return
                    refresh_component(clear_selected=True)
                    ui.notify("附件上传成功", type="positive")
                    if on_updated is not None:
                        on_updated()
                except Exception as exc:
                    refresh_component(clear_selected=True)
                    ui.notify(f"上传失败：{exc}", type="negative")
                finally:
                    if staged is not None:
                        cleanup_staged([staged])

            async def handle_existing_removed(
                event: CustomUploadRemovedEventArguments,
            ) -> None:
                removed = False
                try:
                    for attachment in event.files:
                        file_id = str(attachment.get("id") or attachment.get("key") or "")
                        result = await remove_existing(
                            ecn_id, scope, item_id, task_key, file_id, user, role, service
                        )
                        if not result.ok:
                            ui.notify(result.message, type="warning")
                            continue
                        removed = True
                        ui.notify("附件已删除", type="positive")
                finally:
                    refresh_component()
                if removed and on_updated is not None:
                    on_updated()

            with ui.column().classes("w-full min-w-0 gap-2"):
                ui.label("附件（点击文件名预览或下载）").classes(
                    "text-xs font-semibold text-slate-600"
                )
                upload_control = custom_upload(
                    multiple=True,
                    max_file_size=int(ECN_ATTACHMENT_CONFIG["max_file_size_mb"]) * 1024 * 1024,
                    on_upload=handle_upload,
                    existing_files=component_files(),
                    on_existing_removed=handle_existing_removed,
                    file_area_position="left",
                    show_clear_all=False,
                ).props("accept=*/*")
                ui.label("上传后即保存到当前 ECN。").classes(
                    "text-[11px] text-slate-400"
                )
        else:
            with ui.column().classes("w-full min-w-0 gap-2"):
                ui.label("已上传附件（点击文件名预览或下载）").classes(
                    "text-xs font-semibold text-slate-600"
                )
                attachments = visible_attachments(
                    ecn_id, scope, item_id, task_key, user, role, service
                )
                if not attachments:
                    ui.label("暂无附件").classes("text-sm text-slate-400 py-2")
                for attachment in attachments:
                    file_id = str(attachment.get("id") or "")
                    name = str(attachment.get("name") or "附件")
                    kind = attachment_kind(name)
                    with ui.row().classes(
                        "w-full min-h-[56px] items-center gap-2 rounded-lg "
                        "border border-slate-200 bg-white px-3 py-2"
                    ):
                        if kind == "image":
                            thumbnail_url = issue_attachment_preview_url(
                                ecn_id, scope, item_id, task_key, file_id, user, role
                            )
                            ui.image(thumbnail_url).classes(
                                "w-10 h-10 object-cover rounded cursor-pointer shrink-0"
                            ).on(
                                "click",
                                lambda _, fid=file_id: open_ecn_attachment_file(
                                    ecn_id, scope, item_id, task_key, fid, user, role
                                ),
                            )
                        else:
                            ui.icon(
                                "picture_as_pdf" if kind == "pdf" else "attach_file",
                                size="xs",
                            ).classes("text-slate-500 shrink-0")
                        ui.label(name).classes(
                            "flex-1 min-w-0 text-sm break-all cursor-pointer "
                            "hover:underline text-slate-700"
                        ).on(
                            "click",
                            lambda _, fid=file_id: open_ecn_attachment_file(
                                ecn_id, scope, item_id, task_key, fid, user, role
                            ),
                        )
                        ui.label(
                            f"{attachment.get('uploaded_by', '')} · "
                            f"{attachment.get('uploaded_at', '')}"
                        ).classes("text-xs text-slate-400")
                        action_icon = (
                            "open_in_new"
                            if kind == "pdf"
                            else "zoom_in"
                            if kind == "image"
                            else "download"
                        )
                        ui.button(
                            icon=action_icon,
                            on_click=lambda _, fid=file_id: open_ecn_attachment_file(
                                ecn_id, scope, item_id, task_key, fid, user, role
                            ),
                        ).props("flat round dense size=sm")
    dialog.on("close", dialog.delete)
    dialog.open()
