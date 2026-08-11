# -*- encoding: utf-8 -*-
import asyncio
import copy
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from nicegui import app, ui

from .. import db_storage
from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR, REQ_REMOVE_DIR
from ..overview_warning import get_overview_counts, get_overview_warning, sort_overview_pending_items
from ..requirement_overview_impact import RequirementOverviewImpactConfigError
from ..utils import (
    delete_file,
    get_cache_busted_path,
    get_overviow_page,
    get_project_engineer_project_list_dic,
    get_requirement_overview_impacts,
    logout,
    move_file_with_timestamp_pathlib,
    prepare_requirement_version_tidy,
    project_summary_update,
    refresh_overview_pending_labels,
    restore_file_bytes,
    restore_overview_active_state,
    set_overview_active_state,
    set_project_custom_labels,
    snapshot_file_bytes,
    setup_global_activity_tracking,
    validate_search_path,
    validate_svn_url,
)

# 获取 logger
logger = logging.getLogger(__name__)

# 同一项目的审批必须串行，避免重复点击或不同客户端同时通过同一版本。
_requirement_review_locks = defaultdict(asyncio.Lock)


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

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()
    ui.add_css("""
        @keyframes overview-warning-shake {
            0%, 100% { transform: translateX(0); }
            15%, 45%, 75% { transform: translateX(-4px); }
            30%, 60%, 90% { transform: translateX(4px); }
        }

        @keyframes overview-warning-flash {
            0%, 100% {
                opacity: 1;
                transform: scale(1);
                text-shadow: 0 0 2px currentColor;
            }
            50% {
                opacity: 0.25;
                transform: scale(1.2);
                text-shadow: 0 0 8px currentColor;
            }
        }

        .overview-warning-shake {
            animation: overview-warning-shake 1.0s ease-in-out infinite;
            transform-origin: center;
            will-change: transform;
        }

        .overview-warning-flash {
            animation: overview-warning-flash 1.6s ease-in-out infinite;
            will-change: opacity, transform;
        }

        .overview-warning-number {
            display: inline-block;
        }

        @media (prefers-reduced-motion: reduce) {
            .overview-warning-shake,
            .overview-warning-flash {
                animation: none;
            }
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

    def set_review_revise(p_name, v):
        app.storage.general["wait_review"][p_name][v]["state"] = "待修改"

    async def set_review_pass(container_row, p_name, v):
        """审核通过逻辑"""
        async with _requirement_review_locks[p_name]:
            current_record = app.storage.general.get("wait_review", {}).get(p_name, {}).get(v, {})
            if current_record.get("state") != "待审":
                ui.notify("需求非待审状态，无法通过，已刷新列表", type="warning")
                refresh_review_row(container_row, p_name, v)
                return

            candidate_path = ""
            official_path = Path(OVER_DIR) / f"{p_name}_概述整理.json"
            try:
                # 先整理成不可见候选文件；正式概述文件此时完全不动。
                candidate_path, candidate_data = await prepare_requirement_version_tidy(p_name, v)
                if not candidate_path:
                    raise RuntimeError("需求概述候选文件整理失败")

                affected_labels, missing_node_ids, change_node_ids = get_requirement_overview_impacts(
                    candidate_data,
                    v,
                    p_name,
                )
                logger.info(
                    "需求审批影响范围已解析: project=%s, version=%s, changes=%s, affected_labels=%s, "
                    "unmapped_node_ids=%s",
                    p_name,
                    v,
                    {key: sorted(value) for key, value in change_node_ids.items()},
                    sorted(affected_labels),
                    sorted(missing_node_ids),
                )
            except (RequirementOverviewImpactConfigError, RuntimeError, OSError, ValueError) as exc:
                if candidate_path:
                    Path(candidate_path).unlink(missing_ok=True)
                logger.error("审核通过前整理需求概述或解析影响配置失败: project=%s, version=%s", p_name, v, exc_info=True)
                ui.notify(
                    f"需求概述整理失败，已中止审批通过：{exc}",
                    type="negative",
                    position="center",
                    timeout=0,
                    close_button="✖",
                )
                return

            try:
                official_existed, official_content = snapshot_file_bytes(official_path)
            except OSError as exc:
                Path(candidate_path).unlink(missing_ok=True)
                logger.error("读取正式概述文件快照失败: %s", official_path, exc_info=True)
                ui.notify(f"无法建立正式概述文件回滚点，审批已中止：{exc}", type="negative", position="center")
                return

            overview_rollback_context = {}
            try:
                overview_success, changed_labels = await set_overview_active_state(
                    p_name,
                    v,
                    affected_labels,
                    rollback_context=overview_rollback_context,
                )
            except Exception as exc:
                Path(candidate_path).unlink(missing_ok=True)
                logger.error("审核通过前更新概述激活状态异常: project=%s, version=%s", p_name, v, exc_info=True)
                ui.notify(f"概述状态更新异常，审批未生效：{exc}", type="negative", position="center")
                return
            if not overview_success:
                Path(candidate_path).unlink(missing_ok=True)
                logger.error("审核通过前更新概述激活状态失败: project=%s, version=%s", p_name, v)
                ui.notify(
                    "概述状态更新失败，候选概述文件已丢弃，审批未生效。",
                    type="negative",
                    position="center",
                    timeout=0,
                    close_button="✖",
                )
                return

            overview_before = overview_rollback_context.get("before", {})
            overview_after = overview_rollback_context.get("after", overview_before)
            review_record_before = copy.deepcopy(current_record)
            max_versions = app.storage.general.setdefault("project_req_max_ver", {})
            max_version_existed = p_name in max_versions
            max_version_before = max_versions.get(p_name)
            checked_versions = app.storage.general.setdefault("overview_active_state_checked_versions", {})
            checked_version_existed = p_name in checked_versions
            checked_version_before = checked_versions.get(p_name)

            try:
                # 候选文件与正式文件位于同一目录，替换动作在文件系统层面是原子的。
                os.replace(candidate_path, official_path)
                app.storage.general["wait_review"][p_name][v]["state"] = "已审"
                app.storage.general["wait_review"][p_name][v]["pass_time"] = datetime.now().isoformat()
                max_versions[p_name] = v
                checked_versions[p_name] = v
            except Exception as exc:
                logger.error("发布审批结果失败，开始补偿回滚: project=%s, version=%s", p_name, v, exc_info=True)
                file_rollback_success = True
                try:
                    restore_file_bytes(official_path, official_existed, official_content)
                except Exception:
                    file_rollback_success = False
                    logger.critical("正式概述文件补偿回滚失败: %s", official_path, exc_info=True)

                state_rollback_success = True
                try:
                    app.storage.general["wait_review"][p_name][v] = review_record_before
                    if max_version_existed:
                        max_versions[p_name] = max_version_before
                    else:
                        max_versions.pop(p_name, None)
                    if checked_version_existed:
                        checked_versions[p_name] = checked_version_before
                    else:
                        checked_versions.pop(p_name, None)
                except Exception:
                    state_rollback_success = False
                    logger.critical(
                        "需求审批状态补偿回滚失败: project=%s, version=%s",
                        p_name,
                        v,
                        exc_info=True,
                    )

                overview_rollback_success = await restore_overview_active_state(
                    p_name,
                    overview_before,
                    overview_after,
                )
                Path(candidate_path).unlink(missing_ok=True)
                rollback_message = "审批发布失败，所有已完成步骤均已回滚。"
                if not file_rollback_success or not state_rollback_success or not overview_rollback_success:
                    rollback_message = "审批发布失败，且自动回滚不完整，请立即联系管理员检查。"
                ui.notify(
                    f"{rollback_message}错误：{exc}",
                    type="negative",
                    position="center",
                    timeout=0,
                    close_button="✖",
                )
                return

            # 下面都是已提交后的派生缓存刷新，不再参与审批事务。
            try:
                if changed_labels:
                    from ..components import OverviewVersionManager

                    for label in changed_labels:
                        OverviewVersionManager.bump(p_name, label)
                refresh_overview_pending_labels(p_name, affected_labels)
                Path(OVER_DIR, f"{p_name}_概述整理_temp.json").unlink(missing_ok=True)
                set_project_custom_labels(p_name)
            except Exception:
                logger.error(
                    "需求审批已成功，但派生缓存刷新失败: project=%s, version=%s",
                    p_name,
                    v,
                    exc_info=True,
                )

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

    def create_revoke_dialog():
        """构建研发经理专属的撤销审批对话框"""
        dialog.clear()
        # 运行时状态初始化，避免使用全局变量造成的静态类型冲突或闭包引用问题
        revoke_state = {
            "project_name": "",
            "action_type": "",  # "none", "delete", "revert"
            "target_ver": "",
            "prev_ver": "",
        }

        # ui.card(): NiceGUI 卡片容器组件，提供背景和阴影包裹。
        with dialog, ui.card().classes("w-full p-4"):
            # ui.label(): NiceGUI 文本标签组件。
            ui.label("撤销需求审批 (研发经理专属)").classes("text-lg font-bold text-red-600 mb-4")

            # ui.input(): NiceGUI 文本输入框组件，用于接收用户输入内容。
            model_input = ui.input(label="请输入要撤销审批的项目型号", placeholder="严格区分大小写...").classes(
                "w-full mb-2"
            )

            feedback_label = ui.label("").classes("w-full mt-2 text-sm font-medium whitespace-pre-wrap")

            # ui.row(): NiceGUI 行容器组件，用于水平排列内部元素。
            action_row = ui.row().classes("w-full justify-end mt-4")

            def check_status():
                """检查项目状态并给出将要执行的预判提示"""
                p_name = model_input.value.strip()
                if not p_name:
                    feedback_label.set_text("请输入有效的型号名称。")
                    feedback_label.classes(replace="text-gray-500")
                    confirm_btn.set_visibility(False)
                    return

                # app.storage.general: NiceGUI 全局通用存储字典，常用于持久化后端运行时的跨用户共享数据。
                wait_review_data = app.storage.general.get("wait_review", {}).get(p_name, {})
                max_ver = app.storage.general.get("project_req_max_ver", {}).get(p_name)

                if not max_ver or max_ver not in wait_review_data:
                    feedback_label.set_text(f"【{p_name}】当前无待处理或已审记录，无法回退。")
                    feedback_label.classes(replace="text-gray-500")
                    confirm_btn.set_visibility(False)
                    return

                current_state = wait_review_data[max_ver].get("state")
                if current_state != "已审":
                    feedback_label.set_text(f"【{p_name}】当前V{max_ver}版本状态为“{current_state}”，无需撤销。")
                    feedback_label.classes(replace="text-gray-500")
                    confirm_btn.set_visibility(False)
                    return

                # 记录有效状态准备执行
                revoke_state["project_name"] = p_name
                revoke_state["target_ver"] = max_ver

                if max_ver == "1.0":
                    revoke_state["action_type"] = "delete"
                    feedback_label.set_text(
                        f"⚠️ 打算执行：\n1.复原型号【{p_name}】1.0 版本的审批状态为“待审”，该项目最高需求版本记录删除。\n2.删除概述整理文件，迫使下次访问概述页面时重新生成，确保版本回退后概述内容与需求状态一致。"
                    )
                    feedback_label.classes(replace="text-red-600")
                else:
                    revoke_state["action_type"] = "revert"
                    prev_ver = f"{int(float(max_ver)) - 1}.0"
                    revoke_state["prev_ver"] = prev_ver
                    feedback_label.set_text(
                        f"⚠️ 打算执行：\n1.将型号【{p_name}】V{max_ver} 版本的状态强行恢复为‘待审’，系统最高版本号回退至 V{prev_ver}。\n2.删除概述整理文件，迫使下次访问概述页面时重新生成，确保版本回退后概述内容与需求状态一致。"
                    )
                    feedback_label.classes(replace="text-orange-600")

                # set_visibility(): NiceGUI 元素的显隐控制函数。
                confirm_btn.set_visibility(True)

            def execute_revoke():
                """确认执行撤销操作"""
                p_name = revoke_state["project_name"]
                t_ver = revoke_state["target_ver"]

                if revoke_state["action_type"] == "delete":
                    app.storage.general["wait_review"][p_name][t_ver]["state"] = "待审"
                    app.storage.general["wait_review"][p_name][t_ver].pop("pass_time", None)
                    app.storage.general["project_req_max_ver"].pop(p_name, None)
                elif revoke_state["action_type"] == "revert":
                    app.storage.general["wait_review"][p_name][t_ver]["state"] = "待审"
                    # 清理该版本的审批通过时间
                    app.storage.general["wait_review"][p_name][t_ver].pop("pass_time", None)
                    app.storage.general["project_req_max_ver"][p_name] = revoke_state["prev_ver"]
                # 删除概述整理文件，迫使下次访问概述页面时重新生成，确保版本回退后概述内容与需求状态一致
                # 优化后的写法：直接使用 Path 对象与 / 运算符进行路径拼接
                overview_file_path = Path(OVER_DIR) / f"{p_name}_概述整理.json"
                overview_file_path.unlink(missing_ok=True)

                # ui.notify(): NiceGUI 页面消息通知组件，用于屏幕上方/边缘弹出轻量反馈。
                ui.notify(f"【{p_name}】审批已成功撤销，页面即将刷新", type="positive")
                dialog.close()
                model_input.value = ""
                feedback_label.set_text("")
                confirm_btn.set_visibility(False)

                # ui.timer(): NiceGUI 延时执行函数； ui.navigate.reload(): 刷新当前页面。
                ui.timer(1.0, lambda: ui.navigate.reload())

            # ui.button(): NiceGUI 按钮组件。
            ui.button("检索/预判", on_click=check_status).props("outline color=primary").classes("w-full mt-2")

            with action_row:
                ui.button("取消", on_click=dialog.close).props("flat text-color=grey")
                confirm_btn = ui.button("确认撤销", color="red", on_click=execute_revoke)
                confirm_btn.set_visibility(False)

        dialog.open()

    # 1. 撤回逻辑 (不归档，转为 withdrawn 状态留给用户修改)
    async def handle_withdraw(req_id):
        app.storage.general["overview_change_requests"][req_id]["status"] = "withdrawn"
        ui.notify("申请已撤回")
        ui.navigate.reload()

    # 2. 审批通过 (执行物理动作 + 数据更新 + 归档)
    async def handle_approve(req_id, req_data):
        # --- 业务校验逻辑 ---
        config = req_data["config"]
        project = req_data["project_name"]
        new_val = req_data["new_content"]

        is_valid = True
        msg = ""
        if req_data["chip_type"] == "search":
            is_valid, _, _, _, msg = await validate_search_path(new_val, config, [project])
        elif req_data["chip_type"] == "svn":
            is_valid, _, _, msg = await validate_svn_url(new_val, config, [project])

        if not is_valid:
            ui.notify(f"业务校验未通过：{msg}", type="negative")
            return

        # --- 执行修改 ---
        try:
            # 移动物理文件
            if req_data.get("temp_file_path"):
                import shutil

                shutil.move(req_data["temp_file_path"], Path(config["upload_path"]) / new_val)

            # 更新数据库
            base_path = [f"{project}_over_data", req_data["label"], req_data["chip_id"]]
            if req_data["action"] == "modify":
                await db_storage.set_deep_item(base_path + ["content"], new_val)
                if req_data["chip_type"] == "test":
                    await db_storage.set_deep_item(base_path + ["test_select_data"], req_data["new_test_data"])
            elif req_data["action"] == "delete":
                await db_storage.del_deep_item(base_path)

            # 核心：触发即时刷新
            from ..components import OverviewVersionManager

            OverviewVersionManager.bump(project, req_data["label"])

            # 归档
            await handle_archive(req_id, "approved")
            ui.notify("审批通过并已同步刷新")
            ui.navigate.reload()
        except Exception as e:
            ui.notify(f"错误: {e}", type="negative")

    # 3. 归档逻辑 (清理 app.storage.general，写入数据库持久化)
    async def handle_archive(req_id, final_status):
        archive_data = app.storage.general["overview_change_requests"].pop(req_id)
        archive_data["status"] = final_status
        archive_data["finish_time"] = datetime.now().isoformat()

        # 写入专门的数据库归档节点，防止 general 膨胀
        await db_storage.atomic_deep_update(
            ["overview_change_archives"], lambda old: {**(old or {}), req_id: archive_data}
        )

    def open_reject_modal(req_id):
        """弹出驳回理由填写对话框"""
        dialog.clear()
        with dialog, ui.card().classes("w-[400px]"):
            ui.label("驳回变更申请").classes("text-lg font-bold text-red-600 mb-2")

            # 驳回理由输入框
            reason_input = (
                ui.textarea("驳回理由 (必填)", placeholder="请写明为什么驳回该修改...")
                .classes("w-full")
                .props("outlined autofocus")
            )

            async def confirm_reject():
                reason = reason_input.value
                if not reason or not reason.strip():
                    ui.notify("请填写驳回理由！", type="warning", position="top")
                    return

                try:
                    # 1. 更新申请状态与驳回理由
                    app.storage.general["overview_change_requests"][req_id]["status"] = "rejected"
                    app.storage.general["overview_change_requests"][req_id]["reject_reason"] = reason.strip()

                    ui.notify("已驳回该申请", type="positive")
                    dialog.close()
                    ui.navigate.reload()  # 刷新页面以更新列表
                except Exception as e:
                    ui.notify(f"操作失败: {e}", type="negative")

            with ui.row().classes("w-full justify-end mt-4 gap-2"):
                ui.button("取消", on_click=dialog.close).props("flat text-color=grey")
                ui.button("确认驳回", color="red", on_click=confirm_reject)

        dialog.open()

    def trigger_edit(req_data):
        """引导申请人跳转回项目概述页面进行修改"""
        project_name = req_data.get("project_name")

        # 弹出提示，告知用户接下来的操作
        ui.notify(
            f"正在跳转至【{project_name}】项目概述页面...\n请在对应项上再次点击【申请变更】按钮即可继续修改。",
            type="info",
            position="center",
            timeout=3000,
            multi_line=True,
        )

        # 延迟 1.5 秒后，利用现有的 utils 函数跳转到该项目的概述页面
        ui.timer(1.5, lambda: get_overviow_page(project_name, False), once=True)

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
                # 权限管控：仅研发经理可看见撤销入口
                if current_role == "研发经理":
                    ui.menu_item("撤销需求审批", on_click=lambda: create_revoke_dialog()).classes(
                        "text-red-600 font-bold"
                    )
                    ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # 2. 主内容区域 (Grid布局)
    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.element("div").classes("w-full h-full overflow-y-auto overflow-x-hidden p-4 md:p-6"):
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
                                    project_summary = app.storage.general.get("project_summary", {})

                                    project_states = {
                                        project_name: project_summary.get(project_name, {}).get("state", "未知")
                                        for project_name in my_pending
                                    }
                                    visible_pending_items = [
                                        (project_name, state_dic)
                                        for project_name, state_dic in my_pending.items()
                                        if project_states[project_name] not in ["作废", "待定"]
                                    ]
                                    sorted_pending_items = sort_overview_pending_items(
                                        visible_pending_items, project_states
                                    )

                                    for project_name, state_dic in sorted_pending_items:
                                        # 1. 获取已经过滤过的当前项目状态
                                        proj_state = project_states[project_name]
                                        counts = get_overview_counts(state_dic)
                                        labels_map = {
                                            "false": "项必填概述无内容",
                                            "none": "项概述待确认",
                                            "need": "项需填概述无内容",
                                        }

                                        # 2. 综合项目阶段与概述问题，选出当前最高警示项
                                        warning = get_overview_warning(proj_state, counts)
                                        if warning is None:
                                            continue  # 没有任何积压，跳过渲染

                                        active_key, warning_level = warning

                                        # 3. 根据综合警示级别决定当前行的视觉色彩与动画表现
                                        if warning_level == 4:
                                            # 4级警示：紫色行，数字闪烁
                                            row_bg = "bg-violet-200 border-violet-400 hover:bg-violet-300"
                                            base_color = "violet"
                                            is_flash = True
                                        elif warning_level == 3:
                                            # 3级警示：红色行，数字闪烁
                                            row_bg = "bg-red-50 border-red-200 hover:bg-red-100"
                                            base_color = "red"
                                            is_flash = True
                                        elif warning_level == 2:
                                            # 2级警示：橙色行，数字闪烁
                                            row_bg = "bg-orange-50 border-orange-200 hover:bg-orange-100"
                                            base_color = "orange"
                                            is_flash = True
                                        elif warning_level == 1:
                                            # 1级警示：黄色行，数字闪烁
                                            row_bg = "bg-amber-50 border-amber-200 hover:bg-amber-100"
                                            base_color = "amber"
                                            is_flash = False
                                        else:
                                            # 0级示：蓝色行，仅加粗不闪烁
                                            row_bg = "bg-blue-50 border-blue-200 hover:bg-blue-100"
                                            base_color = "blue"
                                            is_flash = False

                                        # --- 提取并构建 HTML 格式的 Tooltip 内容 ---
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

                                        # 4. 渲染最终容器
                                        row_animation = "overview-warning-shake" if warning_level >= 3 else ""
                                        row_container = ui.row().classes(
                                            f"w-full items-center justify-between p-3 rounded-lg border "
                                            f"transition-colors {row_bg} {row_animation}"
                                        )

                                        with row_container:
                                            # 构造富文本标题（保持原有的中文阅读排版顺序：必填 -> 需填 -> 待确认）
                                            parts_html = []
                                            display_order = ["false", "need", "none"]

                                            for k in display_order:
                                                num = counts[k]
                                                text = labels_map[k]
                                                # 如果当前项正是触发最高优先级的项，实施视觉凸显
                                                if k == active_key:
                                                    if is_flash:
                                                        # 缩放、明暗和发光同时变化，使数字警示更醒目
                                                        num_html = (
                                                            '<span class="overview-warning-flash overview-warning-number '
                                                            f'font-black text-lg text-{base_color}-600">{num}</span>'
                                                        )
                                                    else:
                                                        # 仅加粗高亮
                                                        num_html = f'<span class="font-black text-lg text-{base_color}-600">{num}</span>'
                                                else:
                                                    num_html = str(num)
                                                parts_html.append(f"{num_html}{text}")

                                            title_html = f'<span class="font-medium text-gray-800">{project_name}（{"，".join(parts_html)}）</span>'

                                            # ui.element: 创建基础 DOM 元素作为包裹层，避开 v-html 的内部覆盖效应
                                            title_wrapper = ui.element("div").classes("cursor-help flex items-center gap-2")

                                            with title_wrapper:
                                                ui.html(title_html, sanitize=False)
                                                if warning_level >= 3:
                                                    ui.badge("尽快处理", color=base_color).classes(
                                                        "overview-warning-flash font-bold"
                                                    )
                                                with ui.tooltip().classes("text-xs bg-gray-600/90 text-white p-2"):
                                                    ui.html(tooltip_html, sanitize=False)

                                            # 侧边按钮的颜色与该行代表的优先级颜色基调保持一致
                                            ui.button(
                                                "去处理",
                                                icon="arrow_forward",
                                                on_click=lambda _=None, pn=project_name: get_overviow_page(pn, False),
                                            ).props(f"flat dense color={base_color} size=sm")

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
                                                        ui.label(project_name).classes(
                                                            "font-medium text-sm text-gray-700"
                                                        )
                                                        ui.label(f"V{version} • {user}").classes(
                                                            "text-xs text-gray-400"
                                                        )

                                                    with ui.row().classes("gap-1"):
                                                        # 经理只能看，本人可编辑
                                                        btn_icon = (
                                                            "visibility"
                                                            if (current_role == "研发经理" and user != current_user)
                                                            else "edit"
                                                        )
                                                        ui.button(
                                                            icon=btn_icon,
                                                            on_click=lambda pn=project_name, v=version: get_req_page(
                                                                pn, v
                                                            ),
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
                    # 概述修改申请审批
                    if current_role in module_show_data.get("overview_change_requests", []):
                        with ui.card().classes("w-full rounded-xl shadow-sm border border-gray-100 bg-white mt-4"):
                            ui_card_header("概述变更审批", "fact_check", "orange-600")

                            all_requests = app.storage.general.get("overview_change_requests", {})
                            with ui.column().classes("w-full gap-2"):
                                for rid, req in all_requests.items():
                                    is_manager = current_role == "研发经理"
                                    is_mine = req["submitter"] == current_user

                                    if (is_manager and req["status"] == "pending") or is_mine:
                                        with ui.row().classes(
                                            "w-full items-center justify-between p-3 bg-gray-50 rounded border"
                                        ):
                                            with ui.column().classes("gap-1"):
                                                ui.label(f"{req['project_name']} | {req['action']}").classes(
                                                    "font-bold"
                                                )
                                                ui.label(f"{req['old_content']} → {req['new_content']}").classes(
                                                    "text-sm text-gray-600"
                                                )
                                                status_badge(req["status"])  # 需在 information.py 定义该组件

                                            with ui.row().classes("gap-2"):
                                                if is_manager and req["status"] == "pending":
                                                    ui.button(
                                                        "通过",
                                                        color="green",
                                                        on_click=lambda r=rid, d=req: handle_approve(r, d),
                                                    ).props("dense size=sm")
                                                    ui.button(
                                                        "驳回", color="red", on_click=lambda r=rid: open_reject_modal(r)
                                                    ).props("dense size=sm")

                                                if is_mine:
                                                    if req["status"] in ["rejected", "withdrawn"]:
                                                        # 触发 components.py 中的对话框重新编辑
                                                        ui.button(
                                                            "修改再提",
                                                            color="blue",
                                                            on_click=lambda d=req: trigger_edit(d),
                                                        ).props("dense size=sm")
                                                        ui.button(
                                                            "放弃申请",
                                                            color="grey",
                                                            on_click=lambda r=rid: handle_archive(r, "cancelled"),
                                                        ).props("dense size=sm")
                                                    if req["status"] == "pending":
                                                        ui.button(
                                                            "撤回",
                                                            color="orange",
                                                            on_click=lambda r=rid: handle_withdraw(r),
                                                        ).props("dense size=sm")
