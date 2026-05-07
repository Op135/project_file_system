# -*- encoding: utf-8 -*-
import copy
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from nicegui import app, ui

from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR, REQ_REMOVE_DIR
from ..utils import (
    get_cache_busted_path,
    logout,
    setup_global_activity_tracking,
)

# 获取 logger
logger = logging.getLogger(__name__)

# --- 新增：持久化记录逻辑 ---
# --- 持久化记录配置 ---
STATS_FILE = os.path.join(f"{BASE_DIR}/data", "daily_project_stats.xlsx")


def record_daily_stats(project_summary, pending_data):
    """
    持久化记录函数 (由 APScheduler 每日定时调用)
    处理逻辑：计算当天快照数据，并追加到 Excel 中
    """
    today = datetime.now().strftime("%Y-%m-%d")
    rows = []

    for user, p_dict in pending_data.items():
        stats_map = {}
        for proj, issues in p_dict.items():
            state = project_summary.get(proj, {}).get("state", "未知")
            if state not in stats_map:
                stats_map[state] = {"缺必填": 0, "有待定": 0, "缺需填": 0}

            issue_types = set(issues.values())
            if "缺必填" in issue_types:
                stats_map[state]["缺必填"] += 1
            if "有待定" in issue_types:
                stats_map[state]["有待定"] += 1
            if "缺需填" in issue_types:
                stats_map[state]["缺需填"] += 1

        for state, counts in stats_map.items():
            rows.append(
                {
                    "日期": today,
                    "用户": user,
                    "项目状态": state,
                    "缺必填数": counts["缺必填"],
                    "有待定数": counts["有待定"],
                    "缺需填数": counts["缺需填"],
                }
            )

    if not rows:
        return

    new_df = pd.DataFrame(rows)

    if os.path.exists(STATS_FILE):
        try:
            old_df = pd.read_excel(STATS_FILE)
            # 幂等性处理：如果当天已记录（如手动触发修复），则先剔除当天旧数据
            old_df = old_df[old_df["日期"] != today]
            final_df = pd.concat([old_df, new_df], ignore_index=True)
        except Exception as e:
            logger.error(f"读取历史统计文件失败: {e}")
            final_df = new_df
    else:
        final_df = new_df

    final_df.to_excel(STATS_FILE, index=False)
    logger.info(f"已成功将今日待办数据追加至 {STATS_FILE}")


# --- UI 辅助组件 ---
def ui_card_header(title, icon="assignment", color="blue-500"):
    """统一的卡片标题样式"""
    with ui.row().classes("w-full items-center gap-2 pb-3 border-b border-gray-100 mb-3"):
        ui.icon(icon, color=color.replace("text-", "")).classes("text-xl")
        ui.label(title).classes("text-lg font-bold text-gray-800")


def status_badge(text, color_name="gray"):
    """状态小标签"""
    # 简单的颜色映射
    colors = {
        "待审": ("orange-100", "orange-800"),
        "已审": ("green-100", "green-800"),
        "待修改": ("red-100", "red-800"),
        "研发": ("blue-100", "blue-800"),
    }
    bg, fg = colors.get(text, (f"{color_name}-100", f"{color_name}-800"))
    ui.label(text).classes(f"text-xs px-2 py-0.5 rounded bg-{bg} text-{fg} font-medium")


@ui.page("/statistics")
def statistics_page():
    # 1. 权限与基础数据获取
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")
        return

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    # 彻底锁死根节点尺寸，强制无滚动。
    # 让 Quasar 计算出的滚动条宽度永远为 0，彻底根除弹窗时的防抖动补偿位移。
    ui.add_css("""
        html, body, .q-layout, .q-page-container {
            overflow: hidden !important;
            width: 100vw !important;
            height: 100vh !important;
            margin: 0 !important;
            padding: 0 !important;
        }
    """)

    dialog = ui.dialog().props("persistent").classes("")
    current_user = app.storage.user.get("current_user", "匿名用户")
    current_role = app.storage.user.get("current_role")

    # 读取配置文件
    try:
        with open(f"{BASE_DIR}/module_show_role.json", "r", encoding="utf-8") as f:
            module_show_data = json.load(f)
    except Exception as e:
        logger.error(f"无法读取权限配置: {e}")
        module_show_data = {}  # 防止报错

    # 头像处理
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])
    current_display_path = get_cache_busted_path(current_avatar_path)

    # -------------------------------------------------------------------------
    # 业务逻辑函数 (保持原有逻辑核心，适配新UI容器)
    # -------------------------------------------------------------------------

    # --- 核心UI渲染逻辑：单行刷新 ---

    # -------------------------------------------------------------------------
    # 页面整体布局
    # -------------------------------------------------------------------------
    # 1. 顶部导航栏 (深色主题)
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4 z-50")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("系统统计信息").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # 2. 主内容区域 (Grid布局)
    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.element("div").classes("w-full h-full overflow-y-auto overflow-x-hidden p-4 md:p-6"):
            # Grid: 大屏12列，左8右4；小屏自动换行
            with ui.grid(columns=12).classes("w-full gap-4"):
                # 数据结构：{项目名："state":"状态"，....其它信息}，用于后续统计分析，状态主要有：作废、待定、研发、转产、试产、量产
                project_summary = app.storage.general.get("project_summary", {})
                # 数据结构：{项目名：版本号}，用于后续统计分析
                req_ver_data = app.storage.general.get("project_req_max_ver", {})
                # 数据结构：{人名：{项目名：{概述项label:状态}}}，用于待办统计分析,状态主要有：缺需填、缺必填、有待定
                pending_data = copy.deepcopy(app.storage.general.get("overview_charge_pending", {}))
                for user, pending_project_dic in list(
                    pending_data.items()
                ):  # 之所以要 list() 包裹，是为了在循环中修改字典结构时不报错
                    # if not pending_project_dic or user == "待定负责人":
                    if not pending_project_dic:
                        pending_data.pop(user, None)
                # =========================================================
                # 左侧列 (主要工作流)
                # =========================================================
                with ui.column().classes("col-span-12 lg:col-span-6 gap-4"):
                    # C. 概述统计图表 (Statistics)
                    if current_role in module_show_data.get("overview_charge_pending_statistics", []):
                        # ----------------- 图表 1：团队待办概览 (已修改横纵轴及排序) -----------------
                        # 增加 relative 类以支持绝对定位下拉框
                        with ui.card().classes(
                            "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white mb-2 relative"
                        ):
                            ui_card_header("团队待办概览", "bar_chart", "indigo-500")

                            # 增加状态筛选器，默认选中主要阶段
                            PROJECT_STATE_LIST = ["作废", "待定", "研发", "转产", "试产", "量产"]
                            default_states = ["研发", "转产", "试产", "量产"]

                            # z-10 确保不会被 Echarts 图表层遮挡
                            status_select = (
                                ui.select(
                                    options=PROJECT_STATE_LIST,
                                    value=default_states,
                                    multiple=True,
                                    label="项目状态筛选",
                                )
                                .props("borderless")
                                .classes("max-w-1/3 min-w-1/4 px-4 mb-2 absolute top-0 right-0 z-10")
                            )

                            @ui.refreshable
                            def render_pending_overview(target_states):
                                # 动态过滤数据
                                filtered_pending_data = {}
                                for user, pending_project_dic in pending_data.items():
                                    filtered_projects = {}
                                    for project_name, p_state_dic in pending_project_dic.items():
                                        # 获取当前项目状态，如果查不到默认为"未知"
                                        state = project_summary.get(project_name, {}).get("state", "未知")
                                        if state in target_states:
                                            filtered_projects[project_name] = p_state_dic
                                    if filtered_projects:
                                        filtered_pending_data[user] = filtered_projects

                                if filtered_pending_data:

                                    def classify_pending_project(p_state_dic):
                                        statuses = set(p_state_dic.values())
                                        if "缺必填" in statuses:
                                            return "存在缺必填"
                                        if "有待定" in statuses:
                                            return "无缺必填有待定"
                                        if "缺需填" in statuses:
                                            return "仅缺需填"
                                        return None

                                    stack_meta = {
                                        "存在缺必填": {"color": "#ef4444"},
                                        "无缺必填有待定": {"color": "#f59e0b"},
                                        "仅缺需填": {"color": "#3b82f6"},
                                    }
                                    stack_order = list(stack_meta.keys())

                                    user_stack_details = {}
                                    for user, pending_project_dic in filtered_pending_data.items():
                                        stack_details = {key: [] for key in stack_order}
                                        for project_name, p_state_dic in pending_project_dic.items():
                                            category = classify_pending_project(p_state_dic)
                                            if category:
                                                stack_details[category].append(project_name)
                                        user_stack_details[user] = stack_details

                                    # 数据准备：按待办项目总数降序，同分时按紧急程度排序
                                    sorted_users = sorted(
                                        filtered_pending_data.keys(),
                                        key=lambda user: (
                                            -sum(len(user_stack_details[user][key]) for key in stack_order),
                                            -len(user_stack_details[user]["存在缺必填"]),
                                            -len(user_stack_details[user]["无缺必填有待定"]),
                                            -len(user_stack_details[user]["仅缺需填"]),
                                            user,
                                        ),
                                    )
                                    user_list = sorted_users
                                    user_top_stack = {
                                        user: next(
                                            (
                                                stack_name
                                                for stack_name in reversed(stack_order)
                                                if user_stack_details[user][stack_name]
                                            ),
                                            None,
                                        )
                                        for user in user_list
                                    }

                                    series = []
                                    for stack_name in stack_order:
                                        series.append(
                                            {
                                                "name": stack_name,
                                                "type": "bar",
                                                "stack": "pending",
                                                "barWidth": "50%",
                                                "data": [
                                                    {
                                                        "value": len(user_stack_details[user][stack_name]),
                                                        "projects": user_stack_details[user][stack_name],
                                                        "user": user,
                                                        "itemStyle": {
                                                            "borderRadius": [4, 4, 0, 0]
                                                            if user_top_stack[user] == stack_name
                                                            else [0, 0, 0, 0]
                                                        },
                                                    }
                                                    for user in user_list
                                                ],
                                                "itemStyle": {
                                                    "color": stack_meta[stack_name]["color"],
                                                },
                                                "emphasis": {"focus": "self"},
                                                "blur": {"itemStyle": {"opacity": 0.2}},
                                                "label": {"show": False},
                                            }
                                        )

                                    # 动态调整 Echarts 配置以适应 X 轴名称显示
                                    echart_config = {
                                        "tooltip": {
                                            "trigger": "item",
                                            "confine": True,
                                            "axisPointer": {"type": "shadow"},
                                            ":formatter": """
                                                function(params) {
                                                    const projects = (params.data && params.data.projects) || [];
                                                    const count = typeof params.value === 'number' ? params.value : 0;
                                                    let html = `<b>${params.name}</b><br/>${params.seriesName}: <b>${count}</b>`;
                                                    if (projects.length) {
                                                        html += '<br/>' + projects.map(p => `• ${p}`).join('<br/>');
                                                    } else {
                                                        html += '<br/>暂无项目';
                                                    }
                                                    return html;
                                                }
                                                """,
                                            ":position": """
                                                function(point, params, dom, rect, size) {
                                                    const boxWidth = size.contentSize[0];
                                                    const boxHeight = size.contentSize[1];
                                                    const viewWidth = size.viewSize[0];
                                                    const viewHeight = size.viewSize[1];

                                                    let left = point[0] + 12;
                                                    if (left + boxWidth > viewWidth - 8) {
                                                        left = point[0] - boxWidth - 12;
                                                    }
                                                    if (left < 8) {
                                                        left = 8;
                                                    }

                                                    let top = point[1] - boxHeight - 12;
                                                    if (top < 8) {
                                                        top = point[1] + 12;
                                                    }
                                                    if (top + boxHeight > viewHeight - 8) {
                                                        top = Math.max(8, viewHeight - boxHeight - 8);
                                                    }

                                                    return [left, top];
                                                }
                                                """,
                                        },
                                        "grid": {
                                            "top": 30,
                                            "bottom": 30,
                                            "left": 20,
                                            "right": 20,
                                            "containLabel": True,
                                        },
                                        "xAxis": {
                                            "type": "category",
                                            "data": user_list,
                                            "axisTick": {"show": False},
                                            "axisLabel": {"interval": 0, "rotate": 30},  # 倾斜文字防止人名重叠
                                        },
                                        "yAxis": {
                                            "type": "value",
                                            "splitLine": {"show": True, "lineStyle": {"type": "dashed"}},
                                            "minInterval": 1,
                                        },
                                        "legend": {"top": 0},
                                        "series": series,
                                    }
                                    # ui.echart: 创建并渲染一个 Apache ECharts 数据可视化实例
                                    ui.echart(echart_config).classes("w-full h-68")
                                    # ui.separator()

                                    # ui.expansion: 创建一个可折叠的扩展面板组件
                                    with ui.expansion("查看详细清单").classes(
                                        "w-full text-sm text-gray-600 bg-gray-50"
                                    ):
                                        with ui.column().classes("p-3 gap-2 w-full"):
                                            for user, pending_project_dic in filtered_pending_data.items():
                                                if pending_project_dic:
                                                    with ui.row().classes("w-full justify-left text-xs"):
                                                        ui.label(user).classes("font-bold text-gray-700")
                                                        ui.label(f"{len(pending_project_dic.keys())}").classes(
                                                            "bg-indigo-100 text-indigo-700 px-1.5 rounded-full"
                                                        )
                                                    over_flat = app.storage.general.get("over_config_data_flat", {})
                                                    for p, p_state_dic in pending_project_dic.items():
                                                        # HTML Tooltip 构建
                                                        false_items = [
                                                            k for k, v in p_state_dic.items() if v == "缺必填"
                                                        ]
                                                        need_items = [
                                                            k for k, v in p_state_dic.items() if v == "缺需填"
                                                        ]
                                                        none_items = [
                                                            k for k, v in p_state_dic.items() if v == "有待定"
                                                        ]

                                                        tooltip_html = ""
                                                        if false_items:
                                                            tooltip_html += "<b>【必填无内容】</b><br>" + "<br>".join(
                                                                [
                                                                    f"• {over_flat.get(item, {}).get('title', '未知概述项')}"
                                                                    for item in false_items
                                                                ]
                                                            )
                                                        if need_items:
                                                            if tooltip_html:
                                                                tooltip_html += "<br><br>"
                                                            tooltip_html += "<b>【需填无内容】</b><br>" + "<br>".join(
                                                                [
                                                                    f"• {over_flat.get(item, {}).get('title', '未知概述项')}"
                                                                    for item in need_items
                                                                ]
                                                            )
                                                        if none_items:
                                                            if tooltip_html:
                                                                tooltip_html += "<br><br>"
                                                            tooltip_html += "<b>【待确认】</b><br>" + "<br>".join(
                                                                [
                                                                    f"• {over_flat.get(item, {}).get('title', '未知概述项')}"
                                                                    for item in none_items
                                                                ]
                                                            )

                                                        if not tooltip_html:
                                                            tooltip_html = "状态正常"

                                                        if false_items or none_items:
                                                            project_label = ui.label(
                                                                f"• {p} —— {project_summary.get(p, {}).get('state', '未知')} —— {len(p_state_dic)}"
                                                            ).classes("pl-2 text-red-500 truncate text-xs cursor-help")
                                                        else:
                                                            project_label = ui.label(
                                                                f"• {p} —— {project_summary.get(p, {}).get('state', '未知')} —— {len(p_state_dic)}"
                                                            ).classes(
                                                                "pl-2 text-amber-500 truncate text-xs cursor-help"
                                                            )

                                                        with project_label:
                                                            with ui.tooltip().classes(
                                                                "text-xs bg-gray-600/90 text-white p-2"
                                                            ):
                                                                ui.html(tooltip_html, sanitize=False)
                                else:
                                    ui.label("当前筛选状态下暂无积压数据").classes("p-4 text-gray-400 text-sm mt-4")

                            # 初始渲染
                            render_pending_overview(status_select.value)
                            # 监听筛选框的值变化，并触发刷新
                            status_select.on_value_change(lambda e: render_pending_overview.refresh(e.value))

                        # ----------------- 图表 2：近7日待办项趋势 (新增) -----------------
                        with ui.card().classes(
                            "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white relative"
                        ):
                            ui_card_header("近一周待办项趋势（概述项数量）", "trending_up", "teal-500")
                            # history 数据结构示例：{"2024-06-01": {"人名": {"项目名": label:状态,...},...}, "2024-06-02": {...},...}
                            history = app.storage.general.get("overview_pending_history", {})
                            # 每次查阅都会刷新当前日期的待办快照，确保数据的时效性和准确性
                            now = datetime.now()
                            today_str = now.strftime("%Y-%m-%d")
                            # 获取存储结构
                            current_pending = copy.deepcopy(app.storage.general.get("overview_charge_pending", {}))
                            # if "待定负责人" in current_pending:
                            #     current_pending.pop("待定负责人", None)
                            # 记录当天的最新快照（如果服务器一天内多次重启，会不断刷新当天的最终结果）
                            history[today_str] = current_pending

                            # 1. 强制生成固定的连续 7 天日期列表
                            if history:
                                latest_date_str = max(history.keys())
                                latest_date = datetime.strptime(latest_date_str, "%Y-%m-%d")
                            else:
                                latest_date = datetime.now()

                            # full_dates: 用于在字典中查询实际数据 (例如 "2026-03-21")
                            full_dates = [(latest_date - timedelta(days=6 - i)).strftime("%Y-%m-%d") for i in range(7)]
                            # display_dates: 截取掉 "YYYY-"，仅保留 "MM-DD" 用于横轴显示 (例如 "03-21")
                            display_dates = [d[5:] for d in full_dates]

                            # 2. 找出这 7 天内有过待办项的所有人员
                            all_users = set()
                            for d in full_dates:
                                # 使用 .get(d, {}) 防御性读取，即使某天没数据也不会报错
                                all_users.update(history.get(d, {}).keys())

                            def get_user_pending_count(date_str, user):
                                user_state = history.get(date_str, {}).get(user, {})
                                return sum(len(v) for v in user_state.values())

                            latest_day = full_dates[-1]
                            previous_day = full_dates[-2] if len(full_dates) > 1 else full_dates[-1]

                            all_users_list = sorted(
                                list(all_users),
                                # 3. 排序逻辑：先按近一天待办项数与前一天的差值降序，再按近一天待办项数降序，最后按姓名升序
                                key=lambda user: (
                                    -(
                                        get_user_pending_count(latest_day, user)
                                        - get_user_pending_count(previous_day, user)
                                    ),
                                    -get_user_pending_count(latest_day, user),
                                    user,
                                ),
                            )

                            if not all_users_list:
                                ui.label("近一周暂无待办记录。").classes("p-4 text-gray-400 text-sm")
                            else:
                                # 默认选择前3名人员，防止图表拥挤
                                default_selected = all_users_list[:3] if len(all_users_list) > 3 else all_users_list

                                ui_select_user = (
                                    ui.select(
                                        options=all_users_list,
                                        value=default_selected,
                                        multiple=True,
                                        label="请选择要查看的人员趋势",
                                    )
                                    .props("borderless")
                                    .classes("max-w-1/3 min-w-1/4 px-4 mb-2 absolute top-0 right-0")
                                )

                                # 辅助函数：依据当前勾选的人员动态生成图表数据
                                def get_series_data(selected_users):
                                    series_list = []
                                    for user in selected_users:
                                        user_data = []
                                        for i, d in enumerate(full_dates):
                                            # 1. 判断当天该人员是否有数据，如果没有，则填入 None，ECharts 会自动断开折线
                                            if user not in history.get(d, {}):
                                                user_data.append({"value": None, "name": d})
                                                continue

                                            curr_state = history.get(d, {}).get(user, {})
                                            curr_item_count = sum(len(v) for v in curr_state.values())

                                            prev_state = (
                                                history.get(full_dates[i - 1], {}).get(user, {})
                                                if i > 0
                                                else curr_state
                                            )

                                            diff_texts = []
                                            all_projs = set(curr_state.keys()) | set(prev_state.keys())
                                            for p in all_projs:
                                                c_count = len(curr_state.get(p, {}))
                                                p_count = len(prev_state.get(p, {}))
                                                if c_count > p_count:
                                                    diff_texts.append(f"• {p} (+{c_count - p_count}项)")
                                                elif c_count < p_count:
                                                    diff_texts.append(f"• {p} ({c_count - p_count}项)")

                                            # 2. 使用 \n 作为换行符，配合 tooltip 的 white-space: pre-wrap 样式实现优雅换行
                                            if diff_texts:
                                                diff_str = "\n".join(diff_texts)
                                                point_name = f"{d} [较昨日变化]\n{diff_str}"
                                            else:
                                                point_name = f"{d} [较昨日变化: 无增减]"

                                            user_data.append({"value": curr_item_count, "name": point_name})

                                        series_list.append(
                                            # 3. 将 smooth 改为 False，使用直线折线图
                                            {
                                                "name": user,
                                                "type": "line",
                                                "smooth": False,
                                                "symbolSize": 6,
                                                # connectNulls: ECharts 系列配置项，设为 True 可以在包含 null 的数据点之间连线
                                                "connectNulls": True,
                                                "data": user_data,
                                            }
                                        )
                                    return series_list

                                trend_chart = ui.echart(
                                    {
                                        "tooltip": {
                                            "trigger": "item",
                                            "formatter": "<b>{a}</b> <br/> {b} <br/><br/> 当日总待办项: <b>{c}</b>",
                                            # 开启 pre-wrap，使得字符串中的 \n 能被 CSS 识别为真实的换行
                                            "extraCssText": "white-space: pre-wrap;",
                                        },
                                        "legend": {"type": "scroll", "bottom": 0},
                                        "grid": {
                                            "top": 30,
                                            "bottom": 50,
                                            "left": 40,
                                            "right": 20,
                                            "containLabel": True,
                                        },
                                        "xAxis": {
                                            "type": "category",
                                            "data": display_dates,
                                            "axisTick": {"show": False},
                                            "boundaryGap": False,
                                            "splitLine": {"show": True, "lineStyle": {"type": "dashed"}},
                                        },
                                        "yAxis": {
                                            "type": "value",
                                            "minInterval": 1,
                                            "splitLine": {"lineStyle": {"type": "dashed"}},
                                        },
                                        "series": get_series_data(default_selected),
                                    }
                                ).classes("w-full h-80")

                                def update_chart(e):
                                    trend_chart.options["series"] = get_series_data(e.value)
                                    trend_chart.update()

                                ui_select_user.on_value_change(update_chart)

                # =========================================================
                # 右侧列
                # =========================================================
                with ui.column().classes("col-span-12 lg:col-span-6 gap-4"):
                    # D. 其他统计信息 (Other Statistics)
                    if current_role in module_show_data.get("overview_charge_pending_statistics", []):
                        with ui.card().classes(
                            "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white mb-2"
                        ):
                            ui_card_header("项目需求录入与概述填写统计", "insights", "purple-500")

                            # ==========================================
                            # 新增：点击图表柱子弹窗显示项目明细的回调函数
                            # ==========================================
                            def show_project_details(e):
                                category_name = e.args.get("name")

                                # 终极防御：如果没有名字直接退出
                                if not category_name:
                                    return

                                # 直接从你的内存字典反查项目列表，抛弃前端的不稳定数据回传
                                projects = ordered_status_dict.get(category_name) or overview_categories.get(
                                    category_name, []
                                )
                                count = len(projects)

                                # 创建并打开弹窗，限制最大宽度
                                dialog.clear()
                                with dialog, ui.card().classes("w-full max-w-4xl bg-white"):
                                    # 弹窗头部：明确的中文标题与关闭按钮
                                    with ui.row().classes("w-full justify-between items-center mb-4 border-b pb-2"):
                                        ui.label(f"项目明细：{category_name} (共 {count} 项)").classes(
                                            "text-xl font-bold text-gray-800"
                                        )
                                        # ui.button: NiceGUI 的按钮组件
                                        ui.button(icon="close", on_click=dialog.close).props(
                                            "flat round dense text-color=gray"
                                        )

                                    # 限制最大高度为视口高度的 60%，超出自动出现垂直滚动条
                                    with ui.scroll_area().classes("w-full max-h-[60vh] p-2"):
                                        if not projects:
                                            ui.label("当前分类暂无项目").classes(
                                                "text-gray-500 text-center w-full mt-4"
                                            )
                                        else:
                                            # 响应式网格布局：移动端1列，平板2列，小桌面3列，大屏幕4列，完美适配两三百个项目名
                                            with ui.element("div").classes(
                                                "grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3"
                                            ):
                                                for p_name in projects:
                                                    ui.label(p_name).classes(
                                                        "bg-gray-50 px-3 py-2 rounded border border-gray-200 text-sm "
                                                        "text-gray-700 truncate hover:bg-blue-50 hover:text-blue-600 "
                                                        "transition-colors cursor-default"
                                                    )

                                dialog.open()

                            # 2. 数据处理与清洗
                            # 2.1 统计公司总项目及状态分布 (由计数改为收集项目列表)
                            total_projects = len(project_summary)
                            status_counts_dict = {}  # 结构变为：{"研发": ["项目A", "项目B"]}
                            for p_name, p_info in project_summary.items():
                                p_status = p_info.get("state", "未知状态")
                                if p_status not in status_counts_dict:
                                    status_counts_dict[p_status] = []
                                status_counts_dict[p_status].append(p_name)

                            # 为不同状态预设颜色映射
                            status_color_map = {
                                "作废": "#555555",
                                "待定": "#f59e0b",
                                "研发": "#3b82f6",
                                "转产": "#ef4444",
                                "试产": "#06b6d4",
                                "量产": "#10b981",
                            }
                            fallback_colors = ["#8b5cf6", "#ec4899", "#f97316", "#14b8a6"]

                            # 将预设颜色的状态排在前面，未预设颜色的状态排在后面
                            ordered_status_dict = {}
                            for k in status_color_map.keys():
                                if k in status_counts_dict:
                                    ordered_status_dict[k] = status_counts_dict.pop(k)
                            ordered_status_dict.update(status_counts_dict)

                            status_chart_data = []
                            fallback_idx = 0
                            for k, proj_list in ordered_status_dict.items():
                                color = status_color_map.get(k)
                                if not color:
                                    color = fallback_colors[fallback_idx % len(fallback_colors)]
                                    fallback_idx += 1

                                status_chart_data.append(
                                    {
                                        "value": len(proj_list),
                                        "name": k,
                                        "projects": proj_list,  # 将项目列表挂载到 ECharts 的数据项中
                                        "itemStyle": {"color": color},
                                    }
                                )

                            # 2.2 聚合 pending_data 中的待办状态到项目维度
                            project_issues = {}
                            for user, p_dict in pending_data.items():
                                if not isinstance(p_dict, dict):
                                    continue
                                for proj, issues in p_dict.items():
                                    if proj not in project_issues:
                                        project_issues[proj] = set()
                                    project_issues[proj].update(issues.values())

                            # 2.3 统计已录入需求的项目概述完成度 (由计数改为收集项目列表)
                            overview_categories = {"存在缺必填": [], "仅有待定": [], "仅缺需填": [], "概述已完成": []}

                            for proj in req_ver_data.keys():
                                if proj not in project_issues:
                                    overview_categories["概述已完成"].append(proj)
                                    if proj not in app.storage.general["overview_completed"]:
                                        app.storage.general["overview_completed"].append(proj)
                                else:
                                    statuses = project_issues[proj]
                                    if (
                                        any([status in statuses for status in ["缺必填", "缺需填", "有待定"]])
                                        and proj in app.storage.general["overview_completed"]
                                    ):
                                        app.storage.general["overview_completed"].remove(proj)

                                    # 按照严重程度优先级进行降维判定并收集项目名称
                                    if "缺必填" in statuses:
                                        overview_categories["存在缺必填"].append(proj)
                                    elif "缺需填" in statuses:
                                        overview_categories["仅缺需填"].append(proj)
                                    elif "有待定" in statuses:
                                        overview_categories["仅有待定"].append(proj)
                                    else:
                                        overview_categories["概述已完成"].append(proj)
                                        if proj not in app.storage.general["overview_completed"]:
                                            app.storage.general["overview_completed"].append(proj)

                            # 准备柱状图系列数据
                            overview_chart_data = [
                                {
                                    "value": len(overview_categories["存在缺必填"]),
                                    "name": "存在缺必填",
                                    "projects": overview_categories["存在缺必填"],
                                    "itemStyle": {"color": "#ef4444"},
                                },
                                {
                                    "value": len(overview_categories["仅有待定"]),
                                    "name": "仅有待定",
                                    "projects": overview_categories["仅有待定"],
                                    "itemStyle": {"color": "#f59e0b"},
                                },
                                {
                                    "value": len(overview_categories["仅缺需填"]),
                                    "name": "仅缺需填",
                                    "projects": overview_categories["仅缺需填"],
                                    "itemStyle": {"color": "#3b82f6"},
                                },
                                {
                                    "value": len(overview_categories["概述已完成"]),
                                    "name": "概述已完成",
                                    "projects": overview_categories["概述已完成"],
                                    "itemStyle": {"color": "#10b981"},
                                },
                            ]

                            with ui.element("div").classes("grid grid-cols-1 md:grid-cols-2 w-full gap-4 mt-4"):
                                # ==========================================
                                # 图表 A：公司现有项目状态占比
                                # ==========================================
                                status_x_axis_data = (
                                    [item["name"] for item in status_chart_data] if status_chart_data else ["暂无数据"]
                                )

                                echart_status_config = {
                                    "title": {
                                        "text": "项目总体状态分布",
                                        "subtext": f"项目总计: {total_projects} 个",
                                        "itemGap": 15,
                                        "left": "center",
                                    },
                                    "trigger": "item",
                                    "grid": {
                                        "top": 100,
                                        "left": "3%",
                                        "right": "4%",
                                        "bottom": "15%",
                                        "containLabel": True,
                                    },
                                    "xAxis": {
                                        "type": "category",
                                        "data": status_x_axis_data,
                                        "axisLabel": {"interval": 0, "rotate": 30},
                                    },
                                    "yAxis": {"type": "value", "minInterval": 1},
                                    "series": [
                                        {
                                            "name": "项目数量",
                                            "type": "bar",
                                            "barWidth": "50%",
                                            "colorBy": "data",
                                            "data": status_chart_data
                                            if status_chart_data
                                            else [{"value": 0, "name": "暂无数据"}],
                                            "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                                            "label": {"show": True, "position": "top"},
                                        }
                                    ],
                                }
                                # 渲染图表并绑定点击事件
                                status_chart = ui.echart(echart_status_config).classes("w-full h-80 cursor-pointer")

                                # ==========================================
                                # 图表 B：已录入需求的项目概述质量分析
                                # ==========================================
                                overview_x_axis_data = [item["name"] for item in overview_chart_data]

                                echart_overview_config = {
                                    "title": {
                                        "text": "已录需求项目概述完成度",
                                        "subtext": f"已录需求项目: {len(req_ver_data)} 个",
                                        "itemGap": 15,
                                        "left": "center",
                                    },
                                    "trigger": "item",
                                    "grid": {
                                        "top": 100,
                                        "left": "3%",
                                        "right": "4%",
                                        "bottom": "15%",
                                        "containLabel": True,
                                    },
                                    "xAxis": {
                                        "type": "category",
                                        "data": overview_x_axis_data,
                                        "axisLabel": {"interval": 0},
                                    },
                                    "yAxis": {"type": "value", "minInterval": 1},
                                    "series": [
                                        {
                                            "name": "项目数量",
                                            "type": "bar",
                                            "barWidth": "50%",
                                            "colorBy": "data",
                                            "data": overview_chart_data,
                                            "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                                            "label": {"show": True, "position": "top"},
                                        }
                                    ],
                                }
                                # 渲染图表并绑定点击事件
                                overview_chart = ui.echart(echart_overview_config).classes("w-full h-80 cursor-pointer")

                                status_chart.on("echart_item_click", show_project_details)
                                overview_chart.on("echart_item_click", show_project_details)
                                ui.run_javascript(f"""
                                    setTimeout(() => {{
                                        [{status_chart.id}, {overview_chart.id}].forEach(id => {{
                                            const el = getElement(id);
                                            if (el && el.chart) {{
                                                // 直接监听 ECharts 实例内部真实的 click
                                                el.chart.on('click', function(params) {{
                                                    // 确保只有点击到数据系列（柱子）才触发
                                                    if (params.componentType === 'series') {{
                                                        el.$emit('echart_item_click', {{
                                                            name: params.name,
                                                            value: params.value
                                                        }});
                                                    }}
                                                }});
                                            }}
                                        }});
                                    }}, 200); // 极小延迟，确保图表已被浏览器完全渲染
                                """)
                    # E. 待办项历史趋势分析 (Pending Items Historical Trend Analysis)
                    if current_role in module_show_data.get("overview_charge_pending_statistics", []):
                        # ----------------- 图表 0：30日多维趋势分析 -----------------
                        with ui.card().classes(
                            "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white relative"
                        ):
                            ui_card_header("近30日待办状态趋势分析（项目数）", "history", "amber-600")

                            if os.path.exists(STATS_FILE):
                                try:
                                    df = pd.read_excel(STATS_FILE)
                                    df["日期"] = pd.to_datetime(df["日期"])
                                    cutoff_date = datetime.now() - timedelta(days=30)
                                    df = df[df["日期"] >= cutoff_date].sort_values("日期")
                                    df["日期_str"] = df["日期"].dt.strftime("%m-%d")
                                except Exception as e:
                                    logger.error(f"数据加载失败: {e}")
                                    df = pd.DataFrame()
                            else:
                                df = pd.DataFrame()

                            if df.empty:
                                ui.label("暂无历史统计数据，数据将在每日工作日 18:00 自动累积生成。").classes(
                                    "p-8 text-gray-400 text-center w-full"
                                )
                            else:
                                # 计算转产阶段总积压最多的前三人作为默认项
                                recent_date = df["日期"].max()
                                latest_data = df[df["日期"] == recent_date]
                                top_users_df = latest_data[latest_data["项目状态"] == "转产"].copy()
                                top_users_df["total_issues"] = (
                                    top_users_df["缺必填数"] + top_users_df["有待定数"] + top_users_df["缺需填数"]
                                )
                                default_top_users = (
                                    top_users_df.groupby("用户")["total_issues"].sum().nlargest(3).index.tolist()
                                )

                                if not default_top_users:
                                    default_top_users = df["用户"].unique()[:3].tolist()

                                with ui.row().classes("w-full px-4 gap-4 items-center justify-between"):
                                    sel_users = ui.select(
                                        options=df["用户"].unique().tolist(),
                                        value=default_top_users,
                                        multiple=True,
                                        label="人员选择",
                                    ).classes("w-1/3 min-w-[150px]")

                                    sel_states = ui.select(
                                        options=df["项目状态"].unique().tolist(),
                                        value=["转产"],
                                        multiple=True,
                                        label="阶段过滤",
                                    ).classes("w-1/4 min-w-[120px]")

                                    sel_metric = ui.select(
                                        options={
                                            "缺必填数": "缺必填数",
                                            "有待定数": "有待定数",
                                            "缺需填数": "缺需填数",
                                        },
                                        value="缺必填数",
                                        label="考察指标",
                                    ).classes("w-1/4 min-w-[120px]")

                                @ui.refreshable
                                def render_history_chart(users, states, metric):
                                    if not users or not states:
                                        ui.label("请至少选择一名人员和一个阶段。").classes("p-4 text-gray-400")
                                        return

                                    mask = df["用户"].isin(users) & df["项目状态"].isin(states)
                                    filtered_df = (
                                        df[mask].groupby(["日期_str", "用户"])[metric].sum().unstack().fillna(0)
                                    )

                                    dates = filtered_df.index.tolist()
                                    series = []
                                    for user in filtered_df.columns:
                                        series.append(
                                            {
                                                "name": user,
                                                "type": "line",
                                                "smooth": False,
                                                "symbolSize": 6,
                                                "data": filtered_df[user].tolist(),
                                            }
                                        )

                                    echart_config = {
                                        "tooltip": {"trigger": "axis"},
                                        "legend": {"bottom": 0, "type": "scroll"},
                                        "grid": {
                                            "top": 40,
                                            "bottom": 60,
                                            "left": 40,
                                            "right": 20,
                                            "containLabel": True,
                                        },
                                        "xAxis": {
                                            "type": "category",
                                            "data": dates,
                                            "boundaryGap": False,
                                            "splitLine": {"show": True, "lineStyle": {"type": "dashed"}},
                                        },
                                        "yAxis": {
                                            "type": "value",
                                            "minInterval": 1,
                                            "splitLine": {"lineStyle": {"type": "dashed"}},
                                        },
                                        "series": series,
                                    }
                                    ui.echart(echart_config).classes("w-full h-80")

                                render_history_chart(sel_users.value, sel_states.value, sel_metric.value)

                                sel_users.on_value_change(
                                    lambda: render_history_chart.refresh(
                                        sel_users.value, sel_states.value, sel_metric.value
                                    )
                                )
                                sel_states.on_value_change(
                                    lambda: render_history_chart.refresh(
                                        sel_users.value, sel_states.value, sel_metric.value
                                    )
                                )
                                sel_metric.on_value_change(
                                    lambda: render_history_chart.refresh(
                                        sel_users.value, sel_states.value, sel_metric.value
                                    )
                                )
