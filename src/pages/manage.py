# -*- encoding: utf-8 -*-
from nicegui import app, ui

from ..config import IMG_DIR
from ..utils import logout, updata_overview_config, update_config_service, update_users_data


@ui.page("/manage")
def manage_page():
    # 管理员管理界面
    if app.storage.user.get("current_user") != "admin":
        ui.navigate.to("/main")  # 如果不是管理员，跳转到主界面
        return
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("系统管理员界面").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(
                app.storage.general.get("user_preferences", {})
                .get(app.storage.user.get("current_user"), {})
                .get("avatar", f"{IMG_DIR}/avatars/avatar1.png")
            )
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)
    with ui.column().classes("w-full h-[90vh] -space-y-2"):
        ui.button("更新需求配置文件", on_click=lambda: update_config_service()).props("").classes("")
        ui.button("更新概述项配置文件", on_click=lambda: updata_overview_config()).props("").classes("")
        ui.button("更新用户配置数据", on_click=lambda: update_users_data()).props("").classes("")
