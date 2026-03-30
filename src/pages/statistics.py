# -*- encoding: utf-8 -*-
import copy
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from nicegui import app, ui

from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR, REQ_REMOVE_DIR
from ..utils import (
    delete_file,
    get_cache_busted_path,
    get_overviow_page,
    get_project_engineer_project_list_dic,
    logout,
    move_file_with_timestamp_pathlib,
    project_summary_update,
    requirement_version_tidy,
    set_overview_active_state,
    set_project_custom_labels,
)

# 获取 logger
logger = logging.getLogger(__name__)


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
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
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
    with ui.element("div").classes("w-full h-[calc(100vh-5rem)] p-4 md:p-6"):
        # Grid: 大屏12列，左8右4；小屏自动换行
        with ui.grid(columns=12).classes("w-full gap-4"):
            # 数据结构：{项目名："state":"状态"，....其它信息}，用于后续统计分析，状态主要有：作废、待定、研发、转产、试产、量产
            project_summary = app.storage.general.get("project_summary", {})
            # 数据结构：{项目名：版本号}，用于后续统计分析
            req_ver_data = app.storage.general.get("project_req_max_ver", {})
            # 数据结构：{人名：{项目名：{概述项label:状态}}}，用于待办统计分析,状态主要有：缺需填、缺必填、有待定
            pending_data = app.storage.general.get("overview_charge_pending", {})
            for user, pending_project_dic in list(pending_data.items()):
                if not pending_project_dic:
                    pending_data.pop(user, None)
            # =========================================================
            # 左侧列 (主要工作流)
            # =========================================================
            with ui.column().classes("col-span-12 lg:col-span-5 gap-4"):
                # C. 概述统计图表 (Statistics)
                if current_role in module_show_data.get("overview_charge_pending_statistics", []):
                    # ----------------- 图表 1：团队待办概览 (已修改横纵轴及排序) -----------------
                    with ui.card().classes(
                        "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white mb-4"
                    ):
                        ui_card_header("团队待办概览", "bar_chart", "indigo-500")

                        if pending_data:
                            # 数据准备：按待办项目数降序排序
                            sorted_users = sorted(pending_data.items(), key=lambda x: len(x[1].keys()), reverse=True)
                            user_list = [item[0] for item in sorted_users]
                            count_list = [len(item[1].keys()) for item in sorted_users]

                            # 动态调整 Echarts 配置以适应 X 轴名称显示
                            echart_config = {
                                "tooltip": {"trigger": "axis"},
                                "grid": {"top": 30, "bottom": 40, "left": 40, "right": 20, "containLabel": True},
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
                                "series": [
                                    {
                                        "name": "待办项目数",
                                        "data": count_list,
                                        "type": "bar",
                                        "barWidth": 25,
                                        "itemStyle": {"color": "#6366f1", "borderRadius": [4, 4, 0, 0]},
                                        "label": {"show": True, "position": "top", "color": "#666"},
                                    }
                                ],
                            }
                            # ui.echart: 创建并渲染一个 Apache ECharts 数据可视化实例
                            ui.echart(echart_config).classes("w-full h-72")
                            ui.separator()

                            # ui.expansion: 创建一个可折叠的扩展面板组件
                            with ui.expansion("查看详细清单").classes("w-full text-sm text-gray-600 bg-gray-50"):
                                with ui.column().classes("p-3 gap-2 w-full"):
                                    for user, pending_project_dic in pending_data.items():
                                        if pending_project_dic:
                                            with ui.row().classes("w-full justify-between text-xs"):
                                                ui.label(user).classes("font-bold text-gray-700")
                                                ui.label(f"{len(pending_project_dic.keys())}").classes(
                                                    "bg-indigo-100 text-indigo-700 px-1.5 rounded-full"
                                                )
                                            over_flat = app.storage.general.get("over_config_data_flat", {})
                                            for p, p_state_dic in pending_project_dic.items():
                                                # HTML Tooltip 构建
                                                false_items = [k for k, v in p_state_dic.items() if v == "缺必填"]
                                                need_items = [k for k, v in p_state_dic.items() if v == "缺需填"]
                                                none_items = [k for k, v in p_state_dic.items() if v == "有待定"]

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
                                                    project_label = ui.label(f"• {p}").classes(
                                                        "pl-2 text-red-500 truncate text-xs cursor-help"
                                                    )
                                                else:
                                                    project_label = ui.label(f"• {p}").classes(
                                                        "pl-2 text-amber-500 truncate text-xs cursor-help"
                                                    )

                                                with project_label:
                                                    with ui.tooltip().classes("text-xs bg-gray-600/90 text-white p-2"):
                                                        ui.html(tooltip_html, sanitize=False)
                        else:
                            ui.label("暂无积压数据").classes("p-4 text-gray-400 text-sm")

                    # ----------------- 图表 2：近7日待办项趋势 (新增) -----------------
                    with ui.card().classes(
                        "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white relative"
                    ):
                        ui_card_header("近一周待办项趋势", "trending_up", "teal-500")
                        # history 数据结构示例：{"2024-06-01": {"人名": {"项目名": label:状态,...},...}, "2024-06-02": {...},...}
                        history = app.storage.general.get("overview_pending_history", {})
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

                        all_users_list = sorted(list(all_users))

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
                                            history.get(full_dates[i - 1], {}).get(user, {}) if i > 0 else curr_state
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
                                    "grid": {"top": 30, "bottom": 50, "left": 40, "right": 20, "containLabel": True},
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
            with ui.column().classes("col-span-12 lg:col-span-7 gap-4"):
                # D. 其他统计信息 (Other Statistics)
                if current_role in module_show_data.get("overview_charge_pending_statistics", []):
                    with ui.card().classes(
                        "w-full rounded-xl shadow-sm border border-gray-100 overflow-hidden bg-white mb-4"
                    ):
                        ui_card_header("项目需求录入与概述填写统计", "insights", "purple-500")
                        # 2. 数据处理与清洗
                        # 2.1 统计公司总项目及状态分布
                        total_projects = len(project_summary)
                        status_counts = {}
                        for p_info in project_summary.values():
                            p_status = p_info.get("state", "未知状态")
                            status_counts[p_status] = status_counts.get(p_status, 0) + 1

                        # 为不同状态预设颜色映射
                        status_color_map = {
                            "作废": "#555555",  # 灰色
                            "待定": "#f59e0b",  # 橙色
                            "研发": "#3b82f6",  # 蓝色
                            "转产": "#ef4444",  # 红色
                            "试产": "#06b6d4",  # 青色
                            "量产": "#10b981",  # 绿色
                        }
                        fallback_colors = ["#8b5cf6", "#ec4899", "#f97316", "#14b8a6"]

                        # 将预设颜色的状态排在前面，未预设颜色的状态排在后面，保持原有顺序不变
                        temp_dic = {}
                        for k in status_color_map.keys():
                            if k in status_counts:
                                temp_dic[k] = status_counts.pop(k, {})
                        temp_dic.update(status_counts)  # 将剩余未预设颜色的状态追加到末尾
                        status_counts = temp_dic

                        status_chart_data = []
                        fallback_idx = 0
                        for k, v in status_counts.items():
                            # 如果字典中没有预设该状态的颜色，则从备用颜色池中按取余循环分配
                            color = status_color_map.get(k)
                            if not color:
                                color = fallback_colors[fallback_idx % len(fallback_colors)]
                                fallback_idx += 1

                            status_chart_data.append(
                                {
                                    "value": v,
                                    "name": k,
                                    "itemStyle": {"color": color},  # 直接在数据项中指定颜色
                                }
                            )

                        # 2.2 聚合 pending_data 中的待办状态到项目维度
                        project_issues = {}
                        for user, p_dict in pending_data.items():
                            if not isinstance(p_dict, dict):
                                continue
                            for proj, issues in p_dict.items():
                                if proj not in project_issues:
                                    project_issues[proj] = set()  # 使用集合去重
                                project_issues[proj].update(issues.values())

                        # 2.3 统计已录入需求的项目概述完成度
                        overview_completed = 0
                        only_pending = 0
                        only_need = 0
                        has_must = 0

                        # req_ver_data: {项目名: 版本号}，以此为基准进行判定
                        for proj in req_ver_data.keys():
                            if proj not in project_issues:
                                overview_completed += 1
                            else:
                                statuses = project_issues[proj]
                                # 按照严重程度优先级进行降维判定
                                if "缺必填" in statuses:
                                    has_must += 1
                                elif "缺需填" in statuses:
                                    only_need += 1
                                elif "有待定" in statuses:
                                    only_pending += 1
                                else:
                                    overview_completed += 1

                        # 准备柱状图系列数据，并预设具有警示意义的颜色
                        overview_chart_data = [
                            {"value": has_must, "name": "存在缺必填", "itemStyle": {"color": "#ef4444"}},
                            {"value": only_pending, "name": "仅有待定", "itemStyle": {"color": "#f59e0b"}},
                            {"value": only_need, "name": "仅缺需填", "itemStyle": {"color": "#3b82f6"}},
                            {
                                "value": overview_completed,
                                "name": "概述已完成",
                                "itemStyle": {"color": "#10b981"},
                            },  # 绿
                        ]

                        # 使用普通的 div 元素，并完全交由 Tailwind CSS 的响应式类名来控制列数
                        # grid: 声明网格布局
                        # grid-cols-1: 默认（小屏幕）为 1 列
                        # md:grid-cols-2: 中大屏幕（>=768px）时切换为 2 列
                        with ui.element("div").classes("grid grid-cols-1 md:grid-cols-2 w-full gap-4 mt-4"):
                            # ==========================================
                            # 图表 A：公司现有项目状态占比 (柱状图)
                            # ==========================================
                            status_x_axis_data = (
                                [item["name"] for item in status_chart_data] if status_chart_data else ["暂无数据"]
                            )

                            echart_status_config = {
                                "title": {
                                    "text": "项目总体状态分布",
                                    "subtext": f"项目总计: {total_projects} 个",
                                    # itemGap: ECharts 属性，控制主标题 (text) 与副标题 (subtext) 之间的垂直间距（单位：像素）
                                    "itemGap": 15,
                                    "left": "center",
                                },
                                # tooltip: ECharts 提示框组件，trigger='axis' 表示坐标轴触发，适用于柱状图
                                "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
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
                                        # colorBy: 'data' 是 ECharts 新版本支持的属性，允许柱状图的每个柱子独立取色，
                                        # 结合我们上方数据里传入的 itemStyle.color，实现各分类不同颜色的显示。
                                        "colorBy": "data",
                                        "data": status_chart_data
                                        if status_chart_data
                                        else [{"value": 0, "name": "暂无数据"}],
                                        "itemStyle": {"borderRadius": [4, 4, 0, 0]},
                                        "label": {"show": True, "position": "top"},
                                    }
                                ],
                            }
                            # ui.echart: NiceGUI 封装的 Apache ECharts 渲染实例
                            ui.echart(echart_status_config).classes("w-full h-80")

                            # ==========================================
                            # 图表 B：已录入需求的项目概述质量分析 (柱状图)
                            # ==========================================
                            overview_x_axis_data = [item["name"] for item in overview_chart_data]

                            echart_overview_config = {
                                "title": {
                                    "text": "已录需求项目概述完成度",
                                    "subtext": f"已录需求项目: {len(req_ver_data)} 个",
                                    # itemGap: ECharts 属性，控制主标题 (text) 与副标题 (subtext) 之间的垂直间距（单位：像素）
                                    "itemGap": 15,
                                    "left": "center",
                                },
                                "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
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
                            ui.echart(echart_overview_config).classes("w-full h-80")
