"""对 NiceGUI 官方控件进行统一行为和样式调整的可复用组件。"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nicegui import events, ui
from nicegui.elements.upload import Upload


__all__ = ["CustomUploadRemovedEventArguments", "custom_upload"]


_CUSTOM_UPLOAD_CSS = """
    /* 隐藏标题栏左侧的原生“清除队列/清除已上传”按钮。 */
    .custom-upload .q-uploader__header-content > .flex > .q-btn:has(~ .col) {
        display: none !important;
    }
    .custom-upload-container {
        position: relative;
    }
    /* 多文件的“清空全部”放在右上角加号左侧。 */
    .custom-upload-clear-all {
        position: absolute !important;
        right: 48px;
        top: 6px;
        z-index: 2;
    }
    /* 文件行右侧按钮实际用于移除文件；上传完成后把默认勾号替换为红色叉号。 */
    .custom-upload .q-uploader__file-header > .q-btn .q-icon {
        color: var(--q-negative);
    }
    .custom-upload .q-uploader__file--uploaded .q-uploader__file-header > .q-btn .q-icon {
        font-size: 0 !important;
    }
    .custom-upload .q-uploader__file--uploaded .q-uploader__file-header > .q-btn .q-icon::before {
        content: "close";
        font-family: "Material Icons";
        font-size: 24px;
    }
"""

_FILE_METADATA_JS = """
files => {
    const installSummarySync = () => {
        const wrapper = getElement(__UPLOADER_ID__);
        const qUploader = wrapper?.$refs?.qRef;
        const element = wrapper?.$el;
        const header = element?.querySelector('.q-uploader__header');
        if (!element || !qUploader || !header) return;

        const formatSize = bytes => {
            const units = ['B', 'KB', 'MB', 'GB', 'TB'];
            let value = Number(bytes) || 0;
            let unitIndex = 0;
            while (value >= 1024 && unitIndex < units.length - 1) {
                value /= 1024;
                unitIndex += 1;
            }
            return `${value.toFixed(1)}${units[unitIndex]}`;
        };
        const syncSummary = () => {
            const currentFiles = Array.from(qUploader.files || []);
            const totalSize = currentFiles.reduce((total, file) => total + (Number(file.size) || 0), 0);
            const uploadedSize = currentFiles.reduce(
                (total, file) => total + Math.min(Number(file.__uploaded) || 0, Number(file.size) || 0),
                0,
            );
            const progress = totalSize > 0 ? uploadedSize / totalSize * 100 : 0;
            const text = `${formatSize(totalSize)} / ${progress.toFixed(2)}%`;
            const subtitle = element.querySelector('.q-uploader__subtitle');
            if (subtitle && subtitle.textContent.trim() !== text) subtitle.textContent = text;
        };

        if (!wrapper.__customUploadSummaryObserver) {
            wrapper.__customUploadSummaryObserver = new MutationObserver(() => requestAnimationFrame(syncSummary));
            wrapper.__customUploadSummaryObserver.observe(header, {
                childList: true,
                characterData: true,
                subtree: true,
            });
        }
        syncSummary();
    };
    setTimeout(installSummarySync, 0);
    emit(files.map(file => ({
        key: file.__key,
        name: file.name,
        size: file.size,
        type: file.type,
        last_modified: file.lastModified,
    })));
}
"""


def _file_metadata_js(uploader_id: int) -> str:
    return _FILE_METADATA_JS.replace("__UPLOADER_ID__", str(uploader_id))


@dataclass(kw_only=True, slots=True)
class CustomUploadRemovedEventArguments(events.UiEventArguments):
    """自定义上传控件的文件移除事件。"""

    files: list[dict[str, Any]]
    clear_all: bool


def custom_upload(
    *,
    multiple: bool = False,
    max_files: int | None = None,
    on_upload: Callable[..., Any] | None = None,
    on_multi_upload: Callable[..., Any] | None = None,
    on_removed: Callable[..., Any] | None = None,
    on_begin_upload: Callable[..., Any] | None = None,
    on_rejected: Callable[..., Any] | None = None,
    label: str = "",
    auto_upload: bool = True,
    max_file_size: int | None = None,
    max_total_size: int | None = None,
) -> Upload:
    """创建支持单选和多选、移除操作语义清晰的 NiceGUI 上传控件。

    单文件模式通过 ``multiple=False`` 使用；未指定 ``max_files`` 时自动限制为
    一个文件。多文件模式传入 ``multiple=True``，并可用 ``max_files`` 限制数量。
    多文件模式会在标题栏右上角、加号左侧显示“清空全部”按钮。

    ``on_removed`` 可以不接收参数，也可以接收
    :class:`CustomUploadRemovedEventArguments`，其中包含移除的文件元数据和
    ``clear_all`` 标志。
    """
    effective_max_files = max_files if multiple or max_files is not None else 1
    selected_files: dict[str, dict[str, Any]] = {}
    clear_all_button: Any = None

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

    def update_clear_all_button() -> None:
        if clear_all_button is not None:
            clear_all_button.set_visibility(bool(selected_files))

    def emit_removed(files: list[dict[str, Any]], *, clear_all: bool) -> None:
        if on_removed is None:
            return
        events.handle_event(
            on_removed,
            CustomUploadRemovedEventArguments(
                sender=uploader,
                client=uploader.client,
                files=files,
                clear_all=clear_all,
            ),
        )

    def handle_added(event: events.GenericEventArguments) -> None:
        for file_data in event_files(event):
            selected_files[file_key(file_data)] = file_data
        update_clear_all_button()

    def handle_removed(event: events.GenericEventArguments) -> None:
        removed_files = event_files(event)
        for file_data in removed_files:
            selected_files.pop(file_key(file_data), None)
        if not selected_files:
            # Quasar 逐项移除不会扣减累计大小；最后一个文件移除后完整复位。
            uploader.reset()
        update_clear_all_button()
        emit_removed(removed_files, clear_all=False)

    def clear_all() -> None:
        removed_files = list(selected_files.values())
        selected_files.clear()
        uploader.reset()
        update_clear_all_button()
        emit_removed(removed_files, clear_all=True)

    ui.add_css(_CUSTOM_UPLOAD_CSS)
    with ui.column().classes("w-full gap-1 custom-upload-container"):
        uploader = ui.upload(
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
        ).classes("w-full custom-upload")
        uploader.on("added", handle_added, js_handler=_file_metadata_js(uploader.id))
        uploader.on("removed", handle_removed, js_handler=_file_metadata_js(uploader.id))

        if multiple:
            clear_all_button = (
                ui.button(icon="delete_sweep", on_click=clear_all)
                .props("flat round dense color=white")
                .classes("custom-upload-clear-all")
            )
            clear_all_button.tooltip("清空全部")
            clear_all_button.set_visibility(False)

    return uploader
