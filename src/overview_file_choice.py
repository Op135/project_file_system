"""ECN 与批量概述共用的同名文件选择窗口；不写入正式目录。"""

from pathlib import Path

from nicegui import run, ui

from .overview_file_storage import checked_filename, file_digest, make_file_selection


async def choose_overview_file(directory: Path, name: str, digest: str, projects: list[str]) -> dict | None:
    checked_filename(name)
    target = directory / name
    if not target.exists():
        return make_file_selection(directory, name, "new", digest)
    if not target.is_file():
        raise ValueError("正式目录存在同名目录，请更换文件名")
    existing_digest = await run.io_bound(file_digest, target)
    dialog = ui.dialog().props("persistent")
    with dialog, ui.card().classes("w-[640px] max-w-full gap-3"):
        ui.label("正式目录存在同名文件，请确认处理方式").classes("text-lg font-bold")
        ui.label(f"涉及项目：{'、'.join(projects)}").classes("break-all")
        ui.label(str(target)).classes("text-xs break-all")
        ui.label("内容相同" if digest == existing_digest else "注意：服务器文件与本次上传的内容不同").classes("text-orange-800")
        ui.button("查看/下载服务器已有文件", on_click=lambda: ui.download(target, name)).props("flat")
        choice = ui.radio({"existing": "使用服务器已有文件（忽略本次上传内容）", "new": "修改文件名，保存本次上传内容"}, value="existing")
        filename = ui.input("新文件名（保留扩展名）", value=name).classes("w-full")
        filename.bind_visibility_from(choice, "value", backward=lambda value: value == "new")

        async def confirm():
            try:
                selected_name = checked_filename(str(filename.value or "")) if choice.value == "new" else name
                if choice.value == "new":
                    if Path(selected_name).suffix.lower() != Path(name).suffix.lower():
                        raise ValueError("只能修改文件名称，不能改变扩展名")
                    if (directory / selected_name).exists():
                        raise ValueError("新文件名仍被占用，请重新填写")
                selected = make_file_selection(directory, selected_name, str(choice.value), existing_digest if choice.value == "existing" else digest)
                dialog.submit(selected)
            except (ValueError, OSError) as exc:
                ui.notify(str(exc), type="warning")

        with ui.row().classes("w-full justify-end"):
            ui.button("取消", on_click=lambda: dialog.submit(None)).props("flat")
            ui.button("确认", on_click=confirm)
    try:
        return await dialog
    finally:
        dialog.delete()
