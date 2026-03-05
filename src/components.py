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
    IGNORE_STR,
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
    update_overview_charge_pending_dic,
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
            # --- 新增：1. 每日凌晨全局清洗待定状态，消除运行时可能累积的脏数据 ---
            try:
                from .utils import update_overview_charge_pending_dic  # 局部导入防循环依赖

                update_overview_charge_pending_dic("all")
                logger.info("每日定时任务：全局待定状态刷新完成，内存与静态数据已对齐。")
            except Exception as e:
                logger.error(f"每日定时任务：全局待定状态刷新失败，错误：{e}")

            # --- 2. 执行安全备份 (原有逻辑) ---
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
            # multi_line=True,
            close_button="✖",
        )
        ui.download(self.local_file_path)
        if on_complete:
            on_complete()

    # 我们创建一个新的、更智能的下载处理函数
    async def check_and_download(self) -> None:
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
                # multi_line=True,
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
    基于单个标签属性的交互式按钮与多功能 Chip 容器管理组件。
    高度整合文本、文件、图片、视频、SVN 及搜寻项的创建及状态共享。
    """

    def __init__(
        self,
        project: str,
        role: str,
        title: str,
        label: str,
        processing_type: str,
        permission: dict,
        nature: str = "必填",
        allowed_state: list = ["研发", "转产"],
        impact_list: list = [],
        upload_path: str = SUBMIT_FILES_DIR,
        state_path: dict = {},
        search_scope_regular: str = "",
        content_regular: list = [],
        search_folder_according_li: list = [],
        search_hierarchy: list = [],
        dialog_label: str = "按规定格式输入",
        dialog_placeholder: str = "",
        test_nature_options: list = [],
        state_options: list = [],
        node_options: list = [],
        instrument_options: list = [],
        temp_bool: bool = False,
    ):
        if processing_type not in ["text", "file", "image", "test", "search", "svn", "video"]:
            raise ValueError("processing_type 必须是 'text','file','image','test','search','svn','video'")

        self.role = role
        self.title = title
        self.label = label
        self.project = project
        self.processing_type = processing_type
        self.nature = nature
        self.allowed_state = allowed_state
        self.impact_list = impact_list
        self.upload_path = upload_path
        self.state_path = state_path
        self.search_scope_regular = search_scope_regular
        self.content_regular = content_regular
        self.search_folder_according_li = search_folder_according_li
        self.search_hierarchy = search_hierarchy
        self.dialog_placeholder = dialog_placeholder
        self.dialog_label = dialog_label
        self.permission = permission
        self.test_nature_options = test_nature_options
        self.state_options = state_options
        self.node_options = node_options
        self.instrument_options = instrument_options
        self.temp_bool = temp_bool

        self.offset = (0, 0)
        self.is_dragging = False
        self.last_pos = (0, 0)
        self.image_x = 0.0
        self.image_y = 0.0
        self.zoom_level = 1.0

        # 通用复用弹窗
        self.chip_dialog = ui.dialog().classes("")
        self.img_dialog = ui.dialog().props("").classes("p-0")
        self.overview_video_dialog = ui.dialog().classes("p-0 bg-transparent shadow-none")
        self.check_down_dialog = ui.dialog().classes("")
        self.activ_dialog = ui.dialog().props("persistent").classes("")
        self.history_dialog = ui.dialog().classes("w-full")

        # 轻量级同步哈希值
        self.last_state_hash = None

        if self.processing_type == "file":
            btn_icon = "file_present"
        elif self.processing_type == "image":
            btn_icon = "image"
        elif self.processing_type == "video":
            btn_icon = "video_camera_back"
        elif self.processing_type == "test":
            btn_icon = "gpp_good"
        else:
            btn_icon = "text_fields"

        # 主交互按钮
        btn = (
            ui.button(self.title, icon=btn_icon).props("flat").classes("p-1 text-[14px]/[14px] mt-2 font-bold relative")
        )
        btn.on("click", self._handle_main_button_click, ["ctrlKey"])

        with btn:
            if self.nature == "必填":
                self.btn_label = ui.label("●").classes("absolute top-0 left-0 text-[10px] text-red")
            elif self.nature == "需填":
                self.btn_label = ui.label("○").classes("absolute top-0 left-0 text-[10px] text-red")
            else:
                self.btn_label = ui.label("").classes("absolute top-0 left-0 text-[10px] text-red")

        # 芯片主容器
        self.chip_container = ui.row().classes("w-full items-center gap-2 pl-8")

        if self.processing_type in ["file", "image", "video"]:
            self.uploader = ui.upload(
                on_upload=self._handle_file_upload,
                on_begin_upload=lambda: self.spinner.set_visibility(True) if hasattr(self, "spinner") else None,
                auto_upload=True,
                max_files=1,
            )
            self.uploader.set_visibility(False)

        # 设置定时器，监控并更新数据
        ui.timer(1.0, self._update_chip_display)

    # ==========================================================
    # 1. 核心状态同步与 UI 刷新逻辑
    # ==========================================================

    def _generate_signature(self, filtered_dict: dict) -> int:
        """生成数据源状态的轻量级签名，避免高频深度遍历比对"""
        signature = []
        for chip_id, chip in filtered_dict.items():
            timestamps = chip.get("timestamp", {})
            latest_time = max(timestamps.keys()) if timestamps else ""
            signature.append((chip_id, chip.get("enabled"), latest_time))
        return hash(tuple(signature))

    async def _update_chip_display(self):
        """核心定时同步函数，对比签名后决定是否刷新"""
        if (
            self.chip_dialog.value
            or self.check_down_dialog.value
            or self.activ_dialog.value
            or self.img_dialog.value
            or self.overview_video_dialog.value
            or self.history_dialog.value
        ):
            return

        chips_dict = db_storage.get_deep_item([f"{self.project}_over_data", self.label], {})
        show_all = app.storage.client.get("record_switch")
        conversion_refresh = app.storage.general.get("conversion_refresh", {}).get(self.project)

        filtered_dict = {}
        for k, v in chips_dict.items():
            if conversion_refresh and v.get("type") == "svn" and v.get("enabled") not in [True, None]:
                continue
            if not show_all and v.get("enabled") is False:
                continue
            filtered_dict[k] = v

        current_hash = self._generate_signature(filtered_dict)

        if self.last_state_hash != current_hash:
            self.last_state_hash = current_hash
            await self._refresh_chip_container()
            overview_role_update(self.project, self.role)
            self._update_local_pending()

    async def _refresh_chip_container(self) -> None:
        """物理重绘整个芯片容器"""
        req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
        self.chip_container.clear()

        with self.chip_container:
            search_bool = False
            target_path_list = []
            label_chip_dic = db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()

            latest_user_str = (
                app.storage.general.get("overview_role", {})
                .get(self.project, {})
                .get(self.role, {})
                .get("latest_user", "")
            )
            des_user = latest_user_str.split("：")[1] if latest_user_str else ""
            # 指示灯颜色状态
            chip_enabled_state_list = [chip_info["enabled"] for chip_info in label_chip_dic]
            if chip_enabled_state_list and any(state is None for state in chip_enabled_state_list):
                self.btn_label.classes("text-orange", remove="text-green text-red")
            elif des_user == "不需要" or chip_enabled_state_list and any(chip_enabled_state_list):
                self.btn_label.classes("text-green", remove="text-red text-orange")
            else:
                self.btn_label.classes("text-red", remove="text-green text-orange")

            for chip_info in label_chip_dic:
                if not app.storage.client.get("record_switch") and chip_info.get("enabled") is False:
                    continue

                if self.processing_type == "search":
                    if not search_bool:
                        target_path_list = await self._search_file_path(chip_info["content"])
                    search_bool = True
                    await self._create_chip_from_data(chip_info, target_path_list, req_max_ver)
                else:
                    await self._create_chip_from_data(chip_info, [], req_max_ver)

    async def _create_chip_from_data(self, chip_info: dict, target_path_list: list, req_max_ver: str):
        """从字典数据中渲染独立的 ui.chip 组件或图片缩略图"""
        chip_text = chip_info.get("content", "")
        filepath = ""
        delete_icon = "close" if app.storage.user["current_user"] == "admin" else "settings"

        if app.storage.user["current_user"] == "admin":
            delete_bg = "bg-red text-white"
        else:
            delete_bg = "bg-white text-light-blue"

        # 处理非图片类（含视频）
        if chip_info.get("type") in ["text", "file", "test", "search", "svn", "video"]:
            file_info = (False, None)

            if chip_info["type"] == "file":
                filepath = f"{self.upload_path}/{chip_text}"
                app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            elif chip_info["type"] == "search":
                files_li = []
                target_path_li_str = ""
                for target_path in target_path_list:
                    target_path_li_str += f"{target_path}\n"
                    if target_path and Path(target_path).is_dir():
                        files_li.extend(find_files_pathlib(target_path, chip_text))

                if not files_li:
                    ui.notify(
                        f"引用文件不存在以下所有路径：\n{target_path_li_str}请检查文件命名或相关依赖配置!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                elif len(files_li) > 1:
                    ui.notify(
                        f"引用文件在以下路径：\n{target_path_li_str}有多个同名文件，请确保唯一!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                else:
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
                        # multi_line=True,
                        close_button="✖",
                    )

            chip = (
                ui.chip(text=chip_text, removable=False, icon=chip_info.get("icon"))
                .props(f"data-chip-id={chip_info.get('id')} enabled-state={chip_info.get('enabled')} dense square")
                .classes(f"m-0 {chip_info.get('bg_color')}")
            )

            # 点击事件绑定
            if chip_info.get("type") in ["file", "search"]:
                if chip_info.get("file_type") == "application/pdf" and filepath and Path(filepath).exists():
                    chip.on_click(lambda url=chip_info.get("url_path"): self.open_pdf_in_browser(url))
                elif filepath and Path(filepath).exists():
                    chip.on_click(lambda fp=filepath, fn=chip_text: self.check_and_download(fp, fn))
                else:
                    if chip_info["type"] == "file":
                        chip.set_icon("link_off")
                    elif chip_info["type"] == "search" and chip.icon != "question_mark":
                        chip.set_icon("search_off")
                    chip.on_click(
                        lambda: ui.notify(
                            "文件不存在服务器、路径失效、不唯一，点击不能打开或下载！",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            # multi_line=True,
                            close_button="✖",
                        )
                    )
            elif chip_info.get("type") == "svn":
                if chip_info.get("file_type") == "application/pdf" and file_info[0]:
                    chip.on_click(
                        lambda url=chip_info.get("url_path"), fn=chip_text: self.open_svn_pdf_in_browser(url, fn)
                    )
                elif file_info[0]:
                    chip.on_click(
                        lambda url=chip_info.get("url_path"), fn=chip_text: self.check_and_download_svn(url, fn)
                    )
                else:
                    if chip.icon != "question_mark":
                        chip.set_icon("search_off")
                    chip.on_click(
                        lambda: ui.notify(
                            f"SVN文件：\n{chip_info.get('url_path')}\n已丢失，点击不能打开或下载！",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            multi_line=True,
                            close_button="✖",
                        )
                    )
            elif chip_info.get("type") == "video":
                if filepath and Path(filepath).exists():
                    chip.on_click(lambda url=chip_info.get("url_path"): self.play_overview_video(url))
                else:
                    chip.set_icon("videocam_off")
                    chip.on_click(
                        lambda: ui.notify(
                            f"视频文件：\n{chip_info.get('url_path')}\n已丢失！",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            multi_line=True,
                            close_button="✖",
                        )
                    )

            with chip:
                # Tooltip 处理
                if chip_info.get("type") in ["svn"]:
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>仓库: {chip_info.get('warehouse', '')}<br>注释: <br>●{chip_info.get('notes', '').replace('\n', '<br>')}"
                elif chip_info.get("type") in ["test"]:
                    select_str = "测试条件状态与节点工具："
                    select_bool = False
                    for k, select_value in chip_info.get("test_select_data", {}).items():
                        if select_value:
                            select_bool = True
                            select_str += f"<br>●{select_value}"
                    if not select_bool:
                        select_str = "测试条件状态与节点工具：<br>无"
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>{select_str}<br>注释: <br>●{chip_info.get('notes', '').replace('\n', '<br>')}"
                else:
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>●{chip_info.get('notes', '').replace('\n', '<br>')}"

                with ui.tooltip():
                    ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                # 功能按钮
                delete_button = (
                    ui.button(on_click=lambda c=chip: self.delete_chip_info(c))
                    .classes(f"absolute -top-1 -right-2 m-0 p-0 q-py-0 {delete_bg}")
                    .props(f'round padding="0px 0px" icon={delete_icon}')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                move_down_button = (
                    ui.button(on_click=lambda d=chip_info: self.move_down_data(d))
                    .classes("absolute -top-1 right-2 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_down"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                move_up_button = (
                    ui.button(on_click=lambda d=chip_info: self.move_up_data(d))
                    .classes("absolute -top-1 right-6 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_up"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                history_button = (
                    ui.button(on_click=lambda d=chip_info: self.show_chip_history(d))
                    .classes("absolute -bottom-1 -right-2 m-0 p-0 q-py-0 bg-white text-purple-8")
                    .props('round padding="0px 0px" icon="history"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

            def check_ctrl_and_show(e, btns):
                if e.args.get("ctrlKey"):
                    for b in btns:
                        b.style("display: block;")
                else:
                    for b in btns:
                        b.style("display: none;")

            def check_shift_and_show(e, btn):
                btn.style("display: block;" if e.args.get("shiftKey") else "display: none;")

            control_btns = [delete_button, move_up_button, move_down_button, history_button]
            chip.on("mouseenter", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            chip.on("mousemove", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            chip.on("mouseleave", lambda: [b.style("display: none;") for b in control_btns])
            # chip.on("mouseenter", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # chip.on("mousemove", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # chip.on("mouseleave", lambda: history_button.style("display: none;"))
            chip.on("contextmenu", lambda d=chip_info: self.on_right_click(d))

        elif chip_info.get("type") == "image":
            image_name = chip_info.get("content")
            image_path = f"{self.upload_path}/{image_name}"
            url_path = f"{FILES_URL_DIR}/{image_name}"
            app.add_static_file(local_file=image_path, url_path=url_path)

            thumbnail = (
                ui.interactive_image(url_path)
                .props(f"data-chip-id={chip_info.get('id')} enabled-state={chip_info.get('enabled')}")
                .classes("h-10 cursor-pointer relative-position")
            )
            thumbnail.on("click", lambda u=url_path: self.show_fullscreen(u))

            with thumbnail:
                image_icon = chip_info.get("icon")
                if image_icon == "image":
                    ui.icon(image_icon).props("flat fab").classes("absolute top-0 left-0 text-xl text-blue-500/50")
                elif image_icon == "block":
                    ui.icon(image_icon).props("flat fab").classes("absolute top-0 left-0 text-xl text-red")
                elif image_icon == "question_mark":
                    ui.icon(image_icon).props("flat fab").classes("absolute top-0 left-0 text-xl text-amber-5")

                tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>图片名: {image_name}<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>{chip_info.get('notes', '').replace('\n', '<br>')}"
                with ui.tooltip():
                    ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                delete_button = (
                    ui.button(on_click=lambda t=thumbnail: self.clear_thumbnail(t))
                    .classes(f"absolute -top-1 -right-2 m-0 p-0 q-py-1 {delete_bg}")
                    .props(f'round padding="0px 0px" icon={delete_icon}')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                move_up_button = (
                    ui.button(on_click=lambda d=chip_info: self.move_up_data(d))
                    .classes("absolute bottom-3 -right-2 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_up"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                move_down_button = (
                    ui.button(on_click=lambda d=chip_info: self.move_down_data(d))
                    .classes("absolute -bottom-1 -right-2 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_down"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                history_button = (
                    ui.button(on_click=lambda d=chip_info: self.show_chip_history(d))
                    .classes("absolute -top-1 right-3 m-0 p-0 q-py-0 bg-white text-purple-8")
                    .props('round padding="0px 0px" icon="history"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

            def check_ctrl_and_show(e, btns):
                if e.args.get("ctrlKey"):
                    for b in btns:
                        b.style("display: block;")
                else:
                    for b in btns:
                        b.style("display: none;")

            def check_shift_and_show(e, btn):
                btn.style("display: block;" if e.args.get("shiftKey") else "display: none;")

            control_btns = [delete_button, move_up_button, move_down_button, history_button]
            thumbnail.on("mouseover", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            thumbnail.on("mousemove", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            thumbnail.on("mouseout", lambda: [b.style("display: none;") for b in control_btns])
            # thumbnail.on("mouseover", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # thumbnail.on("mousemove", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # thumbnail.on("mouseout", lambda: history_button.style("display: none;"))

    # ==========================================================
    # 2. 弹窗 UI 配置 (Dialog Setups)
    # ==========================================================
    async def _update_auto_complete_index(self, chip_label: str, content: str):
        """
        将填入的文本内容添加到传入的概述标签对应列表里，以供辅助后续填写。
        """
        index_data = db_storage.get_item("overview_auto_complete_index", {})

        if chip_label not in index_data:
            index_data[chip_label] = [content]
        elif content not in index_data[chip_label]:
            index_data[chip_label].append(content)

        await db_storage.set_item("overview_auto_complete_index", index_data)

    def _setup_text_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加新的概述内容").classes("text-lg font-bold")
            index_list = db_storage.get_deep_item(["overview_auto_complete_index", self.label], [])
            self.chip_label = (
                ui.input(
                    label=self.dialog_label,
                    value=self.dialog_placeholder,
                    autocomplete=index_list,
                    placeholder=self.dialog_placeholder,
                )
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="针对该技术概述的注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._add_text_chip_data(ui_spinner, btn=e.sender))
        self.chip_dialog.open()

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
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._add_search_chip_data(ui_spinner, btn=e.sender))
        self.chip_dialog.open()

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
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._add_svn_chip_data(ui_spinner, btn=e.sender))
        self.chip_dialog.open()

    def _setup_file_notes_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label("添加上传文件的注释").classes("text-lg font-bold")
            ui.label(f"保存根目录：{self.upload_path}").classes("text-xs text-brown-7")
            self.chip_label = (
                ui.input(
                    label="不需要提交文件时填写（选填）",
                    placeholder="无",
                )
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="针对该文件的注释（必填）",
                    placeholder="首次提交/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                self.spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                self.spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._get_file_upload(btn=e.sender))
        self.chip_dialog.open()

    def _setup_test_chip_dialog(self):
        self.chip_dialog.clear()
        with self.chip_dialog, ui.card().classes("w-full"):
            ui.label(f"添加产品的{self.title}").classes("text-lg font-bold")
            test_select_data = {
                "test_nature_select": "",
                "test_nature_other_text": "",
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
                    value=self.dialog_placeholder,
                    placeholder=self.dialog_placeholder,
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )

            def build_options(options_list, key_prefix, label_str):
                if options_list:
                    with ui.column().classes("w-full p-0 m-0"):
                        sel = (
                            ui.select(options_list, multiple=False, label=label_str)
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, f"{key_prefix}_select")
                        )
                        oth = (
                            ui.textarea(
                                label=f"{label_str}特殊要求",
                                placeholder="写明特殊要求",
                                validation={"不能空白": lambda v: v.strip() != ""},
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, f"{key_prefix}_other_text")
                        )
                        oth.set_visibility(False)
                        sel.on_value_change(lambda: self._set_other_ui(oth, sel.value))

            build_options(self.test_nature_options, "test_nature", "测试性质")
            build_options(self.state_options, "state", "条件/状态")
            build_options(self.node_options, "node", "节点/位置")
            build_options(self.instrument_options, "instrument", "工具/仪器/治具")

            self.chip_notes = (
                ui.textarea(
                    label="针对该检测内容与标准的注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button(
                    "添加", on_click=lambda e: self._add_test_chip_data(ui_spinner, test_select_data, btn=e.sender)
                )
        self.chip_dialog.open()

    def _set_other_ui(self, other_ui, select_value):
        other_ui.set_visibility(select_value == "其它")
        if select_value != "其它":
            other_ui.set_value("")

    # ==========================================================
    # 3. 数据添加与保存处理逻辑
    # ==========================================================

    async def _add_text_chip_data(self, ui_spinner, btn=None):
        if btn:
            btn.disable()  # 1. 进门立刻禁用按钮，防止连点
        try:
            text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
            # 如果填写内容有正则表达式管控，则分析内容是否符合规则
            regular_bool = False
            if self.content_regular:
                for regular in self.content_regular:
                    if re.search(regular, text):
                        regular_bool = True
            else:
                regular_bool = True
            if not regular_bool:
                ui.notify(
                    "内容不符合填写格式规范!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                return
            if not text or not notes:
                ui.notify(
                    "内容和注释不能为空!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                return
            if text in [
                d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
            ]:
                ui.notify(
                    "概述内容已存在。",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                return

            ui_spinner.set_visibility(True)
            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")

            chip_data = {
                "id": chip_id,
                "role": self.role,
                "icon": None,
                "enabled": True,
                "bg_color": "bg-light-blue-1",
                "type": "text",
                "content": text,
                "notes": notes,
                "creator": creator,
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
            }

            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
            await self._update_auto_complete_index(self.label, text)
            self.chip_label.value, self.chip_notes.value = "", ""
            ui_spinner.set_visibility(False)
            self.chip_dialog.close()
            ui.notify(
                "内容已添加。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            self._show_related_chip_select_dialog(text, True, "add_chip")
        except Exception as ex:
            # 捕捉潜在的数据库写入等异常
            logger.error(f"添加概述失败: {ex}", exc_info=True)
        finally:
            if btn:
                btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    async def _add_search_chip_data(self, ui_spinner, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(ui_spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
                # 如果填写内容有正则表达式管控，则分析内容是否符合规则
                regular_bool = False
                if self.content_regular:
                    for regular in self.content_regular:
                        if re.search(regular, text):
                            regular_bool = True
                else:
                    regular_bool = True
                if not regular_bool:
                    ui.notify(
                        "内容不符合填写格式规范!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if not text or not notes:
                    ui.notify(
                        "引用文件名和注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                # 补齐查重逻辑
                if text in [
                    d["content"]
                    for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
                ]:
                    ui.notify(
                        "引用文件名已添加过。",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                ui_spinner.set_visibility(True)
                files_li = []
                target_path_li_str = ""
                target_path_list = await self._search_file_path(text)
                for target_path in target_path_list:
                    target_path_li_str += f"{target_path}\n"
                    if target_path and Path(target_path).is_dir():
                        files_li.extend(find_files_pathlib(target_path, text))

                if not files_li:
                    ui.notify(
                        f"引用文件不存在以下所有路径：\n{target_path_li_str}请检查文件命名或相关依赖配置!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                elif len(files_li) > 1:
                    ui.notify(
                        f"引用文件在以下路径：\n{target_path_li_str}有多个同名文件，请确保唯一!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                else:
                    file_type_set = get_file_type_by_extension(str(files_li[0]))
                    chip_id = str(uuid.uuid4())
                    req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                    select_activ_dic = self._get_select_activ_dic(req_max_ver)
                    creator = app.storage.user.get("current_user", "匿名用户")
                    chip_data = {
                        "id": chip_id,
                        "role": self.role,
                        "icon": "saved_search",
                        "enabled": True,
                        "bg_color": "bg-light-blue-1",
                        "type": "search",
                        "file_type": file_type_set[0],
                        "url_path": f"{FILES_URL_DIR}/{text}",
                        "content": text,
                        "notes": notes,
                        "creator": creator,
                        "req_ver": req_max_ver,
                        "select_activ_dic": select_activ_dic,
                        "timestamp": {
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                "creator": creator,
                                "select_activ_dic": select_activ_dic,
                            }
                        },
                    }
                    await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                    self.chip_label.value, self.chip_notes.value = "", ""
                    ui_spinner.set_visibility(False)
                    self.chip_dialog.close()
                    ui.notify(
                        "文件引用已添加。",
                        type="positive",
                        position="bottom",
                        timeout=1000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    self._show_related_chip_select_dialog(text, True, "add_chip")
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态
                ui_spinner.set_visibility(False)

    async def _add_svn_chip_data(self, ui_spinner, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(ui_spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
                project_state = app.storage.general["project_summary"][self.project]["state"]
                warehouse = self.state_path.get(project_state)
                # 如果填写内容有正则表达式管控，则分析内容是否符合规则
                regular_bool = False
                if self.content_regular:
                    for regular in self.content_regular:
                        if re.search(regular, text):
                            regular_bool = True
                else:
                    regular_bool = True
                if not regular_bool:
                    ui.notify(
                        "内容不符合填写格式规范!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if not text or not notes:
                    ui.notify(
                        "引用文件名和注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                elif (text, warehouse) in [
                    (d["content"], d.get("warehouse"))
                    for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
                ]:
                    ui.notify(
                        f"{warehouse}仓库下的相同引用文件名已添加过。",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                ui_spinner.set_visibility(True)
                target_url_li = self._splicing_svn_file_url(text)
                if target_url_li and len(target_url_li) == 1:
                    target_url = target_url_li[0]
                    file_info = await self.get_url_file_info_async(target_url)
                    if not file_info[0]:
                        ui_spinner.set_visibility(False)
                        return
                elif target_url_li and len(target_url_li) > 1:
                    ui.notify(
                        "有多个路径，不合规!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    ui_spinner.set_visibility(False)
                    return
                else:
                    ui_spinner.set_visibility(False)
                    return

                chip_id = str(uuid.uuid4())
                req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")
                file_type = file_info[1]
                if (file_type == "application/octet-stream" or file_type is None) and target_url.lower().endswith(
                    ".pdf"
                ):
                    file_type = "application/pdf"

                chip_data = {
                    "id": chip_id,
                    "role": self.role,
                    "icon": "saved_search",
                    "enabled": True,
                    "bg_color": "bg-light-blue-1",
                    "type": "svn",
                    "file_type": file_type,
                    "url_path": target_url,
                    "content": text,
                    "warehouse": warehouse,
                    "notes": notes,
                    "creator": creator,
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                }
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                self.chip_label.value, self.chip_notes.value = "", ""
                ui_spinner.set_visibility(False)
                self.chip_dialog.close()
                ui.notify(
                    "文件引用已添加。",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                self._show_related_chip_select_dialog(text, True, "add_chip")
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    async def _add_test_chip_data(self, ui_spinner, test_select_data, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(ui_spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
                other_bool = False
                if test_select_data["test_nature_select"] == "其它" and not test_select_data["test_nature_other_text"]:
                    other_bool = True
                if test_select_data["state_select"] == "其它" and not test_select_data["state_other_text"]:
                    other_bool = True
                if test_select_data["node_select"] == "其它" and not test_select_data["node_other_text"]:
                    other_bool = True
                if test_select_data["instrument_select"] == "其它" and not test_select_data["instrument_other_text"]:
                    other_bool = True
                # 如果填写内容有正则表达式管控，则分析内容是否符合规则
                regular_bool = False
                if self.content_regular:
                    for regular in self.content_regular:
                        if re.search(regular, text):
                            regular_bool = True
                else:
                    regular_bool = True
                if not regular_bool:
                    ui.notify(
                        "内容不符合填写格式规范!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if (
                    not text
                    or test_select_data["test_nature_select"] is None
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
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                elif not notes:
                    ui.notify(
                        "注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                elif other_bool:
                    ui.notify(
                        "特殊要求不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                # 更严谨的组合查重判断
                existing_test_data = [
                    (d["content"], d.get("test_select_data"))
                    for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
                ]
                if (text, test_select_data) in existing_test_data:
                    ui.notify(
                        "测试项内容标准已存在。",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                ui_spinner.set_visibility(True)
                chip_id = str(uuid.uuid4())
                req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")

                chip_data = {
                    "id": chip_id,
                    "role": self.role,
                    "icon": None,
                    "enabled": True,
                    "bg_color": "bg-light-blue-1",
                    "type": "test",
                    "content": text,
                    "notes": notes,
                    "test_select_data": test_select_data,
                    "creator": creator,
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                }

                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                self.chip_notes.value = ""
                ui_spinner.set_visibility(False)
                self.chip_dialog.close()
                ui.notify(
                    "内容已添加。",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                self._show_related_chip_select_dialog(text, True, "add_chip")
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    async def _get_file_upload(self, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(self.spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                if not self.chip_notes.value:
                    ui.notify(
                        "注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                else:
                    self.uploader.reset()
                    self.uploader.run_method("pickFiles")
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    async def _handle_file_upload(self, e):
        original_filename = e.file.name
        file_ext = os.path.splitext(original_filename)[1].lower()
        file_type = e.file.content_type

        if self.processing_type == "file" and file_ext not in OVER_UPLOADS_FILE_TYPE:
            ui.notify(
                f'文件 "{original_filename}" 不是规定的：{", ".join(OVER_UPLOADS_FILE_TYPE)} 文件类型，无法上传!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
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
                # multi_line=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return
        elif self.processing_type == "video" and "video" not in file_type:
            ui.notify(
                f'文件 "{original_filename}" 不是视频类型，无法上传!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                multi_line=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
            return

        filepath = f"{self.upload_path}/{original_filename}"
        url_path = f"{FILES_URL_DIR}/{original_filename}"

        if original_filename in [
            d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
        ]:
            ui.notify(
                f'文件 "{original_filename}" 无需重复提交!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            self.spinner.set_visibility(False)
        elif os.path.exists(filepath):
            self._select_file_show(original_filename, file_type, url_path)
        else:
            try:
                file_content = await e.file.read()
                file_content_object = io.BytesIO(file_content)
                with open(filepath, "wb") as f:
                    f.write(file_content_object.read())
            except Exception as ex:
                logger.error("上传处理失败", exc_info=True)
                ui.notify(
                    f"上传文件 '{original_filename}' 失败: {str(ex)}",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    # multi_line=True,
                    close_button="✖",
                )
                self.spinner.set_visibility(False)
                return

            file_icon = "image"
            if self.processing_type == "file":
                file_icon = "attachment"
            elif self.processing_type == "video":
                file_icon = "play_circle"

            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")
            chip_data = {
                "id": chip_id,
                "role": self.role,
                "icon": file_icon,
                "enabled": True,
                "bg_color": "bg-light-blue-1",
                "type": self.processing_type,
                "file_type": file_type,
                "content": original_filename,
                "url_path": url_path,
                "notes": self.chip_notes.value,
                "creator": creator,
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
            }
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
            self.chip_notes.value = ""
            self.spinner.set_visibility(False)
            self.chip_dialog.close()
            ui.notify(
                f'文件 "{original_filename}" 上传成功!',
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            self._show_related_chip_select_dialog(original_filename, True, "add_chip")

    def _select_file_show(self, original_filename, file_type, url_path):
        self.chip_dialog.clear()
        self.chip_dialog.open()
        with self.chip_dialog, ui.card().classes("w-1/2 bg-orange-2"):
            ui.label("服务器已有同名文件，无法上传覆盖，是否使用服务器已有文件？").classes("text-lg")
            with ui.row().classes("w-full justify-end"):
                ui.button(
                    "是", on_click=lambda: self._show_have_file(original_filename, file_type, url_path), color="green-6"
                )
                ui.button("否", on_click=lambda: self.chip_dialog.close(), color="blue-grey-6")

    async def _show_have_file(self, original_filename, file_type, url_path):
        file_icon = "image"
        if self.processing_type == "file":
            file_icon = "attachment"
        elif self.processing_type == "video":
            file_icon = "play_circle"

        chip_id = str(uuid.uuid4())
        req_max_ver = app.storage.general["project_req_max_ver"][self.project]
        select_activ_dic = self._get_select_activ_dic(req_max_ver)
        creator = app.storage.user.get("current_user", "匿名用户")
        chip_data = {
            "id": chip_id,
            "role": self.role,
            "icon": file_icon,
            "enabled": True,
            "bg_color": "bg-light-blue-1",
            "type": self.processing_type,
            "file_type": file_type,
            "content": original_filename,
            "url_path": url_path,
            "notes": self.chip_notes.value,
            "creator": creator,
            "req_ver": req_max_ver,
            "select_activ_dic": select_activ_dic,
            "timestamp": {
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {"creator": creator, "select_activ_dic": select_activ_dic}
            },
        }
        self.chip_notes.value = ""
        self.chip_dialog.close()
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
        ui.notify(
            f'文件 "{original_filename}" 显示成功!',
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )

    # ==========================================================
    # 4. 状态更改与版本联控逻辑
    # ==========================================================

    def _get_select_activ_dic(self, req_max_ver):
        select_dic = {}
        for select_label in [f"{i}.0" for i in range(1, int(float(req_max_ver)) + 1)]:
            select_dic[select_label] = select_label == req_max_ver
        return select_dic

    def set_overview_data_svn_block(self, over_data):
        for label, label_dic in over_data.items():
            for id, chip_dic in label_dic.items():
                if chip_dic.get("type") == "svn":
                    req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                    select_activ_state = chip_dic.get("select_activ_dic", {}).get(req_max_ver)
                    if select_activ_state or select_activ_state is None:
                        over_data[label][id]["select_activ_dic"][req_max_ver] = False
                        over_data[label][id]["icon"] = "block"
                        over_data[label][id]["enabled"] = False
                        over_data[label][id]["bg_color"] = "bg-grey-5"
        return over_data

    def _update_local_pending(self):
        latest_user_str = (
            app.storage.general.get("overview_role", {}).get(self.project, {}).get(self.role, {}).get("latest_user", "")
        )
        des_user = latest_user_str.split("：")[1] if latest_user_str else ""
        if des_user:
            update_overview_charge_pending_dic(
                scope="local", des_user=des_user, project_name=self.project, des_label=self.label
            )

    def _check_version_updated(self, chip_id, new_select_activ_dic, chip_text) -> bool:
        select_activ_dic = copy.deepcopy(
            db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {})
        )
        if len(new_select_activ_dic) != len(select_activ_dic):
            ui.notify(
                "需求刚刚升级了，各项概述的激活配置需要重新确定！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            self._select_set_activ_dialog(chip_id, chip_text)
            return True
        return False

    def cancel_checkbox_change(self, chip_id):
        try:
            app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"].remove(
                app.storage.user.get("current_user", "匿名用户")
            )
        except ValueError:
            pass
        if not app.storage.general["over_change_broadcast"][self.project].get(chip_id, {}).get("editor", []):
            app.storage.general["over_change_broadcast"][self.project].pop(chip_id, None)

    async def _set_related_chip_state(self, chip_text, chip_state, all_related_bool, related_select_dic, type):
        overview_data = db_storage.get_item(f"{self.project}_over_data", {})
        for related_label, chip_dic in overview_data.items():
            if related_label in related_select_dic and (related_select_dic[related_label] or all_related_bool):
                for related_chip_id, chip_data in chip_dic.items():
                    over_chip_ver_li = [int(float(k)) for k in chip_data.get("select_activ_dic", {}).keys()]
                    if not over_chip_ver_li:
                        continue
                    max_over_ver = max(over_chip_ver_li)
                    if chip_data["select_activ_dic"][f"{max_over_ver}.0"]:
                        chip_data["select_activ_dic"][f"{max_over_ver}.0"] = None
                        chip_data["enabled"] = None
                        chip_data["icon"] = "question_mark"
                        chip_data["bg_color"] = "bg-amber-5"

                    if chip_data["select_activ_dic"][f"{max_over_ver}.0"] is not False:
                        open_dic = copy.deepcopy(
                            db_storage.get_deep_item(
                                [f"{self.project}_over_related_record", related_label, related_chip_id, "open"], {}
                            )
                        )
                        if open_dic:
                            open_dic["record"].update(
                                {
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                        "operate_user": app.storage.user.get("current_user", "匿名用户"),
                                        "operate_type": type,
                                        "operate_chip_content": chip_text,
                                        "operate_chip_state": chip_state,
                                    }
                                }
                            )
                            await db_storage.set_deep_item(
                                [f"{self.project}_over_related_record", related_label, related_chip_id, "open"],
                                open_dic,
                            )
                        else:
                            related_role = (
                                app.storage.general.get("over_config_data_flat", {})
                                .get(related_label, {})
                                .get("role", "匿名用户")
                            )
                            await db_storage.set_deep_item(
                                [f"{self.project}_over_related_record", related_label, related_chip_id, "open"],
                                {
                                    "open_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

    def _show_related_chip_select_dialog(self, chip_text, chip_state, type):
        self.activ_dialog.clear()
        with self.activ_dialog, ui.card().classes("w-full max-w-[800px]"):
            ui.label("选择本次操作可能影响的其它概述项：").classes("text-lg font-bold")
            ui.label("选中的概述项，其内部所有激活的内容将变为待确认状态，相关人员会收到提醒。").classes(
                "text-base text-brown font-bold -mt-4"
            )

            with ui.grid(columns=3).classes("w-full gap-0"):
                related_select_dic = {}
                for related_label in self.impact_list:
                    related_select_dic[related_label] = False
                    ui.checkbox(
                        text=app.storage.general["over_config_data_flat"].get(related_label, {}).get("title", "未知项")
                    ).bind_value(related_select_dic, related_label)

            with ui.row().classes("w-full justify-end items-center"):
                ui.button(
                    "勾选的受影响",
                    color="green",
                    on_click=lambda: self._set_related_chip_state(
                        chip_text, chip_state, False, related_select_dic, type
                    ),
                ).on("click", self.activ_dialog.close)
                ui.button(
                    "全部受影响",
                    color="blue",
                    on_click=lambda: self._set_related_chip_state(
                        chip_text, chip_state, True, related_select_dic, type
                    ),
                ).on("click", self.activ_dialog.close)

        self.activ_dialog.open()

    async def handle_checkbox_change(self, ui_spinner, chip_id, chip_text):
        new_select_activ_dic = copy.deepcopy(
            app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"]
        )
        if self._check_version_updated(chip_id, new_select_activ_dic, chip_text):
            return

        try:
            OLD_CHIP_SELECT_DIC = db_storage.get_deep_item(
                [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {}
            )
            if new_select_activ_dic != OLD_CHIP_SELECT_DIC:
                ui_spinner.set_visibility(True)
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], new_select_activ_dic
                )

                req_max_ver = f"{str(max([int(float(v)) for v in new_select_activ_dic.keys()]))}.0"
                chip_state = db_storage.get_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", req_max_ver]
                )

                if chip_state:
                    await self._update_chip_active_parameter(chip_id, chip_text)
                elif chip_state is None:
                    pass
                else:
                    await self._update_chip_block_parameter(chip_id)

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
                    {"creator": creator, "select_activ_dic": new_select_activ_dic},
                )

                self.cancel_checkbox_change(chip_id)
                ui_spinner.set_visibility(False)

                open_dic = db_storage.get_deep_item(
                    [f"{self.project}_over_related_record", self.label, chip_id, "open"], {}
                )
                if open_dic:
                    open_dic["close_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    open_dic["close_related_user"] = creator
                    await db_storage.del_deep_item([f"{self.project}_over_related_record", self.label, chip_id, "open"])
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_related_record", self.label, chip_id, open_dic["close_time"]], open_dic
                    )

                self._show_related_chip_select_dialog(chip_text, chip_state, "activ_change")
                self.last_state_hash = None  # Trigger display update via timer
                await self._update_chip_display()

        except Exception as ex:
            logger.error("数据库更新失败", exc_info=True)
            ui.notify(
                f"错误: {ex}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                # multi_line=True,
                close_button="✖",
            )

    def _select_set_activ_dialog(self, chip_id, chip_text=""):
        self.activ_dialog.clear()
        with self.activ_dialog, ui.card().classes("w-1/2"):
            ui.label("选择概述生效的需求版本").classes("text-lg font-bold")
            select_activ_dic = db_storage.get_deep_item(
                [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {}
            )

            app.storage.general["over_change_broadcast"].setdefault(self.project, {})
            app.storage.general["over_change_broadcast"][self.project].setdefault(chip_id, {})

            if app.storage.general["over_change_broadcast"][self.project][chip_id] and len(
                app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"]
            ) == len(select_activ_dic):
                editor_list = app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"]
                editor_list.append(app.storage.user.get("current_user", "匿名用户"))
                app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"] = list(set(editor_list))
            else:
                app.storage.general["over_change_broadcast"][self.project][chip_id] = {
                    "editor": [app.storage.user.get("current_user", "匿名用户")],
                    "select_activ_dic": copy.deepcopy(select_activ_dic),
                }

            ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
            ui_spinner.set_visibility(False)

            with ui.grid(columns=6).classes("w-full gap-0"):
                for select_label, val in app.storage.general["over_change_broadcast"][self.project][chip_id][
                    "select_activ_dic"
                ].items():
                    ui.checkbox(text=select_label, value=val).bind_value(
                        app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"],
                        select_label,
                    )

            open_dic = db_storage.get_deep_item(
                [f"{self.project}_over_related_record", self.label, chip_id, "open"], {}
            )

            if open_dic:
                ui.label("本次状态变化由以下概述调整引起：").classes("text-base font-bold text-brown")
                for time_key, record in open_dic.get("record", {}).items():
                    op_type = record.get("operate_type", "")
                    if op_type == "add_chip":
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"添加了『{record.get("operate_chip_content", "未知内容")}』"'
                        )
                    elif op_type == "activ_change":
                        state_label = (
                            "激活"
                            if record.get("operate_chip_state")
                            else "失活"
                            if record.get("operate_chip_state") is False
                            else "待确认"
                        )
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
                ui.button(
                    "确定", color="green", on_click=lambda: self.handle_checkbox_change(ui_spinner, chip_id, chip_text)
                ).on("click", self.activ_dialog.close)
                ui.button("取消", on_click=lambda: self.cancel_checkbox_change(chip_id)).on(
                    "click", self.activ_dialog.close
                )

        self.activ_dialog.open()

    # ==========================================================
    # 5. 编辑、移动、删除操作权限逻辑
    # ==========================================================

    def _edit_permission_judge(self):
        if (
            not self.temp_bool
            and app.storage.user["current_role"] in self.permission["edit_role"]
            and app.storage.general["project_summary"][self.project]["state"] in self.allowed_state
        ):
            return True
        elif self.temp_bool:
            ui.notify(
                "当前处于需求审核界面，概述内容锁定不可编辑!",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            return False
        elif app.storage.user["current_role"] not in self.permission["edit_role"]:
            ui.notify(
                "当前用户无该项编辑权限，请联系管理员申请!",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            return False
        else:
            ui.notify(
                "项目当前状态禁止编辑概述!",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            return False

    async def delete_chip_info(self, chip):
        if self._edit_permission_judge():
            if app.storage.user["current_user"] == "admin":
                await db_storage.del_deep_item([f"{self.project}_over_data", self.label, chip.props["data-chip-id"]])
            else:
                self._select_set_activ_dialog(chip.props["data-chip-id"], chip.text)

    async def clear_thumbnail(self, thumbnail):
        if self._edit_permission_judge():
            if app.storage.user["current_user"] == "admin":
                thumbnail.delete()
                await db_storage.del_deep_item(
                    [f"{self.project}_over_data", self.label, thumbnail.props["data-chip-id"]]
                )
            else:
                self._select_set_activ_dialog(thumbnail.props["data-chip-id"])

    def _move_data(self, old_data, chip_id, move_num):
        temp_data = {}
        old_data_keys = list(old_data.keys())
        if not app.storage.client.get("record_switch"):
            num = move_num
            step = int(move_num / abs(move_num))
            current_index = old_data_keys.index(chip_id)
            while num != 0 and (
                (step < 0 and current_index != 0) or (step > 0 and current_index != len(old_data_keys) - 1)
            ):
                current_index += step
                if old_data[old_data_keys[current_index]].get("enabled") in [True, None]:
                    num -= step
                move_num += step
            move_num -= step
        new_data_keys = move_element(old_data_keys, chip_id, move_num)
        for k in new_data_keys:
            temp_data[k] = old_data.get(k, {})
        return temp_data

    async def move_up_data(self, chip_data):
        if self._edit_permission_judge():
            await db_storage.atomic_deep_update(
                [f"{self.project}_over_data", self.label], self._move_data, chip_data["id"], -1
            )
            self.last_state_hash = None
            await self._update_chip_display()

    async def move_down_data(self, chip_data):
        if self._edit_permission_judge():
            await db_storage.atomic_deep_update(
                [f"{self.project}_over_data", self.label], self._move_data, chip_data["id"], 1
            )
            self.last_state_hash = None
            await self._update_chip_display()

    async def _update_chip_block_parameter(self, chip_id):
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "block")
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], False)
        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-grey-5")

    async def _update_chip_active_parameter(self, chip_id, chip_text):
        c_type = db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"])
        if c_type == "file":
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "attachment")
        elif c_type == "search":
            target_path_list = await self._search_file_path(chip_text)
            if target_path_list and find_files_pathlib(target_path_list[0], chip_text):
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "icon"], "saved_search"
                )
            else:
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "search_off")
        elif c_type == "svn":
            url = db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "url_path"])
            file_info = await self.get_url_file_info_async(url)
            icon_val = "saved_search" if file_info[0] else "search_off"
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], icon_val)
        else:
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], None)

        await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], True)
        await db_storage.set_deep_item(
            [f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-light-blue-1"
        )

    # ==========================================================
    # 6. 历史记录弹窗显示
    # ==========================================================

    def show_label_history(self):
        self.history_dialog.clear()
        raw_data = db_storage.get_deep_item([f"{self.project}_over_data", self.label], {})
        history_list = []

        for chip_id, chip_info in raw_data.items():
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

        try:
            history_list.sort(key=lambda x: (float(x["req_ver"]), x["creation_time"]))
        except ValueError:
            history_list.sort(key=lambda x: (x["req_ver"], x["creation_time"]))

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
                    if item["req_ver"] != current_ver:
                        current_ver = item["req_ver"]
                        ui.label(f"需求版本V{current_ver}生效后提交的概述：").classes(
                            "text-base font-bold text-amber-900 mt-3 mb-1 bg-amber-50 px-2 py-1 rounded"
                        )

                    with ui.row().classes(
                        "w-full items-start p-2 border-b border-gray-100 hover:bg-gray-50 transition-colors"
                    ):
                        with ui.column().classes("w-1/5 min-w-[120px] gap-0"):
                            ui.label(item["creation_time"]).classes("text-xs text-gray-500")
                            ui.label(item["creator"]).classes("text-xs font-bold text-blue-600")

                        with ui.column().classes("flex-grow gap-1"):
                            with ui.row().classes("items-center gap-1"):
                                if item["type"] in ["file", "image", "svn", "search", "video"]:
                                    ui.icon("attachment", size="xs", color="grey")
                                color = (
                                    "text-blue-400"
                                    if item["enabled"] is True
                                    else "text-orange-400 italic"
                                    if item["enabled"] is None or str(item["enabled"]).lower() == "null"
                                    else "text-gray-400 line-through"
                                )
                                ui.label(item["content"]).classes(f"text-sm font-medium {color}")
                            if item["notes"]:
                                ui.label(f"注: {item['notes']}").classes("text-xs text-gray-500 italic")

        self.history_dialog.open()

    def show_chip_history(self, chip_data):
        self.history_dialog.clear()
        timestamp_data = chip_data.get("timestamp", {})
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

                        if activ_dic:
                            with ui.row().classes("w-full flex-wrap gap-1"):
                                sorted_vers = sorted(
                                    activ_dic.keys(), key=lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0
                                )
                                for ver in sorted_vers:
                                    is_active = activ_dic[ver]
                                    color, text_col = (
                                        ("green", "white")
                                        if is_active
                                        else ("orange", "white")
                                        if is_active == "null"
                                        else ("grey-4", "grey-7")
                                    )
                                    ui.chip(text=f"V{ver}", color=color, text_color=text_col).props(
                                        "dense square size=sm"
                                    )

        self.history_dialog.open()

    # ==========================================================
    # 7. 路径与 SVN 寻址逻辑
    # ==========================================================

    def _splicing_svn_file_url(self, chip_text) -> list:
        return_url_li, target_url_li, according_folder_name, according_title = [], [], [], ""
        project_state = app.storage.general["project_summary"][self.project]["state"]
        svn_main_folder = self.state_path.get(project_state)

        if not svn_main_folder:
            if overview_state_show_judge(self.role):
                ui.notify(
                    f"该项概述，在当前项目{project_state}状态下，无相应svn管控仓库配置，无法添加概述内容!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
            return target_url_li

        if self.search_folder_according_li:
            for search_folder_according in self.search_folder_according_li:
                title_str = (
                    app.storage.general.get("over_config_data_flat", {})
                    .get(search_folder_according, {})
                    .get("title", "未知项")
                )
                according_title = f"{according_title}\n{title_str}"
                for data in db_storage.get_deep_item(
                    [f"{self.project}_over_data", search_folder_according], {}
                ).values():
                    if data["enabled"]:
                        according_folder_name.append(data["content"])

            if len(according_folder_name) < 1:
                if overview_state_show_judge(self.role):
                    ui.notify(
                        f"概述项：\n{according_title}\n均无有效配置，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                return target_url_li
            else:
                if self.search_scope_regular:
                    for folder_name in according_folder_name:
                        match = re.search(self.search_scope_regular, folder_name)
                        if match:
                            match_folder = f"{match.group(1)}-{match.group(2)}"
                            target_url_li.append(f"{self.upload_path}/{svn_main_folder}/{match_folder}/{folder_name}")
                        elif overview_state_show_judge(self.role):
                            ui.notify(
                                f"文件夹{folder_name}命名不符合规则!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                # multi_line=True,
                                close_button="✖",
                            )
                    if not target_url_li:
                        return target_url_li
                else:
                    for folder_name in according_folder_name:
                        target_url_li.append(f"{self.upload_path}/{svn_main_folder}/{folder_name}")
        else:
            if self.search_scope_regular:
                match = re.search(self.search_scope_regular, chip_text)
                if match:
                    match_folder = f"{match.group(1)}-{match.group(2)}"
                    target_url_li.append(f"{self.upload_path}/{svn_main_folder}/{match_folder}")
                else:
                    if overview_state_show_judge(self.role):
                        ui.notify(
                            f"文件{chip_text}命名不符合规则!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            # multi_line=True,
                            close_button="✖",
                        )
                    return target_url_li
            else:
                target_url_li.append(f"{self.upload_path}/{svn_main_folder}")

        for target_url in target_url_li:
            if self.search_hierarchy:
                for h in self.search_hierarchy:
                    target_url = f"{target_url}/{h}"
            return_url_li.append(f"{target_url}/{chip_text}")
        return return_url_li

    async def _search_file_path(self, chip_text) -> list:
        target_path_list, folder_according_li, according_folder_name_li, according_title = [], [], [], ""

        if self.search_folder_according_li:
            for search_folder_according in self.search_folder_according_li:
                title_str = (
                    app.storage.general.get("over_config_data_flat", {})
                    .get(search_folder_according, {})
                    .get("title", "未知项")
                )
                according_title = f"{according_title}\n{title_str}"
                for data in db_storage.get_deep_item(
                    [f"{self.project}_over_data", search_folder_according], {}
                ).values():
                    if data["enabled"]:
                        according_folder_name_li.append(data["content"])

            if len(according_folder_name_li) < 1:
                if overview_state_show_judge(self.role):
                    ui.notify(
                        f"概述项：\n{according_title}\n均无有效配置，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                return target_path_list
            else:
                if self.search_scope_regular:
                    for according_folder_name in according_folder_name_li:
                        match = re.search(self.search_scope_regular, according_folder_name)
                        if match:
                            folder_according_li.extend(
                                await find_dirs_by_name_os_walk(
                                    f"{self.upload_path}\\{match.group(1)}", according_folder_name
                                )
                            )
                        elif overview_state_show_judge(self.role):
                            ui.notify(
                                f"文件夹{according_folder_name}命名不符合规则!",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                # multi_line=True,
                                close_button="✖",
                            )
                else:
                    for according_folder_name in according_folder_name_li:
                        folder_according_li.extend(
                            await find_dirs_by_name_os_walk(f"{self.upload_path}", according_folder_name)
                        )
                target_path_list = folder_according_li
        else:
            if self.search_scope_regular:
                match = re.search(self.search_scope_regular, chip_text)
                if match:
                    folder_according_li = await find_dirs_by_name_os_walk(f"{self.upload_path}", match.group(1))
                    if folder_according_li:
                        target_path_list = folder_according_li
                elif overview_state_show_judge(self.role):
                    ui.notify(
                        f"文件{chip_text}命名不符合规则!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
            else:
                target_path_list = [self.upload_path]

        if self.search_hierarchy:
            target_path_list = [
                f"{target_path}\\{h}" for target_path in target_path_list for h in self.search_hierarchy
            ]
        return target_path_list

    # ==========================================================
    # 8. 网络交互与文件查看/下载辅助函数
    # ==========================================================

    def play_overview_video(self, url_path):
        self.overview_video_dialog.clear()
        with (
            self.overview_video_dialog,
            ui.card().classes(
                "w-auto max-w-screen-xl min-w-[300px] bg-black p-0 items-center justify-center relative-position overflow-hidden"
            ),
        ):
            ui.video(src=url_path).classes("w-full max-h-[85vh]").props("controls autoplay")
            ui.button(icon="close", on_click=self.overview_video_dialog.close).props("flat round color=white").classes(
                "absolute top-2 right-2 z-10 opacity-70 hover:opacity-100"
            )
        self.overview_video_dialog.open()

    def show_fullscreen(self, url_path):
        self.img_dialog.clear()
        with self.img_dialog:
            self.image_big = ui.interactive_image(url_path).classes("cursor-grab").style("overflow: hidden;")
            self.image_big.on("mousedown", self.start_drag)
            self.image_big.on_mouse(self.get_img_xy)
            self.image_big.on("mousemove", self.handle_drag)
            self.image_big.on("mouseup", self.end_drag)
            self.image_big.on("mouseleave", self.end_drag)
            self.image_big.on("wheel", self.handle_zoom)
        self.img_dialog.open()
        self.reset_transform()

    def start_drag(self, e: GenericEventArguments):
        if e.args.get("button") == 0:
            self.is_dragging = True
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.image_big.classes(replace="cursor-grabbing")
        elif e.args.get("button") == 1:
            self.reset_transform()

    def handle_drag(self, e: GenericEventArguments):
        if self.is_dragging:
            dx, dy = e.args["clientX"] - self.last_pos[0], e.args["clientY"] - self.last_pos[1]
            self.offset = (self.offset[0] + dx, self.offset[1] + dy)
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.update_transform()

    def end_drag(self, e: GenericEventArguments):
        self.is_dragging = False
        if hasattr(self, "image_big"):
            self.image_big.classes(replace="cursor-grab")

    def get_img_xy(self, e: MouseEventArguments):
        self.image_x, self.image_y = e.image_x, e.image_y

    def handle_zoom(self, e: GenericEventArguments):
        new_zoom = self.zoom_level * (1.1 if e.args["deltaY"] < 0 else 0.9)
        self.zoom_level = max(0.01, min(10, new_zoom))
        self.update_transform()

    def update_transform(self):
        if hasattr(self, "image_big"):
            self.image_big.style(
                f"transform: translate({self.offset[0]}px, {self.offset[1]}px) scale({self.zoom_level})"
            )

    def reset_transform(self):
        self.zoom_level = 1.0
        self.offset = (0, 0)
        self.update_transform()

    async def get_url_file_info_async(self, url: str, timeout: int = 15) -> Tuple[bool, Optional[str]]:
        headers = {"User-Agent": "Mozilla/5.0"}
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        auth = BasicAuth(SVN_USERNAME, SVN_PASSWORD) if SVN_USERNAME and SVN_PASSWORD else None

        try:
            async with httpx.AsyncClient(follow_redirects=False, verify=ssl_context, auth=auth) as client:
                async with client.stream("GET", url, timeout=timeout, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        return False, None
                    if response.status_code < 400:
                        ct = response.headers.get("Content-Type")
                        return True, ct.split(";")[0].strip() if ct else None
                    return False, None
        except Exception:
            return False, None

    async def get_svn_file_http_async(self, http_url: str, username: str = "", password: str = "") -> tuple:
        auth = BasicAuth(username, password) if username and password else None
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            async with httpx.AsyncClient(follow_redirects=True, verify=ssl_context, auth=auth) as client:
                response = await client.get(http_url, auth=auth, timeout=10)
                response.raise_for_status()
                return http_url.split("/")[-1], response.content
        except Exception:
            return None, None

    async def check_and_download_svn(self, http_url, file_name):
        storage_key = f"downloaded_{file_name}"
        has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

        if has_downloaded:
            self.check_down_dialog.clear()
            with self.check_down_dialog, ui.card().classes("min-w-[400px]"):
                with ui.card_section():
                    ui.label(f'文件 "{file_name}" 已在本次会话中下载。').classes("text-lg font-medium")
                with ui.card_actions().props("align=right"):
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
            await self.trigger_download_svn_async(http_url, file_name)
            await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

    async def trigger_download_svn_async(self, http_url, file_name, on_finish=None):
        _, content = await self.get_svn_file_http_async(http_url, username=SVN_USERNAME, password=SVN_PASSWORD)
        if content:
            ui.download(content, file_name)
            ui.notify(
                f"已开始下载: {file_name}",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            if on_finish:
                on_finish()

    async def open_svn_pdf_in_browser(self, http_url, file_name):
        ui.notify(
            f"正在从 SVN 准备预览 {file_name}...",
            type="info",
            position="bottom",
            timeout=2000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )
        _, pdf_bytes = await self.get_svn_file_http_async(http_url, username=SVN_USERNAME, password=SVN_PASSWORD)

        if pdf_bytes:
            client_id = ui.context.client.id
            PDF_PREVIEW_CACHE[client_id] = pdf_bytes
            cache_buster = int(time.time())
            ui.run_javascript(f'window.open("/view/svn_pdf?id={client_id}&v={cache_buster}", "_blank");')
            ui.notify(
                f"已在新标签页中打开: {file_name}",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )

    def open_pdf_in_browser(self, url_path):
        encoded_url = url_path.replace(" ", "%20")
        ui.run_javascript(f'window.open("{encoded_url}", "_blank");')

    def trigger_download(self, filepath, file_name, on_complete=None):
        ui.notify(
            f"开始下载文件: {file_name}",
            type="info",
            position="bottom",
            timeout=2000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )
        ui.download(filepath)
        if on_complete:
            on_complete()

    async def check_and_download(self, filepath, file_name):
        storage_key = f"downloaded_{file_name}"
        has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

        if has_downloaded:
            self.check_down_dialog.clear()
            with self.check_down_dialog, ui.card().classes("min-w-[400px]"):
                with ui.card_section():
                    ui.label(f'文件 "{file_name}" 已在本次会话中下载。').classes("text-lg font-medium")
                with ui.card_actions().props("align=right"):
                    ui.button(
                        "仍要重新下载",
                        on_click=lambda fp=filepath, fn=file_name: self.trigger_download(
                            fp, fn, self.check_down_dialog.close
                        ),
                        color="primary",
                    )
                    ui.button("关闭", on_click=self.check_down_dialog.close, color="grey")
            self.check_down_dialog.open()
        else:
            self.trigger_download(filepath, file_name)
            await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

    def on_right_click(self, chip_data):
        text = chip_data.get("content", "")
        ui.run_javascript(f"navigator.clipboard.writeText('{text}');")
        ui.notify(
            "内容已复制到剪贴板！",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )

    # ==========================================================
    # 9. 主入口响应事件
    # ==========================================================

    def _handle_main_button_click(self, e: GenericEventArguments):
        if e.args.get("ctrlKey"):
            self.show_label_history()
            return

        if self._edit_permission_judge():
            if self.processing_type == "text":
                self._setup_text_chip_dialog()
            elif self.processing_type == "test":
                self._setup_test_chip_dialog()
            elif self.processing_type == "search":
                self._setup_search_chip_dialog()
            elif self.processing_type == "svn":
                self._setup_svn_chip_dialog()
            else:
                self._setup_file_notes_dialog()


class OverviewTableGroup:
    """
    基于表格布局的概述内容组管理组件。
    将同一个分组（如"光源"）下的所有配置项渲染为一个二维表格，
    保持后端存储逻辑不变，实现横向的视觉强关联。
    """

    def __init__(
        self,
        project: str,
        role: str,
        group_name: str,
        configs: dict,
        temp_bool: bool = False,
    ):
        self.project = project
        self.role = role
        self.group_name = group_name
        self.configs = configs  # 传入整个分组的配置字典，例如 "光源" 下的所有配置
        self.temp_bool = temp_bool
        # --- 💡 细化权限管控到列层级 ---
        self.permitted_configs = {}
        user_role = app.storage.user.get("current_role", "")
        for key, config in self.configs.items():
            # 只有当用户具备读取或编辑权限时，该列才会被加入最终渲染和监控的列表中
            if user_role in config.get("permission", {}).get("read_role", []) or user_role in config.get(
                "permission", {}
            ).get("edit_role", []):
                self.permitted_configs[key] = config
        self.offset = (0, 0)
        self.is_dragging = False
        self.last_pos = (0, 0)
        self.zoom_level = 1.0

        # 全局复用的对话框（以节省DOM节点）
        self.chip_dialog = ui.dialog().classes("")
        self.img_dialog = ui.dialog().props("").classes("p-0")
        self.overview_video_dialog = ui.dialog().classes("p-0 bg-transparent shadow-none")
        self.check_down_dialog = ui.dialog().classes("")
        self.activ_dialog = ui.dialog().props("persistent").classes("")
        self.history_dialog = ui.dialog().classes("w-full")
        # 采用 w-full max-w-screen-md 确保在大中小屏幕下均有良好的自适应宽度表现
        self.autofill_dialog = ui.dialog().classes("w-full max-w-screen-md px-4 py-2")

        # 隐藏的文件上传器
        self.uploader = ui.upload(
            on_upload=self._handle_file_upload,
            on_begin_upload=lambda: self.spinner.set_visibility(True) if hasattr(self, "spinner") else None,
            auto_upload=True,
            max_files=1,
        ).props("accept=*/*")
        self.uploader.set_visibility(False)

        # 当前正在操作的列配置（非常关键，用于让弹窗知道在处理哪个字段）
        self.current_config = {}

        # 表格的主容器
        self.container = ui.column().classes(
            "w-full gap-0 border border-blue-200 rounded-sm overflow-hidden mt-2 bg-white"
        )

        # --- 状态追踪细化到列 (字典结构) ---
        self.last_state_hashes = {}

        # 初始渲染 & 开启定时器
        ui.timer(1.0, self._update_display)

    async def _group_and_migrate_data(self, col_configs, show_all, conversion_refresh):
        """
        核心方法：将按列存放的数据转换为按行存放，并无缝清洗旧数据。
        修补了跨行移动时跳入“幽灵空行”的 BUG。
        """
        row_dict = {}
        ordered_row_ids = []

        # 1. 预先拉取并过滤所有列的数据
        all_cols_chips = []
        for config in col_configs:
            label = config["label"]
            chips_dict = db_storage.get_deep_item([f"{self.project}_over_data", label], {})

            valid_chips = []
            for chip_id, chip_data in chips_dict.items():
                if (
                    conversion_refresh
                    and chip_data.get("type") == "svn"
                    and chip_data.get("enabled") not in [True, None]
                ):
                    continue
                if not show_all and chip_data.get("enabled") is False:
                    continue
                valid_chips.append(chip_data)
            all_cols_chips.append((label, valid_chips))

        if not all_cols_chips:
            return []

        # 2. 建立行基准线 fallback_row_ids，保证老数据严格按水平 Index 对齐
        fallback_row_ids = []
        max_len = max([len(chips) for _, chips in all_cols_chips]) if all_cols_chips else 0

        for i in range(max_len):
            found_row_id = None
            # 优先在各列中找同水平位置已经存在的真实 row_id
            for _, chips in all_cols_chips:
                if i < len(chips) and chips[i].get("row_id"):
                    found_row_id = chips[i].get("row_id")
                    break
            # 如果这一行全是老数据没 ID，才生成一个唯一行标识
            if not found_row_id:
                found_row_id = str(uuid.uuid4())
            fallback_row_ids.append(found_row_id)

        # 3. 归集数据并分配/清洗 row_id
        first_col_label = col_configs[0]["label"]

        for label, chips in all_cols_chips:
            for i, chip_data in enumerate(chips):
                row_id = chip_data.get("row_id")
                # 如果没有 row_id (旧数据)，则从基准线中分配并异步保存
                if not row_id:
                    row_id = fallback_row_ids[i]
                    chip_data["row_id"] = row_id
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_data", label, chip_data["id"], "row_id"], row_id
                    )

                # 归集到行字典，支持单个单元格内追加多个 chip
                if row_id not in row_dict:
                    row_dict[row_id] = {}
                row_dict[row_id].setdefault(label, []).append(chip_data)

                # 建立行显示主序：以第一列的物理顺序为尊
                if label == first_col_label:
                    if row_id not in ordered_row_ids:
                        ordered_row_ids.append(row_id)

        # 4. 补漏：如果后面列有不属于第一列主序的零散行，统一追加到表格末尾
        for label, chips in all_cols_chips:
            for chip_data in chips:
                row_id = chip_data.get("row_id")
                if row_id not in ordered_row_ids:
                    ordered_row_ids.append(row_id)

        # 5. 保存干净、无空洞的行顺序快照，供后续跨行移动算法准确计算
        self.ordered_row_ids = ordered_row_ids

        # 组装最终渲染所需的数组
        rows_list = [{"row_id": rid, "chips": row_dict[rid]} for rid in ordered_row_ids if rid in row_dict]
        return rows_list

    # ================= UI 渲染核心 =================
    def _handle_header_click(self, e, config):
        """处理表头点击事件"""
        if e.args.get("ctrlKey"):
            self.show_label_history(config)

    async def _render_table(self):
        col_configs = list(self.permitted_configs.values())
        if not col_configs:
            return

        req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
        show_all = app.storage.client.get("record_switch")
        conversion_refresh = app.storage.general.get("conversion_refresh", {}).get(self.project)

        # 1. 获取按行绑定的数据，并完成旧数据清洗
        rows_list = await self._group_and_migrate_data(col_configs, show_all, conversion_refresh)
        # 数据准备完毕，此时再瞬间清空并重绘 UI，不会产生折叠坍缩
        self.container.clear()
        # 2. 渲染 UI
        with self.container:
            # --- 渲染表头 ---
            with ui.row().classes(
                "w-full flex-nowrap bg-blue-50/80 border-b border-blue-200 p-0 m-0 items-center -space-x-4"
            ):
                for config in col_configs:
                    label = config["label"]
                    role = config["role"]
                    latest_user_str = (
                        app.storage.general.get("overview_role", {})
                        .get(self.project, {})
                        .get(role, {})
                        .get("latest_user", "")
                    )
                    des_user = latest_user_str.split("：")[1] if latest_user_str else ""

                    # 💡 恢复 1：计算状态指示灯颜色
                    # 获取该列的所有原始 chip 数据
                    raw_chips_dict = db_storage.get_deep_item([f"{self.project}_over_data", label], {})
                    chip_states = [c.get("enabled") for c in raw_chips_dict.values()]

                    dot_color = "text-red"
                    if chip_states and any(state is None for state in chip_states):
                        dot_color = "text-orange"
                    elif des_user == "不需要" or chip_states and any(state is True for state in chip_states):
                        dot_color = "text-green"

                    # 💡 恢复 2：让表头变成可点击，并绑定 Shift+Click 事件
                    with (
                        ui.column()
                        .classes(
                            "flex-1 p-1 border-r border-blue-200 last:border-r-0 items-center justify-center min-w-[100px] relative cursor-pointer hover:bg-blue-100 transition-colors"
                        )
                        .on("click", lambda e, c=config: self._handle_header_click(e, c), ["ctrlKey"])
                    ):
                        # 渲染小圆点（仅必填项显示，悬浮在左上角）
                        if config.get("nature") == "必填":
                            ui.label("●").classes(f"absolute -top-1 left-0 text-[10px] {dot_color}")
                        # 渲染小圆点（仅必填项显示，悬浮在左上角）
                        elif config.get("nature") == "需填":
                            ui.label("○").classes(f"absolute -top-1 left-0 text-[10px] {dot_color}")

                        # 渲染标题
                        ui.label(config["title"]).classes("font-bold text-sm text-blue-900 text-center").style(
                            "white-space: pre-wrap;"
                        )

                        # 增加贴心的浮动提示
                        # ui.tooltip("按住 Shift 点击查看该列历史记录").classes("text-xs bg-gray-800")

            # --- 渲染数据行 ---
            # 提取第一列的 label 用于判断
            first_col_label = list(self.permitted_configs.values())[0]["label"]
            for index, row_data in enumerate(rows_list):
                bg_color = "bg-white" if index % 2 == 0 else "bg-gray-50/40"
                row_id = row_data["row_id"]
                row_chips = row_data["chips"]

                with ui.row().classes(
                    f"w-full flex-nowrap border-b border-gray-100 {bg_color} items-stretch p-0 m-0  -space-x-4 hover:bg-amber-50/40 transition-colors"
                ):
                    for config in col_configs:
                        label = config["label"]

                        # 💡 优化 1：取消居中对齐改为顶部对齐(items-start)，并增加 pb-6 给绝对定位的按钮留出一点底边距
                        with ui.column().classes(
                            "flex-1 p-2 pb-4 border-r border-gray-100 last:border-r-0 items-start justify-start min-w-[100px] relative group gap-1"
                        ):
                            is_first_col = label == first_col_label
                            has_chip = bool(label in row_chips and row_chips[label])

                            # 1. 渲染该单元格内的所有 chip
                            if has_chip:
                                for chip_data in row_chips[label]:
                                    await self._render_single_chip(chip_data, config, req_max_ver)

                            # 2. 渲染添加按钮：
                            if self._edit_permission_judge(config, notify=False):
                                if not (is_first_col and has_chip):
                                    icon_name = self._get_icon_for_type(config["processing_type"])
                                    btn = ui.button(
                                        icon=icon_name,
                                        on_click=lambda _, c=config, rid=row_id: self._handle_add_click(
                                            c, target_row_id=rid
                                        ),
                                    ).props("flat round dense size=sm")
                                    # 💡 优化 2：绝对定位到右下角，完全脱离文档流，不撑大表格高度
                                    btn.classes(
                                        "absolute -bottom-1 -right-1 text-blue-500 opacity-0 group-hover:opacity-100 transition-all m-0 p-0 z-10"
                                    ).tooltip(f"添加 {config['title']}")
            # --- 渲染表格底部的“添加新行”栏 ---
            # 只有具有任意列的编辑权限时才显示
            with ui.row().classes("w-full bg-blue-50/30 justify-center p-0"):
                ui.button("", icon="add", on_click=lambda: self._handle_add_new_row()).classes(
                    "w-full text-[8px]"
                ).props("flat")

    async def _render_single_chip(self, chip_info: dict, config: dict, req_max_ver: str):
        """渲染单个单元格内的 Chip (移植自原 _create_chip_from_data)"""
        chip_text = chip_info.get("content", "")
        filepath = ""
        upload_path = config.get("upload_path", "")
        delete_icon = "close" if app.storage.user["current_user"] == "admin" else "settings"
        delete_bg = "bg-red text-white" if app.storage.user["current_user"] == "admin" else "bg-white text-light-blue"

        if chip_info.get("type") in ["text", "file", "test", "search", "svn", "video"]:
            file_info = (False, None)
            if chip_info["type"] == "file":
                filepath = f"{upload_path}/{chip_text}"
                app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            elif chip_info["type"] == "search":
                target_path_list = await self._search_file_path(chip_text, config)
                files_li = []
                for target_path in target_path_list:
                    if target_path and Path(target_path).is_dir():
                        files_li.extend(find_files_pathlib(target_path, chip_text))
                if len(files_li) == 1:
                    filepath = str(files_li[0])
                    app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
            elif chip_info["type"] == "svn":
                target_url = chip_info.get("url_path", "")
                file_info = await self.get_url_file_info_async(target_url)

            # 💡 优化 3：新增一个相对定位的容器包裹 Chip，这是解决小按钮被裁切的关键！
            with ui.element("div").classes("relative w-full flex items-center justify-start") as wrapper:
                # 创建主 Chip 元素
                chip = (
                    ui.chip(text=chip_text, removable=False, icon=chip_info.get("icon"))
                    .props(f"data-chip-id={chip_info.get('id')} enabled-state={chip_info.get('enabled')} dense square")
                    # 💡 优化 4：移除 overflow-hidden text-ellipsis，改为高度自适应(h-auto)
                    .classes(
                        f"m-0 {chip_info.get('bg_color')} w-full justify-start shadow-sm h-auto py-1 min-h-[30px] multiline-chip"
                    )
                    # 💡 优化 5：强制允许长串英文、连字符进行自动换行
                    .style("white-space: normal; word-break: break-all; line-height: 1.3;")
                )

                # 点击事件绑定
                # --- 修复 3.1: 补充文件路径失效、丢失时的动态图标修改与警告 ---
                if chip_info.get("type") in ["file", "search"]:
                    if chip_info.get("file_type") == "application/pdf" and filepath and Path(filepath).exists():
                        chip.on_click(lambda url=chip_info.get("url_path"): self.open_pdf_in_browser(url))
                    elif filepath and Path(filepath).exists():
                        chip.on_click(lambda fp=filepath, fn=chip_text: self.check_and_download(fp, fn))
                    else:
                        if chip_info["type"] == "file":
                            chip.set_icon("link_off")
                        elif chip_info["type"] == "search" and chip._props.get("icon") != "question_mark":
                            chip.set_icon("search_off")
                        chip.on_click(
                            lambda: ui.notify(
                                "文件不存在服务器、路径失效、不唯一，点击不能打开或下载！",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                # multi_line=True,
                                close_button="✖",
                            )
                        )

                elif chip_info.get("type") == "svn":
                    if chip_info.get("file_type") == "application/pdf" and file_info[0]:
                        chip.on_click(
                            lambda url=chip_info.get("url_path"), fn=chip_text: self.open_svn_pdf_in_browser(url, fn)
                        )
                    elif file_info[0]:
                        chip.on_click(
                            lambda url=chip_info.get("url_path"), fn=chip_text: self.check_and_download_svn(url, fn)
                        )
                    else:
                        if chip._props.get("icon") != "question_mark":
                            chip.set_icon("search_off")
                        chip.on_click(
                            lambda: ui.notify(
                                f"SVN文件：\n{chip_info.get('url_path')}\n已丢失，点击不能打开或下载！",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                multi_line=True,
                                close_button="✖",
                            )
                        )

                elif chip_info.get("type") == "video":
                    if filepath and Path(filepath).exists():
                        chip.on_click(lambda url=chip_info.get("url_path"): self.play_overview_video(url))
                    else:
                        chip.set_icon("videocam_off")
                        chip.on_click(
                            lambda: ui.notify(
                                f"视频文件：\n{chip_info.get('url_path')}\n已丢失，点击不能打开或下载！",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                multi_line=True,
                                close_button="✖",
                            )
                        )

                # Tooltip & 控制按钮
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

                    # 💡 优化 6：悬浮按钮放在 div 容器内（与 chip 平级），绝对定位，利用 z-20 浮在 chip 上方
                # 注意 style 中 display 设置为 flex 而非 block，防止图标偏离中心
                delete_button = (
                    ui.button(on_click=lambda c=chip, cfg=config: self.delete_chip_info(c, cfg))
                    .classes(f"absolute -top-1 -right-2 m-0 p-0 q-py-0 z-20 {delete_bg} shadow-md")
                    .props(f'round padding="0px 0px" icon={delete_icon}')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                move_down_button = (
                    ui.button(on_click=lambda d=chip_info, cfg=config: self.move_down_data(d, cfg))
                    .classes(
                        "absolute -top-1 right-2 m-0 p-0 q-py-0 z-20 bg-white text-light-blue shadow-md border border-gray-200"
                    )
                    .props('round padding="0px 0px" icon="arrow_drop_down"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                move_up_button = (
                    ui.button(on_click=lambda d=chip_info, cfg=config: self.move_up_data(d, cfg))
                    .classes(
                        "absolute -top-1 right-6 m-0 p-0 q-py-0 z-20 bg-white text-light-blue shadow-md border border-gray-200"
                    )
                    .props('round padding="0px 0px" icon="arrow_drop_up"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )
                history_button = (
                    ui.button(on_click=lambda d=chip_info, cfg=config: self.show_chip_history(d, cfg))
                    .classes("absolute -bottom-1 -right-2 m-0 p-0 q-py-0 z-20 bg-white text-purple-8 shadow-md")
                    .props('round padding="0px 0px" icon="history"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

            # 交互事件
            # 💡 优化 7：使用 display: flex 保持按钮圆圈内的图标居中
            def check_ctrl_and_show(e, btns):
                for b in btns:
                    b.style("display: flex;" if e.args.get("ctrlKey") else "display: none;")

            def check_shift_and_show(e, btn):
                btn.style("display: flex;" if e.args.get("shiftKey") else "display: none;")

            control_btns = [delete_button, move_up_button, move_down_button, history_button]
            wrapper.on("mouseenter", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            wrapper.on("mousemove", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            wrapper.on("mouseleave", lambda: [b.style("display: none;") for b in control_btns])
            # wrapper.on("mouseenter", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # wrapper.on("mousemove", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # wrapper.on("mouseleave", lambda: history_button.style("display: none;"))
            chip.on("contextmenu", lambda d=chip_info: self.on_right_click(d))

        elif chip_info.get("type") == "image":
            image_name = chip_info.get("content")
            image_path = f"{upload_path}/{image_name}"
            url_path = f"{FILES_URL_DIR}/{image_name}"
            app.add_static_file(local_file=image_path, url_path=url_path)

            with ui.element("div").classes("relative w-full") as wrapper:
                thumbnail = (
                    ui.interactive_image(url_path)
                    .props(f"data-chip-id={chip_info.get('id')} enabled-state={chip_info.get('enabled')}")
                    .classes("h-10 cursor-pointer w-full object-cover rounded shadow-sm border border-gray-200")
                )
                thumbnail.on("click", lambda u=url_path: self.show_fullscreen(u))

                with thumbnail:
                    ui.icon(chip_info.get("icon", "")).props("flat fab").classes(
                        "absolute top-0 left-0 text-xl text-blue-500/50"
                    )
                    # 缩略图创建日期提示
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>图片名: {image_name}<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>{chip_info.get('notes', '').replace('\n', '<br>')}"
                    with ui.tooltip():
                        ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                delete_button = (
                    ui.button(on_click=lambda t=thumbnail, cfg=config: self.clear_thumbnail(t, cfg))
                    .classes(f"absolute -top-1 -right-2 z-20 m-0 p-0 q-py-1 {delete_bg}")
                    .props(f'round padding="0px 0px" icon={delete_icon}')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")  # 阻止事件冒泡
                )
                move_up_button = (
                    ui.button(on_click=lambda d=chip_info, cfg=config: self.move_up_data(d, cfg))
                    .classes("absolute bottom-3 -right-2 z-20 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_up"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")  # 阻止事件冒泡
                )
                move_down_button = (
                    ui.button(on_click=lambda d=chip_info, cfg=config: self.move_down_data(d, cfg))
                    .classes("absolute -bottom-1 -right-2 z-20 m-0 p-0 q-py-0 bg-white text-light-blue")
                    .props('round padding="0px 0px" icon="arrow_drop_down"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")  # 阻止事件冒泡
                )
                history_button = (
                    ui.button(on_click=lambda d=chip_info, cfg=config: self.show_chip_history(d, cfg))
                    .classes("absolute -top-1 right-3 z-20 m-0 p-0 q-py-0 bg-white text-purple-8")
                    .props('round padding="0px 0px" icon="history"')
                    .style("font-size: 8px; display: none;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")  # 阻止事件冒泡
                )

            # 交互事件
            # 💡 优化 7：使用 display: flex 保持按钮圆圈内的图标居中
            def check_ctrl_and_show(e, btns):
                for b in btns:
                    b.style("display: flex;" if e.args.get("ctrlKey") else "display: none;")

            def check_shift_and_show(e, btn):
                btn.style("display: flex;" if e.args.get("shiftKey") else "display: none;")

            control_btns = [delete_button, move_up_button, move_down_button, history_button]
            wrapper.on("mouseenter", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            wrapper.on("mousemove", lambda e: check_ctrl_and_show(e, control_btns), ["ctrlKey"])
            wrapper.on("mouseleave", lambda: [b.style("display: none;") for b in control_btns])
            # wrapper.on("mouseenter", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # wrapper.on("mousemove", lambda e: check_shift_and_show(e, history_button), ["shiftKey"])
            # wrapper.on("mouseleave", lambda: history_button.style("display: none;"))

    from typing import Any, Dict

    # 一个专门用于生成轻量级哈希的方法，并加入类型提示
    def _generate_col_signature(self, filtered_dict: Dict[str, Any]) -> int:
        """
        生成列数据的轻量级状态签名，避免使用高开销的 json.dumps
        """
        signature = []

        # 遍历字典（Python 3.7+ 字典保持插入顺序，因此顺序变化也会体现在生成的 tuple 中）
        for chip_id, chip in filtered_dict.items():
            # 提取时间戳中的最新键（代表该 chip 经历的最后一次修改）
            timestamps = chip.get("timestamp", {})
            # 防御性编程：兼容空时间戳或非标准格式
            latest_time = max(timestamps.keys()) if timestamps else ""

            # 仅提取影响 UI 渲染的核心维度，构建轻量级元组
            signature.append((chip_id, chip.get("row_id", ""), chip.get("enabled"), latest_time))

        # tuple 是不可变的，内置的 hash() 函数在 C 语言层面的执行效率极高
        return hash(tuple(signature))

    async def _update_display(self) -> None:
        """通过轻量级 Hash 校验检测列数据变更，仅刷新变化列对应的待处理状态"""
        if (
            self.chip_dialog.value
            or self.check_down_dialog.value
            or self.activ_dialog.value
            or self.img_dialog.value
            or self.overview_video_dialog.value
            or self.history_dialog.value
            or self.autofill_dialog.value
        ):
            return

        changed_labels = []
        show_all = app.storage.client.get("record_switch")

        for config in self.permitted_configs.values():
            label = config["label"]
            chips_dict = db_storage.get_deep_item([f"{self.project}_over_data", label], {})
            filtered_dict = {k: v for k, v in chips_dict.items() if show_all or v.get("enabled") is not False}

            # ---------------------------------------------------------
            # 【优化点】：替换原有的 json.dumps 逻辑，调用轻量级签名函数
            # ---------------------------------------------------------
            col_hash = self._generate_col_signature(filtered_dict)

            # 如果该列哈希发生变化，则记录
            if self.last_state_hashes.get(label) != col_hash:
                self.last_state_hashes[label] = col_hash
                changed_labels.append(label)

        # 只有在确有列发生数据变更时才重绘，并精准推送该列的待处理状态
        if changed_labels:
            await self._render_table()
            # 这里调用的是全局范围的角色更新，建议后续也优化为按需触发
            overview_role_update(self.project, self.role)
            for changed_label in changed_labels:
                self._update_local_pending(changed_label)

    def _get_icon_for_type(self, ptype: str) -> str:
        icons = {
            "file": "post_add",
            "search": "zoom_in",
            "svn": "zoom_in",
            "image": "add_photo_alternate",
            "video": "video_call",
            "test": "add_task",
        }
        return icons.get(ptype, "edit_note")

    # ================= 权限与交互 =================
    def _edit_permission_judge(self, config: dict, notify=True):
        allowed_state = config.get("allowed_state", ["研发", "转产"])
        if (
            not self.temp_bool
            and app.storage.user["current_role"] in config.get("permission", {}).get("edit_role", [])
            and app.storage.general["project_summary"][self.project]["state"] in allowed_state
        ):
            return True

        if notify:
            if self.temp_bool:
                msg = "当前处于需求审核界面，概述内容锁定不可编辑!"
            elif app.storage.user["current_role"] not in config.get("permission", {}).get("edit_role", []):
                msg = "当前用户无该项编辑权限，请联系管理员申请2!"
            else:
                msg = "项目当前状态禁止编辑概述!"
            ui.notify(
                msg,
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
        return False

    def _handle_add_click(self, config: dict, target_row_id: str = ""):
        """处理单元格内的添加点击（绑定到特定行）"""
        self.current_config = config
        # 如果传入了 target_row_id，保存到组件状态中，供后续的 _add_xxx_chip_data 使用
        self.current_target_row_id = target_row_id

        if app.storage.general["project_summary"][self.project]["state"] not in config.get(
            "allowed_state", ["研发", "转产"]
        ):
            ui.notify(
                "项目当前状态禁止添加概述!",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            return

        self.current_config = config
        ptype = config["processing_type"]
        if ptype == "text":
            self._setup_text_chip_dialog()
        elif ptype == "test":
            self._setup_test_chip_dialog()
        elif ptype == "search":
            self._setup_search_chip_dialog()
        elif ptype == "svn":
            self._setup_svn_chip_dialog()
        else:
            self._setup_file_notes_dialog()

    def _handle_add_new_row(self):
        """点击底部添加新行时，触发第一列的添加弹窗，并生成全新 row_id"""
        first_col_config = list(self.permitted_configs.values())[0]
        if self._edit_permission_judge(first_col_config):
            # 生成一个全新的 UUID 作为新行的基准
            new_row_id = str(uuid.uuid4())
            self._handle_add_click(first_col_config, target_row_id=new_row_id)

    # ================= 弹窗 & 添加数据逻辑 (继承并传参化) =================
    def _setup_text_chip_dialog(self):
        self.chip_dialog.clear()
        config = self.current_config
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label(f"添加: {config['title']}").classes("text-lg font-bold text-blue-900")
            self.chip_label = (
                ui.textarea(
                    label=config.get("dialog_label", "按规定格式填写"),
                    value=config.get("dialog_placeholder", ""),
                    placeholder=config.get("dialog_placeholder", ""),
                )
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._add_text_chip_data(ui_spinner, btn=e.sender))
        self.chip_dialog.open()

    async def _add_text_chip_data(self, ui_spinner, btn=None):
        if btn:
            btn.disable()  # 1. 进门立刻禁用按钮，防止连点
        try:
            config = self.current_config
            # 获取要绑定的 row_id，如果没有（理论上现在都有了），就生成一个新的
            row_id = getattr(self, "current_target_row_id", None) or str(uuid.uuid4())
            text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
            # 如果填写内容有正则表达式管控，则分析内容是否符合规则
            regular_bool = False
            if config.get("content_regular", []):
                for regular in config.get("content_regular", []):
                    if re.search(regular, text):
                        regular_bool = True
            else:
                regular_bool = True
            if not regular_bool:
                ui.notify(
                    "内容不符合填写格式规范!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                return
            if not text or not notes:
                ui.notify(
                    "内容和注释均不能为空!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                return
            if (text, row_id) in [
                (d["content"], d.get("row_id", ""))
                for d in db_storage.get_deep_item([f"{self.project}_over_data", config["label"]], {}).values()
            ]:
                ui.notify(
                    "概述内容已存在。",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                return

            ui_spinner.set_visibility(True)
            req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名")

            chip_data = {
                "id": str(uuid.uuid4()),
                "row_id": row_id,
                "role": self.role,
                "icon": None,
                "enabled": True,
                "bg_color": "bg-light-blue-1",
                "type": "text",
                "content": text,
                "notes": notes,
                "creator": creator,
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
            }
            await db_storage.set_deep_item([f"{self.project}_over_data", config["label"], chip_data["id"]], chip_data)

            ui_spinner.set_visibility(False)
            self.chip_dialog.close()
            ui.notify(
                "内容已添加",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            self._show_related_chip_select_dialog(text, True, "add_chip", config)
            await self._check_and_trigger_autofill(row_id, text, config)
        except Exception as ex:
            # 捕捉潜在的数据库写入等异常
            logger.error(f"添加概述失败: {ex}", exc_info=True)
        finally:
            if btn:
                btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    # ---------------- 补充缺失的方法适配 -----------------
    def _setup_test_chip_dialog(self):
        self.chip_dialog.clear()
        config = self.current_config
        with self.chip_dialog, ui.card().classes("w-full"):
            ui.label(f"添加产品的{config['title']}").classes("text-lg font-bold text-blue-900")
            test_select_data = {
                "test_nature_select": "",
                "test_nature_other_text": "",
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
                    value=config.get("dialog_placeholder", ""),
                    placeholder=config.get("dialog_placeholder", ""),
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )

            def bind_select(options, title, prefix):
                if options:
                    with ui.column().classes("w-full p-0 m-0"):
                        sel = (
                            ui.select(options, multiple=False, label=title)
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, f"{prefix}_select")
                        )
                        oth = (
                            ui.textarea(
                                label=f"{title}特殊要求",
                                placeholder="写明特殊要求",
                                validation={"不能空白": lambda v: v.strip() != ""},
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, f"{prefix}_other_text")
                        )
                        oth.set_visibility(False)
                        sel.on_value_change(lambda: self._set_other_ui(oth, sel.value))

            bind_select(config.get("test_nature_options", []), "测试性质", "test_nature")
            bind_select(config.get("state_options", []), "条件/状态", "state")
            bind_select(config.get("node_options", []), "节点/位置", "node")
            bind_select(config.get("instrument_options", []), "工具/仪器/治具", "instrument")

            self.chip_notes = (
                ui.textarea(
                    label="注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button(
                    "添加", on_click=lambda e: self._add_test_chip_data(ui_spinner, test_select_data, btn=e.sender)
                )
        self.chip_dialog.open()

    async def _add_test_chip_data(self, ui_spinner, test_select_data, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(ui_spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
                config = self.current_config
                other_bool = any(
                    test_select_data[f"{p}_select"] == "其它" and not test_select_data[f"{p}_other_text"]
                    for p in ["state", "node", "instrument"]
                )
                if not notes:
                    ui.notify(
                        "注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                # 如果填写内容有正则表达式管控，则分析内容是否符合规则
                regular_bool = False
                if config.get("content_regular", []):
                    for regular in config.get("content_regular", []):
                        if re.search(regular, text):
                            regular_bool = True
                else:
                    regular_bool = True
                if not regular_bool:
                    ui.notify(
                        "内容不符合填写格式规范!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if not text or not all(
                    test_select_data[f"{p}_select"]
                    for p in ["state", "node", "instrument"]
                    if config.get(f"{p}_options")
                ):
                    ui.notify(
                        "测试项内容及选项必须填写和选择!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if other_bool:
                    ui.notify(
                        "特殊要求不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                # --- 修复 1.2: 补充 Test 类型的组合查重拦截 ---
                existing_test_data = [
                    (d["content"], d.get("test_select_data"))
                    for d in db_storage.get_deep_item([f"{self.project}_over_data", config["label"]], {}).values()
                ]
                if (text, test_select_data) in existing_test_data:
                    ui.notify(
                        "测试项内容标准已存在。",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                ui_spinner.set_visibility(True)
                req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")
                row_id = getattr(self, "current_target_row_id", None) or str(uuid.uuid4())
                chip_data = {
                    "id": str(uuid.uuid4()),
                    "row_id": row_id,
                    "role": self.role,
                    "icon": None,
                    "enabled": True,
                    "bg_color": "bg-light-blue-1",
                    "type": "test",
                    "content": text,
                    "notes": notes,
                    "test_select_data": test_select_data,
                    "creator": creator,
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                }

                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", config["label"], chip_data["id"]], chip_data
                )

                ui_spinner.set_visibility(False)
                self.chip_notes.value = ""
                self.chip_dialog.close()
                ui.notify(
                    "内容已添加。",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                self._show_related_chip_select_dialog(text, True, "add_chip", config)
                await self._check_and_trigger_autofill(row_id, text, config)
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    # ---- 文件处理相关 ----
    def _setup_file_notes_dialog(self):
        self.chip_dialog.clear()
        config = self.current_config
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label(f"添加上传文件的注释: {config['title']}").classes("text-lg font-bold text-blue-900")
            ui.label(f"保存根目录：{config.get('upload_path', '')}").classes("text-xs text-brown-7")
            self.chip_label = (
                ui.input(
                    label="不需要提交文件时填写（选填）",
                    placeholder="无",
                )
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="针对该文件的注释（必填）",
                    placeholder="首次提交/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                self.spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                self.spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._get_file_upload(btn=e.sender))
        self.chip_dialog.open()

    async def _get_file_upload(self, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(self.spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                if not self.chip_notes.value:
                    ui.notify(
                        "注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                else:
                    self.uploader.reset()
                    self.uploader.run_method("pickFiles")
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    async def _handle_file_upload(self, e):
        """处理文件/图片/视频上传事件（已修复同名防覆盖与查重漏洞）"""
        config = self.current_config
        original_filename = e.file.name
        file_type = e.file.content_type
        file_ext = os.path.splitext(original_filename)[1].lower()

        # 1. 校验文件类型后缀
        if config["processing_type"] == "file" and file_ext not in OVER_UPLOADS_FILE_TYPE:
            ui.notify(
                f'文件 "{original_filename}" 不是规定的文件类型，无法上传!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            if hasattr(self, "spinner"):
                self.spinner.set_visibility(False)
            return
        if config["processing_type"] == "image" and "image" not in file_type:
            ui.notify(
                f'文件 "{original_filename}" 不是图片类型!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            if hasattr(self, "spinner"):
                self.spinner.set_visibility(False)
            return
        if config["processing_type"] == "video" and "video" not in file_type:
            ui.notify(
                f'文件 "{original_filename}" 不是视频类型!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            if hasattr(self, "spinner"):
                self.spinner.set_visibility(False)
            return

        upload_path = config.get("upload_path", "")
        filepath = f"{upload_path}/{original_filename}"
        url_path = f"{FILES_URL_DIR}/{original_filename}"

        # 2. 逻辑层查重：检查该列是否已绑定过同名文件
        existing_contents = [
            d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", config["label"]], {}).values()
        ]
        if original_filename in existing_contents:
            ui.notify(
                f'文件 "{original_filename}" 无需重复提交!',
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            if hasattr(self, "spinner"):
                self.spinner.set_visibility(False)
            return

        # 获取或生成二维表格行基准 ID
        row_id = getattr(self, "current_target_row_id", None) or str(uuid.uuid4())

        # 3. 物理层防覆盖：检查服务器磁盘是否已有同名文件
        if os.path.exists(filepath):
            if hasattr(self, "spinner"):
                self.spinner.set_visibility(False)
            # 触发防覆盖询问弹窗
            self._select_file_show(original_filename, file_type, url_path, config, row_id)
            return

        # 4. 安全写入新文件
        try:
            file_content = await e.file.read()
            with open(filepath, "wb") as f:
                f.write(io.BytesIO(file_content).read())
        except Exception as ex:
            logger.error("上传处理失败", exc_info=True)
            ui.notify(
                f"上传失败: {ex}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                # multi_line=True,
                close_button="✖",
            )
            if hasattr(self, "spinner"):
                self.spinner.set_visibility(False)
            return

        # 5. 写入成功，创建并绑定数据
        await self._create_file_chip_data(original_filename, file_type, url_path, config, row_id)
        ui.notify(
            f'文件 "{original_filename}" 上传成功!',
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )

    def _select_file_show(self, original_filename, file_type, url_path, config, row_id):
        """询问重复提交文件是否按服务器现有文件显示"""
        self.chip_dialog.clear()
        self.chip_dialog.open()
        with self.chip_dialog, ui.card().classes("w-1/2 bg-orange-2"):
            ui.label("服务器已有同名文件，无法上传覆盖，是否使用服务器已有文件？").classes("text-lg")
            with ui.row().classes("w-full justify-end"):
                ui.button(
                    "是",
                    on_click=lambda: self._show_have_file(original_filename, file_type, url_path, config, row_id),
                    color="green-6",
                )
                ui.button("否", on_click=lambda: self.chip_dialog.close(), color="blue-grey-6")

    async def _show_have_file(self, original_filename, file_type, url_path, config, row_id):
        """复用服务器已有文件，直接生成数据记录"""
        await self._create_file_chip_data(original_filename, file_type, url_path, config, row_id)
        ui.notify(
            f'文件 "{original_filename}" 显示成功!',
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )

    async def _create_file_chip_data(self, original_filename, file_type, url_path, config, row_id):
        """内部辅助函数：统一处理文件类型 Chip 数据的生成与共享存储写入"""
        req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
        creator = app.storage.user.get("current_user", "匿名用户")
        icon_map = {"file": "attachment", "video": "play_circle", "image": "image"}
        select_activ_dic = self._get_select_activ_dic(req_max_ver)
        chip_id = str(uuid.uuid4())

        chip_data = {
            "id": chip_id,
            "row_id": row_id,  # 确保二维行对齐
            "role": self.role,
            "icon": icon_map.get(config["processing_type"], "image"),
            "enabled": True,
            "bg_color": "bg-light-blue-1",
            "type": config["processing_type"],
            "file_type": file_type,
            "content": original_filename,
            "url_path": url_path,
            "notes": self.chip_notes.value,
            "creator": creator,
            "req_ver": req_max_ver,
            "select_activ_dic": select_activ_dic,
            "timestamp": {
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                    "creator": creator,
                    "select_activ_dic": select_activ_dic,
                }
            },
        }

        # 写入数据库
        await db_storage.set_deep_item([f"{self.project}_over_data", config["label"], chip_id], chip_data)

        # 清理状态与UI收尾
        self.chip_notes.value = ""
        if hasattr(self, "spinner"):
            self.spinner.set_visibility(False)
        self.chip_dialog.close()

        # 触发关联项选择弹窗
        self._show_related_chip_select_dialog(original_filename, True, "add_chip", config)
        await self._check_and_trigger_autofill(row_id, original_filename, config)

        # 触发哈希变更与表格重绘
        self.last_state_hashes = {}
        await self._update_display()

    # ---------------- 搜索与SVN弹窗 (保留通用结构传Config) ----------------
    def _setup_search_chip_dialog(self):
        self.chip_dialog.clear()
        config = self.current_config
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label(f"添加引用: {config['title']}").classes("text-lg font-bold text-blue-900")
            self.chip_label = (
                ui.input(
                    label=config.get("dialog_label", "填入包括后缀的完整文件名"),
                    value=config.get("dialog_placeholder", ""),
                    placeholder=config.get("dialog_placeholder", ""),
                )
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = ui.textarea(label="注释（必填）").props("outlined").classes("w-full")
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._add_search_chip_data(ui_spinner, btn=e.sender))
        self.chip_dialog.open()

    async def _add_search_chip_data(self, ui_spinner, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(ui_spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                config = self.current_config
                text, notes = self.chip_label.value.strip(), self.chip_notes.value.strip()
                # 如果填写内容有正则表达式管控，则分析内容是否符合规则
                regular_bool = False
                if config.get("content_regular", []):
                    for regular in config.get("content_regular", []):
                        if re.search(regular, text):
                            regular_bool = True
                else:
                    regular_bool = True
                if not regular_bool:
                    ui.notify(
                        "内容不符合填写格式规范!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if not text or not notes:
                    ui.notify(
                        "引用文件名和注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                # --- 修复 1.1: 补充 Search 类型的查重拦截 ---
                existing_contents = [
                    d["content"]
                    for d in db_storage.get_deep_item([f"{self.project}_over_data", config["label"]], {}).values()
                ]
                if text in existing_contents:
                    ui.notify(
                        "引用文件名已添加过。",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                ui_spinner.set_visibility(True)
                target_path_list = await self._search_file_path(text, config)
                files_li = []
                target_path_li_str = ""  # 用于 Debug 提示

                for target_path in target_path_list:
                    target_path_li_str += f"{target_path}\n"
                    if target_path and Path(target_path).is_dir():
                        files_li.extend(find_files_pathlib(target_path, text))

                # --- 修复 4.1: 恢复详细的查找失败 Debug 路径提示 ---
                if not files_li:
                    ui.notify(
                        f"引用文件不存在以下所有路径：\n{target_path_li_str}请检查文件命名或相关依赖配置!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                elif len(files_li) > 1:
                    ui.notify(
                        f"引用文件在以下路径：\n{target_path_li_str}有多个同名文件，请确保唯一!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                else:
                    req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
                    creator = app.storage.user.get("current_user", "匿名用户")
                    row_id = getattr(self, "current_target_row_id", None) or str(uuid.uuid4())
                    chip_data = {
                        "id": str(uuid.uuid4()),
                        "row_id": row_id,
                        "role": self.role,
                        "icon": "saved_search",
                        "enabled": True,
                        "bg_color": "bg-light-blue-1",
                        "type": "search",
                        "file_type": get_file_type_by_extension(str(files_li[0]))[0],
                        "content": text,
                        "url_path": f"{FILES_URL_DIR}/{text}",
                        "notes": notes,
                        "creator": creator,
                        "req_ver": req_max_ver,
                        "select_activ_dic": self._get_select_activ_dic(req_max_ver),
                        "timestamp": {
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                "creator": creator,
                                "select_activ_dic": self._get_select_activ_dic(req_max_ver),
                            }
                        },
                    }
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_data", config["label"], chip_data["id"]], chip_data
                    )
                    self.chip_dialog.close()
                    ui.notify(
                        "文件引用已添加。",
                        type="positive",
                        position="bottom",
                        timeout=1000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    self._show_related_chip_select_dialog(text, True, "add_chip", config)
                    await self._check_and_trigger_autofill(row_id, text, config)
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态
                ui_spinner.set_visibility(False)

    # ---------------- 辅助方法重构 -----------------
    def _get_select_activ_dic(self, req_max_ver):
        """返回：{"1.0": False, "2.0": False, "req_max_ver": True}"""
        return {f"{i}.0": (f"{i}.0" == req_max_ver) for i in range(1, int(float(req_max_ver)) + 1)}

    def _set_other_ui(self, other_ui, select_value):
        other_ui.set_visibility(select_value == "其它")
        if select_value != "其它":
            other_ui.set_value("")

    def _update_local_pending(self, label):
        latest_user_str = (
            app.storage.general.get("overview_role", {}).get(self.project, {}).get(self.role, {}).get("latest_user", "")
        )
        des_user = latest_user_str.split("：")[1] if latest_user_str else ""
        if des_user:
            update_overview_charge_pending_dic(
                scope="local", des_user=des_user, project_name=self.project, des_label=label
            )

    async def delete_chip_info(self, chip, config):
        if self._edit_permission_judge(config):
            if app.storage.user["current_user"] == "admin":
                await db_storage.del_deep_item(
                    [f"{self.project}_over_data", config["label"], chip.props["data-chip-id"]]
                )
            else:
                self.current_config = config
                self._select_set_activ_dialog(chip.props["data-chip-id"], chip.text, config)

    async def clear_thumbnail(self, thumbnail, config):
        if self._edit_permission_judge(config):
            if app.storage.user["current_user"] == "admin":
                await db_storage.del_deep_item(
                    [f"{self.project}_over_data", config["label"], thumbnail.props["data-chip-id"]]
                )
            else:
                self.current_config = config
                self._select_set_activ_dialog(thumbnail.props["data-chip-id"], "", config)

    def _move_data(self, old_data, chip_id, move_num):
        temp_data = {}
        old_data_keys = list(old_data.keys())
        if not app.storage.client.get("record_switch"):
            num = move_num
            step = int(move_num / abs(move_num))
            current_index = old_data_keys.index(chip_id)
            while num != 0 and (
                (step < 0 and current_index != 0) or (step > 0 and current_index != len(old_data_keys) - 1)
            ):
                current_index += step
                if old_data[old_data_keys[current_index]].get("enabled") in [True, None]:
                    num -= step
                move_num += step
            move_num -= step
        new_data_keys = move_element(old_data_keys, chip_id, move_num)
        for k in new_data_keys:
            temp_data[k] = old_data.get(k, {})
        return temp_data

    async def move_up_data(self, chip_data, config):
        if not self._edit_permission_judge(config):
            return

        # 识别第一列
        first_col_label = list(self.permitted_configs.values())[0]["label"]
        current_label = config["label"]
        current_row_id = chip_data.get("row_id")

        # 获取当前行在视图中的排序索引
        if not hasattr(self, "ordered_row_ids") or current_row_id not in self.ordered_row_ids:
            return
        current_idx = self.ordered_row_ids.index(current_row_id)

        if current_label == first_col_label:
            # 💡 第一列：改变字典顺序，带动整行上移
            await db_storage.atomic_deep_update(
                [f"{self.project}_over_data", current_label], self._move_data, chip_data["id"], -1
            )
        else:
            # 💡 其他列：跨行跳跃（换行 ID 操作）
            if current_idx > 0:  # 如果不是第一行，则允许上移
                target_row_id = self.ordered_row_ids[current_idx - 1]
                chip_data["row_id"] = target_row_id
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", current_label, chip_data["id"], "row_id"], target_row_id
                )

        self.last_state_hashes = {}
        await self._update_display()

    async def move_down_data(self, chip_data, config):
        if not self._edit_permission_judge(config):
            return

        first_col_label = list(self.permitted_configs.values())[0]["label"]
        current_label = config["label"]
        current_row_id = chip_data.get("row_id")

        if not hasattr(self, "ordered_row_ids") or current_row_id not in self.ordered_row_ids:
            return
        current_idx = self.ordered_row_ids.index(current_row_id)

        if current_label == first_col_label:
            # 💡 第一列：改变字典顺序，带动整行下移
            await db_storage.atomic_deep_update(
                [f"{self.project}_over_data", current_label], self._move_data, chip_data["id"], 1
            )
        else:
            # 💡 其他列：跨行跳跃（换行 ID 操作）
            if current_idx < len(self.ordered_row_ids) - 1:  # 如果不是最后一行，则允许下移
                target_row_id = self.ordered_row_ids[current_idx + 1]
                chip_data["row_id"] = target_row_id
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", current_label, chip_data["id"], "row_id"], target_row_id
                )

        self.last_state_hashes = {}
        await self._update_display()

    # -------------- 对话框和联动刷新 --------------
    def _show_related_chip_select_dialog(self, chip_text, chip_state, type, config):
        self.activ_dialog.clear()
        with self.activ_dialog, ui.card().classes("w-full max-w-[800px]"):
            ui.label("选择本次操作可能影响的其它概述项：").classes("text-lg font-bold")
            with ui.grid(columns=3).classes("w-full gap-0"):
                related_select_dic = {label: False for label in config.get("impact_list", [])}
                for related_label in config.get("impact_list", []):
                    ui.checkbox(
                        text=app.storage.general["over_config_data_flat"].get(related_label, {}).get("title", "未知")
                    ).bind_value(related_select_dic, related_label)
            with ui.row().classes("w-full justify-end items-center"):
                ui.button(
                    "勾选的受影响",
                    color="green",
                    on_click=lambda: self._set_related_chip_state(
                        chip_text, chip_state, False, related_select_dic, type, config
                    ),
                ).on("click", self.activ_dialog.close)
                ui.button(
                    "全部受影响",
                    color="blue",
                    on_click=lambda: self._set_related_chip_state(
                        chip_text, chip_state, True, related_select_dic, type, config
                    ),
                ).on("click", self.activ_dialog.close)
        self.activ_dialog.open()

    async def _update_autofill_index(self, first_col_label: str, content: str):
        """
        维护首列内容的倒排索引（惰性记录）
        空间换时间，避免全量扫描项目数据。
        数据结构: { "第一列标签名": { "填入的主内容": ["项目A", "项目B"] } }
        """
        index_data = db_storage.get_item("overview_autofill_index", {})

        if first_col_label not in index_data:
            index_data[first_col_label] = {}

        if content not in index_data[first_col_label]:
            index_data[first_col_label][content] = []

        if self.project not in index_data[first_col_label][content]:
            index_data[first_col_label][content].append(self.project)
            await db_storage.set_item("overview_autofill_index", index_data)

    async def _check_and_trigger_autofill(self, row_id: str, first_col_content: str, config: dict):
        """
        探测并触发快捷填充（基于倒排索引的高效匹配，支持多Chip完整克隆与安全降级）。
        """
        col_configs_list = list(self.permitted_configs.values())
        if not col_configs_list:
            return

        first_col_label = col_configs_list[0]["label"]
        if config["label"] != first_col_label:
            return

        subsequent_configs = col_configs_list[1:]
        if not subsequent_configs:
            return

        req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
        creator = app.storage.user.get("current_user", "匿名用户")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. 优雅降级处理：首列为“无”时，强制将同行其他列以 "text" 类型填充为“无”
        if first_col_content in IGNORE_STR or first_col_content == "无":
            for col_cfg in subsequent_configs:
                label = col_cfg["label"]
                chip_id = str(uuid.uuid4())
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                chip_data = {
                    "id": chip_id,
                    "row_id": row_id,
                    "role": self.role,
                    "icon": None,
                    "enabled": True,
                    "bg_color": "bg-light-blue-1",
                    "type": "text",  # 强制使用 text 类型，避免 file/image 产生 404
                    "content": "无",
                    "notes": "首列为无，系统自动跟随填充",
                    "creator": creator,
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                    "timestamp": {time_str: {"creator": creator, "select_activ_dic": select_activ_dic}},
                }
                await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id], chip_data)

            ui.notify("已自动将同行其他概述列填充为【无】", type="info", position="bottom")
            self.last_state_hashes = {}
            await self._update_display()
            return

        # 2. 读取倒排索引进行精准查找
        # 数据结构: { "第一列标签名": { "填入的主内容": ["项目A", "项目B"] } }
        index_data = db_storage.get_item("overview_autofill_index", {})
        target_projects = index_data.get(first_col_label, {}).get(first_col_content, [])
        # 排除当前项目，防止自引用循环
        target_projects = [p for p in target_projects if p != self.project]

        combinations = {}
        combo_hashes = set()

        if target_projects:
            for proj in target_projects:
                proj_data = db_storage.get_item(f"{proj}_over_data", {})
                if not proj_data:
                    continue

                first_col_data = proj_data.get(first_col_label, {})
                for chip in first_col_data.values():
                    if chip.get("content") == first_col_content and chip.get("enabled") is True:
                        target_row_id = chip.get("row_id")
                        if not target_row_id:
                            continue

                        combo = {}
                        for col_cfg in subsequent_configs:
                            label = col_cfg["label"]
                            title = col_cfg["title"]
                            col_data = proj_data.get(label, {})
                            # 获取同单元格内的所有激活 chip（支持多元素克隆）
                            col_chips = [
                                c
                                for c in col_data.values()
                                if c.get("row_id") == target_row_id and c.get("enabled") is True
                            ]
                            if col_chips:
                                combo[title] = copy.deepcopy(col_chips)

                        if combo:
                            # 为列表生成指纹去重
                            combo_fingerprint = str(
                                {k: [c.get("content") for c in chip_list] for k, chip_list in combo.items()}
                            )
                            if combo_fingerprint not in combo_hashes:
                                combo_hashes.add(combo_fingerprint)
                                combinations.update({proj: combo})

        # 3. 无论是否找到关联，均将当前录入登记到索引中（惰性维护）
        await self._update_autofill_index(first_col_label, first_col_content)

        if combinations:
            self._show_autofill_dialog(row_id, combinations, subsequent_configs)

    def _show_autofill_dialog(self, row_id: str, combinations: dict, col_configs: list):
        """
        展示联动组合勾选弹窗 (支持 test 类型的深度条件解析与多终端自适应滚动)
        """
        self.autofill_dialog.clear()
        selected_idx = {"val": list(combinations.keys())[0]}

        with self.autofill_dialog, ui.card().classes("w-full"):
            ui.label("发现历史项目中存在相同内容的关联配置，是否快捷填充？").classes("text-lg font-bold text-blue-900")

            # 使用 max-h-[50vh] 等限制高度并加上滚动条，保障移动端与小屏显示器的可用性
            with ui.scroll_area().classes("w-full max-h-[50vh] border p-2 bg-gray-50/50 rounded-sm"):
                for project_name, combo in combinations.items():
                    bg_hover = "hover:bg-blue-50"
                    with (
                        ui.row()
                        .classes(
                            f"w-full items-start border-b border-gray-200 py-3 px-2 cursor-pointer transition-colors {bg_hover}"
                        )
                        .on("click", lambda _, i=project_name: selected_idx.update({"val": i}))
                    ):
                        ui.radio([project_name], value=selected_idx["val"]).bind_value(selected_idx, "val").classes(
                            "mt-0"
                        )
                        with ui.column().classes("flex-grow gap-1"):
                            for title, chip_list in combo.items():
                                display_texts = []
                                for c in chip_list:
                                    base_content = c.get("content", "")
                                    # --- 专门针对 test 类型，解析下拉与文本输入条件 ---
                                    if c.get("type") == "test" and "test_select_data" in c:
                                        t_data = c["test_select_data"]

                                        test_nature = t_data.get("test_nature_select", "")
                                        if test_nature == "其它":
                                            test_nature = t_data.get("test_nature_other_text", "")

                                        state = t_data.get("state_select", "")
                                        if state == "其它":
                                            state = t_data.get("state_other_text", "")

                                        node = t_data.get("node_select", "")
                                        if node == "其它":
                                            node = t_data.get("node_other_text", "")

                                        inst = t_data.get("instrument_select", "")
                                        if inst == "其它":
                                            inst = t_data.get("instrument_other_text", "")

                                        details = [x for x in [test_nature, state, node, inst] if x]
                                        if details:
                                            base_content += f" ({', '.join(details)})"

                                    display_texts.append(base_content)

                                content_str = " | ".join(display_texts)
                                ui.label(f"【{title}】: {content_str}").classes("text-sm text-gray-700 break-all")

            unified_notes = (
                ui.textarea(label="统一注释 (必填)", placeholder="例如：继承历史项目配置")
                .props("outlined")
                .classes("w-full mt-2")
            )

            with ui.row().classes("w-full justify-end items-center mt-2 gap-2"):
                ui.button("跳过不填充", color="grey", on_click=self.autofill_dialog.close)
                ui.button(
                    "确认填充",
                    color="green",
                    on_click=lambda: self._execute_autofill(
                        row_id, combinations[selected_idx["val"]], col_configs, unified_notes.value
                    ),
                )

        self.autofill_dialog.open()

    async def _execute_autofill(self, row_id: str, combo: dict, col_configs: list, notes: str):
        """
        将选中的联动组合执行落盘
        """
        if not notes.strip():
            ui.notify("统一注释不能为空!", type="warning", position="bottom", timeout=2000)
            return

        req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
        creator = app.storage.user.get("current_user", "匿名用户")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        select_activ_dic = self._get_select_activ_dic(req_max_ver)
        for col_cfg in col_configs:
            label = col_cfg["label"]
            title = col_cfg["title"]
            if title in combo:
                templates = combo[title]
                for template in templates:
                    chip_id = str(uuid.uuid4())

                    chip_data = {
                        "id": chip_id,
                        "row_id": row_id,
                        "role": self.role,
                        "icon": template.get("icon"),
                        "enabled": True,
                        "bg_color": template.get("bg_color", "bg-light-blue-1"),
                        "type": template.get("type", "text"),
                        "content": template.get("content", ""),
                        "notes": notes,
                        "creator": creator,
                        "req_ver": req_max_ver,
                        "select_activ_dic": select_activ_dic,
                        "timestamp": {time_str: {"creator": creator, "select_activ_dic": select_activ_dic}},
                    }

                    # 安全深度克隆扩展字段
                    for ext_field in ["file_type", "url_path", "warehouse", "test_select_data"]:
                        if ext_field in template:
                            chip_data[ext_field] = copy.deepcopy(template[ext_field])

                    await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id], chip_data)

        self.autofill_dialog.close()
        ui.notify("关联数据自动填充成功!", type="positive", position="bottom")
        self.last_state_hashes = {}
        await self._update_display()

    async def _set_related_chip_state(self, chip_text, chip_state, all_related_bool, related_select_dic, type, config):
        overview_data = db_storage.get_item(f"{self.project}_over_data", {})
        for related_label, chip_dic in overview_data.items():
            if related_label in related_select_dic and (related_select_dic[related_label] or all_related_bool):
                for related_chip_id, chip_data in chip_dic.items():
                    over_chip_ver_li = [int(float(k)) for k in chip_data.get("select_activ_dic", {}).keys()]
                    if not over_chip_ver_li:
                        continue
                    max_over_ver = max(over_chip_ver_li)
                    if chip_data["select_activ_dic"].get(f"{max_over_ver}.0"):
                        chip_data["select_activ_dic"][f"{max_over_ver}.0"] = None
                        chip_data["enabled"] = None
                        chip_data["icon"] = "question_mark"
                        chip_data["bg_color"] = "bg-amber-5"
        if overview_data:
            await db_storage.set_item(f"{self.project}_over_data", overview_data)

    def _select_set_activ_dialog(self, chip_id, chip_text="", config=None):
        if config is None:
            config = self.current_config
        label = config["label"]
        self.activ_dialog.clear()

        with self.activ_dialog, ui.card().classes("w-1/2"):
            ui.label("选择概述生效的需求版本").classes("text-lg font-bold")
            select_activ_dic = db_storage.get_deep_item(
                [f"{self.project}_over_data", label, chip_id, "select_activ_dic"], {}
            )

            app.storage.general["over_change_broadcast"].setdefault(self.project, {})
            app.storage.general["over_change_broadcast"][self.project].setdefault(chip_id, {})

            if app.storage.general["over_change_broadcast"][self.project][chip_id] and len(
                app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"]
            ) == len(select_activ_dic):
                editor_list = app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"]
                editor_list.append(app.storage.user.get("current_user", "匿名用户"))
                app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"] = list(set(editor_list))
            else:
                app.storage.general["over_change_broadcast"][self.project][chip_id] = {
                    "editor": [app.storage.user.get("current_user", "匿名用户")],
                    "select_activ_dic": copy.deepcopy(select_activ_dic),
                }

            ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
            ui_spinner.set_visibility(False)

            with ui.grid(columns=6).classes("w-full gap-0"):
                for select_label, val in app.storage.general["over_change_broadcast"][self.project][chip_id][
                    "select_activ_dic"
                ].items():
                    select_box = ui.checkbox(text=select_label, value=val)
                    select_box.bind_value(
                        app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"],
                        select_label,
                    )

            open_dic = db_storage.get_deep_item([f"{self.project}_over_related_record", label, chip_id, "open"], {})

            if open_dic:
                ui.label("本次状态变化由以下概述调整引起：").classes("text-base font-bold text-brown")
                for time_key, record in open_dic.get("record", {}).items():
                    op_type = record.get("operate_type", "")
                    if op_type == "add_chip":
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"添加了『{record.get("operate_chip_content", "未知内容")}』"'
                        )
                    elif op_type == "activ_change":
                        state = (
                            "激活"
                            if record.get("operate_chip_state")
                            else "失活"
                            if record.get("operate_chip_state") is False
                            else "待确认"
                        )
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"修改『{record.get("operate_chip_content", "未知内容")}』的状态为『{state}』'
                        )
                    else:
                        record_label = ui.label(
                            f'[{time_key}]由用户"{record.get("operate_user", "匿名用户")}"操作了『{record.get("operate_chip_content", "未知内容")}』，操作类型未知'
                        )
                    record_label.classes("text-sm text-brown")

            with ui.row().classes("w-full justify-end items-center") as row:
                ui_spinner.move(row, 1)
                ui.button(
                    "确定",
                    color="green",
                    on_click=lambda: self.handle_checkbox_change(ui_spinner, chip_id, chip_text, config),
                ).on("click", self.activ_dialog.close)
                ui.button("取消", on_click=lambda: self.cancel_checkbox_change(chip_id)).on(
                    "click", self.activ_dialog.close
                )

        self.activ_dialog.open()

    def cancel_checkbox_change(self, chip_id):
        try:
            app.storage.general["over_change_broadcast"][self.project][chip_id]["editor"].remove(
                app.storage.user.get("current_user", "匿名用户")
            )
        except ValueError:
            pass
        if not app.storage.general["over_change_broadcast"][self.project].get(chip_id, {}).get("editor"):
            app.storage.general["over_change_broadcast"][self.project].pop(chip_id, None)

    def _check_version_updated(self, chip_id, new_select_activ_dic, chip_text, config) -> bool:
        select_activ_dic = db_storage.get_deep_item(
            [f"{self.project}_over_data", config["label"], chip_id, "select_activ_dic"], {}
        )

        if len(new_select_activ_dic) != len(select_activ_dic):
            ui.notify(
                "需求刚刚升级了，各项概述的激活配置需要重新确定！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            self._select_set_activ_dialog(chip_id, chip_text, config)
            return True
        return False

    async def _cascade_deactivate_row(self, row_id: str, req_max_ver: str, creator: str):
        """
        处理第一列失活时的连带失活逻辑，包含标准历史记录生成
        """
        first_col_label = list(self.permitted_configs.values())[0]["label"]
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for config in self.permitted_configs.values():
            label = config["label"]
            # 排除第一列自身
            if label == first_col_label:
                continue

            chips_dict = db_storage.get_deep_item([f"{self.project}_over_data", label], {})
            for chip_id, chip_data in chips_dict.items():
                if chip_data.get("row_id") == row_id:
                    # 检查当前是否尚未失活 (True 或 None)
                    current_state = chip_data.get("select_activ_dic", {}).get(req_max_ver)
                    if current_state is not False:
                        # 1. 更新激活字典
                        new_select_activ_dic = copy.deepcopy(chip_data.get("select_activ_dic", {}))
                        new_select_activ_dic[req_max_ver] = False
                        await db_storage.set_deep_item(
                            [f"{self.project}_over_data", label, chip_id, "select_activ_dic"], new_select_activ_dic
                        )

                        # 2. 更新UI样式参数
                        await self._update_chip_block_parameter(chip_id, config)

                        # 3. 产生标准操作记录 (明确标注是连带失活)
                        history_creator_label = f"{creator}(连带失活)"
                        await db_storage.set_deep_item(
                            [f"{self.project}_over_data", label, chip_id, "creator"], history_creator_label
                        )
                        await db_storage.set_deep_item(
                            [f"{self.project}_over_data", label, chip_id, "timestamp", time_str],
                            {"creator": history_creator_label, "select_activ_dic": new_select_activ_dic},
                        )

                        # 4. 闭环历史打开记录 (与手动操作逻辑完全一致)
                        open_dic = db_storage.get_deep_item(
                            [f"{self.project}_over_related_record", label, chip_id, "open"], {}
                        )

                        if open_dic:
                            open_dic["close_time"] = time_str
                            open_dic["close_related_user"] = history_creator_label
                            await db_storage.del_deep_item(
                                [f"{self.project}_over_related_record", label, chip_id, "open"]
                            )
                            await db_storage.set_deep_item(
                                [f"{self.project}_over_related_record", label, chip_id, open_dic["close_time"]],
                                open_dic,
                            )

    async def handle_checkbox_change(self, ui_spinner, chip_id, chip_text, config):
        label = config["label"]
        new_select_activ_dic = copy.deepcopy(
            app.storage.general["over_change_broadcast"][self.project][chip_id]["select_activ_dic"]
        )
        if self._check_version_updated(chip_id, new_select_activ_dic, chip_text, config):
            return

        try:
            OLD_CHIP_SELECT_DIC = db_storage.get_deep_item(
                [f"{self.project}_over_data", label, chip_id, "select_activ_dic"], {}
            )

            if new_select_activ_dic != OLD_CHIP_SELECT_DIC:
                ui_spinner.set_visibility(True)
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", label, chip_id, "select_activ_dic"], new_select_activ_dic
                )

                req_max_ver = f"{str(max([int(float(v)) for v in new_select_activ_dic.keys()]))}.0"
                chip_state = db_storage.get_deep_item(
                    [f"{self.project}_over_data", label, chip_id, "select_activ_dic", req_max_ver]
                )

                if chip_state:
                    await self._update_chip_active_parameter(chip_id, chip_text, config)
                elif chip_state is None:
                    pass
                else:
                    await self._update_chip_block_parameter(chip_id, config)

                creator = app.storage.user.get("current_user", "匿名用户")
                await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "creator"], creator)
                await db_storage.set_deep_item(
                    [
                        f"{self.project}_over_data",
                        label,
                        chip_id,
                        "timestamp",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ],
                    {"creator": creator, "select_activ_dic": new_select_activ_dic},
                )

                self.cancel_checkbox_change(chip_id)
                ui_spinner.set_visibility(False)

                open_dic = db_storage.get_deep_item([f"{self.project}_over_related_record", label, chip_id, "open"], {})

                if open_dic:
                    open_dic["close_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    open_dic["close_related_user"] = creator
                    await db_storage.del_deep_item([f"{self.project}_over_related_record", label, chip_id, "open"])
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_related_record", label, chip_id, open_dic["close_time"]], open_dic
                    )

                self._show_related_chip_select_dialog(chip_text, chip_state, "activ_change", config)
                # 💡 核心新增：级联失活判断逻辑
                first_col_label = list(self.permitted_configs.values())[0]["label"]
                if label == first_col_label and chip_state is False:
                    # 获取当前操作行的 row_id
                    current_row_id = db_storage.get_deep_item([f"{self.project}_over_data", label, chip_id, "row_id"])
                    if current_row_id:
                        await self._cascade_deactivate_row(current_row_id, req_max_ver, creator)
                self.last_state_hashes = {}  # 触发整体重绘
                await self._update_display()

        except Exception as ex:
            ui.notify(
                f"错误: {ex}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                # multi_line=True,
                close_button="✖",
            )

    async def _update_chip_block_parameter(self, chip_id, config):
        label = config["label"]
        await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "icon"], "block")
        await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "enabled"], False)
        await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "bg_color"], "bg-grey-5")

    async def _update_chip_active_parameter(self, chip_id, chip_text, config):
        label = config["label"]
        c_type = db_storage.get_deep_item([f"{self.project}_over_data", label, chip_id, "type"])
        if c_type == "file":
            await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "icon"], "attachment")
        elif c_type == "search":
            target_path_list = await self._search_file_path(chip_text, config)
            if target_path_list and find_files_pathlib(target_path_list[0], chip_text):
                await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "icon"], "saved_search")
            else:
                await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "icon"], "search_off")
        elif c_type == "svn":
            url = db_storage.get_deep_item([f"{self.project}_over_data", label, chip_id, "url_path"])
            file_info = await self.get_url_file_info_async(url)
            icon_val = "saved_search" if file_info[0] else "search_off"
            await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "icon"], icon_val)
        else:
            await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "icon"], None)

        await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "enabled"], True)
        await db_storage.set_deep_item([f"{self.project}_over_data", label, chip_id, "bg_color"], "bg-light-blue-1")

    def show_chip_history(self, chip_data, config):
        self.history_dialog.clear()
        timestamp_data = chip_data.get("timestamp", {})
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

                        if activ_dic:
                            with ui.row().classes("w-full flex-wrap gap-1"):
                                sorted_vers = sorted(
                                    activ_dic.keys(), key=lambda x: float(x) if x.replace(".", "", 1).isdigit() else 0
                                )
                                for ver in sorted_vers:
                                    is_active = activ_dic[ver]
                                    if is_active:
                                        color, text_col = "green", "white"
                                    elif is_active == "null":
                                        color, text_col = "orange", "white"
                                    else:
                                        color, text_col = "grey-4", "grey-7"

                                    ui.chip(text=f"V{ver}", color=color, text_color=text_col).props(
                                        "dense square size=sm"
                                    )

        self.history_dialog.open()

    def show_label_history(self, config):
        """显示整列标签的历史记录"""
        self.history_dialog.clear()
        label = config["label"]
        title = config["title"]

        # 1. 获取该列所有数据
        raw_data = db_storage.get_deep_item([f"{self.project}_over_data", label], {})
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
            history_list.sort(key=lambda x: (x["req_ver"], x["creation_time"]))

        # 3. 构建 UI
        with self.history_dialog, ui.card().classes("w-[800px] max-w-full h-[80vh]"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"历史记录: {title}").classes("text-xl font-bold text-gray-800")
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
                                if item["type"] in ["file", "image", "svn", "search", "video"]:
                                    ui.icon("attachment", size="xs", color="grey")

                                if item["enabled"] is True:
                                    color = "text-blue-400"
                                elif item["enabled"] is None or str(item["enabled"]).lower() == "null":
                                    color = "text-orange-400 italic"
                                else:
                                    color = "text-gray-400 line-through"

                                ui.label(item["content"]).classes(f"text-sm font-medium {color}")
                            if item["notes"]:
                                ui.label(f"注: {item['notes']}").classes("text-xs text-gray-500 italic")

        self.history_dialog.open()

    def on_right_click(self, chip_data):
        ui.run_javascript(f"navigator.clipboard.writeText('{chip_data.get('content', '')}');")
        ui.notify(
            "内容已复制到剪贴板！",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )

    # ------ 依赖原 InteractiveButton 的文件路径搜索方法 ------
    async def _search_file_path(self, chip_text, config) -> list:
        target_path_list = []
        according_folder_name_li = []
        search_folder_according_li = config.get("search_folder_according", [])
        upload_path = config.get("upload_path", "")
        search_scope_regular = config.get("search_scope_regular", "")
        search_hierarchy = config.get("search_hierarchy", [])

        if search_folder_according_li:
            for according in search_folder_according_li:
                for data in db_storage.get_deep_item([f"{self.project}_over_data", according], {}).values():
                    if data["enabled"]:
                        according_folder_name_li.append(data["content"])
            if not according_folder_name_li:
                return target_path_list
            for folder_name in according_folder_name_li:
                if search_scope_regular:
                    match = re.search(search_scope_regular, folder_name)
                    if match:
                        target_path_list.extend(
                            await find_dirs_by_name_os_walk(f"{upload_path}\\{match.group(1)}", folder_name)
                        )
                else:
                    target_path_list.extend(await find_dirs_by_name_os_walk(upload_path, folder_name))
        else:
            if search_scope_regular:
                match = re.search(search_scope_regular, chip_text)
                if match:
                    target_path_list = await find_dirs_by_name_os_walk(upload_path, match.group(1))
            else:
                target_path_list = [upload_path]

        if search_hierarchy:
            target_path_list = [f"{tp}\\{h}" for tp in target_path_list for h in search_hierarchy]
        return target_path_list

    # 模拟通用文件/PDF/视频操作
    def open_pdf_in_browser(self, url_path):
        ui.run_javascript(f'window.open("{url_path.replace(" ", "%20")}", "_blank");')

    async def check_and_download(self, filepath, file_name):
        ui.download(filepath)

    # ================= 完全照搬的视图、下载与网络请求相关 =================
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

    def show_fullscreen(self, url_path):
        self.img_dialog.clear()
        with self.img_dialog:
            self.image_big = ui.interactive_image(url_path).classes("cursor-grab").style("overflow: hidden;")
            self.image_big.on("mousedown", self.start_drag)
            self.image_big.on_mouse(self.get_img_xy)
            self.image_big.on("mousemove", self.handle_drag)
            self.image_big.on("mouseup", self.end_drag)
            self.image_big.on("mouseleave", self.end_drag)
            self.image_big.on("wheel", self.handle_zoom)
        self.img_dialog.open()
        self.reset_transform()

    def start_drag(self, e: GenericEventArguments):
        if e.args.get("button") == 0:
            self.is_dragging = True
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.image_big.classes(replace="cursor-grabbing")
        elif e.args.get("button") == 1:
            self.reset_transform()

    def handle_drag(self, e: GenericEventArguments):
        if self.is_dragging:
            dx = e.args["clientX"] - self.last_pos[0]
            dy = e.args["clientY"] - self.last_pos[1]
            self.offset = (self.offset[0] + dx, self.offset[1] + dy)
            self.last_pos = (e.args["clientX"], e.args["clientY"])
            self.update_transform()

    def end_drag(self, e: GenericEventArguments):
        self.is_dragging = False
        if hasattr(self, "image_big"):
            self.image_big.classes(replace="cursor-grab")

    def get_img_xy(self, e: MouseEventArguments):
        self.image_x, self.image_y = e.image_x, e.image_y

    def handle_zoom(self, e: GenericEventArguments):
        new_zoom = self.zoom_level * (1.1 if e.args["deltaY"] < 0 else 0.9)
        self.zoom_level = max(0.01, min(10, new_zoom))
        self.update_transform()

    def update_transform(self):
        if hasattr(self, "image_big"):
            self.image_big.style(
                f"transform: translate({self.offset[0]}px, {self.offset[1]}px) scale({self.zoom_level})"
            )

    def reset_transform(self):
        self.zoom_level = 1.0
        self.offset = (0, 0)
        self.update_transform()

    async def get_url_file_info_async(self, url: str, timeout: int = 15):
        # 请直接复制 InteractiveButton 原有的 get_url_file_info_async 内部实现
        headers = {"User-Agent": "Mozilla/5.0"}
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        auth = BasicAuth(SVN_USERNAME, SVN_PASSWORD) if SVN_USERNAME and SVN_PASSWORD else None
        try:
            async with httpx.AsyncClient(follow_redirects=False, verify=ssl_context, auth=auth) as client:
                async with client.stream("GET", url, timeout=timeout, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        return False, None
                    if response.status_code < 400:
                        ct = response.headers.get("Content-Type")
                        return True, ct.split(";")[0].strip() if ct else None
                    return False, None
        except Exception:
            return False, None

    async def get_svn_file_http_async(self, http_url: str, username: str = "", password: str = ""):
        auth = BasicAuth(username, password) if username and password else None
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            async with httpx.AsyncClient(follow_redirects=True, verify=ssl_context, auth=auth) as client:
                response = await client.get(http_url, auth=auth, timeout=10)
                response.raise_for_status()
                return http_url.split("/")[-1], response.content
        except Exception:
            return None, None

    async def check_and_download_svn(self, http_url, file_name):
        storage_key = f"downloaded_{file_name}"
        has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')
        if has_downloaded:
            self.check_down_dialog.clear()
            with self.check_down_dialog, ui.card().classes("min-w-[400px]"):
                with ui.card_section():
                    ui.label(f'文件 "{file_name}" 已下载。').classes("text-lg font-medium")
                with ui.card_actions().props("align=right"):
                    ui.button(
                        "仍要重新下载",
                        on_click=lambda: self.trigger_download_svn_async(
                            http_url, file_name, self.check_down_dialog.close
                        ),
                        color="primary",
                    )
                    ui.button("关闭", on_click=self.check_down_dialog.close, color="grey")
            self.check_down_dialog.open()
        else:
            await self.trigger_download_svn_async(http_url, file_name)
            await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

    async def trigger_download_svn_async(self, http_url, file_name, on_finish=None):
        _, content = await self.get_svn_file_http_async(http_url, username=SVN_USERNAME, password=SVN_PASSWORD)
        if content:
            ui.download(content, file_name)
            ui.notify(
                f"已开始下载: {file_name}",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )
            if on_finish:
                on_finish()

    async def open_svn_pdf_in_browser(self, http_url, file_name):
        """修复 4.3: 恢复拉取前与打开后的交互提示"""
        ui.notify(
            f"正在从 SVN 准备预览 {file_name}...",
            type="info",
            position="bottom",
            timeout=2000,
            progress=True,
            # multi_line=True,
            close_button="✖",
        )

        _, pdf_bytes = await self.get_svn_file_http_async(http_url, username=SVN_USERNAME, password=SVN_PASSWORD)

        if pdf_bytes:
            client_id = ui.context.client.id
            PDF_PREVIEW_CACHE[client_id] = pdf_bytes
            cache_buster = int(time.time())
            ui.run_javascript(f'window.open("/view/svn_pdf?id={client_id}&v={cache_buster}", "_blank");')

            ui.notify(
                f"已在新标签页中打开: {file_name}",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                # multi_line=True,
                close_button="✖",
            )

    # ================= 需要将 self.xxx 替换为 config 的方法 =================
    def _setup_svn_chip_dialog(self):
        self.chip_dialog.clear()
        config = self.current_config
        with self.chip_dialog, ui.card().classes("w-1/2"):
            ui.label(f"添加SVN文件引用: {config['title']}").classes("text-lg font-bold text-blue-900")
            self.chip_label = (
                ui.input(
                    label=config.get("dialog_label", "填入包括后缀的完整文件名"),
                    value=config.get("dialog_placeholder", ""),
                    placeholder=config.get("dialog_placeholder", ""),
                )
                .props("outlined")
                .classes("w-full")
            )
            self.chip_notes = (
                ui.textarea(
                    label="注释（必填）",
                    placeholder="首填/变更原因",
                    validation={"不能空白": lambda v: v.strip() != ""},
                )
                .props("outlined")
                .classes("w-full")
            )
            with ui.row().classes("w-full justify-end items-center"):
                ui_spinner = ui.spinner(type="hourglass", size="md", color="amber-8", thickness=8.0)
                ui_spinner.set_visibility(False)
                ui.button("添加", on_click=lambda e: self._add_svn_chip_data(ui_spinner, btn=e.sender))
        self.chip_dialog.open()

    async def _add_svn_chip_data(self, ui_spinner, btn=None):
        # 主内容填写“无”等无效内容情况，转交纯文本方式处理
        if self.chip_label.value.strip() in IGNORE_STR:
            await self._add_text_chip_data(ui_spinner, btn)
        else:
            if btn:
                btn.disable()  # 1. 进门立刻禁用按钮，防止连点
            try:
                config = self.current_config
                text = self.chip_label.value.strip()
                notes = self.chip_notes.value.strip()
                project_state = app.storage.general["project_summary"][self.project]["state"]
                warehouse = config.get("state_path", {}).get(project_state)
                file_info = (False, None)
                # 如果填写内容有正则表达式管控，则分析内容是否符合规则
                regular_bool = False
                if config.get("content_regular", []):
                    for regular in config.get("content_regular", []):
                        if re.search(regular, text):
                            regular_bool = True
                else:
                    regular_bool = True
                if not regular_bool:
                    ui.notify(
                        "内容不符合填写格式规范!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return
                if not text or not notes:
                    ui.notify(
                        "引用文件名和注释不能为空!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                if (text, warehouse) in [
                    (d["content"], d.get("warehouse"))
                    for d in db_storage.get_deep_item([f"{self.project}_over_data", config["label"]], {}).values()
                ]:
                    ui.notify(
                        f"{warehouse}仓库下的相同引用文件名已添加过。",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    return

                ui_spinner.set_visibility(True)
                target_url_li = self._splicing_svn_file_url(text, config)

                if target_url_li and len(target_url_li) == 1:
                    target_url = target_url_li[0]
                    file_info = await self.get_url_file_info_async(target_url)
                    if not file_info[0]:
                        ui_spinner.set_visibility(False)
                        return
                elif target_url_li and len(target_url_li) > 1:
                    ui.notify(
                        "有多个路径，不合规!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        # multi_line=True,
                        close_button="✖",
                    )
                    ui_spinner.set_visibility(False)
                    return
                else:
                    ui_spinner.set_visibility(False)
                    return

                chip_id = str(uuid.uuid4())
                req_max_ver = app.storage.general["project_req_max_ver"].get(self.project, "1.0")
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")

                file_type = file_info[1]
                if (file_type == "application/octet-stream" or file_type is None) and target_url.lower().endswith(
                    ".pdf"
                ):
                    file_type = "application/pdf"
                # 获取要绑定的 row_id，如果没有（理论上现在都有了），就生成一个新的
                row_id = getattr(self, "current_target_row_id", None) or str(uuid.uuid4())
                chip_data = {
                    "id": chip_id,
                    "row_id": row_id,
                    "role": self.role,
                    "icon": "saved_search",
                    "enabled": True,
                    "bg_color": "bg-light-blue-1",
                    "type": "svn",
                    "file_type": file_type,
                    "url_path": target_url,
                    "content": text,
                    "warehouse": warehouse,
                    "notes": notes,
                    "creator": creator,
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                }

                await db_storage.set_deep_item([f"{self.project}_over_data", config["label"], chip_id], chip_data)
                ui_spinner.set_visibility(False)
                self.chip_dialog.close()
                ui.notify(
                    "SVN文件引用已添加。",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
                self._show_related_chip_select_dialog(text, True, "add_chip", config)
                await self._check_and_trigger_autofill(row_id, text, config)
            except Exception as ex:
                # 捕捉潜在的数据库写入等异常
                logger.error(f"添加概述失败: {ex}", exc_info=True)
            finally:
                if btn:
                    btn.enable()  # 3. 最终防线：无论成功、失败验证不通过还是报错，都恢复按钮状态

    def _splicing_svn_file_url(self, chip_text, config) -> list:
        return_url_li = []
        target_url_li = []
        according_folder_name = []
        according_title = ""  # --- 修复 4.2: 恢复收集依赖项的标题名称 ---

        project_state = app.storage.general["project_summary"][self.project]["state"]
        svn_main_folder = config.get("state_path", {}).get(project_state)

        if not svn_main_folder:
            if self._edit_permission_judge(config, notify=False):
                ui.notify(
                    f"该项概述，在当前项目{project_state}状态下，无相应svn管控仓库配置!",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    # multi_line=True,
                    close_button="✖",
                )
            return target_url_li

        search_folder_according_li = config.get("search_folder_according", [])
        search_scope_regular = config.get("search_scope_regular", "")
        upload_path = config.get("upload_path", "")
        search_hierarchy = config.get("search_hierarchy", [])

        if search_folder_according_li:
            for search_folder_according in search_folder_according_li:
                # 抓取中文配置标题，方便报错时精准定位
                title_str = (
                    app.storage.general.get("over_config_data_flat", {})
                    .get(search_folder_according, {})
                    .get("title", "未知项")
                )
                according_title = f"{according_title}\n{title_str}"

                for data in db_storage.get_deep_item(
                    [f"{self.project}_over_data", search_folder_according], {}
                ).values():
                    if data["enabled"]:
                        according_folder_name.append(data["content"])

            if len(according_folder_name) < 1:
                if self._edit_permission_judge(config, notify=False):
                    ui.notify(
                        f"概述项：\n{according_title}\n均无有效配置，链接无效!",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        multi_line=True,
                        close_button="✖",
                    )
                return target_url_li
            else:
                if search_scope_regular:
                    for folder_name in according_folder_name:
                        match = re.search(search_scope_regular, folder_name)
                        if match:
                            match_folder = f"{match.group(1)}-{match.group(2)}"
                            target_url_li.append(f"{upload_path}/{svn_main_folder}/{match_folder}/{folder_name}")
                        else:
                            if self._edit_permission_judge(config, notify=False):
                                ui.notify(
                                    f"文件夹{folder_name}命名不符合规则!",
                                    type="warning",
                                    position="bottom",
                                    timeout=3000,
                                    progress=True,
                                    # multi_line=True,
                                    close_button="✖",
                                )
                    if not target_url_li:
                        return target_url_li
                else:
                    for folder_name in according_folder_name:
                        target_url_li.append(f"{upload_path}/{svn_main_folder}/{folder_name}")
        else:
            if search_scope_regular:
                match = re.search(search_scope_regular, chip_text)
                if match:
                    match_folder = f"{match.group(1)}-{match.group(2)}"
                    target_url_li.append(f"{upload_path}/{svn_main_folder}/{match_folder}")
                else:
                    if self._edit_permission_judge(config, notify=False):
                        ui.notify(
                            f"文件{chip_text}命名不符合规则!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            # multi_line=True,
                            close_button="✖",
                        )
                    return target_url_li
            else:
                target_url_li.append(f"{upload_path}/{svn_main_folder}")

        for target_url in target_url_li:
            if search_hierarchy:
                for h in search_hierarchy:
                    target_url = f"{target_url}/{h}"
            return_url_li.append(f"{target_url}/{chip_text}")
        return return_url_li


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
