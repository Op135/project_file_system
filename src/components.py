# -*- encoding: utf-8 -*-
import asyncio
import copy
import io
import math
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

import wcwidth
from html_sanitizer import Sanitizer
from nicegui import app, events, ui
from nicegui.events import GenericEventArguments, MouseEventArguments

from . import db_storage  # 导入我们创建的模块
from .config import FILES_URL_DIR, IMG_DIR, OVER_UPLOADS_FILE_TYPE, SUBMIT_FILES_DIR, UPLOADS_DIR
from .utils import (
    find_dirs_by_name_pathlib,
    find_files_pathlib,
    get_file_type_by_extension,
    get_time,
    move_element,
    overview_role_update,
    ui_hide,
    ui_show,
)


# 自定义按钮上传文件元素，隐藏nicegui默认的ui.upload元素
class ButtonUploader(ui.element):
    def __init__(self, on_upload=None, label="上传", input_any_suffix=None, classes_str="", props_str="", parents_h=9):
        super().__init__()
        self.on_upload = on_upload
        self.label = label
        self.input_any_suffix = input_any_suffix
        self.classes_str = classes_str
        self.props_str = props_str
        self.parents_h = parents_h

        # 创建隐藏的上传组件
        self.upload = ui.upload(on_upload=self.handle_upload, auto_upload=True, label=self.label).props(
            f"accept={self.input_any_suffix}"
        )
        # 隐藏upload元素
        self.upload.set_visibility(False)

        # 创建一个按钮用于触发上传
        self.upload_button = (
            ui.button(label, icon="upload", on_click=self.pick_file).classes(self.classes_str).props(self.props_str)
        )

    def pick_file(self):
        # 在上传新文件前，先清空upload列表，否则后续删除文件后，不能在重新插入
        self.upload.reset()
        # 触发隐藏的上传组件
        self.upload.run_method("pickFiles")  # 触发浏览器的文件选择对话框

    async def handle_upload(self, e: events.UploadEventArguments):
        # 处理上传事件
        if self.on_upload:
            await self.on_upload(e, self.parents_h)
        else:
            print("上传文件无绑定回调函数")


# 文件缩略图对象，点击可以展示大图，并可进行拖动和缩放
class FileThumbnail:
    def __init__(
        self,
        file_url,
        file_type,
        file_name_suffix,
        file_lab,
        parents_h,
        auto_create: bool = True,
        delet_lab: bool = True,
        on_add_ref_click=lambda *args, **kwargs: None,
        on_question_display_click=lambda *args, **kwargs: None,
    ):
        self.file_url = file_url
        self.local_file_path = f"{UPLOADS_DIR}/{self.file_url.split('/')[-1]}"
        self.file_type = file_type
        self.file_neme_suffix = file_name_suffix
        self.file_neme_hash = self.file_url.split("/")[-1]
        self.file_neme = ""
        self.file_suffix = ""
        self.parents_h = parents_h
        self.zoom_level = 1.0
        self.offset = (0, 0)
        self.is_dragging = False
        self.last_pos = (0, 0)
        self.image_x = 0.0
        self.image_y = 0.0
        self.file_up_time = get_time()
        self.add_lab_bool = False
        self.delet_lab = delet_lab
        self.on_add_ref_click = on_add_ref_click
        self.on_question_display_click = on_question_display_click
        self.dialog = ui.dialog().props("").classes("p-0")
        if self.file_type.startswith("image/"):
            with self.dialog:
                # with (
                #     ui.card()
                #     .classes("relative overflow-hidden items-center justify-center")
                #     .style("background-color: rgba(0,0,0,0);")
                # ):
                # ui.label("按ESC键退出图片查看界面").classes("absolute top-15 text-xl text-red-9 z-999")
                self.image_big = (
                    ui.interactive_image(
                        self.file_url,
                    )
                    .classes("cursor-grab")
                    .style("overflow: hidden;")
                )
                # self.image_big.props("fit=contain")
                # 绑定事件
                self.image_big.on("mousedown", self.start_drag)
                self.image_big.on_mouse(self.get_img_xy)
                self.image_big.on("mousemove", self.handle_drag)
                self.image_big.on("mouseup", self.end_drag)
                self.image_big.on("mouseleave", self.end_drag)
                self.image_big.on("wheel", self.handle_zoom)
        # 存取文件计数值，也就是文件数字标记
        self.file_index = file_lab
        if auto_create:
            # 初始化并显示缩略图
            self.get_thumbnail()

    # 缩略图显示函数
    def get_thumbnail(self):
        file_name_list = self.file_neme_suffix.split(".")
        for i in range(0, len(file_name_list) - 1):
            if not self.file_neme:
                self.file_neme = self.file_neme + file_name_list[i]
        self.file_suffix = file_name_list[-1]
        str_len = wcwidth.wcswidth(self.file_neme)
        str_num = len(self.file_neme)
        font_px = math.floor(self.parents_h * 4 / 3)
        # 计算文件名标题元素的设置宽度
        label_w = math.ceil(((str_len - str_num) + (2 * str_num - str_len) * 0.7) / 3) * font_px

        # 根据文件类型创建缩略图
        if self.file_type.startswith("image/"):
            self.thumbnail = ui.interactive_image(self.file_url).classes(f"h-{str(self.parents_h)} cursor-pointer")
            self.thumbnail.on("click", self.show_fullscreen)

        elif self.file_type == "application/pdf":
            with ui.row().classes(f"h-{str(self.parents_h)} flex-nowrap gap-1") as self.pdf_row:
                # 使用 PDF 图标作为 PDF 文件的缩略图
                # with ui.link(text="NiceGUI on GitHub", target=f"{self.file_url}", new_tab=False) as self.thumbnail:
                #     ui.image("/uploads/1.jpg").classes("h-full cursor-pointer")
                self.thumbnail = (
                    ui.interactive_image(f"{IMG_DIR}/file_type_pdf.png", content="")
                    .classes("h-full aspect-[1/1] cursor-pointer")
                    .on("click", self.open_pdf_in_browser)  # 使用浏览器打开则用.open_pdf_in_browser
                )
                ui.label(self.file_neme).classes(
                    f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-words text-black p-0 m-0 bg-white-500"
                )
        else:
            with ui.row().classes(f"h-{str(self.parents_h)} flex-nowrap gap-1") as self.other_row:
                # 使用 其它文件 图标作为 其它 文件的缩略图
                self.thumbnail = (
                    ui.interactive_image(f"{IMG_DIR}/file_type_other.png", content="")
                    .classes("h-full aspect-[1/1] cursor-pointer")
                    .on("click", self.check_and_download)
                )
                ui.label(self.file_neme).classes(
                    f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-words text-black p-0 m-0 bg-white-500"
                )
                with self.thumbnail:
                    bg_color = "amber-600"
                    if "xls" in self.file_suffix:
                        bg_color = "green-700"
                    elif "ppt" in self.file_suffix:
                        bg_color = "red-600"
                    elif "doc" in self.file_suffix:
                        bg_color = "blue-400"
                    ui.label(self.file_suffix).classes(
                        f"border-2 border-white m-0 p-[2px] bg-{bg_color} text-white text-[10px]/[10px]"
                    ).style("position: absolute; top: 65%; left: 20%; transform: translate(-50%, -50%);")

        with self.thumbnail:
            if self.delet_lab:
                # 缩略图删除按钮
                b = (
                    ui.button(on_click=lambda: self.clear_thumbnail(self.file_neme_hash, self.file_index))
                    .classes("absolute -top-0 -right-0 m-0 p-0 q-py-1 bg-red text-white ")
                    .props('round padding="0px 0px" icon="close"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                self.thumbnail.on("mouseover", lambda b=b: ui_show(b)).on("mouseout", lambda: ui_hide(b))
            # 缩略图创建日期提示
            ui.tooltip(self.file_up_time).classes("text-[10px]/[10px] text-white p-1 m-0 bg-light-blue-6").props(
                'transition-show="fade" transition-hide="fade" max-height="18px"'
            )
            # 缩略图数字标签
            ui.label(str(self.file_index)).classes(
                "absolute top-0 left-0 m-0 p-[2px] bg-black text-white text-[10px]/[10px]"
            ).style("z-index: 1000;")  # 添加数字标记

    # 为缩略图添加“+”号引用按钮
    def add_add_lab(self, ref_row, k, question_k, question):
        with self.thumbnail:
            self.ref_lab = (
                ui.button()
                .classes("absolute -bottom-0 -right-0 m-0 p-0 q-py-1 bg-amber-8 text-white ")
                .props('round padding="0px 0px" icon="add"')
                .style("font-size: 8px;")
            )
            # 而是调用存储在 self.on_add_ref_click 上的回调函数
            self.ref_lab.on("click", lambda: self.on_add_ref_click(self, ref_row, question_k, question, True))
            self.ref_lab.on("click", js_handler="(e) => {e.stopPropagation()}")

    # 删除文件缩略图
    def clear_thumbnail(self, file_neme_suffix, file_index):
        if (
            self.file_index in app.storage.client["ref_question_dic"].keys()
            and app.storage.client["ref_question_dic"][self.file_index]
        ):
            # 创建对话框
            with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
                # 对话框标题
                ui.label("文件引用提示").classes("text-h6 font-bold")

                # 内容区域
                with ui.column().classes("max-h-64 w-full"):
                    ui.label("需将如下确认项里，对该文件的引用解除掉方可删除：").classes("text-subtitle2")

                    # 使用纯文本区域显示问题
                    for q in app.storage.client["ref_question_dic"][self.file_index]:
                        b = (
                            ui.button(q[1], on_click=lambda e, k=q[0]: self.on_question_display_click(e, k))
                            .props("flat")
                            .classes("w-full")
                        )
                        b.on("click", dialog.close)

                # 关闭按钮
                ui.button("确定", on_click=dialog.close).classes("self-center")

            # 打开对话框
            dialog.open()
        else:
            if hasattr(self, "pdf_row"):
                self.pdf_row.delete()
            elif hasattr(self, "other_row"):
                self.other_row.delete()
            elif hasattr(self, "thumbnail"):
                self.thumbnail.delete()
            app.storage.client["deleted_files"].append(file_neme_suffix)
            app.storage.client["file_thumbnail_dic"][self.file_index]["file_information"]["file_del_bool"] = True
        # app.storage.client["file_counter"] -= 1 注释掉使得文件标签数字唯一

    # pdf文件打开函数
    def open_pdf_in_browser(self):
        # 在浏览器中打开 PDF 文件
        async def get_base_url():
            # 通过 JavaScript 获取当前页面的协议、域名和路径
            result = await ui.run_javascript("window.location.origin;")
            return result

        # 2. 异步执行并拼接完整 URL
        async def open_pdf():
            base_url = await get_base_url()
            full_url = f"{base_url}{self.file_url}"
            # 处理空格等特殊字符
            encoded_url = full_url.replace(" ", "%20")
            # 3. 打开新窗口
            ui.run_javascript(f'window.open("{encoded_url}", "_blank");')
            # print(f"尝试打开PDF：{encoded_url}")

        # 启动异步任务
        ui.timer(0.1, lambda: open_pdf(), once=True)

    def trigger_download(self, on_complete=None):
        """专门负责触发下载的辅助函数"""
        ui.notify(
            f"开始下载文件: {self.file_neme_suffix}",
            type="info",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
        ui.download(self.local_file_path)
        if on_complete:
            on_complete()

    # 我们创建一个新的、更智能的下载处理函数
    async def check_and_download(self):
        """
        检查文件是否已在当前会话下载过。
        如果是，则弹出一个带引导信息的对话框；如果否，则开始下载并标记。
        """
        storage_key = f"downloaded_{self.file_neme_hash}"
        has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

        if has_downloaded:
            # 【修改点】对这里的对话框进行全面升级
            with ui.dialog() as dialog, ui.card().classes("min-w-[400px]"):
                with ui.card_section():
                    ui.label(f'文件 "{self.file_neme_suffix}" 已在本次会话中下载。').classes("text-lg font-medium")
                    ui.separator().props("size=1px").classes("my-3")
                    ui.label("您可以：")
                    # 使用 HTML 来创建更丰富的文本格式
                    ui.html(
                        """
                        <ul class="q-pl-lg">
                            <li>在浏览器的<b>下载栏</b>中直接找到它。</li>
                            <li>按键盘快捷键 <kbd>Ctrl</kbd> + <kbd>J</kbd> (Windows/Linux) 或 <kbd>⌘</kbd> + <kbd>Shift</kbd> + <kbd>J</kbd> (Mac) 打开<b>下载内容页面</b>。</li>
                        </ul>
                    """,
                        sanitize=False,
                    ).classes("text-base")  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                with ui.card_actions().props("align=right"):
                    # “重新下载”按钮，保持原样
                    ui.button("仍要重新下载", on_click=lambda: self.trigger_download(dialog.close), color="primary")
                    # 将“取消”按钮改为更中性的“关闭”
                    ui.button("关闭", on_click=dialog.close, color="grey")

            dialog.open()

        # 3. 如果标记不存在 (首次点击)
        else:
            # a. 立即触发下载
            self.trigger_download()
            # b. 通过JavaScript在客户端设置标记
            await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

    # 打开其它文件，废弃，服务器不能命令本地电脑直接打开文件
    # def open_other_file(self):
    #     # 获取操作系统类型
    #     os_type = sys.platform
    #     if os_type == "win32":
    #         # os.startfile(f"{UPLOADS_DIR}/{self.file_neme_suffix}")
    #         os.startfile(self.local_file_path)
    #     elif os_type == "darwin":
    #         # subprocess.run(["open", f"{UPLOADS_DIR}/{self.file_neme_suffix}"])
    #         subprocess.run(["open", self.local_file_path])
    #     else:
    #         ui.notify(
    #             "未适配当前操作系统，不能直接打开。",
    #             type="info",
    #             position="bottom",
    #             timeout=1000,
    #             progress=True,
    #             close_button="✖",
    #         )

    # 处理数字链接的点击事件
    async def handle_index_click(self):
        # print(self.file_neme_hash, app.storage.client["deleted_files"])
        # if self.file_neme_hash in app.storage.client["deleted_files"]:
        if app.storage.client["file_thumbnail_dic"][self.file_index]["file_information"]["file_del_bool"]:
            ui.notify(
                "该文件已被销售删除，虽可查看，但谨慎参考！",
                type="warning",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            await asyncio.sleep(3)
        if self.file_type.startswith("image/"):
            self.show_fullscreen()
        elif self.file_type == "application/pdf":
            self.open_pdf_in_browser()  # 使用浏览器打开则用open_pdf_in_browser()
        else:
            await self.check_and_download()

    # 图片开始拖拽
    def start_drag(self, e: GenericEventArguments):
        if e.args.get("button") == 0:
            self.is_dragging = True
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.image_big.classes(replace="cursor-grabbing")
        elif e.args.get("button") == 1:
            self.reset_transform()

    # 图片移动
    def handle_drag(self, e: GenericEventArguments):
        if self.is_dragging:
            dx = e.args["clientX"] - self.last_pos[0]
            dy = e.args["clientY"] - self.last_pos[1]
            self.offset = (self.offset[0] + dx, self.offset[1] + dy)
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.update_transform()

    # 图片结束拖拽
    def end_drag(self, e: GenericEventArguments):
        self.is_dragging = False
        self.image_big.classes(replace="cursor-grab")

    # 获取鼠标相对图片左上角的坐标值
    def get_img_xy(self, e: MouseEventArguments):
        self.image_x = e.image_x
        self.image_y = e.image_y

    # 处理图片缩放
    def handle_zoom(self, e: GenericEventArguments):
        # 更新缩放级别（限制在0.1x到5x之间）
        new_zoom = self.zoom_level * (1.1 if e.args["deltaY"] < 0 else 0.9)
        self.zoom_level = max(0.01, min(10, new_zoom))
        # 更新图片
        self.update_transform()

    # 显示大图
    def show_fullscreen(self):
        # 打开弹窗
        self.dialog.open()
        # 复位图片
        self.reset_transform()

    # 更新图片变换函数
    def update_transform(self):
        self.image_big.style(f"transform: translate({self.offset[0]}px, {self.offset[1]}px) scale({self.zoom_level})")

    # 重置变换状态
    def reset_transform(self):
        self.zoom_level = 1.0
        self.offset = (0, 0)
        self.update_transform()


class InteractiveButton:
    """
    一个自定义的 NiceGUI 组件，它创建一个按钮用于添加文本或文件 chip。
    所有 chip 的状态都通过 app.storage.general 在所有客户端之间实时同步。
    """

    def __init__(
        self,
        project: str,
        role: str,
        title: str,
        label: str,
        processing_type: str,
        permission: dict,
        upload_path: Path = SUBMIT_FILES_DIR,
        search_folder_according: str = "",
        search_hierarchy: list = [],
        dialog_label: str = "按规定格式输入",
        dialog_placeholder: str = "",
        state_options: list = [],
        node_options: list = [],
        instrument_options: list = [],
        temp_bool: bool = False,
        # delete_bool: bool = True,
    ):
        if processing_type not in ["text", "file", "image", "test", "search"]:
            raise ValueError("processing_type 必须是 'text','file','image','test','search'")

        self.role = role
        self.title = title
        self.label = label
        self.project = project
        self.processing_type = processing_type
        self.upload_path = upload_path
        self.search_folder_according = search_folder_according
        self.search_hierarchy = search_hierarchy
        self.dialog_placeholder = dialog_placeholder
        self.dialog_label = dialog_label
        self.permission = permission
        self.state_options = state_options
        self.node_options = node_options
        self.instrument_options = instrument_options
        self.temp_bool = temp_bool
        # self.delete_bool = delete_bool
        self.offset = (0, 0)
        self.is_dragging = False
        self.last_pos = (0, 0)
        self.image_x = 0.0
        self.image_y = 0.0
        # self.select_ver = {"value": None}
        self.chip_dialog = ui.dialog().classes("")
        self.img_dialog = ui.dialog().props("").classes("p-0")
        self.check_down_dialog = ui.dialog().classes("")
        self.activ_dialog = ui.dialog().props("persistent").classes("")

        # self.image_show = {"image_show": True}
        # self.chip_dialog.bind_value_to(self.image_show, "image_show")

        # 为每个按钮实例在 app.storage.general 概述数据各项目字典里 以self.label作为键，后续保存用户输入
        # 初始化存储，如果 app.storage.general 中不存在对应的列表，则创建一个空列表
        # if self.label not in db_storage.get_item(f"{self.project}_over_data", {}):
        #     await db_storage.set_deep_item([f"{self.project}_over_data", self.label], {})

        # 创建主按钮，并绑定点击事件
        ui.button(f"{self.title}：", on_click=self._handle_main_button_click).props("flat").classes(
            "p-1 text-[14px]/[14px] mt-2 font-semibold"
        )

        # 创建一个行(row)容器，用于存放生成的所有 chip
        self.chip_container = ui.row().classes("w-full items-center gap-2 pl-8")

        if self.processing_type in ["file", "image"]:
            # 创建一个隐藏的 ui.upload 组件，我们将通过程序触发它
            self.uploader = ui.upload(
                on_upload=self._handle_file_upload,
                on_begin_upload=lambda: self.spinner.set_visibility(True),
                auto_upload=True,
                max_files=1,
            )
            # 隐藏upload元素
            self.uploader.set_visibility(False)
        # 设置一个定时器，每隔0.5秒检查一次共享数据是否有变化，并更新UI
        # 这是实现多用户实时同步的关键
        ui.timer(0.5, self._update_chip_display)

    # 显示大图
    def show_fullscreen(self, url_path):
        self.img_dialog.clear()
        with self.img_dialog:
            # with (
            #     ui.card()
            #     .classes("relative overflow-hidden items-center justify-center")
            #     .style("background-color: rgba(0,0,0,0);")
            # ):
            # ui.label("按ESC键退出图片查看界面").classes("absolute top-15 text-xl text-red-9 z-999")
            self.image_big = (
                ui.interactive_image(
                    url_path,
                )
                .classes("cursor-grab")
                .style("overflow: hidden;")
            )
            # self.image_big.props("fit=contain")
            # 绑定事件
            self.image_big.on("mousedown", self.start_drag)
            self.image_big.on_mouse(self.get_img_xy)
            self.image_big.on("mousemove", self.handle_drag)
            self.image_big.on("mouseup", self.end_drag)
            self.image_big.on("mouseleave", self.end_drag)
            self.image_big.on("wheel", self.handle_zoom)
        # 打开弹窗
        self.img_dialog.open()
        # print(self.chip_dialog.value)
        # 复位图片
        self.reset_transform()

    # 图片开始拖拽
    def start_drag(self, e: GenericEventArguments):
        if e.args.get("button") == 0:
            self.is_dragging = True
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.image_big.classes(replace="cursor-grabbing")
        elif e.args.get("button") == 1:
            self.reset_transform()

    # 图片移动
    def handle_drag(self, e: GenericEventArguments):
        if self.is_dragging:
            dx = e.args["clientX"] - self.last_pos[0]
            dy = e.args["clientY"] - self.last_pos[1]
            self.offset = (self.offset[0] + dx, self.offset[1] + dy)
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.update_transform()

    # 图片结束拖拽
    def end_drag(self, e: GenericEventArguments):
        self.is_dragging = False
        self.image_big.classes(replace="cursor-grab")

    # 获取鼠标相对图片左上角的坐标值
    def get_img_xy(self, e: MouseEventArguments):
        self.image_x = e.image_x
        self.image_y = e.image_y

    # 处理图片缩放
    def handle_zoom(self, e: GenericEventArguments):
        # 更新缩放级别（限制在0.1x到5x之间）
        new_zoom = self.zoom_level * (1.1 if e.args["deltaY"] < 0 else 0.9)
        self.zoom_level = max(0.01, min(10, new_zoom))
        # 更新图片
        self.update_transform()

    # 更新图片变换函数
    def update_transform(self):
        self.image_big.style(f"transform: translate({self.offset[0]}px, {self.offset[1]}px) scale({self.zoom_level})")

    # 重置变换状态
    def reset_transform(self):
        self.zoom_level = 1.0
        self.offset = (0, 0)
        self.update_transform()

    # <----------------------------------------------------------------
    # 辅助函数，利用传入的项目最大版本值，生成多选项字典用于存入chip数据里
    def _get_select_activ_dic(self, req_max_ver):
        select_dic = {}
        for select_label in [f"{i}.0" for i in range(1, int(float(req_max_ver)) + 1)]:
            # 新增加的chip，其之前的版本默认属于不激活，只有当前最新版本记录为激活
            if select_label == req_max_ver:
                select_dic[select_label] = True
            else:
                select_dic[select_label] = False
        return select_dic

    # 查找合法路径是否存在且唯一，并返回合法路径
    def _search_file_path(self) -> str:
        target_path = ""
        # 保存依赖文件夹所的概述配置项标签名
        according_title = ""
        # 保存找到的激活的依赖文件夹名
        according_folder_name = []
        # 有依赖文件夹配置，找依赖文件夹配置标签对应的标签标题名
        if self.search_folder_according:
            break_bool = False
            for role, data_li in app.storage.general.get("over_config_data", {}).items():
                if break_bool:
                    break
                for data in data_li:
                    if data["label"] == self.search_folder_according:
                        according_title = data["title"]
                        break_bool = True
                        break
            # 获取文件夹依赖标签里的chip数据
            according_data = db_storage.get_deep_item([f"{self.project}_over_data", self.search_folder_according])
            for data in according_data.values():
                # 将所有激活的chip对应的内容，也就是文件夹名保存起来
                if data["enabled"]:
                    according_folder_name.append(data["content"])
            # 如果少于一个有效文件夹名，即没有有效文件夹配置
            if len(according_folder_name) < 1:
                ui.notify(
                    f"{according_title}概述项无有效的记录，文件匹配根目录待定，无法提交!",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return target_path
            # 如果超过一个有效文件夹名
            elif len(according_folder_name) > 1:
                ui.notify(
                    f"{according_title}概述项超过一个有效记录，文件匹配根目录待定，无法提交!",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return target_path
            # 有且仅有一个有效文件夹配置
            else:
                # 查找这个文件夹
                folder_according_li = find_dirs_by_name_pathlib(str(self.upload_path), according_folder_name[0])
                # 文件夹不存在
                if not folder_according_li:
                    ui.notify(
                        f"{according_title}概述项配置的文件夹不存在，无法提交!",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    return target_path
                elif len(folder_according_li) > 1:
                    path_str = ""
                    for path in folder_according_li:
                        path_str = f"{path_str}\n{str(path)}"
                    ui.notify(
                        f"{according_title}概述项配置的文件夹存在多个:{path_str}\n无法提交!",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        multi_line=True,
                        close_button="✖",
                    )
                    return target_path
                # 有且存在唯一一个依赖文件夹
                else:
                    # 需要再深入层级
                    if self.search_hierarchy:
                        target_path = str(folder_according_li[0])
                        for h in self.search_hierarchy:
                            target_path = f"{target_path}/{h}"
                    # 就放在依赖文件夹
                    else:
                        target_path = str(folder_according_li[0])
        # 无依赖文件夹配置，直接上传到config配置的顶层文件夹
        else:
            # 需要再深入层级
            if self.search_hierarchy:
                target_path = str(self.upload_path)
                for h in self.search_hierarchy:
                    target_path = f"{target_path}/{h}"
            # 就放在顶层文件夹
            else:
                target_path = str(self.upload_path)
        return target_path

    # 当用户点击“添加”按钮时，将文本数据添加到共享存储中
    async def _add_search_chip_data(self):
        text = self.chip_label.value
        notes = self.chip_notes.value
        target_path = self._search_file_path()
        # 最终判断路径是否是文件夹且存在
        if Path(target_path).is_dir():
            if not text:
                ui.notify(
                    "引用文件名不能为空!",
                    type="negative",
                    position="center",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif not notes:
                ui.notify(
                    "注释不能为空!",
                    type="negative",
                    position="center",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif text in [
                d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
            ]:
                ui.notify(
                    "引用文件名已添加过。",
                    type="warning",
                    position="center",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            else:
                files_li = find_files_pathlib(target_path, text)
                if not files_li:
                    ui.notify(
                        f"引用文件不存在该路径下：\n{target_path}",
                        type="warning",
                        position="center",
                        timeout=0,
                        progress=False,
                        multi_line=True,
                        close_button="✖",
                    )
                elif len(files_li) > 1:
                    ui.notify(
                        f"引用文件在该路径下：\n{target_path}\n存在多个同名文件（子文件夹里存在）",
                        type="warning",
                        position="center",
                        timeout=0,
                        progress=False,
                        multi_line=True,
                        close_button="✖",
                    )
                else:
                    # 获取文件的MIME文件类型与编码方式
                    file_type_set = get_file_type_by_extension(str(files_li[0]))
                    # 准备要存储的 chip 数据
                    chip_id = str(uuid.uuid4())
                    req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                    select_activ_dic = self._get_select_activ_dic(req_max_ver)
                    creator = app.storage.user.get("current_user", "匿名用户")
                    url_path = f"{FILES_URL_DIR}/{text}"
                    chip_data = {
                        "id": chip_id,  # 使用UUID确保每个chip都有一个唯一的ID
                        "role": self.role,
                        "icon": "search_check",
                        "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                        # "removable": False,  # 控制元素是否有删除按钮
                        "bg_color": "bg-light-blue-1",
                        "type": "search",
                        "file_type": file_type_set[0],
                        "url_path": url_path,
                        "content": text,
                        "notes": notes,
                        "creator": creator,
                        "timestamp": {
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                "creator": creator,
                                "select_activ_dic": select_activ_dic,
                            }
                        },
                        "req_ver": req_max_ver,
                        "select_activ_dic": select_activ_dic,
                    }

                    # 将新数据追加到 app.storage.general 的列表中
                    await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                    # app.storage.general["overview_data"][self.project][self.label][chip_id] = chip_data
                    # 清空文本框并关闭对话框
                    self.chip_label.value = ""
                    self.chip_notes.value = ""
                    self.chip_dialog.close()
                    ui.notify(
                        "文件引用已添加。",
                        type="positive",
                        position="bottom",
                        timeout=1000,
                        progress=True,
                        close_button="✖",
                    )
        # 路径不存在或不完整，或不是文件夹路径
        else:
            ui.notify(
                f"文件合法存放的路径：{str(Path(target_path))} 不存在或不完整，无法提交!",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                multi_line=True,
                close_button="✖",
            )
            return

    # 当用户点击“添加”按钮时，将文本数据添加到共享存储中
    async def _add_text_chip_data(self):
        text = self.chip_label.value
        notes = self.chip_notes.value
        if not text:
            ui.notify(
                "概述内容不能为空!",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        elif not notes:
            ui.notify(
                "注释不能为空!",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        elif text in [
            d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
        ]:
            ui.notify(
                "概述内容已存在。",
                type="warning",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        else:
            # 准备要存储的 chip 数据
            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")
            chip_data = {
                "id": chip_id,  # 使用UUID确保每个chip都有一个唯一的ID
                "role": self.role,
                "icon": None,
                "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                # "removable": False,  # 控制元素是否有删除按钮
                "bg_color": "bg-light-blue-1",
                "type": "text",
                "content": text,
                "notes": notes,
                "creator": creator,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
            }

            # 将新数据追加到 app.storage.general 的列表中
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
            # app.storage.general["overview_data"][self.project][self.label][chip_id] = chip_data
            # 清空文本框并关闭对话框
            self.chip_label.value = ""
            self.chip_notes.value = ""
            self.chip_dialog.close()
            ui.notify(
                "内容已添加。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # 处理文件/图片上传事件
    async def _handle_file_upload(self, e):
        original_filename = e.file.name
        file_ext = os.path.splitext(original_filename)[1].lower()
        file_type = e.file.content_type  # 图片类返回image/xxx，文件类返回application/xxx，文本类型text/xxx
        # print(f"处理上传{file_type}类型文件")

        if self.processing_type == "file" and file_ext not in OVER_UPLOADS_FILE_TYPE:
            ui.notify(
                f'文件 "{original_filename}" 不是规定的：{", ".join(OVER_UPLOADS_FILE_TYPE)} 文件类型，无法上传!',
                type="warning",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return
        elif self.processing_type == "image" and "image" not in file_type:
            ui.notify(
                f'文件 "{original_filename}" 不是图片类型，无法上传!',
                type="warning",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return
        # 生成一个唯一的内部文件名以避免覆盖，但保留原始文件名用于显示
        # unique_filename = f"{uuid.uuid4().hex}{Path(original_filename).suffix}"
        # filepath = self.upload_path / unique_filename
        filepath = self.upload_path / original_filename
        url_path = f"{FILES_URL_DIR}/{original_filename}"
        # 检查是否已存在该项里了
        if original_filename in [
            d["filename"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
        ]:
            ui.notify(
                f'文件 "{original_filename}" 无需重复提交!',
                type="warning",
                position="center",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
        # 检查服务器是否存在同名文件
        elif os.path.exists(filepath):
            # app.add_static_file(local_file=filepath, url_path=url_path)
            self._select_file_show(original_filename, file_type, url_path)
        else:
            try:
                # 1. 一次性将文件内容完整读入内存中的 bytes 对象
                #    无论 e.file 是 SmallFileUpload 还是 FileUpload，.read() 都是支持的。
                file_content = await e.file.read()
                # 2. 使用 io.BytesIO 将内存中的 bytes 数据包装成一个标准的文件对象
                #    这个 file_like_object 的行为与真实文件完全一致，始终支持 seek()。
                file_content_object = io.BytesIO(file_content)

                # e.file 是一个类文件对象，我们需要读取其内容并写入到本地文件
                with open(filepath, "wb") as f:
                    f.write(file_content_object.read())
                # app.add_static_file(local_file=filepath, url_path=url_path)
                # time.sleep(10)

            except Exception as ex:
                print(f"上传处理失败: {ex}")  # 在服务器端打印错误详情
                ui.notify(
                    f"上传文件 '{original_filename}' 失败: {str(ex)}",
                    type="negative",
                    position="bottom",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return
            file_icon = ""
            # 文件类型的icon与图片的设置不一样
            if self.processing_type == "file":
                # 文件类型才将icon设置为引用小图，图片类不设置
                file_icon = "attach_file"
            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")
            # 生成文件或图片的chip_data
            chip_data = {
                "id": chip_id,
                "role": self.role,
                "icon": file_icon,
                "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                # "removable": False,  # 控制元素是否有删除按钮
                "bg_color": "bg-light-blue-1",
                "type": self.processing_type,
                "file_type": file_type,
                # "filepath": f"{filepath}", 路径不能记死
                "filename": original_filename,
                "url_path": url_path,
                "notes": self.chip_notes.value,
                "creator": creator,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
            }
            self.chip_notes.value = ""
            self.chip_dialog.close()
            # 将新数据追加到共享列表中
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
            ui.notify(
                f'文件 "{original_filename}" 上传成功!',
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # 显示服务器已有文件
    async def _show_have_file(self, original_filename, file_type, url_path):
        # 准备要存储的 chip 数据
        file_icon = ""
        if self.processing_type == "file":
            # 文件类型才将icon设置为引用小图，图片类不设置
            file_icon = "attach_file"
        chip_id = str(uuid.uuid4())
        req_max_ver = app.storage.general["project_req_max_ver"][self.project]
        select_activ_dic = self._get_select_activ_dic(req_max_ver)
        creator = app.storage.user.get("current_user", "匿名用户")
        # 生成文件或图片的chip_data
        chip_data = {
            "id": chip_id,
            "role": self.role,
            "icon": file_icon,
            "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
            # "removable": False,  # 控制元素是否有删除按钮
            "bg_color": "bg-light-blue-1",
            "type": self.processing_type,
            "file_type": file_type,
            # "filepath": f"{filepath}",
            "filename": original_filename,
            "url_path": url_path,
            "notes": self.chip_notes.value,
            "creator": creator,
            "timestamp": {
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                    "creator": creator,
                    "select_activ_dic": select_activ_dic,
                }
            },
            "req_ver": req_max_ver,
            "select_activ_dic": select_activ_dic,
        }
        self.chip_notes.value = ""
        self.chip_dialog.close()
        # 将新数据追加到共享列表中
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
        ui.notify(
            f'文件 "{original_filename}" 显示成功!',
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )

    # 将测试项配置信息添加到共享储存中
    async def _add_test_chip_data(self, test_select_data):
        text = self.chip_label.value
        notes = self.chip_notes.value
        # 判断是否存在选择“其它”但不写明特殊要求的情况
        other_bool = False
        if test_select_data["state_select"] == "其它" and not test_select_data["state_other_text"]:
            other_bool = True
        if test_select_data["node_select"] == "其它" and not test_select_data["node_other_text"]:
            other_bool = True
        if test_select_data["instrument_select"] == "其它" and not test_select_data["instrument_other_text"]:
            other_bool = True

        # 测试项内容不能为空，选项一旦生成也不能不选（None）
        if (
            not text
            or test_select_data["state_select"] is None
            or test_select_data["node_select"] is None
            or test_select_data["instrument_select"] is None
        ):
            ui.notify(
                "测试项内容及选项必须填写和选择!",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        elif not notes:
            ui.notify(
                "注释不能为空!",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        elif other_bool:
            ui.notify(
                "特殊要求不能为空!",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        elif (text, test_select_data) in [
            (d["content"], d["test_select_data"])
            for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
        ]:
            ui.notify(
                "测试项内容标准已存在。",
                type="warning",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        else:
            # 准备要存储的 chip 数据
            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")
            chip_data = {
                "id": chip_id,  # 使用UUID确保每个chip都有一个唯一的ID
                "role": self.role,
                "icon": None,
                "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                # "removable": False,  # 控制元素是否有删除按钮
                "bg_color": "bg-light-blue-1",
                "type": "test",
                "content": text,
                "notes": notes,
                "test_select_data": test_select_data,
                "creator": creator,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
            }

            # 将新数据追加到 app.storage.general 的列表中
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
            # 清空文本框并关闭对话框
            self.chip_notes.value = ""
            self.chip_dialog.close()
            ui.notify(
                "内容已添加。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # ----------------------------------------------------------------->

    # 询问重复提交文件是否按服务器现有文件显示
    def _select_file_show(self, original_filename, file_type, url_path):
        self.chip_dialog.clear()
        self.chip_dialog.open()
        with self.chip_dialog, ui.card().classes("w-1/2 bg-orange-2"):
            ui.label("服务器已有同名文件，无法上传覆盖，是否使用服务器已有文件？").classes("text-lg")
            with ui.row().classes("w-full justify-end"):
                ui.button(
                    "是",
                    on_click=lambda: self._show_have_file(original_filename, file_type, url_path),
                    color="green-6",
                )
                ui.button("否", on_click=lambda: self.chip_dialog.close(), color="blue-grey-6")

    # 刷新chip容器
    def _refresh_chip_container(self):
        # 删除元素重新显示
        self.chip_container.clear()
        with self.chip_container:
            for chip_info in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values():
                if self.processing_type == "search":
                    target_path = self._search_file_path()
                    self._create_chip_from_data(chip_info, target_path)
                else:
                    self._create_chip_from_data(chip_info, "")

    # 同步UI显示与共享存储中的数据
    def _update_chip_display(self):
        """
        同步UI显示与共享存储中的数据。
        这是由定时器调用的核心同步函数。
        """
        # 在用户打开了大图的情况下，不刷对应条目下的缩略图元素
        if not (self.chip_dialog.value or self.check_down_dialog.value or self.activ_dialog.value):
            # if self.processing_type == "image":
            #     print(self.chip_dialog.value, self.title)

            # 获取当前UI上所有 chip 的ID
            displayed_chip_ids = {child.props.get("data-chip-id") for child in self.chip_container}
            # 获取共享存储中所有 chip 的ID
            stored_chips_data = db_storage.get_deep_item([f"{self.project}_over_data", self.label], {})
            stored_chip_ids = set(stored_chips_data.keys())

            # 只有当UI和存储中的ID集合不一致时，才重新渲染，以提高效率
            if displayed_chip_ids != stored_chip_ids:
                # 刷新chip容器内容
                self._refresh_chip_container()
                # 刷新角色负责用户数据
                overview_role_update(self.project)

    # 打开文件
    # def open_file(self, filepath):
    #     # 获取操作系统类型
    #     os_type = sys.platform
    #     if os_type == "win32":
    #         # os.startfile(f"{UPLOADS_DIR}/{self.file_neme_suffix}")
    #         os.startfile(filepath)
    #     elif os_type == "darwin":
    #         # subprocess.run(["open", f"{UPLOADS_DIR}/{self.file_neme_suffix}"])
    #         subprocess.run(["open", filepath])
    #     else:
    #         ui.notify(
    #             "未适配当前操作系统，不能直接打开。",
    #             type="info",
    #             position="bottom",
    #             timeout=1000,
    #             progress=True,
    #             close_button="✖",
    #         )
    # pdf文件打开函数
    def open_pdf_in_browser(self, url_path):
        # 在浏览器中打开 PDF 文件
        async def get_base_url():
            # 通过 JavaScript 获取当前页面的协议、域名和路径
            result = await ui.run_javascript("window.location.origin;")
            return result

        # 2. 异步执行并拼接完整 URL
        async def open_pdf():
            base_url = await get_base_url()
            full_url = f"{base_url}{url_path}"
            # 处理空格等特殊字符
            encoded_url = full_url.replace(" ", "%20")
            # 3. 打开新窗口
            ui.run_javascript(f'window.open("{encoded_url}", "_blank");')
            # print(f"尝试打开PDF：{encoded_url}")

        # 启动异步任务
        ui.timer(0.1, lambda: open_pdf(), once=True)

    def trigger_download(self, filepath, file_name, on_complete=None):
        """专门负责触发下载的辅助函数"""
        ui.notify(
            f"开始下载文件: {file_name}",
            type="info",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
        ui.download(filepath)
        if on_complete:
            on_complete()

    # 我们创建一个新的、更智能的下载处理函数
    async def check_and_download(self, filepath, file_name):
        """
        检查文件是否已在当前会话下载过。
        如果是，则弹出一个带引导信息的对话框；如果否，则开始下载并标记。
        """
        storage_key = f"downloaded_{file_name}"
        has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

        if has_downloaded:
            # 【修改点】对这里的对话框进行全面升级
            self.check_down_dialog.clear()
            with self.check_down_dialog, ui.card().classes("min-w-[400px]"):
                with ui.card_section():
                    ui.label(f'文件 "{file_name}" 已在本次会话中下载。').classes("text-lg font-medium")
                    ui.separator().props("size=1px").classes("my-3")
                    ui.label("您可以：")
                    # 使用 HTML 来创建更丰富的文本格式
                    ui.html(
                        """
                        <ul class="q-pl-lg">
                            <li>在浏览器的<b>下载栏</b>中直接找到它。</li>
                            <li>按键盘快捷键 <kbd>Ctrl</kbd> + <kbd>J</kbd> (Windows/Linux) 或 <kbd>⌘</kbd> + <kbd>Shift</kbd> + <kbd>J</kbd> (Mac) 打开<b>下载内容页面</b>。</li>
                        </ul>
                    """,
                        sanitize=False,
                    ).classes("text-base")  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                with ui.card_actions().props("align=right"):
                    # “重新下载”按钮，保持原样
                    ui.button(
                        "仍要重新下载",
                        on_click=lambda filepath=filepath, file_name=file_name: self.trigger_download(
                            filepath, file_name, self.check_down_dialog.close
                        ),
                        color="primary",
                    )
                    # 将“取消”按钮改为更中性的“关闭”
                    ui.button("关闭", on_click=self.check_down_dialog.close, color="grey")

            self.check_down_dialog.open()

        # 3. 如果标记不存在 (首次点击)
        else:
            # a. 立即触发下载
            self.trigger_download(filepath, file_name)
            # b. 通过JavaScript在客户端设置标记
            await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

    # 当元素被鼠标右键点击时触发的事件处理函数
    def on_right_click(self, chip_data):
        """
        当元素被鼠标右键点击时触发的事件处理函数。
        """
        # 将 Python 变量的内容传递给 JavaScript
        # navigator.clipboard.writeText(text) 是现代浏览器提供的剪贴板 API
        # 这里的 f-string 会将 Python 变量值安全地嵌入到 JS 代码中
        text = ""
        if "content" in chip_data.keys():
            text = chip_data.get("content")
        elif "filename" in chip_data.keys():
            text = chip_data.get("filename")
        js_code = f"navigator.clipboard.writeText('{text}');"
        ui.run_javascript(js_code)
        ui.notify("内容已复制到剪贴板！", type="positive", position="top")

    # <-----------------------------------------------------------------
    # 设置chip的激活状态
    async def _set_chip_activ(self, chip_id, old_chip_select_dic, chip_text):
        # chip以当前最新版本的设置为当前显示状态
        req_max_ver = app.storage.general["project_req_max_ver"][self.project]
        if db_storage.get_deep_item(
            [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", req_max_ver]
        ):
            # 激活chip
            # 修改这里要检查utils和information两个模块是否跟着改
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], True)
            if db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"]) == "file":
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "icon"], "attach_file"
                )
            elif db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"]) == "search":
                target_path = self._search_file_path()
                if target_path:
                    files_li = find_files_pathlib(target_path, chip_text)
                    if len(files_li) == 1:
                        await db_storage.set_deep_item(
                            [f"{self.project}_over_data", self.label, chip_id, "icon"], "search_check"
                        )
                    else:
                        await db_storage.set_deep_item(
                            [f"{self.project}_over_data", self.label, chip_id, "icon"], "search_off"
                        )
                else:
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_data", self.label, chip_id, "icon"], "search_off"
                    )
            else:
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], None)
            await db_storage.set_deep_item(
                [f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-light-blue-1"
            )
        # 防止chip状态None（null）被当成False，当用户在弹窗选择激活状态时不做选择动作，保持原有null状态chip被处理成False显示效果
        elif (
            db_storage.get_deep_item(
                [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", req_max_ver]
            )
            is None
        ):
            # 该情况意味着用户没有修改当前chip最新版本的null状态，看了一下而已
            # 只要跳过这个情况不做任何修改即可
            pass
            # 冗余设计，复用注意检查与整体刷新处设置是否一致
            # 修改这里要检查utils和information两个模块是否跟着改
            # app.storage.general["overview_data"][self.project][self.label][chip_id]["enabled"] = None
            # app.storage.general["overview_data"][self.project][self.label][chip_id]["icon"] = "question_mark"
            # app.storage.general["overview_data"][self.project][self.label][chip_id]["bg_color"] = "bg-amber-5"
        else:
            # 失活chip
            # 修改这里要检查utils和information两个模块是否跟着改
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], False)
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "block")
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-grey-5")

        # 如果激活弹窗关闭时，检测到激活多选项发生了变化，则修改该chip的编辑人
        select_activ_dic = copy.deepcopy(
            db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {})
        )
        if old_chip_select_dic != select_activ_dic:
            creator = app.storage.user.get("current_user", "匿名用户")
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "creator"], creator)
            await db_storage.set_deep_item(
                [
                    f"{self.project}_over_data",
                    self.label,
                    chip_id,
                    "timestamp",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ],
                {
                    "creator": creator,
                    "select_activ_dic": select_activ_dic,
                },
            )
        # 刷新chip容器内容
        self._refresh_chip_container()
        # 刷新概述负责人
        overview_role_update(self.project)

    # 创建用于让用户选择chip激活范围的弹窗
    def _select_activ_dialog(self, chip_id, chip_text=""):
        self.activ_dialog.clear()
        with self.activ_dialog, ui.card().classes("w-1/2"):
            ui.label("选择概述生效的需求版本").classes("text-lg font-bold")
            chip_select_dic = db_storage.get_deep_item(
                [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {}
            )
            old_chip_select_dic = copy.deepcopy(chip_select_dic)
            with ui.grid(columns=6).classes("w-full gap-0"):
                for select_label, val in chip_select_dic.items():
                    ui.checkbox(
                        text=select_label,
                        value=val,
                        on_change=lambda e: db_storage.set_deep_item(
                            [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", select_label],
                            e.value,
                        ),
                    )
            with ui.row().classes("w-full justify-end"):
                ui.label("注意以上改动是即时生效的").classes("text-lg font-bold")
                ui.button("关闭", on_click=lambda: self._set_chip_activ(chip_id, old_chip_select_dic, chip_text)).on(
                    "click", lambda: self.activ_dialog.close()
                )
        self.activ_dialog.open()

    # 删除或修改chip在app.storage.general对应的数据
    async def delete_chip_info(self, chip):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            if app.storage.user["current_user"] == "admin":
                # del app.storage.general["overview_data"][self.project][self.label][chip.props["data-chip-id"]]
                await db_storage.del_deep_item([f"{self.project}_over_data", self.label, chip.props["data-chip-id"]])

            elif app.storage.user["current_user"] != "admin":
                # app.storage.general["overview_data"][self.project][self.label][chip.props["data-chip-id"]]["removable"] = False
                chip_id = chip.props["data-chip-id"]
                self._select_activ_dialog(chip_id, chip["text"])

    # 删除或修改文件缩略图及其在app.storage.general的数据
    async def clear_thumbnail(self, thumbnail):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            if app.storage.user["current_user"] == "admin":
                thumbnail.delete()
                # del app.storage.general["overview_data"][self.project][self.label][thumbnail.props["data-chip-id"]]
                await db_storage.del_deep_item(
                    [f"{self.project}_over_data", self.label, thumbnail.props["data-chip-id"]]
                )
            elif app.storage.user["current_user"] != "admin":
                chip_id = thumbnail.props["data-chip-id"]
                self._select_activ_dialog(chip_id)

    # 将该项插入的chip里指定chip上移一个位置
    async def move_up_data(self, chip_data):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            temp_data = {}
            old_data_keys = list(db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).keys())
            new_data_keys = move_element(old_data_keys, chip_data["id"], -1)
            for k in new_data_keys:
                # temp_data[k] = app.storage.general["overview_data"][self.project][self.label][k]
                temp_data[k] = db_storage.get_deep_item([f"{self.project}_over_data", self.label, k], {})
            # app.storage.general["overview_data"][self.project][self.label] = temp_data
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label], temp_data)
            # 刷新chip容器内容
            self._refresh_chip_container()

    # 将该项插入的chip里指定chip上移一个位置
    async def move_down_data(self, chip_data):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            temp_data = {}
            old_data_keys = list(db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).keys())
            new_data_keys = move_element(old_data_keys, chip_data["id"], 1)
            for k in new_data_keys:
                # temp_data[k] = app.storage.general["overview_data"][self.project][self.label][k]
                temp_data[k] = db_storage.get_deep_item([f"{self.project}_over_data", self.label, k], {})
            # app.storage.general["overview_data"][self.project][self.label] = temp_data
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label], temp_data)
            # 刷新chip容器内容
            self._refresh_chip_container()

    # 根据字典数据创建一个具体的 ui.chip 组件。
    def _create_chip_from_data(self, chip_info: dict, target_path: str):
        chip_text = ""
        filepath = ""
        delete_icon = ""
        delete_bg = ""
        # 根据用户类型及删除按钮状态设置新的删除按钮类型
        if app.storage.user["current_user"] == "admin":
            delete_icon = "close"
            delete_bg = "bg-red text-white"
        else:
            if chip_info.get("icon") == "block":
                delete_icon = "settings"  # 之前是check
                delete_bg = "bg-white text-light-blue"
            else:
                delete_icon = "settings"  # 之前是block
                delete_bg = "bg-white text-light-blue"  # 之前是text-grey-10

        if chip_info.get("type") in ["text", "file", "test", "search"]:
            # 根据chip类型配置文字标签内容
            filepath = ""
            if chip_info.get("type") in ["text", "test"]:
                chip_text = chip_info.get("content", "")
            elif chip_info["type"] == "file":
                chip_text = chip_info.get("filename", "")
                # 每次生成都用更新配置的路径
                filepath = f"{self.upload_path}/{chip_text}"
                # 以后改了文件夹配置，chip不会失效
                app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            elif chip_info["type"] == "search":
                chip_text = chip_info.get("content", "")
                # 每次生成都用更新配置的路径
                # 判断路径是否是文件夹且存在
                if target_path and Path(target_path).is_dir():
                    files_li = find_files_pathlib(target_path, chip_text)
                    if not files_li:
                        ui.notify(
                            f"引用文件不存在该路径下：\n{target_path}",
                            type="warning",
                            position="center",
                            timeout=0,
                            progress=False,
                            multi_line=True,
                            close_button="✖",
                        )
                    elif len(files_li) > 1:
                        ui.notify(
                            f"引用文件在该路径下：\n{target_path}\n存在多个同名文件（子文件夹里存在）",
                            type="warning",
                            position="center",
                            timeout=0,
                            progress=False,
                            multi_line=True,
                            close_button="✖",
                        )
                    else:
                        # 以后改了文件夹配置，chip不会失效
                        filepath = str(files_li[0])
                        app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            # 创建 chip 并附加一个自定义属性 `data-chip-id` 用于后续的同步检查
            chip = (
                ui.chip(text=chip_text, removable=False, icon=chip_info.get("icon"))
                .props(f"data-chip-id={chip_info.get('id')} dense square")
                .classes(f"m-0 {chip_info.get('bg_color')}")
            )
            if chip_info.get("type") == "text":
                pass
                # chip.on_click(lambda: print(chip.value))
                # chip.set_enabled(False)
            elif chip_info.get("type") in ["file", "search"]:
                if chip_info.get("file_type") == "application/pdf" and filepath and Path(filepath).exists():
                    # 使用浏览器打开则用open_pdf_in_browser()
                    chip.on_click(lambda url_path=chip_info.get("url_path"): self.open_pdf_in_browser(url_path))
                elif filepath and Path(filepath).exists():
                    chip.on_click(
                        lambda filepath=filepath, file_name=chip_text: self.check_and_download(filepath, file_name)
                    )
                else:
                    if chip_info["type"] == "file":
                        chip.set_icon("attach_file_off")
                    elif chip_info["type"] == "search":
                        chip.set_icon("search_off")
                    chip.on_click(
                        lambda: ui.notify(
                            "文件不存在服务器、路径失效、不唯一，点击不能打开或下载！",
                            type="warning",
                            position="center",
                            timeout=0,
                            progress=False,
                            multi_line=True,
                            close_button="✖",
                        )
                    )

            # 创建chip元素的附属元素
            with chip:
                # 为 chip 添加 tooltip
                tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>{chip_info.get('notes', '').replace('\n', '<br>')}"

                with ui.tooltip():
                    ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                # 注意：我们将on_click事件直接绑定在这里
                delete_button = (
                    ui.button(on_click=lambda c=chip: self.delete_chip_info(c))
                    .classes(f"absolute -top-1 -right-1 m-0 p-0 q-py-0 {delete_bg}")
                    .props(f'round padding="0px 0px" icon={delete_icon}')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                # chip上移按钮
                move_up_button = (
                    ui.button(on_click=lambda chip_data=chip_info: self.move_up_data(chip_data))
                    .classes("absolute -top-1 right-7 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_up"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                # chip下移按钮
                move_down_button = (
                    ui.button(on_click=lambda chip_data=chip_info: self.move_down_data(chip_data))
                    .classes("absolute -top-1 right-3 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_down"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
            # 设置chip元素是否显示
            # chip.set_value(chip_info["value"])
            # 设置chip元素是否可点击，会导致其上的好标签出不来
            # chip.set_enabled(chip_info["enabled"])

            # 为chip绑定各种事件
            chip.on("contextmenu", lambda chip_data=chip_info: self.on_right_click(chip_data))
            chip.on("mouseenter", lambda b=delete_button: ui_show(b)).on(
                "mouseleave", lambda b=delete_button: ui_hide(b)
            )
            chip.on("mouseenter", lambda b=move_up_button: ui_show(b)).on(
                "mouseleave", lambda b=move_up_button: ui_hide(b)
            )
            chip.on("mouseenter", lambda b=move_down_button: ui_show(b)).on(
                "mouseleave", lambda b=move_down_button: ui_hide(b)
            )

        # chip类型为缩略图
        elif chip_info.get("type") == "image":
            image_name = chip_info.get("filename")

            # 每次生成都用更新配置的路径
            image_path = f"{self.upload_path}/{image_name}"

            url_path = f"{FILES_URL_DIR}/{image_name}"
            # print(image_path, url_path)
            # 以后改了文件夹配置，chip不会失效
            app.add_static_file(local_file=image_path, url_path=url_path)
            # 根据文件类型创建缩略图
            thumbnail = (
                ui.interactive_image(url_path)
                .props(f"data-chip-id={chip_info.get('id')}")
                .classes("h-10 cursor-pointer")
            )
            thumbnail.on("click", lambda url_path=url_path: self.show_fullscreen(url_path))

            # 创建缩略图的附属元素
            with thumbnail:
                if chip_info.get("icon"):
                    ui.icon(chip_info.get("icon", "")).props("flat fab color=red").classes(
                        "absolute top-0 left-0 text-xl"
                    )
                # 缩略图创建日期提示
                tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>图片名: {image_name}<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>{chip_info.get('notes', '').replace('\n', '<br>')}"
                with ui.tooltip():
                    ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                # 缩略图删除按钮
                delete_button = (
                    ui.button(on_click=lambda thumbnail=thumbnail: self.clear_thumbnail(thumbnail))
                    .classes(f"absolute -top-1 -right-1 m-0 p-0 q-py-1 {delete_bg}")
                    .props(f'round padding="0px 0px" icon={delete_icon}')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                # 缩略图上移按钮
                move_up_button = (
                    ui.button(on_click=lambda chip_data=chip_info: self.move_up_data(chip_data))
                    .classes("absolute bottom-3 -right-1 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_up"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                # 缩略图下移按钮
                move_down_button = (
                    ui.button(on_click=lambda chip_data=chip_info: self.move_down_data(chip_data))
                    .classes("absolute -bottom-1 -right-1 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_down"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

            # 为缩略图绑定各种事件
            thumbnail.on("mouseover", lambda b=delete_button: ui_show(b)).on(
                "mouseout", lambda b=delete_button: ui_hide(b)
            )
            thumbnail.on("mouseover", lambda b=move_up_button: ui_show(b)).on(
                "mouseout", lambda b=move_up_button: ui_hide(b)
            )
            thumbnail.on("mouseover", lambda b=move_down_button: ui_show(b)).on(
                "mouseout", lambda b=move_down_button: ui_hide(b)
            )

    # <-----------------------------------------------------------------
    # 创建用于输入文本chip的概述内容与注释的对话框
    def _setup_text_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加新的概述内容").classes("text-lg font-bold")
            self.chip_label = (
                ui.textarea(label=self.dialog_label, placeholder=self.dialog_placeholder)
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="针对该技术概述的注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda value: value.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("添加", on_click=self._add_text_chip_data)
        self.chip_dialog.open()

    # 创建用于搜寻文件类型chip的概述内容与注释的对话框
    def _setup_search_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加新的文件引用概述内容").classes("text-lg font-bold")
            self.chip_label = (
                ui.input(label=self.dialog_label, placeholder="填入包括后缀的完整文件名")
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="针对该技术概述的注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda value: value.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("添加", on_click=self._add_search_chip_data)
        self.chip_dialog.open()

    # 触发文件上传界面，用于给用户选择文件，然后自动触发文件处理函数
    def _get_file_upload(self):
        if not self.chip_notes.value:
            ui.notify(
                "注释不能为空!",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        else:
            # 在上传新文件前，先清空uploader列表，否则后续删除文件后，不能在重新插入
            self.uploader.reset()
            # 调用JavaScript方法来触发隐藏的<input type="file">元素的点击事件
            self.uploader.run_method("pickFiles")

    # 创建用于输入文件注释的对话框
    def _setup_file_notes_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加上传文件的注释").classes("text-lg font-bold")
            self.chip_notes = (
                ui.textarea(
                    label="针对该文件的注释（必填）",
                    placeholder="首次提交/变更原因",
                    validation={"不能空白": lambda value: value.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                self.spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                self.spinner.set_visibility(False)
                ui.button("添加", on_click=self._get_file_upload)
        self.chip_dialog.open()

    def _set_other_ui(self, other_ui, select_value):
        if select_value == "其它":
            other_ui.set_visibility(True)
        elif other_ui:
            other_ui.set_visibility(False)
            other_ui.set_value("")

    # 创建用于配置测试项的对话框
    def _setup_test_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-full"):
            ui.label(f"添加产品的{self.title}").classes("text-lg font-bold")
            placeholder = ""
            test_select_data = {
                "state_select": "",
                "state_other_text": "",
                "node_select": "",
                "node_other_text": "",
                "instrument_select": "",
                "instrument_other_text": "",
            }

            if self.label == "optical_testing":
                placeholder = "色温：5500K±500K"
            elif self.label == "mechanical_testing":
                placeholder = "测试项名称：测试条件、产品状态、操作步骤、合格标准等信息。"
            elif self.label == "electronic_testing":
                placeholder = "电压：12V±3%"
            elif self.label == "software_testing":
                placeholder = "测试项名称：操作步骤、合格标准等信息；或指明依据的文档。"
            elif self.label == "ui_testing":
                placeholder = "写明UI检查内容与要求。"
            self.chip_label = (
                ui.textarea(
                    label="检测内容与标准",
                    placeholder=placeholder,
                    validation={"不能空白": lambda value: value.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            if self.state_options:
                with ui.column().classes("w-full p-0 m-0"):
                    state_select = (
                        ui.select(
                            self.state_options,
                            multiple=False,
                            label="条件/状态",
                        )
                        .props("outlined")
                        .classes("w-full")
                        .bind_value(test_select_data, "state_select")
                    )
                    state_other_ui = (
                        ui.textarea(
                            label="条件/状态特殊要求",
                            placeholder="写明特殊要求",
                            validation={"不能空白": lambda value: value.strip() != ""},
                        )
                        .props("outlined")
                        .classes("w-full")
                        .bind_value(test_select_data, "state_other_text")
                    )
                    state_other_ui.set_visibility(False)
                    state_select.on_value_change(lambda: self._set_other_ui(state_other_ui, state_select.value))
            if self.node_options:
                with ui.column().classes("w-full p-0 m-0"):
                    node_select = (
                        ui.select(
                            self.node_options,
                            multiple=False,
                            label="节点/位置",
                        )
                        .props("outlined")
                        .classes("w-full")
                        .bind_value(test_select_data, "node_select")
                    )
                    node_other_ui = (
                        ui.textarea(
                            label="节点/位置特殊要求",
                            placeholder="写明特殊要求",
                            validation={"不能空白": lambda value: value.strip() != ""},
                        )
                        .props("outlined")
                        .classes("w-full")
                        .bind_value(test_select_data, "node_other_text")
                    )
                    node_other_ui.set_visibility(False)
                    node_select.on_value_change(lambda: self._set_other_ui(node_other_ui, node_select.value))
            if self.instrument_options:
                with ui.column().classes("w-full p-0 m-0"):
                    instrument_select = (
                        ui.select(
                            self.instrument_options,
                            multiple=False,
                            label="工具/仪器/治具",
                        )
                        .props("outlined")
                        .classes("w-full")
                        .bind_value(test_select_data, "instrument_select")
                    )
                    instrument_other_ui = (
                        ui.textarea(
                            label="工具/仪器/治具特殊要求",
                            placeholder="写明特殊要求",
                            validation={"不能空白": lambda value: value.strip() != ""},
                        )
                        .props("outlined")
                        .classes("w-full")
                        .bind_value(test_select_data, "instrument_other_text")
                    )
                    instrument_other_ui.set_visibility(False)
                    instrument_select.on_value_change(
                        lambda: self._set_other_ui(instrument_other_ui, instrument_select.value)
                    )
            self.chip_notes = (
                ui.textarea(
                    label="针对该检测内容与标准的注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda value: value.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("添加", on_click=lambda: self._add_test_chip_data(test_select_data))
        self.chip_dialog.open()

    # ----------------------------------------------------------------->

    # 判断当前用户是否具有编辑权限
    def _edit_permission_judge(self):
        # 判断用户是否具有编辑权限 且 不处于概述审核界面
        if app.storage.user["current_role"] in self.permission["edit_role"] and not self.temp_bool:
            return True
        elif self.temp_bool:
            ui.notify(
                "当前处于需求审核界面，概述内容锁定不可编辑!",
                type="info",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            return False
        else:
            ui.notify(
                "当前用户无该项编辑权限，请联系管理员申请!",
                type="info",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            return False

    # 处理主按钮的点击事件
    def _handle_main_button_click(self):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            # 根据处理类型，设置不同的交互逻辑
            if self.processing_type == "text":
                # 设置文本chip的弹窗格式
                self._setup_text_chip_dialog()
            elif self.processing_type == "test":
                # 设置文件类chip的弹窗格式
                self._setup_test_chip_dialog()
            elif self.processing_type == "search":
                # 设置文件类chip的弹窗格式
                self._setup_search_chip_dialog()
            else:
                # 设置文件类chip的弹窗格式
                self._setup_file_notes_dialog()
