# -*- encoding: utf-8 -*-
from nicegui import app, ui

from ..config import IMG_DIR  # 用于获取 IMG_DIR
from ..utils import logout

# 步骤 1: 从我们重构的 login.py 中导入可重用的函数
from .login import create_password_dialog

# config.IMG_DIR 是在 config.py 中定义的绝对路径
PRESET_AVATARS = [
    f"{IMG_DIR}/avatars/avatar1.png",
    f"{IMG_DIR}/avatars/avatar2.png",
    f"{IMG_DIR}/avatars/avatar3.png",
    f"{IMG_DIR}/avatars/avatar4.png",
    f"{IMG_DIR}/avatars/avatar5.png",
    f"{IMG_DIR}/avatars/avatar6.png",
    f"{IMG_DIR}/avatars/avatar7.png",
    f"{IMG_DIR}/avatars/avatar8.png",
    f"{IMG_DIR}/avatars/avatar9.png",
    f"{IMG_DIR}/avatars/avatar10.png",
    f"{IMG_DIR}/avatars/avatar11.png",
    f"{IMG_DIR}/avatars/avatar12.png",
    f"{IMG_DIR}/avatars/avatar13.png",
    f"{IMG_DIR}/avatars/avatar14.png",
    f"{IMG_DIR}/avatars/avatar15.png",
]


@ui.page("/profile")
def user_profile_page():
    # 1. 验证用户是否登录
    if not (current_user := app.storage.user.get("current_user")):
        ui.navigate.to("/login")
        return

    # 2. 从全局存储中获取用户当前的头像设置
    # (我们将在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个

    # 3. 定义头像更新函数
    def set_avatar(avatar_path: str):
        # setdefault 确保字典键存在
        app.storage.general["user_preferences"].setdefault(current_user, {})
        # 更新全局存储
        app.storage.general["user_preferences"][current_user]["avatar"] = avatar_path
        # 更新页面上显示的头像
        current_avatar_display.set_source(avatar_path)
        ui.notify("头像已更新")

    # --- 页面 UI 布局 ---
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("用户信息管理").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(
                app.storage.general.get("user_preferences", {})
                .get(app.storage.user.get("current_user"), {})
                .get("avatar", f"{IMG_DIR}/avatars/avatar1.png")
            )
            with ui.menu().props("auto-close flex-nowrap") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)

    with ui.column().classes("w-full max-w-md mx-auto p-4 gap-4"):
        # --- 头像选择 ---
        ui.label("当前头像").classes("text-xl font-semibold")

        # ui.image 会自动处理本地文件路径的伺服
        current_avatar_display = ui.image(current_avatar_path).classes(
            "w-32 h-32 rounded-full self-center ring-4 ring-blue-500"
        )

        ui.separator()

        ui.label("选择新头像").classes("text-xl font-semibold")
        with ui.row().classes("gap-2 flex-wrap justify-center"):
            for avatar_path in PRESET_AVATARS:
                ui.image(avatar_path).classes(
                    "w-16 h-16 rounded-full cursor-pointer hover:ring-4 hover:ring-blue-300"
                ).on("click", lambda _, path=avatar_path: set_avatar(path))  # 关键：使用 lambda 捕获正确的 path

        ui.separator()

        # --- 密码修改 ---
        ui.label("账户安全").classes("text-xl font-semibold")

        # 步骤 2: 点击按钮，直接调用导入的函数
        ui.button("修改密码", on_click=lambda: create_password_dialog(current_user)).props("icon=lock")
