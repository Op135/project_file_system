# -*- encoding: utf-8 -*-
from nicegui import app, ui

from ..config import IMG_DIR, PRESET_AVATARS
from ..utils import (
    get_cache_busted_path,
    logout,
    project_overview_config_update,
    project_summary_update,
    updata_overview_config,
    update_config_service,
    update_users_data,
)


@ui.page("/manage")
def manage_page():
    # 管理员管理界面
    if app.storage.user.get("current_user") != "admin":
        ui.navigate.to("/main")  # 如果不是管理员，跳转到主界面
        return
    current_user = app.storage.user.get("current_user")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("系统管理员界面").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.menu_item("关闭菜单", menu.close)
    with ui.column().classes("w-full h-[90vh] -space-y-2"):
        ui.button("从excel更新需求配置文件到json", on_click=lambda: update_config_service()).props("").classes("")
        ui.button("从json更新概述项配置数据到服务器内存", on_click=lambda: updata_overview_config()).props("").classes(
            ""
        )
        ui.button("从json更新用户配置数据到服务器内存", on_click=lambda: update_users_data()).props("").classes("")
        ui.button("从json更新项目列表(新增项目)到服务器general储存", on_click=lambda: project_summary_update()).props(
            ""
        ).classes("")
        ui.button(
            "从json更新项目表滚动信息关联配置到服务器general储存", on_click=lambda: project_overview_config_update()
        ).props("").classes("")
