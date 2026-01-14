# -*- encoding: utf-8 -*-
import logging

from nicegui import app, ui

from ..config import IMG_DIR, PRESET_AVATARS
from ..utils import get_cache_busted_path, logout

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/main")
def main_page():
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    current_user = app.storage.user.get("current_user")
    current_role = app.storage.user.get("current_role")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)
    # 定义导航项目
    # 格式：(图标, 标题, 描述, 目标路径)
    menu_items = [
        ("assignment", "项目资料", "录入与查看项目资料", "/project_table"),
        ("rule", "项目待办项", "查阅项目相关待办项", "/information"),
        ("handyman", "分析工具", "提供用于专业分析计算的工具", "/tool"),
        ("handyman", "需求树状图", "提供用于专业分析计算的工具", "/question_tree"),
    ]

    # 主界面
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("百炼光研发管理系统").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.menu_item("用户信息", on_click=lambda: ui.navigate.to("/profile"))
                if current_user == "admin":
                    ui.separator().props("size=1px")
                    ui.menu_item("系统管理", on_click=lambda: ui.navigate.to("/manage"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.menu_item("关闭菜单", menu.close)

    # 使用 ui.grid 创建一个响应式的网格布局
    # a-classes: 应用于所有子元素的通用样式
    # b-classes: 应用于特定子元素的样式 (这里没用，但可以写 b-col-6 c-col-4 等)
    with ui.column().classes("w-full h-[calc(100vh-5rem)] items-center justify-center"):
        with ui.grid(columns=3).classes("w-[calc(70vw)] gap-4 h-[calc(30vh)]"):
            # 所有非已审项目数量
            state_num_sum = 0
            # 所有登录用户提交的非已审项目数量
            state_num_user = 0
            # 所有登录用户负责的概述维护项目数量
            over_charge_num = 0
            for project_name, ver_dic in app.storage.general["wait_review"].items():
                for ver, dic in ver_dic.items():
                    state = dic.get("state")
                    submitter = dic.get("submitter")
                    if state != "已审":
                        state_num_sum += 1
                        if submitter == current_user:
                            state_num_user += 1
            if current_user in app.storage.general["overview_charge_pending"]:
                over_charge_num = len(app.storage.general["overview_charge_pending"][current_user])

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
                    if target == "/information":
                        if current_role in ["研发经理"] and (state_num_sum or over_charge_num):
                            ui.badge(str(state_num_sum + over_charge_num), color="red").props(
                                "floating rounded transparent"
                            )
                        elif state_num_user or over_charge_num:
                            ui.badge(str(state_num_user + over_charge_num), color="red").props(
                                "floating rounded transparent"
                            )
