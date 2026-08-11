# -*- encoding: utf-8 -*-
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict  # 引入类型提示，便于静态类型检查

from nicegui import app, ui

from .. import db_storage  # 导入我们创建的模块
from ..config import IMG_DIR, PRESET_AVATARS
from ..ecn_management_config import ECN_DATA_KEY, get_ecn_dashboard_pending_count
from ..overview_warning import get_urgent_overview_projects
from ..utils import (
    get_cache_busted_path,
    get_project_engineer_project_list_dic,
    logout,
    online_users,
    setup_global_activity_tracking,
    sync_current_user_role,
)
from .design_knowledge import DESIGN_KNOWLEDGE_DATA_KEY, get_design_knowledge_dashboard_pending_count
from .error_management import ERROR_DATA_KEY, get_error_dashboard_pending_count
from .sample_issue_collection import SAMPLE_ISSUE_DATA_KEY, get_sample_dashboard_pending_count
from .sample_order_dashboard import (
    get_all_sample_order_records,
    get_sample_order_dashboard_pending_count,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/main")
def main_page():
    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()
    ui.add_head_html("""
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

            @keyframes dashboard-urgent-flash {
                0%, 100% { opacity: 1; transform: scale(1); }
                50% { opacity: 0.35; transform: scale(1.08); }
            }

            @keyframes dashboard-urgent-red-glow {
                0%, 100% { box-shadow: 0 0 0 rgba(239, 68, 68, 0.15); }
                50% { box-shadow: 0 0 22px rgba(239, 68, 68, 0.55); }
            }

            @keyframes dashboard-urgent-violet-glow {
                0%, 100% { box-shadow: 0 0 0 rgba(124, 58, 237, 0.2); }
                50% { box-shadow: 0 0 28px rgba(124, 58, 237, 0.7); }
            }

            @keyframes dashboard-urgent-nudge {
                0%, 86%, 100% { translate: 0 0; }
                88%, 92%, 96% { translate: -4px 0; }
                90%, 94%, 98% { translate: 4px 0; }
            }

            .dashboard-urgent-level-3 {
                border: 2px solid #ef4444 !important;
                animation: dashboard-urgent-red-glow 1.8s ease-in-out infinite;
            }

            .dashboard-urgent-level-4 {
                border: 2px solid #7c3aed !important;
                animation:
                    dashboard-urgent-violet-glow 1.6s ease-in-out infinite,
                    dashboard-urgent-nudge 4s ease-in-out infinite;
                will-change: translate, box-shadow;
            }

            .dashboard-urgent-flash {
                animation: dashboard-urgent-flash 1.6s ease-in-out infinite;
            }

            @media (prefers-reduced-motion: reduce) {
                .dashboard-urgent-level-3,
                .dashboard-urgent-level-4,
                .dashboard-urgent-flash {
                    animation: none;
                }
            }

            /* --- 新增：防穿透与背景底纹 --- */
            /* 1. 禁用全局 html/body 的滚动，将滚动权下放给局部容器 */
            html, body {
                overflow: hidden !important; 
                margin: 0;
                padding: 0;
                height: 100vh;
                /* 2. 浅色光晕 (Mesh Gradient / Soft Glow) */
                background-color: #f8fafc; /* 底色：极浅的高级灰蓝 */
                background-image: 
                    /* 左上角：极淡的科技蓝光晕 */
                    radial-gradient(circle at 10% 10%, rgba(224, 242, 254, 0.7) 0%, transparent 45%),
                    /* 右下角：极淡的紫蓝色光晕 */
                    radial-gradient(circle at 90% 90%, rgba(237, 233, 254, 0.6) 0%, transparent 45%);
                background-repeat: no-repeat;
                background-attachment: fixed;
            }
        </style>
    """)
    online_data = {"online_count": "", "online_users": [], "tooltip_text": ""}

    # --- 刷新在线人数的逻辑，基于绝对时间戳过滤 ---
    def refresh_online_num():
        unique_users_map: Dict[str, Any] = {}
        # 定义在线心跳阈值：1分钟（60 秒）
        ONLINE_THRESHOLD_SEC = 1 * 60
        current_time = time.time()

        for user_data in online_users.values():
            username = user_data.get("username", "未知用户")
            # last_seen_ts 是页面心跳，表示页面仍在连接；last_activity_ts 是最后一次物理操作。
            last_seen_ts = user_data.get("last_seen_ts", user_data.get("last_activity_ts", 0))
            last_activity_ts = user_data.get("last_activity_ts", 0)

            idle_time_sec = current_time - last_seen_ts

            if idle_time_sec < ONLINE_THRESHOLD_SEC:
                if username not in unique_users_map:
                    unique_users_map[username] = dict(user_data)
                    unique_users_map[username]["last_seen_ts"] = last_seen_ts
                    unique_users_map[username]["last_activity_ts"] = last_activity_ts
                else:
                    # 多标签页情况下，在线心跳和最后操作分别取最新时间，避免显示在多个标签页之间跳动。
                    unique_users_map[username]["last_seen_ts"] = max(
                        unique_users_map[username].get("last_seen_ts", 0),
                        last_seen_ts,
                    )
                    unique_users_map[username]["last_activity_ts"] = max(
                        unique_users_map[username].get("last_activity_ts", 0),
                        last_activity_ts,
                    )

        online_data["online_count"] = str(len(unique_users_map))

        tooltip_text = "当前在线用户:<br>"
        for user in unique_users_map.values():
            u_name = user.get("username", "未知用户")
            u_ts = user.get("last_activity_ts", 0)

            # 格式化时间
            if u_ts > 0:
                u_time_str = datetime.fromtimestamp(u_ts).strftime("%H:%M:%S")
            else:
                u_time_str = "未知时间"

            tooltip_text += f"{u_name} - 最后操作: {u_time_str}<br>"

        if not unique_users_map:
            tooltip_text = "当前无在线用户"

        online_data["tooltip_text"] = tooltip_text

    # 每 3 秒刷新一次 UI 显示
    ui.timer(3.0, refresh_online_num)

    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    current_user = app.storage.user.get("current_user", "匿名用户")
    current_role = sync_current_user_role()
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
        ("handyman", "分析工具", "专业分析计算工具", "/tool"),
        ("account_tree", "需求项结构", "查阅需求项结构", "/question_tree_tabs"),
        ("equalizer", "统计信息", "查阅系统统计信息", "/statistics"),
        ("published_with_changes", "工程变更", "ECR与ECN流程管理", "/ecn_management"),
        ("error", "异常单跟进", "查阅记录异常单处理进度", "/error_management"),
        ("science", "样品问题跟进", "查阅记录样品问题处理进度", "/sample_issue_collection"),
        ("fact_check", "样品订单看板", "录入并跟踪样品订单交期", "/sample_order_dashboard"),
        ("menu_book", "设计知识库", "沉淀规范与设计案例", "/design_knowledge"),
    ]
    menu_items = []
    for items in menu_items_metadata:
        # 需求结构图只对角色字符串里含有如下关键字的用户展示
        if items[3] == "/information" and not any(
            k in str(current_role) for k in ["销售", "研发", "工程", "质量", "boss", "admin"]
        ):
            continue
        # 需求结构图只对角色字符串里含有如下关键字的用户展示
        elif items[3] == "/question_tree_tabs" and not any(
            k in str(current_role) for k in ["销售", "研发", "boss", "admin"]
        ):
            continue
        # 统计信息只对角色字符串里含有如下关键字的用户展示
        elif items[3] == "/statistics" and not any(
            k in str(current_role) for k in ["总监", "经理", "主管", "boss", "admin"]
        ):
            continue
        # 异常单跟进只对角色字符串里含有如下关键字的用户展示
        elif items[3] == "/error_management" and not any(
            k in str(current_role) for k in ["质量", "销售", "工程", "研发", "boss", "admin"]
        ):
            continue
        # 样品问题跟进只对角色字符串里含有如下关键字的用户展示
        elif items[3] == "/sample_issue_collection" and not any(
            k in str(current_role) for k in ["质量", "销售", "工程", "研发", "boss", "admin"]
        ):
            continue
        # 样品问题跟进只对角色字符串里含有如下关键字的用户展示
        elif items[3] == "/design_knowledge" and not any(
            k in str(current_role) for k in ["质量", "销售", "工程", "研发", "boss", "admin"]
        ):
            continue
        # 样品单执行看板只对角色字符串里含有如下关键字的用户展示
        elif items[3] == "/sample_order_dashboard" and not any(
            k in str(current_role) for k in ["销售", "工程", "研发", "boss", "admin"]
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
            label = ui.label().bind_text_from(online_data, "online_count", backward=lambda x: f"当前在线: {x}")
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
    # 增加 h-[calc(100vh-3rem)] 严格限制高度，并增加 overflow-y-auto 开启局部滚动
    with ui.column().classes("w-full h-[calc(100vh-3rem)] overflow-y-auto items-center justify-center"):
        # 使用 Tailwind 响应式网格布局，替代原先的 calc(70vw) 和动态算列数
        # max-w-6xl 限制最大宽度，在超大屏幕下不会显得过于稀疏
        # grid-cols-1 到 xl:grid-cols-5 实现浏览器大中小窗口的自适应
        with ui.grid().classes("w-full max-w-7xl grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5 gap-6 px-6"):
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
            # 当前用户负责且达到3级以上的概述项目
            overview_urgent_projects: list[tuple[str, int]] = []
            # 所有登录用户负责的概述变更任务数量
            change_task_count = 0
            # ECN 待办统一按具体审批人、申请人或影响处理人计算。
            ecn_pending_num_user = get_ecn_dashboard_pending_count(
                db_storage.get_item(ECN_DATA_KEY, {}),
                current_user,
                current_role,
            )
            # 异常模块待办：普通角色按待处理异常单计数，研发经理按待审批延期申请计数。
            error_pending_num_user = get_error_dashboard_pending_count(
                db_storage.get_item(ERROR_DATA_KEY, {}),
                current_user,
                current_role,
            )
            sample_issue_pending_num_user = get_sample_dashboard_pending_count(
                db_storage.get_item(SAMPLE_ISSUE_DATA_KEY, {}),
                current_user,
                current_role,
            )
            sample_order_pending_num = get_sample_order_dashboard_pending_count(
                get_all_sample_order_records(),
                current_role=current_role,
            )
            design_knowledge_pending_num_user = get_design_knowledge_dashboard_pending_count(
                db_storage.get_item(DESIGN_KNOWLEDGE_DATA_KEY, {}),
                current_user,
                current_role,
            )
            # {项目工程师名:[负责项目,负责项目]}
            project_engineer_dic = get_project_engineer_project_list_dic()
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

            # --- 仅针对项目概述待办进行状态过滤 ---
            project_summary = app.storage.general.get("project_summary", {})
            overview_pending_by_user = app.storage.general.get("overview_charge_pending", {})
            current_overview_pending = overview_pending_by_user.get(current_user, {})
            if current_overview_pending:
                # 仅统计状态非“作废”且非“待定”的项目，确保与 information.py 逻辑一致
                over_charge_num = sum(
                    1
                    for p_name in current_overview_pending
                    if project_summary.get(p_name, {}).get("state", "未知") not in ["作废", "待定"]
                )
                overview_project_states = {
                    project_name: project_summary.get(project_name, {}).get("state", "未知")
                    for project_name in current_overview_pending
                }
                overview_urgent_projects = get_urgent_overview_projects(
                    current_overview_pending,
                    overview_project_states,
                )

            # 销售角色的项目待办卡片不包含概述维护任务，避免显示无关紧急提示
            if current_role in ["销售", "销售总监"]:
                overview_urgent_projects = []

            overview_urgent_count = len(overview_urgent_projects)
            overview_max_warning_level = max(
                (warning_level for _, warning_level in overview_urgent_projects),
                default=-1,
            )

            change_reqs = app.storage.general.get("overview_change_requests", {})

            if current_role == "研发经理":
                # 经理统计所有待审批(pending)
                change_task_count = sum(1 for r in change_reqs.values() if r["status"] == "pending")
            else:
                # 普通用户统计自己被驳回或撤回需要处理的任务
                change_task_count = sum(
                    1
                    for r in change_reqs.values()
                    if r["submitter"] == current_user and r["status"] in ["rejected", "withdrawn"]
                )

            for icon, title, subtitle, target in menu_items:
                # 1. 预先计算该模块的待办数量 (Logic Pre-calculation)
                #    这样我们可以根据数量来决定图标的颜色
                pending_count = 0
                if target == "/information":
                    # 根据当前用户角色判断统计口径
                    if current_role in ["研发经理"]:
                        # 经理看到的是所有待审项目数量 + 自己负责的概述
                        pending_count = pending_num_sum + over_charge_num + change_task_count
                    elif current_role in ["销售", "销售总监"]:
                        # 销售看到的是自己提交的待修改项目数量
                        pending_count = revise_num_user
                    else:
                        # 其他人看到的是自己负责审核的待审项目数量（项目工程师才有） + 自己负责的概述
                        pending_count = pending_num_user + over_charge_num + change_task_count
                elif target == "/ecn_management":
                    # 将算出的 ECN 待办数量赋给这个卡片
                    pending_count = ecn_pending_num_user
                elif target == "/error_management":
                    pending_count = error_pending_num_user
                elif target == "/sample_issue_collection":
                    pending_count = sample_issue_pending_num_user
                elif target == "/sample_order_dashboard":
                    pending_count = sample_order_pending_num
                elif target == "/design_knowledge":
                    pending_count = design_knowledge_pending_num_user

                # 2. 定义动态样式 (Dynamic Styling)
                #    如果有待办，图标变黄；否则保持原本的蓝色
                #    text-orange-500: 警示色
                #    text-blue-500: 正常色
                icon_color_class = "text-red-500 animate-pulse" if pending_count > 0 else "text-blue-500"
                # 为图标底座准备一个极淡的背景色
                icon_bg_class = "bg-red-50" if pending_count > 0 else "bg-blue-50"
                card_urgent_class = ""
                if target == "/information" and overview_max_warning_level >= 4:
                    icon_color_class = "text-violet-600 animate-pulse"
                    icon_bg_class = "bg-violet-100"
                    card_urgent_class = "dashboard-urgent-level-4"
                elif target == "/information" and overview_max_warning_level >= 3:
                    card_urgent_class = "dashboard-urgent-level-3"

                # 3. 渲染卡片 (【修改重点】增加大圆角、软阴影、悬浮抬升和过渡动画)
                with ui.card().classes(
                    "relative flex flex-col items-center justify-center p-6 -space-y-2 cursor-pointer bg-white/90 backdrop-blur-sm "
                    "rounded-xl "
                    "shadow-[0_6px_20px_-4px_rgba(0,0,0,0.06)] "
                    "hover:shadow-[0_12px_30px_-6px_rgba(0,0,0,0.15)] "
                    f"hover:-translate-y-1.5 transition-all duration-300 ease-out {card_urgent_class}"
                ) as card:
                    card.on("click", lambda _, t=target: ui.navigate.to(t))

                    # 【修改】图标被包裹在一个带有圆角的色块底座中，视觉重心更稳
                    with ui.element("div").classes(f"p-4 rounded-xl mb-3 {icon_bg_class}"):
                        ui.icon(icon).classes(f"text-5xl {icon_color_class}")

                    # 【修改】标题文字加粗，颜色加深，使其更锐利
                    ui.label(title).classes("text-xl font-bold text-gray-800")
                    ui.label(subtitle).classes("text-center text-gray-500 text-sm mt-2")

                    if target == "/information" and overview_urgent_count > 0:
                        if overview_max_warning_level >= 4:
                            urgent_text = f"重要警示 · {overview_urgent_count}个待办"
                            urgent_classes = "bg-violet-100/40 text-violet-800 border-violet-300/50"
                        else:
                            urgent_text = f"尽快处理 · {overview_urgent_count}个待办"
                            urgent_classes = "bg-red-100/40 text-red-800 border-red-300/50"

                        with ui.element("div").classes("absolute bottom-10 left-1/2 z-10 w-max -translate-x-1/2"):
                            with ui.element("div").classes(
                                f"dashboard-urgent-flash inline-flex items-center gap-1.5 whitespace-nowrap "
                                f"px-3 py-1 rounded-full border shadow-sm backdrop-blur-xs "
                                f"text-base font-bold {urgent_classes}"
                            ):
                                ui.icon("notification_important").classes("text-base")
                                ui.label(urgent_text)
                                # with ui.tooltip().classes("bg-gray-800 text-white p-2"):
                                #     ui.label(f"最高警示级别：{overview_max_warning_level}级").classes("font-bold")
                                #     for project_name, warning_level in overview_urgent_projects[:3]:
                                #         ui.label(f"{warning_level}级 · {project_name}")
                                #     remaining_count = overview_urgent_count - 3
                                #     if remaining_count > 0:
                                #         ui.label(f"另有{remaining_count}个紧急项目")

                    # 4. 渲染增强后的 Badge (红点)
                    if pending_count > 0:
                        # 【修改】微调了位置，并去除了 transparent，让红点更饱满
                        ui.badge(str(pending_count), color="red").props("floating rounded").classes(
                            "animate-shake ring-2 ring-white shadow-md text-xs font-bold px-2 top-0 right-0 transform translate-x-1/3 -translate-y-1/3"
                        )
