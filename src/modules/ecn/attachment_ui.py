"""ECN 通用附件清单与自定义上传控件。"""

from __future__ import annotations

from typing import Any, Callable

from nicegui import app, ui

from ...components import build_contained_image_preview
from ...custom_ui import custom_upload
from ...ecn_access import get_active_ecn_actor_role
from ...ecn_management_config import ECN_ATTACHMENT_CONFIG
from .attachment_preview import (
    attachment_kind,
    issue_attachment_preview_url,
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
    preview = ui.dialog().props("maximized").classes("p-0")
    with preview:
        build_contained_image_preview(url, preview.close)
    preview.on("close", preview.delete)
    preview.open()


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
        ui.navigate.to(issue_attachment_preview_url(
            ecn_id, scope, item_id, task_key, file_id, user, role,
        ), new_tab=True)
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
        ui.navigate.to(issue_staged_attachment_preview_url(attachment, user, role), new_tab=True)
    else:
        ui.download(path, name)


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
                ui.notify(result.message, type="warning")
                return
            ui.notify("附件上传成功", type="positive")
            render_list()
        except Exception as exc:
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
        content = ui.element("div").classes(
            "w-full grid grid-cols-1 md:grid-cols-2 gap-5 items-start" if can_upload else "w-full"
        )
        with content, ui.column().classes("w-full min-w-0 gap-2"):
            ui.label("已上传附件（点击文件名预览或下载）").classes("text-xs font-semibold text-slate-600")
            list_host = ui.column().classes("w-full gap-3 max-h-[55vh] overflow-y-auto")

        def render_list() -> None:
            list_host.clear()
            attachments = visible_attachments(ecn_id, scope, item_id, task_key, user, role, service)
            with list_host:
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
                                ecn_id, scope, item_id, task_key, file_id, user, role,
                            )
                            ui.image(thumbnail_url).classes(
                                "w-10 h-10 object-cover rounded cursor-pointer shrink-0"
                            ).on("click", lambda _, fid=file_id: open_file(fid))
                        else:
                            ui.icon("picture_as_pdf" if kind == "pdf" else "attach_file", size="xs").classes(
                                "text-slate-500 shrink-0"
                            )
                        ui.label(name).classes(
                            "flex-1 min-w-0 text-sm break-all cursor-pointer hover:underline text-slate-700"
                        ).on("click", lambda _, fid=file_id: open_file(fid))
                        ui.label(
                            f"{attachment.get('uploaded_by', '')} · {attachment.get('uploaded_at', '')}"
                        ).classes("text-xs text-slate-400")

                        def open_file(file_id: str) -> None:
                            open_ecn_attachment_file(
                                ecn_id, scope, item_id, task_key, file_id, user, role,
                            )

                        action_icon = "open_in_new" if kind == "pdf" else "download" if kind == "download" else "zoom_in"
                        action_text = "新标签页打开 PDF" if kind == "pdf" else "下载附件" if kind == "download" else "查看大图"
                        ui.button(
                            icon=action_icon, on_click=lambda _, fid=file_id: open_file(fid),
                        ).props("flat round dense size=sm").tooltip(action_text)
                        if can_upload and attachment.get("uploaded_by") == user:
                            async def remove_file(file_id=str(attachment.get("id") or "")) -> None:
                                result = await remove_existing(
                                    ecn_id, scope, item_id, task_key, file_id, user, role, service
                                )
                                if not result.ok:
                                    ui.notify(result.message, type="warning")
                                    return
                                ui.notify("附件已删除", type="positive")
                                render_list()
                                if on_updated is not None:
                                    on_updated()

                            ui.button(icon="delete_outline", on_click=remove_file).props(
                                "flat round dense size=sm color=red-5"
                            ).tooltip("删除我上传的附件")

        render_list()
        if can_upload:
            async def handle_upload(event: Any) -> None:
                staged: dict | None = None
                try:
                    staged = await stage_upload(event.file, user)
                    result = await attach_existing(
                        ecn_id, scope, item_id, task_key, staged, user, role, service
                    )
                    if not result.ok:
                        ui.notify(result.message, type="warning")
                        return
                    ui.notify("附件上传成功", type="positive")
                    render_list()
                    if on_updated is not None:
                        on_updated()
                except Exception as exc:
                    ui.notify(f"上传失败：{exc}", type="negative")
                finally:
                    if staged is not None:
                        cleanup_staged([staged])

            with content, ui.column().classes("w-full min-w-0 gap-2"):
                ui.label("上传附件").classes("text-xs font-semibold text-slate-600")
                custom_upload(
                    multiple=True,
                    max_file_size=int(ECN_ATTACHMENT_CONFIG["max_file_size_mb"]) * 1024 * 1024,
                    on_upload=handle_upload,
                ).props("accept=*/*")
                ui.label("上传后即保存到当前 ECN；删除已归档文件请点击清单中的删除图标。").classes(
                    "text-[11px] text-slate-400"
                )
    dialog.on("close", dialog.delete)
    dialog.open()
