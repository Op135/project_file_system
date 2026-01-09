# -*- encoding: utf-8 -*-
import copy
import json
import logging
import os
from datetime import datetime

from nicegui import app, ui

from src.tools.etendue_calculator import EtendueCalculator

from .. import db_storage
from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR, REQ_REMOVE_DIR
from ..utils import (
    get_cache_busted_path,
    logout,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/tool")
def tool_page():
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    dialog = ui.dialog().props("persistent").classes("")

    # 获取用户信息
    current_user = app.storage.user.get("current_user")
    is_admin = app.storage.user.get("is_admin")
    current_role = app.storage.user.get("current_role")

    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

    # --- 1. 定义打开计算器的函数 ---
    def open_etendue_calculator():
        # [修改点 1]：增加 maximized 属性实现全屏，并添加滑入滑出动画
        with ui.dialog().props("maximized transition-show=slide-up transition-hide=slide-down") as dialog:
            # [修改点 2]：卡片类改为 w-full h-full，移除圆角和边框限制
            with ui.card().classes("w-full h-full p-0 gap-0"):
                calc = EtendueCalculator()
                calc.show(dialog)
        dialog.open()

    # 主界面
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("分析工具").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.menu_item("关闭菜单", menu.close)
    # --- 2. 页面主体内容 ---
    with ui.row().classes("w-full p-6"):
        ui.label("研发辅助工具箱").classes("text-2xl font-bold text-gray-700 w-full mb-4")

        # --- 工具卡片：光学计算器 ---
        with (
            ui.card()
            .classes(
                "w-64 h-40 hover:shadow-lg hover:border-blue-500 transition-all cursor-pointer flex flex-col items-center justify-center gap-2 border-2 border-transparent"
            )
            .on("click", open_etendue_calculator)
        ):  # <--- 关键修改：点击卡片触发
            ui.icon("calculate", size="48px").classes("text-blue-500")
            ui.label("Etendue 极限计算器").classes("font-bold text-lg text-gray-700")
            ui.label("光源/光纤耦合效率估算").classes("text-xs text-gray-400")
