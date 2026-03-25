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


@ui.page("/information")
def information_page():
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

    def set_review_revise(p_name, v):
        app.storage.general["wait_review"][p_name][v]["state"] = "待修改"

    async def set_review_pass(container_row, p_name, v):
        """审核通过逻辑"""
        app.storage.general["wait_review"][p_name][v]["state"] = "已审"
        app.storage.general["wait_review"][p_name][v]["pass_time"] = datetime.now().isoformat()
        app.storage.general["project_req_max_ver"][p_name] = v
        await set_overview_active_state(p_name, v)
        delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")
        await requirement_version_tidy(p_name, False)
        set_project_custom_labels(p_name)

        # 刷新UI行
        refresh_review_row(container_row, p_name, v)
        dialog.close()

    async def set_temporary_project_review_pass(container_row, p_name, v, data):
        if data.get("introduction").strip() and data.get("customer").strip():
            temp_data = {
                p_name: {
                    "state": "研发",
                    "model_notes": data.get("notes").strip(),
                    "creation_date": datetime.now().strftime("%Y-%m-%d"),
                    "introduction": data.get("introduction").strip(),
                    "customer": data.get("customer").strip(),
                }
            }
            # 更新 project_summary
            project_data = {}
            try:
                with open(f"{BASE_DIR}/data/project_summary.json", "r", encoding="utf-8") as f:
                    project_data = json.load(f)
            except FileNotFoundError:
                pass
            project_data.update(temp_data)
            with open(f"{BASE_DIR}/data/project_summary.json", "w", encoding="utf-8") as f:
                json.dump(project_data, f, indent=4, ensure_ascii=False)

            project_summary_update()
            await set_review_pass(container_row, p_name, v)
        else:
            ui.notify("项目简介与客户简称必须填写!", type="warning", position="bottom", close_button="✖")

    async def set_temporary_project_dialog(container_row, p_name, v):
        if "RFTS" in p_name and p_name not in app.storage.general["project_summary"]:
            dialog.clear()
            with dialog, ui.card().classes("w-full max-w-lg"):
                ui.label("🆕 新建项目补全信息").classes("text-lg font-bold mb-2")
                pro_data = {"notes": "", "introduction": "", "customer": ""}

                ui.input(label="项目备注", placeholder="选填").bind_value(pro_data, "notes").classes("w-full")
                ui.textarea(label="项目简介", placeholder="必填").bind_value(pro_data, "introduction").classes("w-full")
                ui.input(label="项目客户", placeholder="必填").bind_value(pro_data, "customer").classes("w-full")

                with ui.row().classes("w-full justify-end mt-4"):
                    ui.button("取消", on_click=dialog.close).props("flat color=grey")
                    ui.button(
                        "确认创建",
                        color="primary",
                        on_click=lambda: set_temporary_project_review_pass(container_row, p_name, v, pro_data),
                    )
            dialog.open()
        else:
            await set_review_pass(container_row, p_name, v)

    async def set_review_pass_dialog(container_row, p_name, v):
        """点击审核通过的入口"""
        current_state = app.storage.general["wait_review"][p_name].get(v, {}).get("state")

        if current_state == "待审":
            old_v = "1.0" if v == "1.0" else f"{int(float(v)) - 1}.0"
            new_submitter = app.storage.general["wait_review"][p_name].get(v, {}).get("submitter")
            old_submitter = app.storage.general["wait_review"][p_name].get(old_v, {}).get("submitter")

            if new_submitter != old_submitter:
                dialog.clear()
                with dialog, ui.card():
                    ui.label("⚠️ 提交人变更提醒").classes("text-lg font-bold text-orange-600")
                    ui.label(f"提交人从 {old_submitter} 变更为 {new_submitter}，是否继续？")
                    with ui.row().classes("w-full justify-end mt-4"):
                        ui.button("取消", on_click=dialog.close).props("flat")
                        ui.button(
                            "继续通过",
                            color="red",
                            on_click=lambda: set_temporary_project_dialog(container_row, p_name, v),
                        )
                dialog.open()
            else:
                dialog.clear()
                with dialog, ui.card():
                    ui.label("⚠️ 确认通过该项目需求内容的评审吗？").classes("text-lg font-bold text-orange-600")
                    with ui.row().classes("w-full justify-end mt-4"):
                        ui.button("取消", on_click=dialog.close).props("flat")
                        ui.button(
                            "继续通过",
                            color="red",
                            on_click=lambda: set_temporary_project_dialog(container_row, p_name, v),
                        )
                dialog.open()
        else:
            ui.notify("需求非待审状态，无法通过，已刷新列表", type="warning")
            refresh_review_row(container_row, p_name, v)

    def remove_requirement_file(container_row, p_name, v):
        move_file_with_timestamp_pathlib(f"{REQ_DIR}/{p_name}_需求配置_V{v}.json", REQ_REMOVE_DIR)
        delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")
        app.storage.general["wait_review"][p_name].pop(v, None)
        container_row.delete()  # 删除整行UI
        dialog.close()

    def remove_requirement_dialog(container_row, p_name, v):
        dialog.clear()
        with dialog, ui.card():
            ui.label("⚠️ 危险操作").classes("text-lg font-bold text-red-600")
            ui.label(f"确认移除 {p_name}_V{v} 吗？移除后需联系管理员恢复。")
            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("取消", on_click=dialog.close).props("flat")
                ui.button("确认移除", color="red", on_click=lambda: remove_requirement_file(container_row, p_name, v))
        dialog.open()

    def get_requirement_page(project_name, ver):
        file_path = os.path.join(REQ_DIR, f"{project_name}_需求配置_V{ver}.json")
        ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    def get_req_page(project_name, version):
        file_path = os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_需求配置_V{version}.json")
        ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    def dele_temp_req_row(container_row, project_name, version):
        """删除暂存记录的行"""
        try:
            app.storage.general["temp_req"][current_user][project_name].remove(version)
            file_path = Path(os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_需求配置_V{version}.json"))
            file_path.unlink(missing_ok=True)
            container_row.delete()
            ui.notify("已移除暂存记录", type="positive")
        except Exception as e:
            logger.error(f"删除失败: {e}")
            ui.notify("删除失败", type="negative")

    # --- 核心UI渲染逻辑：单行刷新 ---

    def refresh_review_row(container, project_name, ver):
        """
        刷新单个评审条目的UI。
        如果状态变为'已审'，则删除该行；否则重新渲染按钮。
        """
        # 1. 获取最新状态
        try:
            review_data = app.storage.general["wait_review"][project_name][ver]
            review_state = review_data.get("state")
            submitter = review_data.get("submitter")
        except (KeyError, TypeError):
            container.delete()  # 数据丢失，删除UI
            return

        project_engineer_dic = get_project_engineer_project_list_dic()
        is_manager = current_role in ["研发经理"]
        # is_engineer = current_user == project_engineer_dic.get(project_name, "")
        is_engineer = current_user in project_engineer_dic
        # 2. 如果已审 或 属于项目工程师 且 为待修改 且不是研发经理（研发经理可能也兼任项目工程师），删除该行
        if review_state == "已审" or review_state == "待修改" and not is_manager and is_engineer:
            container.delete()
            return

        # 3. 重新渲染内容
        container.clear()
        with container:
            # --- 卡片布局 ---
            with ui.card().classes(
                "w-full p-3 border-l-4 border-l-blue-500 shadow-sm hover:shadow-md transition-shadow duration-300 bg-white"
            ):
                with ui.row().classes("w-full justify-between items-center wrap gap-2"):
                    # 左侧：信息展示
                    with ui.column().classes("gap-1"):
                        with ui.row().classes("items-center gap-2"):
                            ui.label(project_name).classes("font-bold text-gray-800 text-base")
                            project_engineer = app.storage.general.get("project_engineer", {}).get(
                                project_name, "未指定"
                            )
                            status_badge(f"V{ver}", "blue")
                            status_badge(review_state)
                        ui.label(f"提交人: {submitter}，项目工程师：{project_engineer}").classes(
                            "text-xs text-gray-500"
                        )

                    # 右侧：操作按钮组
                    with ui.row().classes("items-center gap-2"):
                        # 权限判断
                        if is_manager or is_engineer:
                            # 审核者视角
                            ui.button(icon="visibility", on_click=lambda: get_overviow_page(project_name, True)).props(
                                "flat round dense text-color=grey-7"
                            ).tooltip("查看需求详情")

                            ui.button(
                                icon="check",
                                color="green",
                                on_click=lambda: set_review_pass_dialog(container, project_name, ver),
                            ).props("flat round dense").tooltip("审核通过")

                            ui.button(
                                icon="edit_note", color="orange", on_click=lambda: set_review_revise(project_name, ver)
                            ).on("click", lambda: refresh_review_row(container, project_name, ver)).props(
                                "flat round dense"
                            ).tooltip("退回修改")

                            ui.button(
                                icon="delete",
                                color="red",
                                on_click=lambda: remove_requirement_dialog(container, project_name, ver),
                            ).props("flat round dense").tooltip("移除记录")
                        else:
                            # 普通提交者视角
                            ui.button(icon="visibility", on_click=lambda: get_overviow_page(project_name, True)).props(
                                "flat round dense text-color=grey-7"
                            ).tooltip("查看需求详情")
                            ui.button(
                                icon="edit_note", color="blue", on_click=lambda: get_requirement_page(project_name, ver)
                            ).props("flat round dense").tooltip("配置需求")

                            ui.button(
                                icon="replay", color="orange", on_click=lambda: set_review_revise(project_name, ver)
                            ).on("click", lambda: refresh_review_row(container, project_name, ver)).props(
                                "flat round dense"
                            ).tooltip("申请修改")

    # -------------------------------------------------------------------------
    # 页面整体布局
    # -------------------------------------------------------------------------
    # 1. 顶部导航栏 (深色主题)
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("项目待办项").classes(
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
    with ui.element("div").classes("w-full h-[calc(100vh-5rem)] bg-gray-50 p-4 md:p-6"):
        project_engineer_dic = get_project_engineer_project_list_dic()

        # Grid: 大屏12列，左8右4；小屏自动换行
        with ui.grid(columns=12).classes("w-full gap-4"):
            # =========================================================
            # 左侧列 (主要工作流)
            # =========================================================
            with ui.column().classes("col-span-12 lg:col-span-6 gap-4"):
                # A. 待判断概述 (Priority Task)
                if current_role in module_show_data.get("overview_charge_pending_module", []):
                    my_pending = app.storage.general["overview_charge_pending"].get(current_user, {})
                    if my_pending:
                        with ui.card().classes("w-full rounded-xl shadow-sm border border-red-100 bg-white"):
                            ui_card_header("待处理：项目概述", "edit_document", "red-600")
                            with ui.column().classes("w-full gap-2 px-1"):
                                over_flat = app.storage.general.get("over_config_data_flat", {})
                                for project_name, state_dic in list(my_pending.items()):
                                    # 无内容的必填概述分项数量
                                    false_num = list(state_dic.values()).count("缺必填")
                                    # 无内容的需填概述分项数量
                                    need_num = list(state_dic.values()).count("缺需填")
                                    # 待确认的概述分项数量
                                    none_num = list(state_dic.values()).count("有待定")
                                    # --- 新增：提取并构建 HTML 格式的 Tooltip 内容 ---
                                    false_items = [k for k, v in state_dic.items() if v == "缺必填"]
                                    need_items = [k for k, v in state_dic.items() if v == "缺需填"]
                                    none_items = [k for k, v in state_dic.items() if v == "有待定"]

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
                                    # ------------------------------------

                                    # 每一行项目
                                    if false_num > 0 or none_num > 0:
                                        row_container = ui.row().classes(
                                            "w-full items-center justify-between p-3 bg-red-50 rounded-lg border border-red-100 hover:bg-red-100 transition-colors"
                                        )
                                    else:
                                        row_container = ui.row().classes(
                                            "w-full items-center justify-between p-3 bg-amber-50 rounded-lg border border-amber-100 hover:bg-amber-100 transition-colors"
                                        )
                                    with row_container:
                                        title_label = ui.label(
                                            f"{project_name}（{str(false_num)}项必填概述无内容，{str(need_num)}项需填概述无内容，{str(none_num)}项概述待确认）"
                                        ).classes("font-medium text-gray-800 cursor-help")

                                        # 使用上下文管理器结合 ui.html 渲染支持换行的原生 HTML
                                        with title_label:
                                            with ui.tooltip().classes("text-xs bg-gray-600/90 text-white p-2"):
                                                ui.html(tooltip_html, sanitize=False)

                                        ui.button(
                                            "去处理",
                                            icon="arrow_forward",
                                            on_click=lambda pn=project_name: get_overviow_page(pn, False),
                                        ).props("flat dense color=red size=sm")

                # B. 需求评审队列 (Review Queue)
                if (
                    current_role in module_show_data.get("wait_review_module", [])
                    or current_user in project_engineer_dic
                ):
                    with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white"):
                        ui_card_header("需求评审看板", "rate_review", "blue-600")

                        review_container = ui.column().classes("w-full gap-3")
                        has_review_data = False

                        with review_container:
                            if app.storage.general.get("wait_review", {}):
                                for project_name, ver_dic in app.storage.general["wait_review"].items():
                                    for ver, dic in ver_dic.items():
                                        # 过滤显示逻辑
                                        is_manager = current_role in ["研发经理"]
                                        is_engineer = project_name in project_engineer_dic.get(current_user, [])
                                        is_submitter = dic.get("submitter") == current_user

                                        should_show = False
                                        # 销售查看非已审项目待办项
                                        if (is_manager or is_submitter) and dic.get("state") != "已审":
                                            should_show = True
                                        # 研发经理或项目工程师查看待审项目待办项
                                        elif is_engineer and dic.get("state") == "待审":
                                            should_show = True

                                        if should_show:
                                            has_review_data = True
                                            # 创建行容器
                                            row = ui.row().classes("w-full p-0 gap-0")
                                            refresh_review_row(row, project_name, ver)

                        if not has_review_data:
                            with ui.column().classes("w-full items-center py-8 text-gray-400"):
                                ui.icon("task_alt", size="4em").classes("mb-2 opacity-50")
                                ui.label("当前没有待评审的需求").classes("text-sm")

            # =========================================================
            # 右侧列
            # =========================================================
            with ui.column().classes("col-span-12 lg:col-span-6 gap-4"):
                # D. 草稿箱 (Drafts)
                if current_role in module_show_data.get("temp_req_module", []):
                    with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white"):
                        ui_card_header("需求草稿箱", "save_as", "amber-600")

                        temp_req_dic = app.storage.general.get("temp_req", {})
                        has_drafts = False

                        with ui.scroll_area().classes("h-64 w-full pr-2"):
                            for user, project_dic in temp_req_dic.items():
                                if user == current_user or current_role == "研发经理":
                                    for project_name, version_li in project_dic.items():
                                        for version in version_li:
                                            has_drafts = True
                                            row = ui.row().classes(
                                                "w-full items-center justify-between py-2 border-b border-gray-100 last:border-0"
                                            )
                                            with row:
                                                with ui.column().classes("gap-0"):
                                                    ui.label(project_name).classes("font-medium text-sm text-gray-700")
                                                    ui.label(f"V{version} • {user}").classes("text-xs text-gray-400")

                                                with ui.row().classes("gap-1"):
                                                    # 经理只能看，本人可编辑
                                                    btn_icon = (
                                                        "visibility"
                                                        if (current_role == "研发经理" and user != current_user)
                                                        else "edit"
                                                    )
                                                    ui.button(
                                                        icon=btn_icon,
                                                        on_click=lambda pn=project_name, v=version: get_req_page(pn, v),
                                                    ).props("flat dense size=sm color=amber").tooltip("查看/编辑")

                                                    # 只有非经理(本人)可以删除
                                                    if current_role != "研发经理":
                                                        ui.button(
                                                            icon="close",
                                                            color="red",
                                                            on_click=lambda r=row, pn=project_name, v=version: (
                                                                dele_temp_req_row(r, pn, v)
                                                            ),
                                                        ).props("flat dense size=sm").tooltip("丢弃草稿")

                        if not has_drafts:
                            ui.label("暂无草稿记录").classes("text-sm text-gray-400 p-2")


# 注意：此文件被设计为模块导入模式，不需要 ui.run()
