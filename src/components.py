# -*- encoding: utf-8 -*-
import ast
import asyncio
import atexit
import copy
import io
import itertools
import json
import logging
import math
import os
import re
import shutil
import ssl
import sys
import time
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Final, Optional, Tuple

import httpx
import wcwidth
from html_sanitizer import Sanitizer
from httpx import BasicAuth
from nicegui import app, events, ui
from nicegui.events import GenericEventArguments, MouseEventArguments, ValueChangeEventArguments

from . import db_storage  # 导入我们创建的模块
from .config import (
    FILES_URL_DIR,
    IMG_DIR,
    OVER_UPLOADS_FILE_TYPE,
    PDF_PREVIEW_CACHE,
    SUBMIT_FILES_DIR,
    SVN_PASSWORD,
    SVN_USERNAME,
    UPLOADS_DIR,
)
from .utils import (
    find_dirs_by_name_os_walk,
    find_files_pathlib,
    get_file_type_by_extension,
    get_time,
    move_element,
    overview_role_update,
    overview_state_show_judge,
    ui_hide,
    ui_show,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


class StorageBackupManager:
    def __init__(
        self, json_storage_file: str = "storage-general.json", backup_dir: str = "backups", retention_days: int = 30
    ):
        """
        混合备份管理器：同时负责 JSON 文件和 SQLite 数据库的备份。

        :param json_storage_file: 原有的 JSON 存储文件名
        :param backup_dir: 备份存放目录 (相对于项目根目录)
        :param retention_days: 备份保留天数
        """
        # --- 1. JSON 文件配置 ---
        self.json_file = Path(json_storage_file)

        # --- 2. SQLite 数据库配置 ---
        # 直接引用 db_storage 中的路径配置，确保一致性
        self.db_path = Path(db_storage.DB_PATH)

        # --- 3. 公共配置 ---
        self.backup_dir_name = backup_dir
        # 构造备份文件夹的绝对路径
        self.backup_dir_path = db_storage.BASE_DIR / backup_dir
        self.retention_days = retention_days

        # 创建目录
        self.backup_dir_path.mkdir(parents=True, exist_ok=True)  # 自动创建备份文件夹（如果不存在）

        # 确保在初始化时绑定钩子,一启动就挂载监听钩子,让该管理器立即开始监听系统的生与死
        self._register_hooks()
        logger.info(f"备份管理器已启动. 监控目标: JSON={self.json_file.name}, DB={self.db_path.name}")

    async def run_safe_backup(self, trigger_type: str):
        """
        【安全模式 - 异步】
        适用于：定时任务、正常关机、手动触发。
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.info(f"[{trigger_type}] 开始执行全量安全备份...")

        # --- 任务 A: 备份 JSON 文件 (原有逻辑) ---
        self._backup_json_file(trigger_type, timestamp)

        # --- 任务 B: 备份 SQLite 数据库 (新逻辑 - 异步) ---
        try:
            # 调用 db_storage 的原生接口，它处理了锁和 WAL 刷新
            saved_db_path = await db_storage.backup_db(
                backup_dir=self.backup_dir_name, retention_days=self.retention_days
            )
            if saved_db_path:
                logger.info(f"[{trigger_type}] SQLite 数据库备份成功")
        except Exception:
            logger.error(f"[{trigger_type}] SQLite 数据库备份失败", exc_info=True)

    def run_emergency_backup(self, trigger_type: str):
        """
        【紧急模式 - 同步】
        适用于：系统崩溃 (Crash)。
        强制拷贝所有文件，不等待异步锁。
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        logger.critical(f"[{trigger_type}] 正在执行紧急备份 (Crash Dump)...")

        # --- 任务 A: 备份 JSON 文件 ---
        self._backup_json_file(trigger_type, timestamp)

        # --- 任务 B: 备份 SQLite 数据库 (强制文件拷贝) ---
        self._backup_sqlite_force(trigger_type, timestamp)

    def _backup_json_file(self, trigger_type: str, timestamp: str):
        """内部辅助：复制 JSON 文件"""
        if not self.json_file.exists():
            return  # 文件不存在则跳过

        try:
            # 备份文件名格式为 原文件名_触发类型_时间戳.json
            target_name = f"{self.json_file.stem}_{trigger_type}_{timestamp}{self.json_file.suffix}"
            target_path = self.backup_dir_path / target_name
            # 不仅仅是复制文件内容，还保留了文件的元数据（如创建时间、最后修改时间），这对于数据恢复时的判断非常重要
            shutil.copy2(self.json_file, target_path)
            logger.info(f"[{trigger_type}] JSON 备份成功: {target_name}")
        except Exception:
            logger.error(f"[{trigger_type}] JSON 备份失败", exc_info=True)

    def _backup_sqlite_force(self, trigger_type: str, timestamp: str):
        """内部辅助：强制复制 SQLite 文件 (包含 WAL/SHM)"""
        if not self.db_path.exists():
            return

        try:
            # 1. 拷贝 .db 主文件
            base_name = f"{self.db_path.stem}_EMERGENCY_{trigger_type}_{timestamp}"
            shutil.copy2(self.db_path, self.backup_dir_path / f"{base_name}.db")

            # 2. 拷贝 .db-wal (预写日志) - 崩溃恢复的关键
            wal_path = self.db_path.with_suffix(".db-wal")
            if wal_path.exists():
                shutil.copy2(wal_path, self.backup_dir_path / f"{base_name}.db-wal")

            # 3. 拷贝 .db-shm (共享内存)
            shm_path = self.db_path.with_suffix(".db-shm")
            if shm_path.exists():
                shutil.copy2(shm_path, self.backup_dir_path / f"{base_name}.db-shm")

            logger.info(f"[{trigger_type}] DB 紧急文件已保存 (包含WAL日志)")
        except Exception:
            logger.error(f"[{trigger_type}] DB 紧急备份失败", exc_info=True)

    def _register_hooks(self):
        """
        注册系统级和框架级钩子 (修复版)
        """

        # -------------------------------------------------------
        # 1. 正常关闭 (NiceGUI Lifecycle) - 修复 RuntimeWarning
        # -------------------------------------------------------
        async def shutdown_handler():
            # 这里必须是一个 async 函数，NiceGUI 才能识别并 await 它
            try:
                await self.run_safe_backup("SHUTDOWN_NORMAL")
            except Exception:
                logger.error("关闭时备份失败", exc_info=True)

        # 直接传入这个 async 函数，不要用 lambda
        app.on_shutdown(shutdown_handler)

        # -------------------------------------------------------
        # 2. 异常崩溃 (Unhandled Exception Hook)
        # -------------------------------------------------------
        # 保留原始钩子，防止覆盖其他库的逻辑
        original_excepthook = sys.excepthook

        def custom_excepthook(exc_type, exc_value, exc_traceback):
            # 调试信息：确保钩子真的被触发了
            # print(f"\n!!! 检测到崩溃: {exc_type.__name__}: {exc_value} !!!", file=sys.stderr)
            # 使用 logger.error() 替换 print(..., file=sys.stderr)
            # 我们使用 f-string 来格式化消息，并保持原语句的结构。
            error_message = f"检测到崩溃: {exc_type.__name__}: {exc_value}"
            # 使用 logger.error() 记录错误
            logger.error("!!! %s !!!", error_message, exc_info=(exc_type, exc_value, exc_traceback))

            # 过滤掉键盘中断 (Ctrl+C)，通常这是用户手动停止，不算崩溃
            # 除非你希望 Ctrl+C 也触发紧急备份，可以去掉这个判断
            if not issubclass(exc_type, KeyboardInterrupt):
                logger.critical("检测到未捕获异常，正在尝试紧急备份...", exc_info=(exc_type, exc_value, exc_traceback))
                # 调用同步的紧急备份
                self.run_emergency_backup("CRASH_EXCEPTION")
            # 调用原始的 hook 打印错误堆栈并退出
            original_excepthook(exc_type, exc_value, exc_traceback)  # 恢复原本的报错流程

        # “劫持”了 Python 默认的报错机制。
        # 当代码里出现未捕获的错误导致程序要崩溃时，它会先执行备份，然后再让程序崩溃并打印错误日志
        sys.excepthook = custom_excepthook

    def start_daily_schedule(self, hour: int = 2, minute: int = 0):
        """
        启动每日定时备份任务
        :param hour: 24小时制的小时
        :param minute: 分钟
        """
        now = datetime.now()
        target_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if target_time <= now:
            target_time += timedelta(days=1)

        delay_seconds = (target_time - now).total_seconds()
        logger.info(f"每日备份计划已设定: {target_time}")

        # 包装器：确保在 timer 中可以调用 async 函数
        async def run_schedule():
            await self.run_safe_backup("DAILY_SCHEDULE")
            # 重新调度下一次 (24小时后)
            app.timer(86400, run_schedule, once=True)

        app.timer(delay_seconds, run_schedule, once=True)


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
            logger.info("上传文件无绑定回调函数")


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
        # --- 视频弹窗 ---
        self.video_dialog = ui.dialog().classes("p-0 bg-transparent shadow-none")
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
        # 2. 视频处理 (新增!!!)
        elif self.file_type.startswith("video/"):
            with ui.row().classes(f"h-{str(self.parents_h)} flex-nowrap gap-1") as self.video_row:
                # 使用视频图标，或者你可以截取一帧作为封面(比较复杂)，这里用图标最简单
                self.thumbnail = (
                    ui.interactive_image(f"{IMG_DIR}/file_type_video.png", content="")
                    .classes("h-full text-5xl cursor-pointer")
                    .classes("h-full aspect-[1/1] cursor-pointer")
                    .on("click", self.play_video)  # 绑定播放函数
                )
                # 叠加一个播放的小图标在上面，增加辨识度
                with self.thumbnail:
                    ui.icon("play_circle_outline").classes(
                        "absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 text-black text-xl opacity-80"
                    )

                ui.label(self.file_neme).classes(
                    f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-all text-black p-0 m-0 bg-white-500"
                )
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
                    f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-all text-black p-0 m-0 bg-white-500 "
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
                    f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-all text-black p-0 m-0 bg-white-500"
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

        # 启动异步任务
        ui.timer(0.2, lambda: open_pdf(), once=True)

    def trigger_download(self, on_complete=None):
        """专门负责触发下载的辅助函数"""
        ui.notify(
            f"开始下载文件: {self.file_neme_suffix}",
            type="info",
            position="bottom",
            timeout=2000,
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

    # 处理数字链接的点击事件
    async def handle_index_click(self):
        # if self.file_neme_hash in app.storage.client["deleted_files"]:
        if app.storage.client["file_thumbnail_dic"][self.file_index]["file_information"]["file_del_bool"]:
            ui.notify(
                "该文件已被销售删除，虽可查看，但谨慎参考！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            await asyncio.sleep(3)
        if self.file_type.startswith("image/"):
            self.show_fullscreen()
        elif self.file_type.startswith("video/"):
            self.play_video()
        elif self.file_type == "application/pdf":
            self.open_pdf_in_browser()  # 使用浏览器打开则用open_pdf_in_browser()
        else:
            await self.check_and_download()

    # 播放视频函数 (新增)
    def play_video(self):
        self.video_dialog.clear()
        with self.video_dialog:
            # 修改 Card 样式：
            # 1. w-auto: 让卡片宽度适应视频宽度
            # 2. max-w-screen-xl: 限制最大宽度，防止视频过大溢出屏幕
            # 3. overflow-hidden: 防止圆角处漏出
            with ui.card().classes(
                "w-auto max-w-screen-xl min-w-[300px] bg-black p-0 items-center justify-center relative-position overflow-hidden"
            ):
                # 修改 Video 样式：
                # 1. w-full: 让视频填满卡片宽
                # 2. max-h-[85vh]: 限制高度，防止超出垂直屏幕范围
                ui.video(src=self.file_url).classes("w-full max-h-[85vh]").props("controls autoplay")
                # 关闭按钮保持不变
                ui.button(icon="close", on_click=self.video_dialog.close).props("flat round color=white").classes(
                    "absolute top-2 right-2 z-10 opacity-70 hover:opacity-100"
                )
        self.video_dialog.open()

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
        impact_list: list = [],
        upload_path: str = SUBMIT_FILES_DIR,
        state_path: dict = {},
        search_scope_regular: str = "",
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
        if processing_type not in ["text", "file", "image", "test", "search", "svn", "video"]:
            raise ValueError("processing_type 必须是 'text','file','image','test','search','svn','video'")

        self.role = role
        self.title = title
        self.label = label
        self.project = project
        self.processing_type = processing_type
        self.impact_list = impact_list
        self.upload_path = upload_path
        self.state_path = state_path
        self.search_scope_regular = search_scope_regular
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
        self.overview_video_dialog = ui.dialog().classes("p-0 bg-transparent shadow-none")
        self.check_down_dialog = ui.dialog().classes("")
        self.activ_dialog = ui.dialog().props("persistent").classes("")
        self.history_dialog = ui.dialog().classes("w-full")
        # self.image_show = {"image_show": True}
        # self.chip_dialog.bind_value_to(self.image_show, "image_show")

        # 为每个按钮实例在 app.storage.general 概述数据各项目字典里 以self.label作为键，后续保存用户输入
        # 初始化存储，如果 app.storage.general 中不存在对应的列表，则创建一个空列表
        # if self.label not in db_storage.get_item(f"{self.project}_over_data", {}):
        #     await db_storage.set_deep_item([f"{self.project}_over_data", self.label], {})

        # 创建主按钮，并绑定点击事件
        if self.processing_type in ["file", "image", "video"]:
            text_color = "text-orange-7"
        elif self.processing_type == "test":
            text_color = "text-deep-purple-7"
        else:
            text_color = "text-blue-7"
        ui.button(f"{self.title}：").props("flat").classes(
            f"p-1 text-[14px]/[14px] {text_color} mt-2 font-semibold"
        ).on("click", self._handle_main_button_click, ["shiftKey"])

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

    def play_overview_video(self, url_path):
        self.overview_video_dialog.clear()
        with self.overview_video_dialog:
            with ui.card().classes(
                "w-auto max-w-screen-xl min-w-[300px] bg-black p-0 items-center justify-center relative-position overflow-hidden"
            ):
                ui.video(src=url_path).classes("w-full max-h-[85vh]").props("controls autoplay")
                ui.button(icon="close", on_click=self.overview_video_dialog.close).props(
                    "flat round color=white"
                ).classes("absolute top-2 right-2 z-10 opacity-70 hover:opacity-100")

        self.overview_video_dialog.open()

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

    async def check_and_download_svn(self, http_url, file_name):
        """
        [已更新为异步] 检查 SVN 文件是否已在当前会话下载过。
        """
        storage_key = f"downloaded_{file_name}"
        has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

        if has_downloaded:
            # 复用同一个对话框
            self.check_down_dialog.clear()
            with self.check_down_dialog, ui.card().classes("min-w-[400px]"):
                with ui.card_section():
                    ui.label(f'文件 "{file_name}" 已在本次会话中下载。').classes("text-lg font-medium")
                    ui.separator().props("size=1px").classes("my-3")
                    ui.label("您可以：")
                    ui.html(
                        """
                        <ul class="q-pl-lg">
                            <li>在浏览器的<b>下载栏</b>中直接找到它。</li>
                            <li>按键盘快捷键 <kbd>Ctrl</kbd> + <kbd>J</kbd> (Windows/Linux) 或 <kbd>⌘</kbd> + <kbd>Shift</kbd> + <kbd>J</kbd> (Mac) 打开<b>下载内容页面</b>。</li>
                        </ul>
                        """,
                        sanitize=False,
                    ).classes("text-base")

                with ui.card_actions().props("align=right"):
                    # “重新下载”按钮，调用新的 *异步* 触发器
                    # NiceGUI 会自动 await on_click 中的协程
                    ui.button(
                        "仍要重新下载",
                        on_click=lambda url=http_url, name=file_name: self.trigger_download_svn_async(
                            url, name, self.check_down_dialog.close
                        ),
                        color="primary",
                    )
                    ui.button("关闭", on_click=self.check_down_dialog.close, color="grey")

            self.check_down_dialog.open()

        else:
            # 首次点击
            # a. 立即触发 SVN 下载 (!!! 关键: 使用 await !!!)
            await self.trigger_download_svn_async(http_url, file_name)
            # b. 通过JavaScript在客户端设置标记
            await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

    async def trigger_download_svn_async(self, http_url, file_name, on_finish=None):
        """
        [新的异步版本] 从 SVN 获取文件内容，并使用 ui.download 发送给客户端。
        """

        # 1. (!!! 关键: 使用 await 调用新的异步 http 函数 !!!)
        svn_filename_from_url, content = await self.get_svn_file_http_async(
            http_url,
            username=SVN_USERNAME,
            password=SVN_PASSWORD,
        )

        if content:
            # 2. 触发 NiceGUI 下载 (发送 bytes 内容)
            ui.download(content, file_name)
            ui.notify(
                f"已开始下载: {file_name}",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

            # 3. (可选) 如果下载成功，关闭对话框
            if on_finish:
                on_finish()  # .close() 是同步的, 直接调用即可
        else:
            # get_svn_file_http_async 内部失败时已经 ui.notify 了
            pass

    # 通过 HTTP(S) 从 SVN 仓库下载文件
    async def get_svn_file_http_async(
        self, http_url: str, username: str = "", password: str = ""
    ) -> tuple[str | None, bytes | None]:
        """
        [新的异步版本] 通过 HTTP(S) 从 SVN 仓库下载文件。
        使用 httpx 替代 requests。
        """
        auth = None
        if username and password:
            # 使用 httpx.BasicAuth
            auth = BasicAuth(username, password)

        # 1. !!! [新] 添加 SSL 上下文 (与 checker 函数相同) !!!
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            # 2. !!! [关键修改] 在客户端上同时传入 verify 和 auth !!!
            async with httpx.AsyncClient(
                follow_redirects=True,
                verify=ssl_context,  # <--- 在这里添加
                auth=auth,
            ) as client:
                # 使用 await client.get
                response = await client.get(http_url, auth=auth, timeout=10)

                # 检查请求是否成功
                response.raise_for_status()  # 如果状态码是 4xx 或 5xx，则引发异常

                filename = http_url.split("/")[-1]

                # response.content 是同步的 (在 httpx 中)
                return filename, response.content

        except httpx.HTTPStatusError as e:
            # 对应 requests.exceptions.HTTPError
            ui.notify(
                f"SVN HTTP 请求失败: {e.response.status_code} {e.response.reason_phrase}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            logger.error("SVN HTTP 错误", exc_info=True)
            return None, None
        except httpx.RequestError as e:
            # 对应 requests.exceptions.RequestException (包含连接、超时等)
            ui.notify(
                f"SVN 请求异常: {e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            logger.error("SVN 请求异常", exc_info=True)
            return None, None
        except Exception as e:
            # 捕获其他意外错误
            ui.notify(
                f"发生未知错误: {e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            logger.error("发生未知错误", exc_info=True)
            return None, None

    # 从 SVN 获取 PDF，将其存储在会话中
    async def open_svn_pdf_in_browser(self, http_url, file_name):
        """
        [已更新为异步] 从 SVN 获取 PDF，将其存储在会话中，
        并打开 /view/svn_pdf 路由在新标签页中显示它。
        """
        ui.notify(
            f"正在从 SVN 准备预览 {file_name}...",
            type="info",
            position="bottom",
            timeout=2000,
            progress=True,
            close_button="✖",
        )

        # 2. (!!! 关键: 使用 await 调用新的异步 http 函数 !!!)
        _, pdf_bytes = await self.get_svn_file_http_async(
            http_url,
            username=SVN_USERNAME,
            password=SVN_PASSWORD,
        )

        if pdf_bytes:
            # 3. !!! 关键修改：将 PDF 字节存入 PDF_PREVIEW_CACHE !!!

            # a. 获取唯一的客户端 ID
            client_id = ui.context.client.id

            # b. 将 bytes 存入缓存
            PDF_PREVIEW_CACHE[client_id] = pdf_bytes

            # 4. !!! 关键修改：在 URL 中传递 client_id !!!
            cache_buster = int(time.time())
            ui.run_javascript(f'window.open("/view/svn_pdf?id={client_id}&v={cache_buster}", "_blank");')

            ui.notify(
                f"已在新标签页中打开: {file_name}",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

        # else: get_svn_file_http_async 内部已经处理了错误通知

    # 检查给定 URL 的可访问性并获取其 MIME 文件类型
    async def get_url_file_info_async(self, url: str, timeout: int = 15) -> Tuple[bool, Optional[str]]:
        """
        [异步版] 检查给定 URL 的可访问性并获取其 MIME 文件类型。

        使用 httpx 异步客户端,
        通过发送一个流式 GET 请求 (stream=True) 来实现，
        只获取响应头而不下载文件体，从而实现高效检查。
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/58.0.3029.110 Safari/537.36"
        }

        # 【重要安全警告】
        # 禁用 SSL 验证 (verify=False) 会使连接容易受到中间人攻击。
        # 这在访问您自己完全信任的、使用自签名证书的内部服务器时
        # 是可以接受的，但绝不能用于访问公共互联网上的 API。
        #
        # httpx.create_ssl_context(verify=False) 是推荐的写法
        # 它明确创建了一个不验证证书的 SSL 上下文。
        # 您也可以直接使用 verify=False，效果相同。
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        # 2. !!! [新] 添加认证信息 !!!
        auth = None
        if SVN_USERNAME and SVN_PASSWORD:
            auth = BasicAuth(SVN_USERNAME, SVN_PASSWORD)

        try:
            # 修改点 2：follow_redirects 改为 False 进行测试
            # 为什么？如果服务器返回 302 跳转到登录页，我们希望立即捕获这个状态，而不是让代码傻傻地去下载登录页从而超时。
            async with httpx.AsyncClient(follow_redirects=False, verify=ssl_context, auth=auth) as client:
                async with client.stream("GET", url, timeout=timeout, headers=headers) as response:
                    # 处理重定向 (301, 302) - 这意味着 Basic Auth 失败了，被踢到了登录页
                    if 300 <= response.status_code < 400:
                        logger.info(f"检测到重定向至: {response.headers.get('Location')}")
                        # 可以在这里 return False 或者尝试进一步处理
                        ui.notify(
                            "认证失效，服务器要求重定向（可能是SSO或登录页）",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                        return False, None
                    # 检查 URL 是否可访问 (状态码 < 400)
                    if response.status_code < 400:
                        content_type = response.headers.get("Content-Type")
                        if content_type:
                            mime_type = content_type.split(";")[0].strip()
                            return True, mime_type
                        else:
                            # URL 存在，但服务器未指定 MIME 类型
                            return True, None
                    elif response.status_code == 401:
                        ui.notify(
                            "用户名或密码错误 (401)",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                        return False, None
                    else:
                        # URL 不存在 (404) 或禁止访问 (403)
                        ui.notify(
                            "引用文件不存在，请检查文件命名或SVN路径是否正确!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                        return False, None

        except httpx.TimeoutException:
            msg = f"网络超时：({timeout}s)，链接：{url}；服务器响应过慢或正在尝试重定向。"
            logger.error(msg, exc_info=True)
            ui.notify(
                msg,
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False, None

        except httpx.ConnectError:
            msg = f"连接错误: {url} (请检查网络或域名)"
            logger.error(msg, exc_info=True)
            ui.notify(
                msg,
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False, None

        except httpx.RequestError:
            # 捕获所有其他的 httpx 异常 (例如 URL 格式错误)
            logger.error(f"请求发生错误: {url}", exc_info=True)
            ui.notify(
                f"请求发生错误: {url}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False, None

        except Exception as e:
            # 捕获其他意外错误
            logger.error("发生未知错误", exc_info=True)
            ui.notify(
                f"发生未知错误: {e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False, None

    # 查找合法路径是否存在且唯一，并返回合法路径
    def _splicing_svn_file_url(self, chip_text) -> str:
        target_url = ""
        # 保存依赖文件夹所的概述配置项标签名
        according_title = ""
        # 保存找到的激活的依赖文件夹名
        according_folder_name = []
        # 获取当前项目的状态
        project_state = app.storage.general["project_summary"][self.project]["state"]
        # 获取当前状态下，提交到svn的主文件夹
        svn_main_folder = self.state_path.get(project_state)
        if not svn_main_folder:
            if overview_state_show_judge(self.role):
                ui.notify(
                    f"该项概述，在当前项目{project_state}状态下，无相应svn管控仓库配置，无法添加概述内容!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return target_url
        # 有依赖文件夹配置，找依赖文件夹配置标签对应的标签标题名
        if self.search_folder_according:
            break_bool = False
            # 遍历大类，比如：光学、结构、硬件
            for over_data in app.storage.general.get("over_config_data", {}).values():
                # 遍历小类，比如：光源、光学件
                for data_dic in over_data.values():
                    if break_bool:
                        break
                    for data in data_dic.values():
                        if data["label"] == self.search_folder_according:
                            # 得到概述项标签名，用于后续提示用户使用
                            according_title = data["title"]
                            break_bool = True
                            break
            # 获取文件夹依赖标签里的chip数据
            for data in db_storage.get_deep_item(
                [f"{self.project}_over_data", self.search_folder_according], {}
            ).values():
                # 将所有激活的chip对应的内容，也就是文件夹名保存起来
                if data["enabled"]:
                    according_folder_name.append(data["content"])

            # 如果少于一个有效文件夹名，即没有有效文件夹配置
            if len(according_folder_name) < 1:
                if overview_state_show_judge(self.role):
                    ui.notify(
                        f"概述项{according_title}无有效配置，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                return target_url
            # 如果超过一个有效文件夹名
            elif len(according_folder_name) > 1:
                if overview_state_show_judge(self.role):
                    ui.notify(
                        f"概述项{according_title}有效配置不唯一，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                return target_url
            # 有且仅有一个有效文件夹配置
            else:
                # 有缩小范围的正则表达式配置
                if self.search_scope_regular:
                    # 查找这个文件夹
                    match = re.search(self.search_scope_regular, according_folder_name[0])
                    if match:
                        search_target = match.group(1)
                        target_url = f"{self.upload_path}/{svn_main_folder}/{search_target}/{according_folder_name[0]}"
                    else:
                        if overview_state_show_judge(self.role):
                            ui.notify(
                                f"文件夹{according_folder_name[0]}命名不符合规则!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                        return target_url
                # 没有缩小范围的正则表达式配置
                else:
                    target_url = f"{self.upload_path}/{svn_main_folder}/{according_folder_name[0]}"

        # 无依赖文件夹配置，直接上传到config配置的顶层文件夹
        else:
            # 有正则表达式缩小范围
            if self.search_scope_regular:
                # 查找这个文件夹
                match = re.search(self.search_scope_regular, chip_text)
                if match:
                    search_target = match.group(1)
                    target_url = f"{self.upload_path}/{svn_main_folder}/{search_target}"
                else:
                    if overview_state_show_judge(self.role):
                        ui.notify(
                            f"文件{chip_text}命名不符合规则!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                    return target_url
            # 没有正则表达式缩小范围
            else:
                target_url = f"{self.upload_path}/{svn_main_folder}"

        # 需要再深入层级
        if self.search_hierarchy:
            for h in self.search_hierarchy:
                target_url = f"{target_url}/{h}"
        return f"{target_url}/{chip_text}"

    # 查找合法路径是否存在且唯一，并返回合法路径
    async def _search_file_path(self, chip_text) -> str:
        target_path = ""
        # 保存依赖文件夹所的概述配置项标签名
        according_title = ""
        # 保存找到的激活的依赖文件夹名
        according_folder_name = []
        # 有依赖文件夹配置，找依赖文件夹配置标签对应的标签标题名
        if self.search_folder_according:
            break_bool = False
            for over_data in app.storage.general.get("over_config_data", {}).values():
                for data_dic in over_data.values():
                    if break_bool:
                        break
                    for data in data_dic.values():
                        if data["label"] == self.search_folder_according:
                            according_title = data["title"]
                            break_bool = True
                            break
            # 获取文件夹依赖标签里的chip数据
            for data in db_storage.get_deep_item(
                [f"{self.project}_over_data", self.search_folder_according], {}
            ).values():
                # 将所有激活的chip对应的内容，也就是文件夹名保存起来
                if data["enabled"]:
                    according_folder_name.append(data["content"])

            # 如果少于一个有效文件夹名，即没有有效文件夹配置
            if len(according_folder_name) < 1:
                if overview_state_show_judge(self.role):
                    ui.notify(
                        f"概述项{according_title}无有效配置，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                return target_path
            # 如果超过一个有效文件夹名
            elif len(according_folder_name) > 1:
                if overview_state_show_judge(self.role):
                    ui.notify(
                        f"概述项{according_title}有效配置不唯一，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                return target_path
            # 有且仅有一个有效文件夹配置
            else:
                # 有缩小范围的正则表达式配置
                if self.search_scope_regular:
                    # 查找这个文件夹
                    match = re.search(self.search_scope_regular, according_folder_name[0])
                    if match:
                        search_target = match.group(1)
                        # search_target = according_folder_name[0].split("_")[0]
                        folder_according_li = await find_dirs_by_name_os_walk(
                            f"{self.upload_path}\\{search_target}", according_folder_name[0]
                        )
                        # 文件夹不存在
                        if not folder_according_li:
                            if overview_state_show_judge(self.role):
                                ui.notify(
                                    f"{self.upload_path}\\{search_target}\n不存在目录{according_folder_name[0]}，链接无效!",
                                    type="warning",
                                    position="bottom",
                                    timeout=3000,
                                    progress=True,
                                    close_button="✖",
                                )
                            return target_path
                    else:
                        if overview_state_show_judge(self.role):
                            ui.notify(
                                f"文件夹{according_folder_name[0]}命名不符合规则!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                        return target_path
                # 没有缩小范围的正则表达式配置
                else:
                    folder_according_li = await find_dirs_by_name_os_walk(
                        f"{self.upload_path}", according_folder_name[0]
                    )
                    # 文件夹不存在
                    if not folder_according_li:
                        if overview_state_show_judge(self.role):
                            ui.notify(
                                f"{self.upload_path}\n不存在目录{according_folder_name[0]}，链接无效!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                        return target_path

                if len(folder_according_li) > 1:
                    if overview_state_show_judge(self.role):
                        path_str = ""
                        for path in folder_according_li:
                            path_str = f"{path_str}\n{str(path)}"
                        ui.notify(
                            f"{according_title}概述项配置的文件夹存在多个:{path_str}\n链接无效!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                    return target_path
                # 有且存在唯一一个依赖文件夹
                else:
                    target_path = str(folder_according_li[0])

        # 无依赖文件夹配置，直接上传到config配置的顶层文件夹
        else:
            # 有正则表达式缩小范围
            if self.search_scope_regular:
                # 查找这个文件夹
                match = re.search(self.search_scope_regular, chip_text)
                if match:
                    search_target = match.group(1)
                    # search_target = according_folder_name[0].split("_")[0]
                    folder_according_li = await find_dirs_by_name_os_walk(f"{self.upload_path}", search_target)
                    # 文件夹不存在
                    if not folder_according_li:
                        if overview_state_show_judge(self.role):
                            ui.notify(
                                f"{self.upload_path}\n不存在目录{search_target}，链接无效!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                        return target_path
                    elif len(folder_according_li) > 1:
                        if overview_state_show_judge(self.role):
                            path_str = ""
                            for path in folder_according_li:
                                path_str = f"{path_str}\n{str(path)}"
                            ui.notify(
                                f"{according_title}概述项配置的文件夹存在多个:{path_str}\n链接无效!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                        return target_path
                    else:
                        target_path = str(folder_according_li[0])
                else:
                    if overview_state_show_judge(self.role):
                        ui.notify(
                            f"文件{chip_text}命名不符合规则!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                    return target_path
            # 没有正则表达式缩小范围
            else:
                target_path = self.upload_path

        # 需要再深入层级
        if self.search_hierarchy:
            for h in self.search_hierarchy:
                target_path = f"{target_path}\\{h}"
        return target_path

    # 当用户点击“添加”按钮时，将服务器文件引用数据添加到共享存储中
    async def _add_search_chip_data(self, ui_spinner):
        # 开始显示漏斗
        ui_spinner.set_visibility(True)
        text = self.chip_label.value.strip()
        notes = self.chip_notes.value.strip()
        target_path = await self._search_file_path(text)
        # 有目标路径，且存在，且是文件夹路径
        if target_path and Path(target_path).is_dir():
            if not text:
                ui.notify(
                    "引用文件名不能为空!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
            elif not notes:
                ui.notify(
                    "注释不能为空!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
            elif text in [
                d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
            ]:
                ui.notify(
                    "引用文件名已添加过。",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
            else:
                files_li = find_files_pathlib(target_path, text)
                if not files_li:
                    ui.notify(
                        f"引用文件不存在该路径下：\n{target_path}",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                elif len(files_li) > 1:
                    ui.notify(
                        f"引用文件在该路径下：\n{target_path}\n存在多个同名文件（子文件夹里存在）",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
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
                        "icon": "saved_search",
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
                    # 隐藏漏斗
                    ui_spinner.set_visibility(False)
                    self.chip_dialog.close()
                    ui.notify(
                        "文件引用已添加。",
                        type="positive",
                        position="bottom",
                        timeout=1000,
                        progress=True,
                        close_button="✖",
                    )
        # 有目标路径，但路径不存在或不是文件夹路径
        elif target_path:
            if overview_state_show_judge(self.role):
                ui.notify(
                    f"路径：\n{target_path}\n不存在!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
        # 隐藏漏斗
        ui_spinner.set_visibility(False)
        # 显示相关芯片选择对话框
        self._show_related_chip_select_dialog(text, True, "add_chip")

    # 当用户点击“添加”按钮时，将SVN文件引用数据添加到共享存储中
    async def _add_svn_chip_data(self, ui_spinner):
        # 开始显示漏斗
        ui_spinner.set_visibility(True)
        text = self.chip_label.value.strip()
        notes = self.chip_notes.value.strip()
        project_state = app.storage.general["project_summary"][self.project]["state"]
        warehouse = self.state_path[project_state]
        file_info = (False, None)

        if not text:
            ui.notify(
                "引用文件名不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        elif not notes:
            ui.notify(
                "注释不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        # chip内容和项目状态信息都一样情况下
        elif (text, warehouse) in [
            (d["content"], d["warehouse"])
            for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
        ]:
            ui.notify(
                f"{warehouse}仓库下的相同引用文件名已添加过。",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        else:
            target_url = self._splicing_svn_file_url(text)
            if target_url:
                file_info = await self.get_url_file_info_async(target_url)
                # 文件不存在，上面函数调用已提示，这里隐藏沙漏即可
                if not file_info[0]:
                    ui_spinner.set_visibility(False)
                    return
            else:
                # 拼接不成路径的异常情况已在_splicing_svn_file_url函数里有弹出提示框
                ui_spinner.set_visibility(False)
                return

            # 准备要存储的 chip 数据
            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")
            # url_path = f"{FILES_URL_DIR}/{text}"
            file_type = file_info[1]
            if (file_type == "application/octet-stream" or file_type is None) and target_url.lower().endswith(".pdf"):
                file_type = "application/pdf"
            chip_data = {
                "id": chip_id,  # 使用UUID确保每个chip都有一个唯一的ID
                "role": self.role,
                "icon": "saved_search",
                "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                # "removable": False,  # 控制元素是否有删除按钮
                "bg_color": "bg-light-blue-1",
                "type": "svn",
                "file_type": file_type,  # 获取文件的MIME文件类型与编码方式
                "url_path": target_url,
                "content": text,
                "warehouse": warehouse,
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
            # 隐藏漏斗
            ui_spinner.set_visibility(False)
            self.chip_dialog.close()
            ui.notify(
                "文件引用已添加。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

        # 隐藏漏斗
        ui_spinner.set_visibility(False)
        # 显示相关芯片选择对话框
        self._show_related_chip_select_dialog(text, True, "add_chip")

    # 当用户点击“添加”按钮时，将文本数据添加到共享存储中
    async def _add_text_chip_data(self, ui_spinner):
        text = self.chip_label.value.strip()
        notes = self.chip_notes.value.strip()
        if not text:
            ui.notify(
                "概述内容不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        elif not notes:
            ui.notify(
                "注释不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
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
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        else:
            # 显示漏斗
            ui_spinner.set_visibility(True)
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
            # 隐藏漏斗
            ui_spinner.set_visibility(False)
            self.chip_dialog.close()
            ui.notify(
                "内容已添加。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            self._show_related_chip_select_dialog(text, True, "add_chip")

    # 处理文件/图片上传事件
    async def _handle_file_upload(self, e):
        original_filename = e.file.name
        file_ext = os.path.splitext(original_filename)[1].lower()
        file_type = e.file.content_type  # 图片类返回image/xxx，文件类返回application/xxx，文本类型text/xxx

        if self.processing_type == "file" and file_ext not in OVER_UPLOADS_FILE_TYPE:
            ui.notify(
                f'文件 "{original_filename}" 不是规定的：{", ".join(OVER_UPLOADS_FILE_TYPE)} 文件类型，无法上传!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return
        elif self.processing_type == "image" and "image" not in file_type:
            ui.notify(
                f'文件 "{original_filename}" 不是图片类型，无法上传!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return
        # 增加视频类型校验
        elif self.processing_type == "video" and "video" not in file_type:
            ui.notify(
                f'文件 "{original_filename}" 不是视频类型，无法上传!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return
        # 生成一个唯一的内部文件名以避免覆盖，但保留原始文件名用于显示
        # unique_filename = f"{uuid.uuid4().hex}{Path(original_filename).suffix}"
        # filepath = self.upload_path / unique_filename
        filepath = f"{self.upload_path}/{original_filename}"
        url_path = f"{FILES_URL_DIR}/{original_filename}"
        # 检查是否已存在该项里了
        if original_filename in [
            d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
        ]:
            ui.notify(
                f'文件 "{original_filename}" 无需重复提交!',
                type="warning",
                position="bottom",
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
                logger.error("上传处理失败", exc_info=True)  # 在服务器端打印错误详情
                ui.notify(
                    f"上传文件 '{original_filename}' 失败: {str(ex)}",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return
            file_icon = "image"
            # 文件类型的icon与图片的设置不一样
            if self.processing_type == "file":
                # 文件类型才将icon设置为引用小图，图片类不设置
                file_icon = "attachment"
            elif self.processing_type == "video":
                file_icon = "play_circle"
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
                "content": original_filename,
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
            # 显示相关芯片选择对话框
            self._show_related_chip_select_dialog(original_filename, True, "add_chip")

    # 显示服务器已有文件
    async def _show_have_file(self, original_filename, file_type, url_path):
        # 准备要存储的 chip 数据
        file_icon = ""
        if self.processing_type == "file":
            # 文件类型才将icon设置为引用小图，图片类不设置
            file_icon = "attachment"
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
            "content": original_filename,
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
        text = self.chip_label.value.strip()
        notes = self.chip_notes.value.strip()
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
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        elif not notes:
            ui.notify(
                "注释不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        elif other_bool:
            ui.notify(
                "特殊要求不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
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
                timeout=3000,
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
        # 显示相关芯片选择对话框
        self._show_related_chip_select_dialog(text, True, "add_chip")

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
    async def _refresh_chip_container(self) -> None:
        # 获取该项目最高版本
        req_max_ver = app.storage.general["project_req_max_ver"][self.project]
        # 删除元素重新显示
        self.chip_container.clear()
        with self.chip_container:
            search_bool = False
            target_path = ""
            for chip_info in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values():
                # 如果打开显示所有记录的开关，失活chip不显示，跳过
                if not app.storage.client.get("record_switch") and chip_info.get("enabled") is False:
                    continue
                if self.processing_type == "search":
                    # 只找一次概述项配置的文件路径在不在，不管找的结果
                    if not search_bool:
                        target_path = await self._search_file_path(chip_info["content"])
                    search_bool = True
                    # target_path 可能是空、有效文件夹路径，长得像文件夹的文件路径，调用函数会针对情况处理
                    await self._create_chip_from_data(chip_info, target_path, req_max_ver)
                else:
                    await self._create_chip_from_data(chip_info, "", req_max_ver)

    # 遍历传入的整个概述资料，找到svn类型chip，如果其最高版本激活状态不是False，则将其设置成False
    def set_overview_data_svn_block(self, over_data):
        for label, label_dic in over_data.items():
            for id, chip_dic in label_dic.items():
                # 只处理svn类型
                if chip_dic.get("type") == "svn":
                    req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                    select_activ_state = chip_dic.get("select_activ_dic", {}).get(req_max_ver)
                    # 最高激活状态不是False
                    if select_activ_state or select_activ_state is None:
                        over_data[label][id]["select_activ_dic"][req_max_ver] = False
                        over_data[label][id]["icon"] = "block"
                        over_data[label][id]["enabled"] = False
                        over_data[label][id]["bg_color"] = "bg-grey-5"
        return over_data

    # 同步UI显示与共享存储中的数据
    async def _update_chip_display(self):
        """
        同步UI显示与共享存储中的数据。
        这是由定时器调用的核心同步函数。
        """
        # 在用户打开了特定弹窗的情况下，不刷对应条目下的缩略图元素
        if not (self.chip_dialog.value or self.check_down_dialog.value or self.activ_dialog.value):
            # 获取当前UI上所有 chip 的ID
            displayed_chip_feature = {
                (child.props.get("data-chip-id"), child.props.get("enabled-state")) for child in self.chip_container
            }

            # 获取共享存储中所有 chip 的ID
            # 用户打开开关，想看全部记录情况下
            if app.storage.client.get("record_switch"):
                # 如果研发转产标记激活，则比较数据库与界面已显示chip数量异同时，
                # 排除掉svn类且失活的chip，使得研发切换为转产时，所有人会刷新一下
                if app.storage.general["conversion_refresh"].get(self.project):
                    # 抽取所有非svn类型的chip，及 svn类但激活或None的chip
                    stored_chip_feature = set(
                        [
                            (id, str(chip_dic["enabled"]))
                            for id, chip_dic in db_storage.get_deep_item(
                                [f"{self.project}_over_data", self.label], {}
                            ).items()
                            if chip_dic.get("type") != "svn" or chip_dic.get("enabled") in [True, None]
                        ]
                    )
                else:
                    # 所有chip均显示
                    stored_chip_feature = set(
                        [
                            (id, str(chip_dic["enabled"]))
                            for id, chip_dic in db_storage.get_deep_item(
                                [f"{self.project}_over_data", self.label], {}
                            ).items()
                        ]
                    )
            # 关闭开关，只看激活chip记录情况下
            else:
                stored_chip_feature = set(
                    [
                        (id, str(chip_dic["enabled"]))
                        for id, chip_dic in db_storage.get_deep_item(
                            [f"{self.project}_over_data", self.label], {}
                        ).items()
                        if chip_dic.get("enabled") in [True, None]
                    ]
                )

            # 只有当UI和存储中的ID集合不一致时，才重新渲染，以提高效率
            # print(self.title, displayed_chip_feature, stored_chip_feature)
            if displayed_chip_feature != stored_chip_feature:
                # 刷新chip容器内容
                await self._refresh_chip_container()
                # 刷新角色负责用户数据
                overview_role_update(self.project)

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

        # 启动异步任务
        ui.timer(0.2, lambda: open_pdf(), once=True)

    def trigger_download(self, filepath, file_name, on_complete=None):
        """专门负责触发下载的辅助函数"""
        ui.notify(
            f"开始下载文件: {file_name}",
            type="info",
            position="bottom",
            timeout=2000,
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
        text = chip_data.get("content", "")
        js_code = f"navigator.clipboard.writeText('{text}');"
        ui.run_javascript(js_code)
        ui.notify(
            "内容已复制到剪贴板！",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )

    # <-----------------------------------------------------------------

    # 以当前最新版本用户设置的激活状态，更新chip资料相应参数，如icon、enabled、bg_color等等
    def _check_version_updated(self, chip_id, new_select_activ_dic, chip_text) -> bool:
        # 如果激活弹窗关闭时，检测到激活多选项发生了变化，则修改该chip的编辑人
        select_activ_dic = copy.deepcopy(
            db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {})
        )
        # 激活状态发生变化，记录编辑人和编辑时间记录
        if len(new_select_activ_dic) != len(select_activ_dic):
            ui.notify(
                "需求刚刚升级了，各项概述的激活配置需要重新确定！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            self._select_set_activ_dialog(chip_id, chip_text)
            return True
        return False

    def cancel_checkbox_change(self, chip_id):
        # 移除当前用户的编辑标记
        app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"].remove(
            app.storage.user.get("current_user", "匿名用户")
        )
        # 如果没有用户在编辑该chip，删除该chip的编辑记录
        if not app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"]:
            del app.storage.general["over_change_broadcast"][self.project][chip_id]

    async def _set_related_chip_state(self, chip_text, chip_state, all_related_bool, related_select_dic, type):
        overview_data = copy.deepcopy(db_storage.get_item(f"{self.project}_over_data", {}))
        # 遍历该项目概述内容，字典键为概述的各分类项，值为该项下chip字典
        for related_label, chip_dic in overview_data.items():
            if related_label in related_select_dic and (related_select_dic[related_label] or all_related_bool):
                # 遍历各个chip数据
                for related_chip_id, chip_data in chip_dic.items():
                    # 将chip数据里的选项激活设置字典的键，也就是版本整理成列表
                    over_chip_ver_li = [int(float(k)) for k in chip_data.get("select_activ_dic", {}).keys()]
                    # 获取选项激活设置里最大的版本值
                    max_over_ver = max(over_chip_ver_li)
                    # 如果chip状态为True，将该chip数据的最高版本激活状态设置为None
                    if related_label == "tec_type":
                        pass
                    if chip_data["select_activ_dic"][f"{max_over_ver}.0"]:
                        chip_data["select_activ_dic"][f"{max_over_ver}.0"] = None
                        chip_data["enabled"] = None
                        chip_data["icon"] = "question_mark"
                        chip_data["bg_color"] = "bg-amber-5"
                    if chip_data["select_activ_dic"][f"{max_over_ver}.0"] is not False:
                        # 如果已经有处于打开状态的记录，则在记录里追加操作记录
                        if db_storage.get_deep_item(
                            [f"{self.project}_over_related_record", related_label, related_chip_id, "open"]
                        ):
                            # 获取已有的打开记录字典
                            open_dic = copy.deepcopy(
                                db_storage.get_deep_item(
                                    [f"{self.project}_over_related_record", related_label, related_chip_id, "open"], {}
                                )
                            )
                            # 在记录字典里追加本次操作记录
                            open_dic["record"].update(
                                {
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                        "operate_user": app.storage.user.get("current_user", "匿名用户"),
                                        "operate_type": type,
                                        "operate_chip_content": chip_text,
                                        "operate_chip_state": chip_state,
                                    }
                                },
                            )
                            # 将更新后的记录字典写回数据库
                            await db_storage.set_deep_item(
                                [f"{self.project}_over_related_record", related_label, related_chip_id, "open"],
                                open_dic,
                            )
                        # 如果没有打开记录，则创建新的记录
                        else:
                            # 受影响chip的负责角色
                            related_role = (
                                app.storage.general.get("over_config_data_flat", {})
                                .get(related_label, {})
                                .get("role", "匿名用户")
                            )
                            await db_storage.set_deep_item(
                                [f"{self.project}_over_related_record", related_label, related_chip_id, "open"],
                                {
                                    # 受影响chip打开记录的时间
                                    "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    # 记录受影响chip的负责人
                                    "open_related_user": app.storage.general.get("overview_role", {})
                                    .get(self.project, {})
                                    .get(related_role, {})
                                    .get("latest_user", "匿名用户"),
                                    "close_time": "",
                                    "close_related_user": "",
                                    "record": {
                                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                            "operate_user": app.storage.user.get("current_user", "匿名用户"),
                                            "operate_type": type,
                                            "operate_chip_content": chip_text,
                                            "operate_chip_state": chip_state,
                                        }
                                    },
                                },
                            )

        if overview_data:
            await db_storage.set_item(f"{self.project}_over_data", overview_data)
        # 更新数据后，chip的刷新机制检测到数据变化会自动刷新UI显示

    def _show_related_chip_select_dialog(self, chip_text, chip_state, type):
        self.activ_dialog.clear()
        with self.activ_dialog, ui.card().classes("w-full").style("max-width: 800px;"):
            ui.label("选择本次操作可能影响的其它概述项：").classes("text-lg font-bold")
            ui.label("选中的概述项，其内部所有激活的内容将变为待确认状态，相关人员会收到提醒。").classes(
                "text-base text-brown font-bold -mt-4"
            )

            with ui.grid(columns=3).classes("w-full gap-0"):
                related_select_dic = {}
                for related_label in self.impact_list:
                    related_select_dic.update({related_label: False})
                    select_box = ui.checkbox(
                        text=app.storage.general["over_config_data_flat"]
                        .get(related_label, {})
                        .get("title", "未知标题"),
                    )
                    select_box.bind_value(related_select_dic, related_label)

            with ui.row().classes("w-full justify-end items-center"):
                ui.button(
                    "勾选的受影响",
                    color="green",
                    on_click=lambda: self._set_related_chip_state(
                        chip_text, chip_state, False, related_select_dic, type
                    ),
                ).on("click", lambda: self.activ_dialog.close())
                ui.button(
                    "全部受影响",
                    color="blue",
                    on_click=lambda: self._set_related_chip_state(
                        chip_text, chip_state, True, related_select_dic, type
                    ),
                ).on("click", lambda: self.activ_dialog.close())

        self.activ_dialog.open()

    async def handle_checkbox_change(self, ui_spinner, chip_id, chip_text):
        new_select_activ_dic = copy.deepcopy(
            app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"]
        )
        # 检查版本是否更新导致的激活长度变化，弹窗提醒用户重新确认
        is_version_updated = self._check_version_updated(chip_id, new_select_activ_dic, chip_text)
        # 如果版本更新，弹窗已打开选择激活范围，则直接返回不做后续处理
        if is_version_updated:
            return
        try:
            # 立即显示 Spinner
            ui_spinner.set_visibility(True)

            # 备份旧的激活状态字典,用于对比
            OLD_CHIP_SELECT_DIC = copy.deepcopy(
                db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {})
            )

            # 只有当激活状态字典发生变化时，才进行后续处理
            if new_select_activ_dic != OLD_CHIP_SELECT_DIC:
                # 执行异步函数 并等待它完成
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"],
                    new_select_activ_dic,
                )
                # 获取该项目最高版本
                req_max_ver = f"{str(max([int(float(v)) for v in new_select_activ_dic.keys()]))}.0"
                chip_state = db_storage.get_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", req_max_ver]
                )
                # 如果最高版本激活状态为True
                if chip_state:
                    await self._update_chip_active_parameter(chip_id, chip_text)
                # 防止chip状态None（null）被当成False，当用户在弹窗选择激活状态时不做选择动作，保持原有null状态chip被处理成False显示效果
                elif chip_state is None:
                    # 该情况意味着用户没有修改当前chip最新版本的null状态，看了一下而已
                    # 只要跳过这个情况不做任何修改即可
                    pass
                    # 冗余设计，复用注意检查与整体刷新处设置是否一致
                    # 修改这里要检查utils和information两个模块是否跟着改
                    # app.storage.general["overview_data"][self.project][self.label][chip_id]["enabled"] = None
                    # app.storage.general["overview_data"][self.project][self.label][chip_id]["icon"] = "question_mark"
                    # app.storage.general["overview_data"][self.project][self.label][chip_id]["bg_color"] = "bg-amber-5"
                else:
                    await self._update_chip_block_parameter(chip_id)

                # 记录最后修改人和修改时间
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
                        "select_activ_dic": new_select_activ_dic,
                    },
                )

                try:
                    # 移除当前用户的编辑标记
                    app.storage.general["over_change_broadcast"][self.project].get(chip_id, {}).get(
                        "editor", []
                    ).remove(app.storage.user.get("current_user", "匿名用户"))
                except ValueError:
                    pass  # 找不到就什么都不做
                # 如果没有用户在编辑该chip，删除该chip的编辑记录
                if not app.storage.general["over_change_broadcast"][self.project].get(chip_id, {}).get("editor", []):
                    app.storage.general["over_change_broadcast"][self.project].pop(chip_id, None)  # 如果无此key则不报错
                # 无论成功还是失败，都隐藏 Spinner
                ui_spinner.set_visibility(False)

                open_dic = copy.deepcopy(
                    db_storage.get_deep_item([f"{self.project}_over_related_record", self.label, chip_id, "open"], {})
                )
                # 如果有打开的记录，则将其关闭，记录关闭时间和关闭人
                if open_dic:
                    # 更新关闭时间和关闭人
                    open_dic["close_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    open_dic["close_related_user"] = creator
                    # 删除旧的打开记录，改为以关闭时间为键存储
                    await db_storage.del_deep_item([f"{self.project}_over_related_record", self.label, chip_id, "open"])
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_related_record", self.label, chip_id, open_dic["close_time"]], open_dic
                    )

                # 显示相关芯片选择对话框
                self._show_related_chip_select_dialog(chip_text, chip_state, "activ_change")

                # 刷新chip容器内容
                await self._refresh_chip_container()
                # 刷新概述负责人
                overview_role_update(self.project)

                # 检查版本是否更新导致的激活长度变化，弹窗提醒用户重新确认
                self._check_version_updated(chip_id, new_select_activ_dic, chip_text)

        except Exception as ex:
            # (可选) 处理错误
            logger.error("数据库更新失败", exc_info=True)
            ui.notify(
                f"错误: {ex}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 创建用于让用户选择chip激活范围的弹窗
    def _select_set_activ_dialog(self, chip_id, chip_text=""):
        self.activ_dialog.clear()
        with self.activ_dialog, ui.card().classes("w-1/2"):
            ui.label("选择概述生效的需求版本").classes("text-lg font-bold")
            # 获取当前chip激活状态字典
            select_activ_dic = copy.deepcopy(
                db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {})
            )
            # 在全局存储中为该chip创建共享实时编辑数据，用以关联多选项框的状态
            app.storage.general["over_change_broadcast"].setdefault(self.project, {})
            app.storage.general["over_change_broadcast"][self.project].setdefault(chip_id, {})

            # 如果该chip已经存在编辑记录，且选项数量没变，则继续使用该用户的编辑状态，只是追加当前用户到编辑列表中
            if app.storage.general["over_change_broadcast"][self.project][chip_id] and len(
                app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"]
            ) == len(select_activ_dic):
                editor_list = app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"]
                # 如果当前用户不在编辑列表里，则追加
                editor_list.append(app.storage.user.get("current_user", "匿名用户"))
                # 去重后写回编辑列表
                app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"] = list(set(editor_list))

            # 如果该chip没有编辑记录，或选项数量变了，则重新创建该chip的编辑记录，初始化当前用户为唯一编辑者
            # 其它用户不存在清单里，后面提交时拦截提示
            else:
                app.storage.general["over_change_broadcast"][self.project][chip_id] = {
                    "editor": [app.storage.user.get("current_user", "匿名用户")],
                    "select_activ_dic": copy.deepcopy(
                        db_storage.get_deep_item(
                            [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {}
                        )
                    ),
                }

            ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
            ui_spinner.set_visibility(False)
            with ui.grid(columns=6).classes("w-full gap-0"):
                max_ver = f"{str(max([int(float(v)) for v in app.storage.general['over_change_broadcast'][self.project][chip_id]['select_activ_dic'].keys()]))}.0"
                for select_label, val in app.storage.general["over_change_broadcast"][self.project][chip_id][
                    "select_activ_dic"
                ].items():
                    select_box = ui.checkbox(
                        text=select_label,
                        value=val,
                    )
                    select_box.bind_value(
                        app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"],
                        select_label,
                    )
                    # 如果不是最高版本，则禁用该选项，防止用户修改旧版本激活状态
                    # if select_label != max_ver:
                    #     select_box.disable()
            open_dic = copy.deepcopy(
                db_storage.get_deep_item([f"{self.project}_over_related_record", self.label, chip_id, "open"], {})
            )
            # 显示本次状态变化由哪些操作引起
            if open_dic:
                ui.label("本次状态变化由以下概述调整引起：").classes("text-base font-bold text-brown")
                for time_key, record in open_dic.get("record", {}).items():
                    if record.get("operate_type", "") == "add_chip":
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"添加了『{record.get("operate_chip_content", "未知内容")}』"'
                        )
                    elif record.get("operate_type", "") == "activ_change":
                        if record.get("operate_chip_state"):
                            state_label = "激活"
                        elif record.get("operate_chip_state") is False:
                            state_label = "失活"
                        else:
                            state_label = "待确认"
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"修改『{record.get("operate_chip_content", "未知内容")}』的状态为『{state_label}』'
                        )
                    else:
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"操作了『{record.get("operate_chip_content", "未知内容")}』，操作类型未知'
                        )
                    record_label.classes("text-sm text-brown")

            with ui.row().classes("w-full justify-end items-center") as row:
                ui_spinner.move(row, 1)
                # ui.label("注意以上改动是即时生效的").classes("text-lg font-bold")
                # 关闭时，会以重新检测到的最高版本激活状态来更新chip相关参数，且是并发综合处理结果
                # 甚至多了新的版本，但chip最终都以最高版本激活状态来正确显示
                ui.button(
                    "确定", color="green", on_click=lambda: self.handle_checkbox_change(ui_spinner, chip_id, chip_text)
                ).on("click", lambda: self.activ_dialog.close())
                ui.button("取消", on_click=lambda: self.cancel_checkbox_change(chip_id)).on(
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
                self._select_set_activ_dialog(chip_id, chip.text)

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
                self._select_set_activ_dialog(chip_id)

    def _move_data(self, old_data, chip_id, move_num):
        temp_data = {}
        old_data_keys = list(old_data.keys())
        # 当用户只看激活chip时
        if not app.storage.client.get("record_switch"):
            num = move_num
            # 计算带方向的移动单位
            step = int(move_num / abs(move_num))
            # 获取当前chip的下标
            current_index = old_data_keys.index(chip_id)
            # 计算跳过非激活chip情况下的正确移动步距
            # num迭代到0则找到等数量的想移动的激活chip，结束循环
            while num != 0 and (
                # 将下标迭代到边缘时，意味着没必要继续迭代，迭代目标超过了边缘，按照移动到边缘处理
                (step < 0 and current_index != 0) or (step > 0 and current_index != len(old_data_keys) - 1)
            ):
                current_index += step
                # chip激活这扣除移动目标步距1个单位
                if old_data[old_data_keys[current_index]].get("enabled") in [True, None]:
                    num -= step
                # 每次迭代累加一个单位
                move_num += step
            # 回拨一个单位
            move_num -= step
        new_data_keys = move_element(old_data_keys, chip_id, move_num)
        for k in new_data_keys:
            # temp_data[k] = app.storage.general["overview_data"][self.project][self.label][k]
            temp_data[k] = old_data.get(k, {})
        return temp_data

    # 将该项插入的chip里指定chip上移一个位置
    async def move_up_data(self, chip_data):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            # 处理指定深度的字典数据，由_move_data函数处理，并给_move_data函数传入chip_data["id"], -1两个参数
            await db_storage.atomic_deep_update(
                [f"{self.project}_over_data", self.label], self._move_data, chip_data["id"], -1
            )
            # 刷新chip容器内容
            await self._refresh_chip_container()

    # 将该项插入的chip里指定chip上移一个位置
    async def move_down_data(self, chip_data):
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            # 处理指定深度的字典数据，由_move_data函数处理，并给_move_data函数传入chip_data["id"], -1两个参数
            await db_storage.atomic_deep_update(
                [f"{self.project}_over_data", self.label], self._move_data, chip_data["id"], 1
            )
            # 刷新chip容器内容
            await self._refresh_chip_container()

    # 更新失活chip资料相应参数，如icon、enabled、bg_color等等
    async def _update_chip_block_parameter(self, chip_id):
        # 修改这里要检查utils和information两个模块 和 set_overview_data_svn_block函数 是否跟着改
        # 更新icon参数
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "block")
        # 更新enabled参数
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], False)
        # 更新bg_color参数
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-grey-5")

    # 更新激活chip资料相应参数，如icon、enabled、bg_color等等
    async def _update_chip_active_parameter(self, chip_id, chip_text):
        # 修改这里要检查utils和information两个模块是否跟着改
        # 更新icon参数
        if db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"]) == "file":
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "attachment")
        elif db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"]) == "search":
            target_path = await self._search_file_path(chip_text)
            if target_path:
                files_li = find_files_pathlib(target_path, chip_text)
                if len(files_li) == 1:
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_data", self.label, chip_id, "icon"], "saved_search"
                    )
                else:
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_data", self.label, chip_id, "icon"], "search_off"
                    )
            else:
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "search_off")
        elif db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"]) == "svn":
            file_info = await self.get_url_file_info_async(
                db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "url_path"])
            )
            if file_info[0]:
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "icon"], "saved_search"
                )
            else:
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "search_off")
        else:
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], None)
        # 更新enabled参数
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], True)
        # 更新bg_color参数
        await db_storage.set_deep_item(
            [f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-light-blue-1"
        )

    # 根据字典数据创建一个具体的 ui.chip 组件。
    async def _create_chip_from_data(self, chip_info: dict, target_path: str, req_max_ver: str):
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

        if chip_info.get("type") in ["text", "file", "test", "search", "svn"]:
            file_info = (False, None)
            # 根据chip类型配置文字标签内容
            filepath = ""

            # chip显示文本准备 与 连接文件服务器准备
            chip_text = chip_info.get("content", "")
            if chip_info["type"] == "file":
                # 每次生成都用更新配置的路径
                filepath = f"{self.upload_path}/{chip_text}"
                # 以后改了文件夹配置，chip不会失效
                app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            elif chip_info["type"] == "search":
                # 每次生成都用更新配置的路径
                # 判断路径是否是文件夹且存在，target_path 可能是空、有效文件夹路径，长得像文件夹的文件路径
                if target_path and Path(target_path).is_dir():
                    files_li = find_files_pathlib(target_path, chip_text)
                    if not files_li:
                        if overview_state_show_judge(self.role):
                            ui.notify(
                                f"引用文件不存在该路径下：\n{target_path}",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                    elif len(files_li) > 1:
                        if overview_state_show_judge(self.role):
                            ui.notify(
                                f"引用文件在该路径下：\n{target_path}\n存在多个同名文件（子文件夹里存在）",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                    else:
                        # 以后改了文件夹配置，chip不会失效
                        filepath = str(files_li[0])
                        app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            elif chip_info["type"] == "svn":
                target_url = chip_info.get("url_path", "")
                file_info = await self.get_url_file_info_async(target_url)
                if not file_info[0]:
                    ui.notify(
                        f"引用文件：{chip_text}，已丢失!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )

            # 创建 chip 并附加一个自定义属性 `data-chip-id` 用于后续的同步检查
            chip = (
                ui.chip(text=chip_text, removable=False, icon=chip_info.get("icon"))
                .props(f"data-chip-id={chip_info.get('id')} enabled-state={chip_info.get('enabled')} dense square")
                .classes(f"m-0 {chip_info.get('bg_color')}")
            )

            # 为文件类chip绑定点击事件
            if chip_info.get("type") == "text":
                pass
            elif chip_info.get("type") in ["file", "search"]:
                # 如果文件类型是pdf类型、且文件服务器路径非空、且存在，创建有效点击处理
                if chip_info.get("file_type") == "application/pdf" and filepath and Path(filepath).exists():
                    # 使用浏览器打开则用open_pdf_in_browser()
                    chip.on_click(lambda url_path=chip_info.get("url_path"): self.open_pdf_in_browser(url_path))
                # 如果文件类型是其它类型、且文件服务器路径非空、且存在，创建有效点击处理
                elif filepath and Path(filepath).exists():
                    chip.on_click(
                        lambda filepath=filepath, file_name=chip_text: self.check_and_download(filepath, file_name)
                    )
                # 文件服务器路径空或者不存在，创建点击警告提示栏
                else:
                    # 根据文件类型，修改文件icon为无效文件icon
                    if chip_info["type"] == "file":
                        chip.set_icon("link_off")
                    # 如果是待选择激活状态的chip，不修改其icon
                    elif chip_info["type"] == "search" and chip.icon != "question_mark":
                        chip.set_icon("search_off")
                    chip.on_click(
                        lambda: ui.notify(
                            "文件不存在服务器、路径失效、不唯一，点击不能打开或下载！",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                    )
            elif chip_info.get("type") in ["svn"]:
                # 如果文件类型是pdf类型、且文件服务器路径非空、且存在，创建有效点击处理
                if chip_info.get("file_type") == "application/pdf" and file_info[0]:
                    # 使用浏览器打开则用open_pdf_in_browser()
                    chip.on_click(
                        lambda url_path=chip_info.get("url_path"), file_name=chip_text: self.open_svn_pdf_in_browser(
                            url_path, file_name
                        )
                    )
                # 如果文件类型是其它类型、且文件服务器路径非空、且存在，创建有效点击处理
                elif file_info[0]:
                    chip.on_click(
                        lambda url_path=chip_info.get("url_path"), file_name=chip_text: self.check_and_download_svn(
                            url_path, file_name
                        )
                    )
                # 文件服务器路径空或者不存在，创建点击警告提示栏
                else:
                    # 如果是待选择激活状态的chip，不修改其icon
                    if chip.icon != "question_mark":
                        chip.set_icon("search_off")
                    chip.on_click(
                        lambda: ui.notify(
                            f"SVN文件：\n{chip_info.get('url_path')}\n已丢失，点击不能打开或下载！",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                    )
            # 新增 video 处理
            elif chip_info.get("type") == "video":
                # 确保路径存在
                if filepath and Path(filepath).exists():
                    chip.on_click(lambda url_path=chip_info.get("url_path"): self.play_overview_video(url_path))
                else:
                    chip.set_icon("videocam_off")
                    chip.on_click(
                        lambda: ui.notify(
                            f"视频文件：\n{chip_info.get('url_path')}\n已丢失，点击不能打开或下载！",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                    )
            # 创建chip元素的附属元素
            with chip:
                # 为 chip 添加 tooltip
                if chip_info.get("type") in ["svn"]:
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>仓库: {chip_info.get('warehouse', '')}<br>注释: <br>●{chip_info.get('notes', '').replace('\n', '<br>')}"
                elif chip_info.get("type") in ["test"]:
                    select_str = "测试条件状态与节点工具："
                    select_bool = False
                    for k, select_value in chip_info["test_select_data"].items():
                        if select_value:
                            select_bool = True
                            select_str = f"{select_str}<br>●{select_value}"
                    if not select_bool:
                        select_str = "测试条件状态与节点工具：<br>无"
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>{select_str}<br>注释: <br>●{chip_info.get('notes', '').replace('\n', '<br>')}"
                else:
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>●{chip_info.get('notes', '').replace('\n', '<br>')}"

                with ui.tooltip():
                    ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                # 创建功能按钮
                # 创建chip删除/设置按钮
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
                # --- 新增历史按钮 ---
                history_button = (
                    ui.button(on_click=lambda d=chip_info: self.show_chip_history(d))
                    .classes("absolute -top-1 -right-1 m-0 p-0 q-py-0 bg-purple-1 text-purple-8")
                    .props('round padding="0px 0px" icon="history"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
            # 设置chip元素是否显示
            # chip.set_value(chip_info["value"])
            # 设置chip元素是否可点击，会导致其上的好标签出不来
            # chip.set_enabled(chip_info["enabled"])

            # 辅助函数：JS 处理器，只有当 shiftKey 按下时才显示
            # js_show_if_shift = "(e) => { if (e.shiftKey) { $el.style.display = 'block'; } }"
            # 辅助函数：普通的显示
            # js_show = "() => { $el.style.display = 'block'; }"
            # 辅助函数：隐藏
            # js_hide = "() => { $el.style.display = 'none'; }"
            # 为 chip 绑定事件
            # 注意：NiceGUI 的 ui_show/ui_hide 是 Python 端控制，网络延迟可能导致闪烁。
            # 这里对于 Shift+Hover 建议使用 JS 控制或者 Python 端检查 e.args['shiftKey']。
            # 下面演示 Python 端控制方法：
            def check_shift_and_show(e, btn):
                if e.args.get("shiftKey"):
                    btn.style("display: block;")
                else:
                    btn.style("display: none;")

            # --- 辅助函数：检查 Ctrl 键状态并控制显示 ---
            def check_ctrl_and_show(e, btns):
                # 如果按下了 Ctrl 键，显示按钮；否则隐藏
                if e.args.get("ctrlKey"):
                    for b in btns:
                        b.style("display: block;")
                else:
                    for b in btns:
                        b.style("display: none;")

            # --- 定义需要受 Ctrl 键控制的按钮组 ---
            control_btns = [delete_button, move_up_button, move_down_button]
            # 1. 控制功能按钮 (Delete/Move) - 需要 Ctrl
            # mouseenter: 鼠标划入瞬间检查
            chip.on("mouseenter", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            # mousemove: 鼠标在元素上移动时持续检查 (为了支持先悬停，后按 Ctrl 的情况)
            chip.on("mousemove", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            # mouseleave: 鼠标离开时强制隐藏
            chip.on("mouseleave", lambda: [b.style("display: none;") for b in control_btns])

            # 绑定 History 按钮 (Shift + Hover)
            # 我们需要监听 chip 的 mouseenter，并检查 modifier
            chip.on("mouseenter", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # 当鼠标在 chip 上移动时（防止用户先 hover 再按 shift），也需要检查
            chip.on("mousemove", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # 离开时隐藏
            chip.on("mouseleave", lambda: history_button.style("display: none;"))

            # 为chip绑定按钮点击事件与鼠标事件
            chip.on("contextmenu", lambda chip_data=chip_info: self.on_right_click(chip_data))
            # chip.on("mouseenter", lambda b=delete_button: ui_show(b)).on(
            #     "mouseleave", lambda b=delete_button: ui_hide(b)
            # )
            # chip.on("mouseenter", lambda b=move_up_button: ui_show(b)).on(
            #     "mouseleave", lambda b=move_up_button: ui_hide(b)
            # )
            # chip.on("mouseenter", lambda b=move_down_button: ui_show(b)).on(
            #     "mouseleave", lambda b=move_down_button: ui_hide(b)
            # )

        # chip类型为缩略图
        elif chip_info.get("type") == "image":
            image_name = chip_info.get("content")

            # 每次生成都用更新配置的路径
            image_path = f"{self.upload_path}/{image_name}"

            url_path = f"{FILES_URL_DIR}/{image_name}"
            # 以后改了文件夹配置，chip不会失效
            app.add_static_file(local_file=image_path, url_path=url_path)
            # 根据文件类型创建缩略图
            thumbnail = (
                ui.interactive_image(url_path)
                .props(f"data-chip-id={chip_info.get('id')} enabled-state={chip_info.get('enabled')}")
                .classes("h-10 cursor-pointer relative-position")
            )
            thumbnail.on("click", lambda url_path=url_path: self.show_fullscreen(url_path))

            # 创建缩略图的附属元素
            with thumbnail:
                if chip_info.get("icon"):
                    ui.icon(chip_info.get("icon", "image")).props("flat fab color=red").classes(
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
                # --- Feature 3: 图片的历史按钮 ---
                history_button = (
                    ui.button(on_click=lambda d=chip_info: self.show_chip_history(d))
                    .classes("absolute -top-1 -right-1 m-0 p-0 q-py-0 bg-purple-1 text-purple-8")
                    .props('round padding="0px 0px" icon="history"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

            # 绑定事件
            def check_shift_and_show(e, btn):
                if e.args.get("shiftKey"):
                    btn.style("display: block;")
                else:
                    btn.style("display: none;")

            # --- 辅助函数 (可以直接复用上面的逻辑，或者重新定义) ---
            def check_ctrl_and_show(e, btns):
                if e.args.get("ctrlKey"):
                    for b in btns:
                        b.style("display: block;")
                else:
                    for b in btns:
                        b.style("display: none;")

            # --- 定义按钮组 ---
            control_btns = [delete_button, move_up_button, move_down_button]
            # 1. 控制功能按钮 (Delete/Move) - 需要 Ctrl
            thumbnail.on("mouseover", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            # 注意：InteractiveImage 组件可能不支持 mousemove 绑定，如果不支持，可以尝试仅用 mouseover
            # 但为了体验最佳，通常尽量加上 mousemove。如果报错，请删除下面这行 mousemove
            thumbnail.on("mousemove", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            thumbnail.on("mouseout", lambda: [b.style("display: none;") for b in control_btns])
            # 历史按钮逻辑
            thumbnail.on("mouseover", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            thumbnail.on("mousemove", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            thumbnail.on("mouseout", lambda: history_button.style("display: none;"))
            # 为缩略图绑定各种事件
            # thumbnail.on("mouseover", lambda b=delete_button: ui_show(b)).on(
            #     "mouseout", lambda b=delete_button: ui_hide(b)
            # )
            # thumbnail.on("mouseover", lambda b=move_up_button: ui_show(b)).on(
            #     "mouseout", lambda b=move_up_button: ui_hide(b)
            # )
            # thumbnail.on("mouseover", lambda b=move_down_button: ui_show(b)).on(
            #     "mouseout", lambda b=move_down_button: ui_hide(b)
            # )

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
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda: self._add_text_chip_data(ui_spinner))
        self.chip_dialog.open()

    # 创建用于搜寻服务器文件类型chip的概述内容与注释的对话框
    def _setup_search_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加新的文件引用概述内容").classes("text-lg font-bold")
            ui.label(f"搜寻根目录：{self.upload_path}").classes("text-xs text-brown-7")
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
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda: self._add_search_chip_data(ui_spinner))
        self.chip_dialog.open()

    # 创建用于SVN文件类型chip的概述内容与注释的对话框
    def _setup_svn_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加SVN文件引用概述内容").classes("text-lg font-bold")
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
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda: self._add_svn_chip_data(ui_spinner))
        self.chip_dialog.open()

    # 触发文件上传界面，用于给用户选择文件，然后自动触发文件处理函数
    def _get_file_upload(self):
        if not self.chip_notes.value:
            ui.notify(
                "注释不能为空!",
                type="warning",
                position="bottom",
                timeout=3000,
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
            ui.label(f"保存根目录：{self.upload_path}").classes("text-xs text-brown-7")
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

            test_select_data = {
                "state_select": "",
                "state_other_text": "",
                "node_select": "",
                "node_other_text": "",
                "instrument_select": "",
                "instrument_other_text": "",
            }

            self.chip_label = (
                ui.textarea(
                    label="检测内容与标准",
                    placeholder=self.dialog_placeholder,
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
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return False
        else:
            ui.notify(
                "当前用户无该项编辑权限，请联系管理员申请!",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return False

    # --- 显示标签历史记录 ---
    def show_label_history(self):
        self.history_dialog.clear()

        # 1. 获取数据
        raw_data = db_storage.get_deep_item([f"{self.project}_over_data", self.label], {})
        history_list = []

        for chip_id, chip_info in raw_data.items():
            # 获取创建时间 (timestamp 字典中最早的时间)
            timestamps = chip_info.get("timestamp", {})
            creation_time = min(timestamps.keys()) if timestamps else "N/A"

            history_list.append(
                {
                    "content": chip_info.get("content", "N/A"),
                    "req_ver": chip_info.get("req_ver", "0.0"),
                    "creation_time": creation_time,
                    "creator": chip_info.get("creator", "未知"),
                    "notes": chip_info.get("notes", ""),
                    "enabled": chip_info.get("enabled", True),
                    "type": chip_info.get("type", ""),
                }
            )

        # 2. 排序：先按版本号(float)排序，版本相同按时间排序
        try:
            history_list.sort(key=lambda x: (float(x["req_ver"]), x["creation_time"]))
        except ValueError:
            # 防止版本号无法转float的情况
            history_list.sort(key=lambda x: (x["req_ver"], x["creation_time"]))

        # 3. 构建 UI
        with self.history_dialog, ui.card().classes("w-[800px] max-w-full h-[80vh]"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"历史记录: {self.title}").classes("text-xl font-bold text-gray-800")
                ui.button(icon="close", on_click=self.history_dialog.close).props("flat round dense")
            ui.label("文字颜色效果代表当前激活状态").classes("text-sm text-gray-500 mt-0 mb-1")
            ui.separator()

            with ui.scroll_area().classes("w-full flex-grow"):
                if not history_list:
                    ui.label("暂无记录").classes("w-full text-center text-gray-500 mt-4")

                current_ver = None
                for item in history_list:
                    # 版本分组标题
                    if item["req_ver"] != current_ver:
                        current_ver = item["req_ver"]
                        ui.label(f"需求版本V{current_ver}生效后提交的概述：").classes(
                            "text-base font-bold text-amber-900 mt-3 mb-1 bg-amber-50 px-2 py-1 rounded"
                        )

                    # 条目卡片
                    with ui.row().classes(
                        "w-full items-start p-2 border-b border-gray-100 hover:bg-gray-50 transition-colors"
                    ):
                        # 左侧：时间和创建人
                        with ui.column().classes("w-1/5 min-w-[120px] gap-0"):
                            ui.label(item["creation_time"]).classes("text-xs text-gray-500")
                            ui.label(item["creator"]).classes("text-xs font-bold text-blue-600")

                        # 中间：内容
                        with ui.column().classes("flex-grow gap-1"):
                            # 内容显示，如果是文件或图片显示图标
                            with ui.row().classes("items-center gap-1"):
                                if item["type"] in ["file", "image", "svn", "search"]:
                                    ui.icon("attachment", size="xs", color="grey")
                                if item["enabled"]:
                                    color = "text-blue-400"
                                elif item["enabled"] == "null":
                                    color = "text-orange-400 italic"
                                else:
                                    color = "text-gray-400 line-through"
                                ui.label(item["content"]).classes(f"text-sm font-medium {color}")
                            if item["notes"]:
                                ui.label(f"注: {item['notes']}").classes("text-xs text-gray-500 italic")

        self.history_dialog.open()

    # --- 显示 Chip 激活变更历史 ---
    def show_chip_history(self, chip_data):
        self.history_dialog.clear()

        # 1. 获取时间戳记录
        timestamp_data = chip_data.get("timestamp", {})
        # 按时间倒序排列 (最新的在上面)
        sorted_times = sorted(timestamp_data.keys(), reverse=True)

        chip_content = chip_data.get("content", "未知内容")

        with self.history_dialog, ui.card().classes("w-[600px] max-w-full -space-y-2"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"变更历史: {chip_content}").classes("text-lg font-bold")
                ui.button(icon="close", on_click=self.history_dialog.close).props("flat round dense")

            ui.separator().classes("mb-1")

            with ui.column().classes("w-full gap-1"):
                if not sorted_times:
                    ui.label("暂无变更记录").classes("text-gray-500")

                for time_str in sorted_times:
                    record = timestamp_data[time_str]
                    creator = record.get("creator", "未知")
                    activ_dic = record.get("select_activ_dic", {})

                    with ui.card().classes("w-full p-2 bg-gray-50 border border-gray-200 -space-y-2"):
                        with ui.row().classes("w-full justify-between items-center mb-1"):
                            with ui.row().classes("gap-2 items-center"):
                                ui.icon("history", size="xs", color="blue")
                                ui.label(time_str).classes("text-sm font-mono text-gray-700")
                            ui.badge(creator, color="blue-grey").props("outline")

                        # ui.separator().classes("mb-1")

                        # 显示该时刻的激活状态快照
                        if activ_dic:
                            with ui.row().classes("w-full flex-wrap gap-1"):
                                sorted_vers = sorted(
                                    activ_dic.keys(), key=lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0
                                )
                                for ver in sorted_vers:
                                    is_active = activ_dic[ver]
                                    if is_active:
                                        color = "green"
                                        text_col = "white"
                                    elif is_active == "null":
                                        color = "orange"
                                        text_col = "white"
                                    else:
                                        color = "grey-4"
                                        text_col = "grey-7"

                                    ui.chip(text=f"V{ver}", color=color, text_color=text_col).props(
                                        "dense square size=sm"
                                    )

        self.history_dialog.open()

    # 处理主按钮的点击事件
    def _handle_main_button_click(self, e: GenericEventArguments):
        if app.storage.general["project_summary"][self.project]["state"] not in ["研发", "转产", "量产"]:
            ui.notify(
                "项目当前状态禁止添加概述!",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return
        # 检查是否按下了 Shift 键
        if e.args.get("shiftKey"):
            self.show_label_history()
            return
        # 如果用户具有编辑权限
        if self._edit_permission_judge():
            # 根据处理类型，设置不同的交互逻辑
            if self.processing_type == "text":
                # 设置文本chip的弹窗格式
                self._setup_text_chip_dialog()
            elif self.processing_type == "test":
                # 设置测试项类chip的弹窗格式
                self._setup_test_chip_dialog()
            elif self.processing_type == "search":
                # 设置服务器文件搜寻类chip的弹窗格式
                self._setup_search_chip_dialog()
            elif self.processing_type == "svn":
                # 设置服务器文件搜寻类chip的弹窗格式
                self._setup_svn_chip_dialog()
            else:
                # 设置文件类chip的弹窗格式
                self._setup_file_notes_dialog()


class ConfigValidator:
    def __init__(self, json_path):
        self.raw_data = self.load_json(json_path)
        self.data = self.raw_data.get("data", {}) if "data" in self.raw_data else self.raw_data

    def load_json(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"无法加载JSON文件: {e}")
            return {}

    # -------------------------------------------------------------------------
    # 新增：静态语法与拼写检查 (核心改进部分)
    # -------------------------------------------------------------------------
    def validate_syntax(self, current_node_id, condition_str):
        """
        静态分析 condition 字符串
        特点：
        1. any/all: 必须是列表格式，如 ['A', 'B']
        2. ==/!= : 支持 Python 格式 (123, True) 也支持无引号字符串 (代工)
        """
        if not condition_str or condition_str == "无条件":
            return True, []

        errors = []
        # 1. 切分顶层逻辑
        parts = re.split(r"\s+(?:and|or)\s+", condition_str)

        # 正则提取: ID, 操作符, 值
        pattern = re.compile(r"^\s*(\d+)\s*(==|!=|any|all)\s*(.+)$")

        for part in parts:
            part = part.strip()
            if not part:
                continue

            if part.startswith("not "):
                part = part[4:].strip()

            match = pattern.match(part)
            if not match:
                errors.append(f"表达式格式错误: '{part}'")
                continue

            ref_id, operator, val_str = match.groups()
            val_str = val_str.strip()

            # --- A. 检查引用ID是否存在 ---
            if ref_id not in self.data:
                errors.append(f"引用了不存在的节点 ID: {ref_id}")
                continue

            # --- B. 智能解析值 (核心修改) ---
            target_val = None

            try:
                # 尝试按 Python 标准语法解析 (数字、布尔、列表、带引号的字符串)
                target_val = ast.literal_eval(val_str)
            except (ValueError, SyntaxError):
                # 解析失败了 (例如遇到了 "代工" 这种无引号字符串)

                if operator in ["==", "!="]:
                    # 【豁免逻辑】：如果是等于/不等于，解析失败则视为“原生字符串”
                    # 这样 "代工" 就会被当做字符串 "代工" 处理
                    target_val = val_str
                else:
                    # 【严格逻辑】：如果是 any/all，解析失败通常是因为漏了括号或引号
                    # 例如: any['结构' (漏右括号) 或 any[结构] (漏内部引号)
                    errors.append(f"列表语法错误 '{val_str}': 请确保使用方括号 [] 且内部元素加引号")
                    continue

            # --- C. 检查类型匹配 ---
            if operator in ["any", "all"]:
                if not isinstance(target_val, (list, tuple)):
                    errors.append(f"操作符 '{operator}' 要求列表格式 [...]，但检测到: {val_str}")
                    continue

            # --- D. 检查拼写/选项是否存在 ---
            ref_node_options = self.data[ref_id].get("options", [])

            if ref_node_options:
                valid_outs = {opt["option_out"] for opt in ref_node_options if "option_out" in opt}

                # 统一转成列表处理
                values_to_check = []
                if isinstance(target_val, (list, tuple)):
                    values_to_check = target_val
                else:
                    values_to_check = [target_val]

                for v in values_to_check:
                    # 这里的比对需要转字符串，因为 JSON 里可能是 "12" 而值是 12
                    # 同时也要处理布尔值
                    v_str = str(v)
                    valid_outs_str = [str(vo) for vo in valid_outs]

                    if v_str not in valid_outs_str:
                        # 特殊放行：有些逻辑可能用 True/False 代表有无，即使 option_out 里没写
                        if v_str in ["True", "False"]:
                            continue

                        errors.append(f"无效选项值: '{v}' 不在节点 {ref_id} 的定义中")
                        preview = list(valid_outs)[:3]
                        errors.append(f"    (节点 {ref_id} 合法值示例: {preview}...)")

        return len(errors) == 0, errors

    # -------------------------------------------------------------------------
    # 之前的核心逻辑函数 (逻辑运算)
    # -------------------------------------------------------------------------
    def logic_out(self, k, cond_logic_str, mock_data_snapshot):
        """
        验证器专用逻辑运算函数
        Args:
            k: 当前节点ID (仅用于日志)
            cond_logic_str: 条件字符串 (e.g. "1==True and 2any['A']")
            mock_data_snapshot: 模拟的运行时数据快照
        """
        # 初始化默认返回值
        logic_out_bool = False

        # 设定多条件逻辑分隔字符串列表
        logic_delimiters = ["and", "or"]
        # 设定条件逻辑分隔字符串列表
        cond_delimiters = ["any", "all", "==", "!="]

        # 如果无条件，默认为 True
        if not cond_logic_str or cond_logic_str == "无条件":
            return True

        # 构造正则表达式
        logic_pattern = "|".join(f"({re.escape(delimiter)})" for delimiter in logic_delimiters)
        cond_pattern = "|".join(map(re.escape, cond_delimiters))

        # 1. 拆分顶层逻辑 (and/or)
        logic_result = re.split(logic_pattern, cond_logic_str)
        # 过滤空字符串
        logic_result = [s for s in logic_result if s]

        # 分离条件表达式和逻辑连接符
        elements = [s for s in logic_result if s.strip() not in logic_delimiters]
        separators = [s for s in logic_result if s.strip() in logic_delimiters]

        bool_list = []

        # 2. 遍历每个单项条件进行计算
        for p in elements:
            if not p.strip():
                continue

            # 拆分 ID、操作符、值
            cond_result = re.split(cond_pattern, p)

            # 提取依赖项 ID (cond_result[0] 可能是 "not 4" 或 "4")
            match = re.search(r"\d+", cond_result[0])
            if not match:
                # 语法错误已在 validate_syntax 处理，这里默认 False 防止崩溃
                bool_list.append(False)
                continue

            current_cond_id = match.group()

            # --- 数据获取 ---
            # 检查模拟数据中是否存在该依赖项
            if current_cond_id not in mock_data_snapshot:
                # 依赖项不存在（可能是被过滤掉或ID错误），视为条件不满足
                return False

            # 获取模拟的用户填写数据
            user_out = mock_data_snapshot[current_cond_id].get("user_must_out", {})
            # 获取依赖项的静态配置（用于判断类型）
            # 注意：优先从 mock 取类型（因为 generate_permutations 塞进去了），没有则去 self.data 取
            answer_type = mock_data_snapshot[current_cond_id].get("answer_type") or self.data[current_cond_id].get(
                "answer_type", ""
            )

            # --- 数据提取 (核心修改：直接提取 option_out) ---
            op_user_out_list = []

            # 情况 A: 多选 (结构: {"Red": True, "Blue": False}) -> 提取 ["Red"]
            if "多选" in answer_type:
                op_user_out_list = [str(key) for key, val in user_out.items() if val]

            # 情况 B: 单选/下拉单选 (结构: {"value": "Red"}) -> 提取 ["Red"]
            elif answer_type in ["单选", "下拉单选"]:
                val = user_out.get("value")
                if val is not None:
                    op_user_out_list = [str(val)]
                else:
                    # 单选没填值，视为空列表
                    pass

            # 情况 C: 输入类 (结构: {"1": "100", "2": "200"}) -> 提取 ["100", "200"]
            else:
                op_user_out_list = [str(v) for v in user_out.values()]

            # --- 逻辑比对 ---
            try:
                # 解析条件值 (e.g. "['A', 'B']" -> list, "True" -> True/str)
                raw_val = cond_result[1].strip()
                try:
                    target_val = ast.literal_eval(raw_val)
                except (ValueError, SyntaxError):
                    # 解析失败通常意味着它是纯字符串 (如: 代工)
                    target_val = raw_val

                # 1. ANY 逻辑 (列表交集)
                if "any" in p:
                    # 容错：确保 target_val 是列表
                    c_val = target_val if isinstance(target_val, (list, tuple)) else [target_val]
                    # 转字符串比较，防止类型不匹配
                    c_val_str = [str(i) for i in c_val]

                    res = any(item in c_val_str for item in op_user_out_list)
                    bool_list.append(not res if "not" in p else res)

                # 2. ALL 逻辑 (子集)
                elif "all" in p:
                    c_val = target_val if isinstance(target_val, (list, tuple)) else [target_val]
                    c_val_str = [str(i) for i in c_val]

                    op_user_set = set(op_user_out_list)
                    cond_set = set(c_val_str)

                    res = op_user_set.issubset(cond_set)
                    bool_list.append(not res if "not" in p else res)

                # 3. == (相等)
                elif "==" in p:
                    # 取用户填写的第一个值进行比较
                    user_val = op_user_out_list[0] if op_user_out_list else "None"
                    bool_list.append(str(user_val) == str(target_val))

                # 4. != (不等)
                elif "!=" in p:
                    user_val = op_user_out_list[0] if op_user_out_list else "None"
                    bool_list.append(str(user_val) != str(target_val))

                else:
                    logger.warning(f"未知操作符 in expression: {p}")
                    bool_list.append(False)

            except Exception:
                # 逻辑计算出错时，为了不中断验证流程，记为 False 并记录
                # logger.debug(f"逻辑计算异常: {e}")
                bool_list.append(False)

        # 4. 拼接最终结果并执行 (True and False or True...)
        result_str = "".join(f"{str(x)} {y} " for x, y in itertools.zip_longest(bool_list, separators, fillvalue=""))

        try:
            # 使用 eval 计算布尔表达式
            logic_out_bool = eval(result_str)
        except Exception:
            return False

        return logic_out_bool
        # 复制逻辑函数结束

    def get_dependent_ids(self, condition_str):
        if not condition_str or condition_str == "无条件":
            return []
        pattern = r"(\d+)\s*(?:==|!=|any|all)"
        return list(set(re.findall(pattern, condition_str)))

    def generate_permutations(self, dependent_ids):
        # ... (保持上一版代码一致) ...
        possibilities = {}
        for nid in dependent_ids:
            if nid not in self.data:
                continue
            node_config = self.data[nid]
            options = node_config.get("options", [])
            answer_type = node_config.get("answer_type", "")
            node_states = []
            # 情况 0: 没有任何选项 (纯文本输入类)，模拟有值和无值
            if not options:
                node_states.append({})  # 空
                node_states.append({"1": "100"})  # 模拟填了一个值
            else:
                # --- 【核心修改】：提取 option_out 而不是 option_content ---
                # 转字符串以防万一
                out_list = [str(opt["option_out"]) for opt in options if "option_out" in opt]

                # 情况 A: 单选/下拉单选 -> 结构 {"value": "XXX"}
                if answer_type in ["单选", "下拉单选"]:
                    node_states.append({"value": None})  # 未选状态
                    for out_val in out_list:
                        node_states.append({"value": out_val})

                # 情况 B: 多选 -> 结构 {"XXX": True}
                elif "多选" in answer_type:
                    node_states.append({})  # 全不选
                    # 单个选中
                    for out_val in out_list:
                        node_states.append({out_val: True})
                    # 全选 (测试 all 逻辑)
                    all_selected = {out_val: True for out_val in out_list}
                    if all_selected:
                        node_states.append(all_selected)

                # 情况 C: 其他情况兜底
                else:
                    node_states.append({})

            possibilities[nid] = node_states

        keys = list(possibilities.keys())
        value_lists = [possibilities[k] for k in keys]
        combinations = []
        for combo in itertools.product(*value_lists):
            mock_snapshot = {}
            for i, nid in enumerate(keys):
                mock_snapshot[nid] = {"user_must_out": combo[i], "options": self.data[nid].get("options", [])}
            combinations.append(mock_snapshot)
        return combinations

    # -------------------------------------------------------------------------
    # 主验证流程 (逻辑升级)
    # -------------------------------------------------------------------------
    def run_validation(self):
        logger.info(f"{'=' * 30} 开始详细验证 {'=' * 30}")
        syntax_error_count = 0
        logic_crash_count = 0

        for node_id, node_data in self.data.items():
            condition = node_data.get("condition", "无条件")
            if condition == "无条件":
                continue

            # --- 步骤 1: 静态语法检查 (新增) ---
            # 这步专门用来抓漏括号、漏冒号、拼写错误
            is_valid, syntax_msgs = self.validate_syntax(node_id, condition)
            if not is_valid:
                logger.info(f"❌ [语法/拼写错误] 节点 {node_id}")
                logger.info(f"   Condition: {condition}")
                for msg in syntax_msgs:
                    logger.info(f"   -> {msg}")
                logger.info("-" * 20)
                syntax_error_count += 1
                # 如果语法都错了，后面的逻辑模拟肯定会挂，直接跳过该节点
                continue

            # --- 步骤 2: 逻辑崩溃模拟 (原有逻辑) ---
            dep_ids = self.get_dependent_ids(condition)
            missing_ids = [did for did in dep_ids if did not in self.data]
            if missing_ids:
                continue  # 已经在validate_syntax报过了

            mock_scenarios = self.generate_permutations(dep_ids)
            for mock_data in mock_scenarios:
                try:
                    result = self.logic_out(node_id, condition, mock_data)
                    if not isinstance(result, bool):
                        logger.info(f"❌ [逻辑错误] 节点 {node_id}: 返回非布尔值")
                        logic_crash_count += 1
                        break
                except Exception:
                    logger.error(f"❌ [运行时崩溃] 节点 {node_id}; Condition: {condition}", exc_info=True)
                    logic_crash_count += 1
                    break

        logger.info("\n" + "=" * 30)
        logger.info("验证结果摘要:")
        logger.info(f"1. 语法/拼写错误: {syntax_error_count} 个 (优先修复!)")
        logger.info(f"2. 逻辑/崩溃错误: {logic_crash_count} 个")

        if syntax_error_count == 0 and logic_crash_count == 0:
            logger.info("\n🎉 完美！配置文件无语法错误且逻辑稳定。")
