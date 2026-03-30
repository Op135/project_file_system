# -*- encoding: utf-8 -*-
import logging
from typing import Any, Dict  # 引入类型提示，便于静态类型检查

from nicegui import app, ui

from .. import db_storage  # 导入我们创建的模块
from ..config import ECN_SCHEME_WRITER_ROLES, IMG_DIR, PRESET_AVATARS, ECNState
from ..utils import (
    get_cache_busted_path,
    get_project_engineer_project_list_dic,
    logout,
    online_users,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/main")
def main_page():
    ui.add_head_html("""
        <script>
            // 记录页面加载时的初始时间戳
            window.lastActivityTime = Date.now();
            const updateActivity = () => { window.lastActivityTime = Date.now(); };
            
            // 监听真实的物理交互事件（鼠标、键盘、滚动、触屏）
            ['mousedown', 'mousemove', 'keydown', 'scroll', 'touchstart'].forEach(evt =>
                document.addEventListener(evt, updateActivity, {passive: true})
            );
        </script>
        <style>
            @keyframes hard-shake {
                0% { transform: translateX(0); }
                20% { transform: translateX(-2px) rotate(-3deg); }
                40% { transform: translateX(2px) rotate(3deg); }
                55% { transform: translateX(-1px) rotate(-2deg); }
                70% { transform: translateX(1px) rotate(2deg); }
                80% { transform: translateX(-1px) rotate(-1deg); }
                90% { transform: translateX(1px) rotate(1deg); }
                100% { transform: translateX(0); }
            }
            .animate-shake {
                animation: hard-shake 1.0s ease-in-out infinite; /* n秒循环一次 */
            }
        </style>
    """)
    online_data = {"online_count": "", "online_users": [], "tooltip_text": ""}

    # --- 新增：心跳上报机制 ---
    async def report_heartbeat() -> None:
        """定时向后端同步当前客户端的空闲时间"""
        try:
            # ui.run_javascript: NiceGUI (基于 Vue/Quasar) 提供的在客户端浏览器异步执行 JavaScript 代码并获取返回结果的函数。
            # 这里用于获取自上次用户交互后经过的毫秒数。
            idle_time_ms = await ui.run_javascript("return Date.now() - window.lastActivityTime;", timeout=2.0)

            if idle_time_ms is not None:
                # 遍历全局 online_users 字典，将当前用户的 idle_time 写入
                # 注意：如果同一用户开了多个标签页，此操作会更新该用户在系统中的最新活跃状态
                for client_id, user_data in online_users.items():
                    if user_data.get("username") == current_user:
                        user_data["idle_time_ms"] = idle_time_ms
        except Exception as e:
            # 捕获因网络波动或页面正在跳转导致的 JS 执行超时
            logger.debug(f"用户 {current_user} 心跳状态上报超时: {e}")

    # ui.timer: NiceGUI 提供的定时器类，用于在 Asyncio 事件循环中非阻塞地周期性执行指定的函数。
    # 这里设置为每 10 秒从客户端拉取一次活跃状态。
    ui.timer(10.0, report_heartbeat)

    # --- 修改：刷新在线人数的逻辑，加入活跃阈值过滤 ---
    def refresh_online_num():
        unique_users_map: Dict[str, Any] = {}
        # 定义真实活跃阈值：5分钟（300000 毫秒）。超过此时间无键鼠动作视为挂机
        ACTIVE_THRESHOLD_MS = 5 * 60 * 1000

        for user_data in online_users.values():
            username = user_data.get("username", "未知用户")
            # 获取记录的空闲时间，如果尚未记录过，默认为 0（视为刚进入页面）
            idle_time = user_data.get("idle_time_ms", 0)

            # 仅统计真实在操作的用户（过滤掉单纯挂机标签页）
            if idle_time < ACTIVE_THRESHOLD_MS:
                if username not in unique_users_map:
                    unique_users_map[username] = user_data
                else:
                    # 如果多标签页存在不同状态，保留最活跃（空闲时间最短）的状态
                    if idle_time < unique_users_map[username].get("idle_time_ms", float("inf")):
                        unique_users_map[username] = user_data

        online_data["online_count"] = str(len(unique_users_map))

        tooltip_text = "当前活跃用户:<br>"
        for user in unique_users_map.values():
            u_name = user.get("username", "未知用户")
            u_time = user.get("login_time", "未知时间")
            tooltip_text += f"{u_name} - {u_time}<br>"

        if not unique_users_map:
            tooltip_text = "当前无活跃用户"

        online_data["tooltip_text"] = tooltip_text

    # 每 3 秒刷新一次 UI 显示
    ui.timer(3.0, refresh_online_num)

    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    current_user = app.storage.user.get("current_user", "匿名用户")
    current_role = app.storage.user.get("current_role", "未知角色")
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
        ("equalizer", "统计信息", "查阅系统统计信息", "/statistics"),
        ("published_with_changes", "工程变更", "ECR与ECN流程管理", "/ecn_management"),
    ]
    menu_items = []
    for items in menu_items_metadata:
        # 需求结构图只对角色字符串里含有如下关键字的用户展示
        if items[3] == "/question_tree_tabs" and not any(
            k in str(current_role) for k in ["销售", "研发", "boss", "admin"]
        ):
            continue
        # elif items[3] == "/ecn_management" and not any(k in str(current_role) for k in ["研发经理", "admin"]):
        #     continue
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
            label = ui.label().bind_text_from(online_data, "online_count", backward=lambda x: f"活跃在线: {x}")
            label.classes("text-sm font-medium tracking-wide")
            with label:
                with ui.tooltip(""):
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
            # 所有待修改项目数量
            revise_num_sum = 0
            # 所有登录用户提交的待修改项目数量
            revise_num_user = 0
            # 所有待审项目数量
            pending_num_sum = 0
            # 所有登录用户负责审核的待审核项目数量
            pending_num_user = 0
            # 所有登录用户负责的概述维护项目数量
            over_charge_num = 0
            # --- 新增：统计工程变更 (ECN) 的待办数量 ---
            ecn_pending_num_user = 0
            # {项目工程师名:[负责项目,负责项目]}
            project_engineer_dic = get_project_engineer_project_list_dic()
            # 假设你的 ECN 数据统一存在 db_storage 的 ecn_management_data 键下
            # 注意：实际使用时如果觉得每次在主页拉取全量 ECN 较慢，可以像 overview 那样做个概要缓存，这里按直接读取演示
            all_ecns = db_storage.get_item("ecn_management_data", {})
            for ecn_id, ecn_data in all_ecns.items():
                workflow = ecn_data.get("workflow", {})
                basic_info = ecn_data.get("basic_info", {})
                current_state = workflow.get("current_state")
                pending_roles = workflow.get("pending_roles", [])
                applicant = basic_info.get("applicant")

                # 1. 常规审批流待办：如果当前用户角色在待审批角色列表中
                if current_role in pending_roles:
                    ecn_pending_num_user += 1

                # 2. 项目工程师专属待办：遇到动态角色且当前用户是目标项目的负责人
                # elif "PROJECT_ENGINEER" in pending_roles:
                #     target_projects = ecn_data.get("target_projects", [])
                #     for proj in target_projects:
                #         if proj in project_engineer_dic.get(current_user, []):
                #             ecn_pending_num_user += 1
                #             break  # 该单已算过，跳出当前项目遍历

                # 3. 申请人专属待办：申请被驳回 (REJECTED) 或 已撤回/草稿 (DRAFT)，需要申请人操作
                elif current_state in [ECNState.REJECTED, ECNState.DRAFT] and applicant == current_user:
                    ecn_pending_num_user += 1

                # 4. 方案编写人专属待办：处于方案设计阶段，当前用户有权限编写且尚未点击“确认完成”
                elif current_state == ECNState.ECN_SCHEMING and any(r in current_role for r in ECN_SCHEME_WRITER_ROLES):
                    # 获取参与方案编写的人员状态字典
                    participants = workflow.get("scheme_participants", {})
                    # 如果状态不是已确认 (confirmed)，则视为有待办事项
                    if participants.get(current_user) != "confirmed":
                        ecn_pending_num_user += 1
            for project_name, ver_dic in app.storage.general["wait_review"].items():
                for ver, dic in ver_dic.items():
                    state = dic.get("state")
                    submitter = dic.get("submitter")
                    if state == "待修改":
                        revise_num_sum += 1
                        # 待修改项目提交人与当前用户匹配
                        if submitter == current_user:
                            revise_num_user += 1
                    elif state == "待审":
                        pending_num_sum += 1
                        # 待审项目的项目工程师由当前用户负责跟进
                        if project_name in project_engineer_dic.get(current_user, []):
                            pending_num_user += 1

            if current_user in app.storage.general["overview_charge_pending"]:
                over_charge_num = len(app.storage.general["overview_charge_pending"][current_user].keys())

            for icon, title, subtitle, target in menu_items:
                # 1. 预先计算该模块的待办数量 (Logic Pre-calculation)
                #    这样我们可以根据数量来决定图标的颜色
                pending_count = 0
                if target == "/information":
                    # 根据当前用户角色判断统计口径
                    if current_role in ["研发经理"]:
                        # 经理看到的是所有待审项目数量 + 自己负责的概述
                        pending_count = pending_num_sum + over_charge_num
                    elif current_role in ["销售", "销售总监"]:
                        # 销售看到的是自己提交的待修改项目数量
                        pending_count = revise_num_user
                    else:
                        # 其他人看到的是自己负责审核的待审项目数量（项目工程师才有） + 自己负责的概述
                        pending_count = pending_num_user + over_charge_num
                elif target == "/ecn_management":
                    # 将算出的 ECN 待办数量赋给这个卡片
                    pending_count = ecn_pending_num_user

                # 2. 定义动态样式 (Dynamic Styling)
                #    如果有待办，图标变黄；否则保持原本的蓝色
                #    text-orange-500: 警示色
                #    text-blue-500: 正常色
                icon_color_class = "text-red-500 animate-pulse" if pending_count > 0 else "text-blue-500"

                # 3. 渲染卡片
                with ui.card().classes(
                    "flex flex-col items-center justify-center cursor-pointer "
                    "hover:shadow-xl hover:-translate-y-1 transition-all duration-300 ease-in-out"
                ) as card:
                    card.on("click", lambda _, t=target: ui.navigate.to(t))

                    # 应用动态颜色到图标
                    ui.icon(icon).classes(f"text-5xl {icon_color_class} mb-4")

                    ui.label(title).classes("text-xl font-semibold")
                    ui.label(subtitle).classes("text-center text-gray-500 text-sm mt-1")

                    # 4. 渲染增强后的 Badge
                    if pending_count > 0:
                        # color="red": 红色背景
                        # animate-pulse: 呼吸灯效果，模拟“活着”的紧迫感
                        # ring-2 ring-white: 2像素白色描边，将红点与图标视觉分离，增加体积感
                        ui.badge(str(pending_count), color="red").props("floating rounded transparent").classes(
                            "animate-shake ring-2 ring-white"
                        )

    # --- 定时刷新在线用户数据 ---
    # 每 3 秒检查一次全局字典，更新UI
    # 这样如果有用户下线或上线，管理员在3秒内就能看到变化
    ui.timer(3.0, refresh_online_num)
