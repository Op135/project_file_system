# -*- encoding: utf-8 -*-
from nicegui import app, ui

from ..config import IMG_DIR
from ..utils import logout


@ui.page("/main")
def main_page():
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return

    # 定义导航项目
    # 格式：(图标, 标题, 描述, 目标路径)
    menu_items = [
        ("assignment", "正式项目", "录入与查看正式项目信息", "/project_table"),
        ("history_edu", "临时项目", "录入与查看临时项目信息", "/main"),
        # ("manage_accounts", "XX", "XX", "/main"),
        ("insert_chart", "状态图表", "查阅项目相关消息与统计图表", "/information"),
    ]

    # 主界面
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("百炼光研发管理系统").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(
                app.storage.general.get("user_preferences", {})
                .get(app.storage.user.get("current_user"), {})
                .get("avatar", f"{IMG_DIR}/avatars/avatar1.png")
            )
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.menu_item("用户信息", on_click=lambda: ui.navigate.to("/profile"))
                ui.menu_item("注销登录", on_click=lambda: logout())
                if app.storage.user.get("current_user") == "admin":
                    ui.separator().props("size=1px")
                    ui.menu_item("系统管理", on_click=lambda: ui.navigate.to("/manage"))
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)

    # 使用 ui.grid 创建一个响应式的网格布局
    # a-classes: 应用于所有子元素的通用样式
    # b-classes: 应用于特定子元素的样式 (这里没用，但可以写 b-col-6 c-col-4 等)
    with ui.column().classes("w-full h-[calc(100vh-5rem)] items-center justify-center"):
        with ui.grid(columns=3).classes("w-[calc(70vw)] gap-4 h-[calc(30vh)]"):
            for icon, title, subtitle, target in menu_items:
                # 每个功能模块都用一个 ui.card 包裹
                with ui.card().classes(
                    "flex flex-col items-center justify-center cursor-pointer "
                    "hover:shadow-xl hover:-translate-y-1 transition-all duration-300 ease-in-out"
                ) as card:
                    # 设置点击事件，导航到指定页面
                    # 当点击发生时，GenericEventArguments 对象被赋值给 _ 因为我们不需要处理这个点击事件对象，所以不关心它
                    card.on("click", lambda _, t=target: ui.navigate.to(t))

                    # 大图标
                    ui.icon(icon).classes("text-5xl text-blue-500 mb-4")
                    # 模块标题
                    ui.label(title).classes("text-xl font-semibold")
                    # 模块描述
                    ui.label(subtitle).classes("text-center text-gray-500 text-sm mt-1")
