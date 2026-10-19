"""对 NiceGUI 官方控件进行统一行为和样式调整的可复用组件。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from nicegui import events, ui
from nicegui.elements.upload import Upload

__all__ = ["CustomUploadRemovedEventArguments", "custom_upload"]

_CUSTOM_UPLOAD_CSS = """
    .custom-upload-panel { display: grid; width: 100%; gap: 8px; align-items: start; }
    .custom-upload-position-bottom { grid-template-areas: "action" "files"; }
    .custom-upload-position-top { grid-template-areas: "files" "action"; }
    .custom-upload-position-left {
        grid-template-areas: "files action";
        grid-template-columns: minmax(0, 1fr) auto;
    }
    .custom-upload-position-right {
        grid-template-areas: "action files";
        grid-template-columns: auto minmax(0, 1fr);
    }
    .custom-upload-action { grid-area: action; min-width: 128px; }
    .custom-upload-files { grid-area: files; width: 100%; }
    .custom-upload-file-row {
        width: 100%; min-height: 58px; padding: 6px 8px;
        border: 1px solid #e2e8f0; border-radius: 8px; background: #fff;
    }
    .custom-upload-thumbnail {
        width: 44px !important; height: 44px !important; min-width: 44px;
        border-radius: 6px; overflow: hidden; background: #f1f5f9;
        object-fit: cover; cursor: pointer;
    }
    .custom-upload-file-icon {
        width: 44px; height: 44px; min-width: 44px; border-radius: 6px;
        background: #f1f5f9; cursor: pointer;
    }
    .custom-upload-native { display: none !important; }
    @media (max-width: 640px) {
        .custom-upload-position-left, .custom-upload-position-right {
            grid-template-areas: "action" "files";
            grid-template-columns: minmax(0, 1fr);
        }
    }
"""

_FILE_METADATA_JS = """
files => {
    const wrapper = getElement(__UPLOADER_ID__);
    wrapper.__customUploadObjectUrls ||= {};
    emit(files.map(file => {
        const key = String(file.__key || `${file.name}:${file.size}:${file.lastModified}`);
        if (!wrapper.__customUploadObjectUrls[key]) {
            wrapper.__customUploadObjectUrls[key] = URL.createObjectURL(file);
        }
        return {
            key,
            name: file.name,
            size: file.size,
            type: file.type,
            last_modified: file.lastModified,
            preview_url: wrapper.__customUploadObjectUrls[key],
        };
    }));
}
"""


def _file_metadata_js(uploader_id: int) -> str:
    return _FILE_METADATA_JS.replace("__UPLOADER_ID__", str(uploader_id))


def _format_size(size: object) -> str:
    value = float(size) if isinstance(size, (int, float, str)) else 0.0
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    return f"{value:.1f}{units[unit_index]}"


def _file_kind(file_data: dict[str, Any]) -> str:
    name = str(file_data.get("name") or "").lower()
    content_type = str(file_data.get("type") or file_data.get("file_type") or "").lower()
    if content_type.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif")
    ):
        return "image"
    if content_type == "application/pdf" or name.endswith(".pdf"):
        return "pdf"
    return "file"


@dataclass(kw_only=True, slots=True)
class CustomUploadRemovedEventArguments(events.UiEventArguments):
    """自定义上传控件的文件移除事件。"""

    files: list[dict[str, Any]]
    clear_all: bool


class _CompactUpload(Upload):
    """保留 Upload 公共接口，并在外部 reset 时同步紧凑文件列表。"""

    def __init__(self, **kwargs: Any) -> None:
        self._compact_reset_callback: Callable[[], None] | None = None
        super().__init__(**kwargs)

    def reset(self) -> None:
        super().reset()
        if self._compact_reset_callback is not None:
            self._compact_reset_callback()


def custom_upload(
    *,
    multiple: bool = False,
    max_files: int | None = None,
    on_upload: Callable[..., Any] | None = None,
    on_multi_upload: Callable[..., Any] | None = None,
    on_removed: Callable[..., Any] | None = None,
    existing_files: list[dict[str, Any]] | None = None,
    on_existing_removed: Callable[..., Any] | None = None,
    on_begin_upload: Callable[..., Any] | None = None,
    on_rejected: Callable[..., Any] | None = None,
    label: str = "",
    auto_upload: bool = True,
    max_file_size: int | None = None,
    max_total_size: int | None = None,
    file_area_position: Literal["top", "bottom", "left", "right"] = "bottom",
    show_thumbnails: bool = True,
) -> Upload:
    """创建紧凑的文件上传组件，并保持 NiceGUI Upload 的调用兼容性。"""
    if file_area_position not in {"top", "bottom", "left", "right"}:
        raise ValueError("file_area_position 必须是 top、bottom、left 或 right")

    effective_max_files = max_files if multiple or max_files is not None else 1
    selected_files: dict[str, dict[str, Any]] = {}
    saved_files: dict[str, dict[str, Any]] = {}
    clear_all_button: Any = None
    for index, item in enumerate(existing_files or []):
        entry = dict(item)
        key = str(entry.get("key") or entry.get("id") or f"existing-{index}")
        entry.update(key=key, existing=True)
        saved_files[key] = entry

    def file_key(file_data: dict[str, Any]) -> str:
        return str(
            file_data.get("key")
            or f"{file_data.get('name', '')}:{file_data.get('size', '')}:{file_data.get('last_modified', '')}"
        )

    def event_files(event: events.GenericEventArguments) -> list[dict[str, Any]]:
        value = event.args
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
            value = value[0]
        return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    ui.add_css(_CUSTOM_UPLOAD_CSS)
    panel = ui.element("div").classes(f"custom-upload-panel custom-upload-position-{file_area_position}")
    with panel:
        action_area = ui.column().classes("custom-upload-action gap-1 items-start")
        file_area = ui.column().classes("custom-upload-files gap-1")
        uploader = _CompactUpload(
            multiple=multiple,
            max_files=effective_max_files,
            on_begin_upload=on_begin_upload,
            on_upload=on_upload,
            on_multi_upload=on_multi_upload,
            on_rejected=on_rejected,
            label=label,
            auto_upload=auto_upload,
            max_file_size=max_file_size,
            max_total_size=max_total_size,
        ).classes("custom-upload-native")

    def all_files() -> list[dict[str, Any]]:
        return [*saved_files.values(), *selected_files.values()]

    def update_action_state() -> None:
        files = all_files()
        total_size = sum(int(item.get("size") or 0) for item in files)
        info_label.set_text("尚未选择文件" if not files else f"{len(files)}个文件，共{_format_size(total_size)}")
        file_area.set_visibility(bool(files))
        at_limit = effective_max_files is not None and len(files) >= effective_max_files
        upload_button.set_enabled(not at_limit)
        if clear_all_button is not None:
            clear_all_button.set_visibility(bool(files))

    def open_file(file_data: dict[str, Any]) -> None:
        open_callback = file_data.get("on_open")
        if callable(open_callback):
            open_callback()
            return
        url = str(
            file_data.get("preview_url")
            or file_data.get("download_url")
            or file_data.get("url_path")
            or ""
        )
        if not url:
            return
        name = str(file_data.get("name") or "文件")
        if _file_kind(file_data) in {"image", "pdf"}:
            ui.run_javascript(f"window.open({json.dumps(url)}, '_blank');")
        else:
            ui.run_javascript(
                "const a=document.createElement('a');"
                f"a.href={json.dumps(url)};a.download={json.dumps(name)};"
                "document.body.appendChild(a);a.click();a.remove();"
            )

    def emit_removed(files: list[dict[str, Any]], *, clear_all: bool, existing: bool = False) -> None:
        callback = on_existing_removed if existing else on_removed
        if callback is None:
            return
        events.handle_event(
            callback,
            CustomUploadRemovedEventArguments(
                sender=uploader,
                client=uploader.client,
                files=[dict(item) for item in files],
                clear_all=clear_all,
            ),
        )

    def remove_selected(file_data: dict[str, Any]) -> None:
        key = file_key(file_data)
        ui.run_javascript(
            "(() => {"
            f"const wrapper=getElement({uploader.id});"
            "const q=wrapper?.$refs?.qRef;"
            f"const file=Array.from(q?.files || []).find(item => String(item.__key) === {json.dumps(key)});"
            "if (file) q.removeFile(file);"
            "})()"
        )

    def remove_existing(file_data: dict[str, Any]) -> None:
        removed = saved_files.pop(file_key(file_data), None)
        if removed is None:
            return
        render_files()
        emit_removed([removed], clear_all=False, existing=True)

    def render_files() -> None:
        file_area.clear()
        with file_area:
            for file_data in all_files():
                existing = bool(file_data.get("existing"))
                kind = _file_kind(file_data)
                preview_url = str(file_data.get("preview_url") or "")
                with ui.row().classes("custom-upload-file-row items-center gap-2 flex-nowrap"):
                    if kind == "image" and show_thumbnails and preview_url:
                        ui.image(preview_url).classes("custom-upload-thumbnail").on(
                            "click", lambda _, entry=file_data: open_file(entry)
                        )
                    else:
                        with ui.row().classes(
                            "custom-upload-file-icon items-center justify-center shrink-0"
                        ).on("click", lambda _, entry=file_data: open_file(entry)):
                            ui.icon(
                                "picture_as_pdf" if kind == "pdf" else "image" if kind == "image" else "attach_file",
                                size="sm",
                            ).classes("text-slate-600")
                    with ui.column().classes("gap-0 flex-1 min-w-0"):
                        ui.label(str(file_data.get("name") or "文件")).classes(
                            "text-xs text-slate-800 break-all cursor-pointer hover:text-primary"
                        ).on("click", lambda _, entry=file_data: open_file(entry))
                        ui.label(_format_size(file_data.get("size"))).classes("text-[10px] text-slate-500")
                    remove_handler = (
                        (lambda _, entry=file_data: remove_existing(entry))
                        if existing
                        else (lambda _, entry=file_data: remove_selected(entry))
                    )
                    ui.button(icon="delete_outline", on_click=remove_handler).props(
                        "flat round dense size=sm color=negative"
                    ).tooltip("删除文件")
        update_action_state()

    def handle_added(event: events.GenericEventArguments) -> None:
        for file_data in event_files(event):
            selected_files[file_key(file_data)] = file_data
        render_files()

    def handle_removed(event: events.GenericEventArguments) -> None:
        removed_files = event_files(event)
        for file_data in removed_files:
            selected_files.pop(file_key(file_data), None)
        if not selected_files:
            Upload.reset(uploader)
        render_files()
        emit_removed(removed_files, clear_all=False)

    def clear_all() -> None:
        removed_selected = list(selected_files.values())
        removed_existing = list(saved_files.values())
        selected_files.clear()
        saved_files.clear()
        Upload.reset(uploader)
        render_files()
        if removed_selected:
            emit_removed(removed_selected, clear_all=True)
        if removed_existing:
            emit_removed(removed_existing, clear_all=True, existing=True)

    def reset_display() -> None:
        selected_files.clear()
        render_files()

    uploader._compact_reset_callback = reset_display
    uploader.on("added", handle_added, js_handler=_file_metadata_js(uploader.id))
    uploader.on("removed", handle_removed, js_handler=_file_metadata_js(uploader.id))

    with action_area:
        upload_button = ui.button(
            label or "添加文件",
            icon="upload_file",
            on_click=lambda: uploader.run_method("pickFiles"),
        ).props("outline dense no-caps color=primary")
        info_label = ui.label().classes("text-[11px] text-slate-500")
        if multiple:
            clear_all_button = ui.button("清空全部", icon="delete_sweep", on_click=clear_all).props(
                "flat dense no-caps color=negative"
            ).classes("text-xs")

    panel.bind_visibility_from(uploader, "visible")
    render_files()
    return uploader
