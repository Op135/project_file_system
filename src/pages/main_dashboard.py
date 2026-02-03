# -*- encoding: utf-8 -*-
import logging

from nicegui import app, ui

from ..config import IMG_DIR, PRESET_AVATARS
from ..utils import get_cache_busted_path, get_project_engineer_project_list_dic, logout, online_users

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/main")
def main_page():
    online_data = {"online_count": "", "online_users": [], "tooltip_text": ""}

    def refresh_online_num():
        # --- 新增去重逻辑 ---
        # 使用一个临时字典，以 username 为 Key 来存储用户信息
        # 这样同一个 username 无论开多少个网页，在这个字典里只会保留一份
        unique_users_map = {}

        for user_data in online_users.values():
            username = user_data.get("username", "未知用户")
            # 如果该用户还没被记录，或者你希望更新到最新的连接信息，就存入
            if username not in unique_users_map:
                unique_users_map[username] = user_data

        # --- 更新统计数据 ---

        # 1. 数量：计算去重后的字典长度
        online_data["online_count"] = str(len(unique_users_map))

        # 2. Tooltip 文本：基于去重后的数据生成
        tooltip_text = ""
        for user in unique_users_map.values():
            # 这里的 user 已经是去重后的单条数据了
            u_name = user.get("username", "未知用户")
            u_ip = user.get("ip", "未知IP")
            u_time = user.get("login_time", "未知时间")
            tooltip_text += f"{u_name} - {u_time}<br>"

        online_data["tooltip_text"] = tooltip_text

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
    menu_items_metadata = [
        ("assignment", "项目资料", "录入与查看项目资料", "/project_table"),
        ("rule", "项目待办项", "查阅项目相关待办项", "/information"),
        ("handyman", "分析工具", "提供用于专业分析计算的工具", "/tool"),
        ("account_tree", "需求项结构", "查阅需求项结构", "/question_tree_tabs"),
    ]
    menu_items = []
    for items in menu_items_metadata:
        # 需求结构图只对角色字符串里含有如下关键字的用户展示
        if items[3] == "/question_tree_tabs" and not any(
            k in str(current_role) for k in ["销售", "研发", "boss", "admin"]
        ):
            continue
        menu_items.append(items)

    # 主界面
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("百炼光研发项目文件管理系统").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        # --- 【核心代码】在线人数 酷炫胶囊组件 ---
        # 使用 row 布局，items-center 垂直居中
        # bg-white/20: 白色背景，20%不透明度 (透出底下的蓝)
        # rounded-full: 胶囊圆角
        # shadow-sm: 轻微阴影增加层次感
        # backdrop-blur-sm: 磨砂玻璃效果 (可选，浏览器支持时更酷)
        with ui.row().classes(
            "absolute right-20 items-center gap-2 bg-white/10 px-3 py-1.5 rounded-full shadow-sm text-white ml-4 transition-all hover:bg-white/20 cursor-default"
        ):
            # 1. 动态呼吸灯 (animate-pulse 是 Tailwind 自带动画)
            # 这是一个绿色的圆点，一直在"呼吸"
            with ui.element("div").classes("relative flex h-3 w-3"):
                ui.element("span").classes(
                    "animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"
                )
                ui.element("span").classes("relative inline-flex rounded-full h-3 w-3 bg-green-500")
            # 2. 图标
            ui.icon("groups", size="xs").classes("opacity-90")
            # 3. 数字显示
            # 使用 bind_text 绑定数据，实现实时更新
            label = ui.label().bind_text_from(online_data, "online_count", backward=lambda x: f"在线: {x}")
            label.classes("text-sm font-medium tracking-wide")
            with label:
                with ui.tooltip("在线用户列表"):
                    ui.html(sanitize=False).bind_content_from(online_data, "tooltip_text")

        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.menu_item("用户信息", on_click=lambda: ui.navigate.to("/profile"))
                if current_user == "admin":
                    ui.separator().props("size=1px")
                    ui.menu_item("系统管理", on_click=lambda: ui.navigate.to("/manage"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # 使用 ui.grid 创建一个响应式的网格布局
    # a-classes: 应用于所有子元素的通用样式
    # b-classes: 应用于特定子元素的样式 (这里没用，但可以写 b-col-6 c-col-4 等)
    with ui.column().classes("w-full h-[calc(100vh-5rem)] items-center justify-center"):
        num = min(4, len(menu_items))
        with ui.grid(columns=num).classes("w-[calc(70vw)] gap-4"):
            # 所有非已审项目数量
            state_num_sum = 0
            # 所有登录用户提交的非已审项目数量
            state_num_user = 0
            # 所有登录用户负责的概述维护项目数量
            over_charge_num = 0
            # {项目工程师名:[负责项目,负责项目]}
            project_engineer_dic = get_project_engineer_project_list_dic()
            for project_name, ver_dic in app.storage.general["wait_review"].items():
                for ver, dic in ver_dic.items():
                    state = dic.get("state")
                    submitter = dic.get("submitter")
                    if state != "已审":
                        state_num_sum += 1
                        # 待审项目提交人与当前用户匹配 或 待审项目的项目工程师由当前用户负责跟进
                        if submitter == current_user or project_name in project_engineer_dic.get(current_user, []):
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

    # --- 定时刷新在线用户数据 ---
    # 每 3 秒检查一次全局字典，更新UI
    # 这样如果有用户下线或上线，管理员在3秒内就能看到变化
    ui.timer(3.0, refresh_online_num)
