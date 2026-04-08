# -*- encoding: utf-8 -*-
import copy  # copy: Python标准库，用于创建对象的副本
import logging
import os
import time
import uuid  # uuid: Python标准库，用于生成全局唯一的标识符
from datetime import datetime

from nicegui import app, ui  # nicegui: 第三方轻量级Python Web框架，用于纯Python编写前端UI

from .. import db_storage
from ..config import (
    ECN_ALLOWED_PROJECT_STATES,
    ECN_SCHEMA_CONFIG,
    ECN_SCHEME_INITIATOR_ROLES,
    ECN_SCHEME_WRITER_ROLES,
    ECN_WORKFLOW_ROUTES,
    FILES_URL_DIR,
    IMG_DIR,
    PRESET_AVATARS,
    UPLOADS_DIR,
    ECNState,
)
from ..utils import get_cache_busted_path, logout

logger = logging.getLogger(__name__)


# ==========================================
# 数据模板与合并机制 (防御性架构核心)
# ==========================================
def get_ecn_template() -> dict:
    """
    生成当前系统最新版本的 ECN 标准数据结构模板。
    """
    return {
        "ecn_id": "",
        "form_no": "RF-FM-280-A4",
        "basic_info": {
            "title": "",
            "applicant_dept": "",
            "applicant": "",
            "apply_date": "",
            "requirement_date": "",
            "file_no": "",
            "nature": "永久变更",
            "erp_no": "",
            "reasons": {r: False for r in ECN_SCHEMA_CONFIG["reasons"]},
            "other_reason_desc": "",
            "requirements": [],
            "reason_desc": "",
        },
        "target_projects": [],
        "review_info": {
            "expanded_projects_mass": [],
            "expanded_projects_non_mass": [],
            "impacts": {dim: False for dim in ECN_SCHEMA_CONFIG["impact_dimensions"]},
            "involved_docs": {doc: False for doc in ECN_SCHEMA_CONFIG["document_types"]},
            "other_docs_desc": "",
            "involved_materials": {
                mat: {act: False for act in ECN_SCHEMA_CONFIG["material_actions"]}
                for mat in ECN_SCHEMA_CONFIG["material_categories"]
            },
            # "sop_impact": "无影响",
            # "fixture_impact": "无影响",
            # "tool_impact": "无影响",
            # "tool_impact_desc": "",
        },
        "execution_info": {
            "traceability_level": "无影响",
            "handling_measures": {"报废": False, "返工": False},
            "trial_conclusion": "",
        },
        "change_items": [],
        "workflow": {
            "current_state": ECNState.DRAFT,
            "current_phase": "ECR_PHASE",
            "current_step_index": 0,
            "route_type": "",
            "pending_roles": [],
            "step_approvals": {},
            "scheme_participants": {},
        },
        "approval_log": [],
        "timestamp": {},
    }


def merge_with_template(db_data: dict, template: dict) -> dict:
    """
    将数据库读取的旧数据与最新模板进行深度合并。
    防止旧版单据缺少新字段引发报错，同时修正被污染的旧数据类型。
    """
    # copy.deepcopy: 创建深层隔离的副本，避免污染全局模板字典
    merged = copy.deepcopy(template)

    if not isinstance(db_data, dict):
        return merged

    for key, value in db_data.items():
        if key in merged:
            if isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = merge_with_template(value, merged[key])
            elif type(merged[key]) is type(value) or merged[key] is None or value is None:
                # 严格类型校验：仅当旧数据类型与模板一致时才覆盖。
                # 这直接解决了以前旧版本中 '电子料' 可能是 bool 类型从而冲掉 dict 类型的问题
                merged[key] = copy.deepcopy(value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


# ==========================================
# 辅助与业务逻辑函数 (独立于 UI 树)
# ==========================================
def get_dept_from_role(role: str) -> str:
    """
    如果传入的角色名称里，含有指定字符串，返回该字符串对应的该角色的部门名称
    """
    role_to_dept_map = {
        "研发": "研发部",
        "销售": "销售部",
        "工程": "工程部",
        "生产": "生产部",
        "质量": "质量部",
        "采购": "采购部",
        "PMC": "物资部",
    }
    for key, dept in role_to_dept_map.items():
        if key in role:
            return dept
    return "其它部门"


def generate_ecn_id(all_ecns: dict) -> str:
    """
    找到all_ecns里最大的当前日期最大序号，加1生成新的ECN编号
    """
    today_str = datetime.now().strftime("%y%m%d")
    prefix = f"ECN{today_str}"
    max_count = 0
    for ecn_id in all_ecns.keys():
        if ecn_id.startswith(prefix):
            try:
                num = int(ecn_id[-2:])
                if num > max_count:
                    max_count = num
            except ValueError:
                pass
    return f"{prefix}{str(max_count + 1).zfill(2)}"


def generate_initial_ecn_data(applicant: str, role: str, all_ecns: dict) -> dict:
    """
    在模板基础上，初始化运行时强相关的动态ECN数据（如单号、时间、申请人）
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ecn_id = generate_ecn_id(all_ecns)
    applicant_dept = get_dept_from_role(role)

    new_data = get_ecn_template()
    new_data["ecn_id"] = ecn_id
    new_data["basic_info"]["applicant_dept"] = applicant_dept
    new_data["basic_info"]["applicant"] = applicant
    new_data["basic_info"]["apply_date"] = now_str
    new_data["basic_info"]["file_no"] = ecn_id
    new_data["timestamp"][now_str] = f"由 {applicant} 创建草稿"

    return new_data


# ==========================================
# ECN 专属数据写入代理 (O(1) 轮询架构核心)
# ==========================================
async def save_ecn_deep_item(path: list, data):
    """拦截深层数据保存，并更新全局版本戳"""
    await db_storage.set_deep_item(path, data)
    # 写入一个仅供 ECN 轮询使用的时间戳
    await db_storage.set_item("ecn_global_version_stamp", time.time())


async def save_ecn_root_item(key: str, data):
    """拦截根节点数据保存 (例如整个 all_ecns)，并更新全局版本戳"""
    await db_storage.set_item(key, data)
    await db_storage.set_item("ecn_global_version_stamp", time.time())


# ==========================================
# 主路由页面定义
# ==========================================
# @ui.page: NiceGUI框架的路由装饰器，用于定义页面路径
@ui.page("/ecn_management")
async def ecn_management_page():
    ui.add_head_html("""
        <style>
            .q-dialog__inner--minimized>div { max-width: 4000px; }
            .pdf-border { border: 1px solid #cbd5e1; }
            .pdf-border-b { border-bottom: 1px solid #cbd5e1; }
            .pdf-border-r { border-right: 1px solid #cbd5e1; }
            
            /*::-webkit-scrollbar {
                width: 3px; /* 极细滚动条 */
                background-color: transparent; /* 轨道透明，不占视觉空间 */
            }
            ::-webkit-scrollbar-thumb {
                background-color: #cbd5e1; /* 滚动条颜色 */
                border-radius: 1px;*/
        </style>
    """)
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")
        return

    current_user = app.storage.user.get("current_user", "未知用户")
    current_role = app.storage.user.get("current_role", "未知角色")
    current_display_path = get_cache_busted_path(
        app.storage.general.get("user_preferences", {}).get(current_user, {}).get("avatar", PRESET_AVATARS[0])
    )

    page_state = {"search_keyword": "", "filter_state": "全部"}

    # ui.dialog: NiceGUI框架提供的模态对话框组件
    dialog = ui.dialog().props("persistent")
    root_dialog = ui.dialog().props("maximized persistent")

    # ==========================================
    # 独立解耦弹窗 1：底层数据变更方案设计
    # ==========================================
    # ==========================================
    # 独立解耦弹窗 1：底层数据变更方案设计 (修复缩进版)
    # ==========================================
    # ==========================================
    # 独立解耦弹窗 1：底层数据变更方案设计 (多项目矩阵与1vN重构版)
    # ==========================================
    def open_overview_change_dialog(ecn_data, current_user, on_save_callback, edit_item=None):
        is_edit = edit_item is not None
        edit_data = edit_item or {}

        # 兼容旧版本数据格式，统一转为 project_states 结构
        initial_projects = edit_data.get("projects") or ([edit_data.get("project")] if edit_data.get("project") else [])
        initial_project_states = edit_data.get("project_states", {})

        # 如果是旧数据（只有 old_data 和 chip_id），做一下数据迁移
        if is_edit and not initial_project_states and initial_projects:
            for p in initial_projects:
                initial_project_states[p] = {
                    "action": "update" if edit_data.get("chip_id") else "add",
                    "chip_id": edit_data.get("chip_id") if edit_data.get("chip_id") else "NEW",
                    "anchor_row_id": None,
                    "old_data": copy.deepcopy(edit_data.get("old_data", {})),
                }

        sel_state = {
            "projects": initial_projects,
            "role": edit_data.get("role"),
            "label": edit_data.get("label"),
            "project_states": initial_project_states,  # {proj: {"action": "update"/"add", "chip_id": id/"NEW", "anchor_row_id": id, "old_data": {}}}
            "new_data": edit_data.get("new_data", {}) if is_edit else {},
            "req_idxs": edit_data.get("req_idxs", []),
            "linked_docs": edit_data.get("linked_docs", []),
            "linked_materials": edit_data.get("linked_materials", []),
            "config": edit_data.get("config", {}),
            "processing_type": edit_data.get("config_processing_type", "text"),
            "is_valid": is_edit,
            "validated_url": edit_data.get("new_data", {}).get("url_path", ""),
            "validated_file_type": edit_data.get("new_data", {}).get("file_type", ""),
            "first_col_label": edit_data.get("first_col_label", ""),  # 记录当前 role 的第一列 label
            "has_enabled_bool": True,  # <--- 新增这行，用于控制底部按钮状态
        }

        target_projects = list(
            set(
                ecn_data.get("target_projects", [])
                + ecn_data.get("review_info", {}).get("expanded_projects_mass", [])
                + ecn_data.get("review_info", {}).get("expanded_projects_non_mass", [])
            )
        )
        roles = list(app.storage.general.get("over_config_data", {}).keys())
        req_options = {
            req["idx"]: f"[{req['idx']}] {req['content'][:15]}..." for req in ecn_data["basic_info"]["requirements"]
        }
        req_docs = [k for k, v in ecn_data["review_info"]["involved_docs"].items() if v]
        req_mats = [
            f"{mat}-{act}"
            for mat, actions in ecn_data["review_info"]["involved_materials"].items()
            if isinstance(actions, dict)
            for act, val in actions.items()
            if val
        ]

        def get_labels(r):
            return {
                i["label"]: f"{i.get('title', '未命名')}"
                for gl in app.storage.general.get("over_config_data", {}).get(r, {}).values()
                for i in gl
            }

        def get_first_col_label(r, current_label):
            """获取当前 label 所在分组的第一列 label，用作基准锚点"""
            groups = app.storage.general.get("over_config_data", {}).get(r, {})
            for group_configs in groups.values():
                for cfg in group_configs:
                    if cfg.get("label") == current_label:
                        return group_configs[0].get("label")
            return current_label

        def get_chips_for_project(p, ll):
            """获取指定项目和标签下的所有激活卡片"""
            req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(p, "1.0")
            chips = {}
            raw_data = db_storage.get_deep_item([f"{p}_over_data", ll], {})
            for c_id, c in raw_data.items():
                # 仅筛选出当前版本处于激活状态的数据
                if c.get("select_activ_dic", {}).get(req_max_ver) is True:
                    chips[c_id] = c.get("content", "")
            return chips

        dialog.clear()
        with dialog, ui.card().classes("w-[1000px] max-w-full max-h-[90vh] flex flex-col flex-nowrap"):
            ui.label("修改概述数据变更方案" if is_edit else "添加概述数据变更方案").classes(
                "text-lg font-bold text-blue-900 shrink-0"
            )

            with ui.element("div").classes("w-full flex-1 min-h-0 overflow-y-auto pr-2"):
                with ui.column().classes("w-full gap-2"):
                    # === 区域 1：对应关联卡片 ===
                    with ui.card().classes("w-full p-3 bg-gray-50 border border-gray-200 shadow-sm gap-2"):
                        ui.label("对应关联 (必填)").classes("text-xs font-bold text-indigo-700")
                        ui.select(options=req_options, multiple=True, label="对应解决的要求序号").classes(
                            "w-full"
                        ).bind_value(sel_state, "req_idxs")
                        with ui.row().classes("w-full gap-2"):
                            if req_docs:
                                ui.select(options=req_docs, multiple=True, label="对应勾选的文档/图纸项").classes(
                                    "flex-1"
                                ).bind_value(sel_state, "linked_docs")
                            if req_mats:
                                ui.select(options=req_mats, multiple=True, label="对应勾选的物料动作").classes(
                                    "flex-1"
                                ).bind_value(sel_state, "linked_materials")

                    # === 区域 2：目标项目与技术维度选择 ===
                    with ui.grid(columns=3).classes("w-full gap-2 mt-2 items-start"):
                        sel_proj = ui.select(
                            options=target_projects, label="1. 目标项目", value=sel_state["projects"], multiple=True
                        ).classes("w-full")
                        sel_role = ui.select(options=roles, label="2. 技术维度", value=sel_state["role"]).classes(
                            "w-full"
                        )
                        sel_label = ui.select(
                            options=get_labels(sel_state["role"]) if sel_state["role"] else {},
                            label="3. 具体参数",
                            value=sel_state["label"],
                        ).classes("w-full")

                    # === 区域 3：多项目配置矩阵 (核心重构区) ===
                    matrix_container = (
                        ui.column()
                        .classes("w-full gap-1 mt-2 border border-blue-100 rounded bg-white p-2")
                        .style("display: none;")
                    )

                    def build_matrix_and_sync_state():
                        matrix_container.clear()
                        projects = sel_proj.value or []
                        sel_state["projects"] = projects
                        role = sel_role.value
                        label = sel_label.value
                        # 每次重新生成矩阵前，先重置致命错误状态为 True
                        sel_state["has_enabled_bool"] = True

                        if not projects or not role or not label:
                            matrix_container.style("display: none;")
                            sel_state["project_states"].clear()
                            render_dynamic_form()
                            return

                        matrix_container.style("display: flex;")
                        sel_state["first_col_label"] = get_first_col_label(role, label)
                        is_first_col = label == sel_state["first_col_label"]

                        with matrix_container:
                            ui.label("4. 多项目基准配置矩阵").classes("text-xs font-bold text-blue-800")
                            with ui.grid().classes(
                                "w-full grid-cols-[120px_1fr_1fr] bg-blue-50 p-1 rounded font-bold text-xs text-gray-600 mb-1 items-center"
                            ):
                                ui.label("目标项目")
                                ui.label("处理方式 (选择旧数据或新增)")
                                ui.label("绑定基准行" if not is_first_col else "")

                            for p in projects:
                                # 状态初始化
                                p_state = sel_state["project_states"].setdefault(
                                    p, {"action": "add", "chip_id": "NEW", "anchor_row_id": None, "old_data": {}}
                                )
                                chips_options = get_chips_for_project(p, label)

                                # 将 "NEW" 选项加入选项字典，并置顶
                                display_options = {"NEW": "[➕ 不覆盖，作为新数据添加]"}
                                display_options.update(chips_options)

                                # 智能默认值：如果此前没选过，且有历史数据，默认选中最新的一个；否则默认 NEW
                                if p_state["chip_id"] not in display_options:
                                    if chips_options:
                                        p_state["chip_id"] = list(chips_options.keys())[-1]
                                        p_state["action"] = "update"
                                    else:
                                        p_state["chip_id"] = "NEW"
                                        p_state["action"] = "add"

                                # 同步 old_data
                                if p_state["chip_id"] != "NEW":
                                    p_state["old_data"] = db_storage.get_deep_item(
                                        [f"{p}_over_data", label, p_state["chip_id"]], {}
                                    )
                                else:
                                    p_state["old_data"] = {}

                                with ui.grid().classes(
                                    "w-full grid-cols-[120px_1fr_1fr] items-center border-b border-dashed border-gray-200 pb-1 gap-2"
                                ):
                                    # 建议给项目名加上 break-all 和右边距，防止某些项目名过长导致样式溢出
                                    ui.label(p).classes("text-sm font-bold text-gray-700 break-all pr-2")

                                    # 内部事件闭包：处理下拉框选择变化
                                    def on_chip_select(e, current_p=p):
                                        val = e.value
                                        state = sel_state["project_states"][current_p]
                                        state["chip_id"] = val
                                        if val == "NEW":
                                            state["action"] = "add"
                                            state["old_data"] = {}
                                        else:
                                            state["action"] = "update"
                                            state["old_data"] = db_storage.get_deep_item(
                                                [f"{current_p}_over_data", sel_state["label"], val], {}
                                            )

                                        # 新增节点如果不是第一列，需要重置 anchor
                                        if state["action"] == "add" and not is_first_col:
                                            state["anchor_row_id"] = None

                                        # 重置校验锁，因为可能需要重新填写
                                        sel_state["is_valid"] = False
                                        sel_state["validated_url"] = ""
                                        render_dynamic_form()
                                        build_matrix_and_sync_state()  # 触发自身重绘以更新第三列

                                    ui.select(
                                        options=display_options, value=p_state["chip_id"], on_change=on_chip_select
                                    ).props("dense outlined bg-white").classes("w-full")

                                    # 第三列：Anchor 绑定逻辑 (仅在非第一列且为新增时出现)
                                    anchor_container = ui.element("div").classes("w-full")
                                    with anchor_container:
                                        if p_state["action"] == "add" and not is_first_col:
                                            # --- 优化点 2：融合暂存方案，创建虚拟锚点与替换变更的显示偷换 ---
                                            def get_chips_for_project_with_pending(proj, label_str):
                                                c_opts = get_chips_for_project(proj, label_str)
                                                # 回溯查找当前正在编辑但未落地的单据数据
                                                for c_item in ecn_data.get("change_items", []):
                                                    if (
                                                        c_item.get("type") == "overview_update"
                                                        and c_item.get("label") == label_str
                                                    ):
                                                        sub_states = c_item.get("project_states", {})
                                                        if proj in sub_states:
                                                            action = sub_states[proj].get("action")

                                                            # 动态截断机制：扩大阈值至 50 字符，保留长标识符，防范极端长文本
                                                            raw_content = c_item.get("new_data", {}).get(
                                                                "content", "暂无内容"
                                                            )
                                                            display_content = str(raw_content)
                                                            if len(display_content) > 50:
                                                                display_content = display_content[:50] + "..."

                                                            if action == "add":
                                                                # 新增操作：生成虚拟 ID
                                                                virtual_id = f"PENDING_NEW_{c_item['item_id']}"
                                                                c_opts[virtual_id] = f"[本单暂存新增] {display_content}"
                                                            elif action == "update":
                                                                # 替换操作：偷梁换柱，底层 ID 不变，强制覆盖 UI 显示层为新内容
                                                                old_chip_id = sub_states[proj].get("chip_id")
                                                                if old_chip_id and old_chip_id in c_opts:
                                                                    c_opts[old_chip_id] = (
                                                                        f"[本单暂存变更] {display_content}"
                                                                    )
                                                return c_opts

                                            first_col_chips = get_chips_for_project_with_pending(
                                                p, sel_state["first_col_label"]
                                            )

                                            def on_anchor_select(e, current_p=p):
                                                if e.value and e.value.startswith("PENDING_NEW_"):
                                                    sel_state["project_states"][current_p]["anchor_row_id"] = e.value
                                                else:
                                                    selected_chip = db_storage.get_deep_item(
                                                        [
                                                            f"{current_p}_over_data",
                                                            sel_state["first_col_label"],
                                                            e.value,
                                                        ],
                                                        {},
                                                    )
                                                    sel_state["project_states"][current_p]["anchor_row_id"] = (
                                                        selected_chip.get("row_id")
                                                    )

                                            # 回显逻辑
                                            current_anchor_chip_id = None
                                            for f_cid, _ in first_col_chips.items():
                                                if f_cid.startswith("PENDING_NEW_"):
                                                    if p_state["anchor_row_id"] == f_cid:
                                                        current_anchor_chip_id = f_cid
                                                        break
                                                else:
                                                    c_data = db_storage.get_deep_item(
                                                        [f"{p}_over_data", sel_state["first_col_label"], f_cid], {}
                                                    )
                                                    if c_data.get("row_id") == p_state["anchor_row_id"]:
                                                        current_anchor_chip_id = f_cid
                                                        break

                                            if not first_col_chips:
                                                ui.label("⚠️ 第一列暂无数据，请先为第一列添加变更方案").classes(
                                                    "text-xs text-red-500 font-bold"
                                                )
                                                sel_state["has_enabled_bool"] = False
                                            else:
                                                ui.select(
                                                    options=first_col_chips,
                                                    value=current_anchor_chip_id,
                                                    label="选择绑定的第一列基准行",
                                                    on_change=on_anchor_select,
                                                ).props("dense outlined bg-amber-50").classes("w-full")

                        render_dynamic_form()

                    # 绑定级联更新事件
                    sel_proj.on_value_change(build_matrix_and_sync_state)

                    def on_role_change(e):
                        sel_state["role"] = e.value
                        sel_label.set_options(get_labels(e.value))
                        sel_label.set_value(None)
                        sel_state["label"] = None
                        sel_state["project_states"].clear()
                        build_matrix_and_sync_state()

                    sel_role.on_value_change(on_role_change)

                    def on_label_change(e):
                        sel_state["label"] = e.value
                        sel_state["config"] = app.storage.general.get("over_config_data_flat", {}).get(e.value, {})
                        sel_state["processing_type"] = sel_state["config"].get("processing_type", "text")
                        sel_state["project_states"].clear()
                        build_matrix_and_sync_state()

                    sel_label.on_value_change(on_label_change)

                    # === 区域 4：1vN 对比表单容器 ===
                    dynamic_form_container = ui.column().classes("w-full gap-2 mt-2")

                    def render_dynamic_form():
                        dynamic_form_container.clear()
                        if not sel_state["projects"] or not sel_state["label"]:
                            return

                        ptype = sel_state["processing_type"]
                        config = sel_state["config"]

                        with dynamic_form_container:
                            ui.label(f"检测到对应的业务数据类型为: {ptype.upper()}").classes(
                                "text-xs font-bold text-teal-700 bg-teal-50 px-2 py-1 rounded w-fit"
                            )

                            with ui.grid(columns=2).classes("w-full gap-4"):
                                # === 左侧：1vN 多项目现状瀑布流展示 ===
                                with ui.card().classes(
                                    "w-full bg-gray-50 shadow-inner p-2 gap-1 max-h-[300px] overflow-y-auto"
                                ):
                                    ui.label("各项目现状对比 (N v 1)").classes(
                                        "text-xs text-gray-500 font-bold mb-1 sticky top-0 bg-gray-50 z-10 w-full pb-1 border-b"
                                    )

                                    for p in sel_state["projects"]:
                                        p_state = sel_state["project_states"].get(p, {})
                                        with ui.row().classes(
                                            "w-full items-start gap-2 border-b border-dashed border-gray-200 pb-1 mb-1"
                                        ):
                                            ui.label(f"[{p}]").classes(
                                                "text-xs font-bold text-blue-800 w-24 shrink-0 break-all"
                                            )

                                            if p_state.get("action") == "add":
                                                ui.label("将作为全新节点添加").classes(
                                                    "text-xs font-bold text-orange-500 bg-orange-50 px-1 rounded"
                                                )
                                            else:
                                                old_d = p_state.get("old_data", {})
                                                with ui.column().classes("gap-0 flex-1"):
                                                    ui.label(old_d.get("content", "无")).classes(
                                                        "text-sm text-gray-700 break-all"
                                                    )
                                                    if ptype == "test":
                                                        old_test = old_d.get("test_select_data", {})
                                                        text_str = f"性质: {old_test.get('test_nature_select', '')} | 状态: {old_test.get('state_select', '')} | 节点: {old_test.get('node_select', '')} | 工具: {old_test.get('instrument_select', '')}"
                                                        ui.label(text_str).classes("text-[10px] text-gray-500")

                                # === 右侧：单一的新方案输入区 ===
                                with ui.card().classes("w-full bg-blue-50 shadow-inner p-3 border border-blue-100"):
                                    ui.label("统一方案 / 新内容 (必填)").classes("text-xs text-blue-700 font-bold mb-2")

                                    if ptype == "text":
                                        ui.textarea("新文本内容").bind_value(sel_state["new_data"], "content").classes(
                                            "w-full"
                                        ).props("outlined auto-grow rows=2 bg-white")
                                        sel_state["is_valid"] = True

                                    elif ptype == "test":
                                        ui.textarea("新检测内容与标准").bind_value(
                                            sel_state["new_data"], "content"
                                        ).classes("w-full").props("outlined auto-grow rows=2 bg-white")
                                        test_data = sel_state["new_data"].setdefault("test_select_data", {})

                                        def build_test_options(options_list, key_prefix, label_str):
                                            if options_list:
                                                with ui.column().classes("w-full gap-0 m-0 p-0"):
                                                    sel = (
                                                        ui.select(options_list, label=label_str)
                                                        .bind_value(test_data, f"{key_prefix}_select")
                                                        .props("outlined dense")
                                                        .classes("w-full bg-white")
                                                    )
                                                    oth = (
                                                        ui.input(f"{label_str}特殊要求")
                                                        .bind_value(test_data, f"{key_prefix}_other_text")
                                                        .props("outlined dense")
                                                        .classes("w-full mt-1 bg-white")
                                                    )
                                                    oth.bind_visibility_from(sel, "value", value="其它")

                                        with ui.grid(columns=2).classes("w-full gap-2 mt-2"):
                                            build_test_options(
                                                config.get("test_nature_options", []), "test_nature", "测试性质"
                                            )
                                            build_test_options(config.get("state_options", []), "state", "条件/状态")
                                            build_test_options(config.get("node_options", []), "node", "节点/位置")
                                            build_test_options(
                                                config.get("instrument_options", []), "instrument", "工具/仪器/治具"
                                            )
                                        sel_state["is_valid"] = True

                                    elif ptype in ["search", "svn"]:
                                        with ui.row().classes("w-full items-center gap-2"):
                                            ui.input("新引用文件名").bind_value(sel_state["new_data"], "content").props(
                                                "outlined dense bg-white"
                                            ).classes("flex-grow")

                                            async def validate_path():
                                                val = sel_state["new_data"].get("content", "").strip()
                                                if not val:
                                                    return ui.notify("请先填写文件名", type="warning")
                                                from ..utils import validate_search_path, validate_svn_url

                                                # --- 核心优化：动态提取本单已有的暂存变更，作为校验依赖 ---
                                                pending_overrides = {}
                                                primary_proj = sel_state["projects"][0] if sel_state["projects"] else ""
                                                if primary_proj:
                                                    for c_item in ecn_data.get("change_items", []):
                                                        if c_item.get("type") == "overview_update":
                                                            lbl = c_item.get("label")
                                                            proj_states = c_item.get("project_states", {})
                                                            if primary_proj in proj_states:
                                                                action = proj_states[primary_proj].get("action")
                                                                # 如果基准列或任何前置列被新增/修改了，记录它的新内容
                                                                if action in ["add", "update"]:
                                                                    new_val = c_item.get("new_data", {}).get(
                                                                        "content", ""
                                                                    )
                                                                    if new_val:
                                                                        pending_overrides[lbl] = new_val

                                                if ptype == "search":
                                                    is_valid, url, ftype, _, msg = await validate_search_path(
                                                        val, config, sel_state["projects"], pending_overrides
                                                    )
                                                else:
                                                    is_valid, url, ftype, msg = await validate_svn_url(
                                                        val, config, sel_state["projects"], pending_overrides
                                                    )

                                                if is_valid:
                                                    sel_state["is_valid"] = True
                                                    sel_state["validated_url"] = url
                                                    sel_state["validated_file_type"] = ftype
                                                    ui.notify(msg, type="positive")
                                                else:
                                                    sel_state["is_valid"] = False
                                                    ui.notify(msg, type="negative")

                                            ui.button("校验有效性", on_click=validate_path).props(
                                                "color=primary outline dense"
                                            )
                                            ui.icon("check_circle", color="green", size="sm").bind_visibility_from(
                                                sel_state, "is_valid"
                                            )

                                    elif ptype in ["file", "image", "video"]:
                                        ui.label("上传新文件").classes("text-xs text-gray-500 mb-1")

                                        async def handle_upload(e):
                                            from ..config import FILES_URL_DIR, UPLOADS_DIR

                                            original_filename = e.file.name
                                            file_type = e.file.content_type
                                            upload_path = config.get("upload_path", UPLOADS_DIR)
                                            filepath = f"{upload_path}/{original_filename}"
                                            try:
                                                file_content = await e.file.read()
                                                os.makedirs(upload_path, exist_ok=True)
                                                with open(filepath, "wb") as f:
                                                    f.write(file_content)
                                                sel_state["new_data"]["content"] = original_filename
                                                sel_state["validated_url"] = f"{FILES_URL_DIR}/{original_filename}"
                                                sel_state["validated_file_type"] = file_type
                                                sel_state["is_valid"] = True
                                                ui.notify(f"文件 {original_filename} 暂存成功", type="positive")
                                            except Exception as ex:
                                                sel_state["is_valid"] = False
                                                ui.notify(f"上传失败: {ex}", type="negative")

                                        ui.upload(on_upload=handle_upload, auto_upload=True, max_files=1).props(
                                            "accept=*/*"
                                        )
                                        ui.label().bind_text_from(
                                            sel_state["new_data"],
                                            "content",
                                            backward=lambda x: f"暂存文件: {x}" if x else "",
                                        ).classes("text-sm text-green-600 mt-1")

                                    ui.label("注: 原因和记录将被系统自动接管").classes(
                                        "text-[10px] text-gray-400 mt-2 block"
                                    )

                    # 初始化执行
                    if is_edit or sel_state["projects"]:
                        build_matrix_and_sync_state()

            # === 底部操作栏 ===
            async def save_item():
                if not sel_state["projects"]:
                    return ui.notify("请至少选择一个目标项目", type="warning")

                # --- 优化点 3：通过前置拦截代替按钮禁用，让校验逻辑顺畅 ---
                if not sel_state["has_enabled_bool"]:
                    return ui.notify("缺少第一列的基准数据，请先为基准列添加方案！", type="warning")
                if not sel_state["is_valid"]:
                    return ui.notify("未完成文件/路径校验，或数据不合法，请先点击校验有效性。", type="warning")
                if not sel_state["new_data"].get("content", "").strip():
                    return ui.notify("请完善新内容", type="warning")

                # 前置校验：非第一列的新增必须绑定 anchor_row_id
                is_first_col = sel_state["label"] == sel_state["first_col_label"]
                for p, p_state in sel_state["project_states"].items():
                    if p_state["action"] == "add" and not is_first_col and not p_state["anchor_row_id"]:
                        return ui.notify(f"项目 [{p}] 作为新增项，必须绑定第一列基准行！", type="warning")

                if sel_state["processing_type"] in ["search", "svn", "file", "image", "video"]:
                    sel_state["new_data"]["url_path"] = sel_state["validated_url"]
                    sel_state["new_data"]["file_type"] = sel_state["validated_file_type"]

                sel_state["new_data"].pop("notes", None)

                payload = {
                    "item_id": edit_data.get("item_id", str(uuid.uuid4())),
                    "type": "overview_update",
                    "author": current_user,
                    "req_idxs": sel_state["req_idxs"],
                    "linked_docs": sel_state["linked_docs"],
                    "linked_materials": sel_state["linked_materials"],
                    "projects": copy.deepcopy(sel_state["projects"]),  # 兼容显示
                    "role": sel_state["role"],
                    "label": sel_state["label"],
                    "first_col_label": sel_state["first_col_label"],
                    "project_states": copy.deepcopy(sel_state["project_states"]),  # 传递给底层执行的核心状态集
                    "new_data": copy.deepcopy(sel_state["new_data"]),
                    "config_processing_type": sel_state["processing_type"],
                    "execute_status": "pending",
                }
                await on_save_callback(payload, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4 shrink-0"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                # 移除 bind_enabled_from，全权交给 save_item 内部拦截，解决假死错觉
                ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")

        dialog.open()

    # ==========================================
    # 独立解耦弹窗 2：文本描述方案设计
    # ==========================================
    def open_text_change_dialog(ecn_data, current_user, on_save_callback, edit_item=None):
        is_edit = edit_item is not None
        edit_data = edit_item or {}

        sel_state = {
            "req_idxs": edit_data.get("req_idxs", []),
            "linked_docs": edit_data.get("linked_docs", []),
            "linked_materials": edit_data.get("linked_materials", []),
            "change_type": edit_data.get("change_type", "物料变更"),
        }

        req_options = {
            req["idx"]: f"[{req['idx']}] {req['content'][:15]}..." for req in ecn_data["basic_info"]["requirements"]
        }
        req_docs = [k for k, v in ecn_data["review_info"]["involved_docs"].items() if v]
        req_mats = [
            f"{mat}-{act}"
            for mat, actions in ecn_data["review_info"]["involved_materials"].items()
            if isinstance(actions, dict)
            for act, val in actions.items()
            if val
        ]

        dialog.clear()
        with dialog, ui.card().classes("w-[900px] max-w-full"):
            ui.label("修改文本方案" if is_edit else "添加文本方案").classes("text-lg font-bold text-blue-900")
            with ui.card().classes("w-full p-3 bg-gray-50 border border-gray-200 shadow-sm gap-2 mt-2"):
                ui.label("对应关联 (必填)").classes("text-xs font-bold text-indigo-700")
                ui.select(options=req_options, multiple=True, label="对应解决的要求序号").classes("w-full").bind_value(
                    sel_state, "req_idxs"
                )
                with ui.row().classes("w-full gap-2"):
                    if req_docs:
                        ui.select(options=req_docs, multiple=True, label="对应勾选的文档/图纸项").classes(
                            "flex-1"
                        ).bind_value(sel_state, "linked_docs")
                    if req_mats:
                        ui.select(options=req_mats, multiple=True, label="对应勾选的物料动作").classes(
                            "flex-1"
                        ).bind_value(sel_state, "linked_materials")

            ui.select(["物料变更", "图纸更新", "工艺调整", "SOP修改", "其它"], label="方案分类").classes(
                "w-48 mt-4"
            ).bind_value(sel_state, "change_type")
            with ui.grid(columns=2).classes("w-full gap-4 mt-2"):
                old_content_ui = (
                    ui.textarea(label="现状 / 原内容 (必填)", value=edit_data.get("old_content", ""))
                    .classes("w-full")
                    .props("outlined auto-grow rows=4")
                )
                new_content_ui = (
                    ui.textarea(label="变更方案 / 新内容 (必填)", value=edit_data.get("new_content", ""))
                    .classes("w-full")
                    .props("outlined auto-grow rows=4 bg-blue-50")
                )

            async def save_item():
                if not old_content_ui.value.strip() or not new_content_ui.value.strip():
                    return ui.notify("原内容与新内容均不能为空", type="warning")
                payload = {
                    "item_id": edit_data.get("item_id", str(uuid.uuid4())),
                    "type": "text_desc",
                    "author": current_user,
                    "req_idxs": sel_state["req_idxs"],
                    "linked_docs": sel_state["linked_docs"],
                    "linked_materials": sel_state["linked_materials"],
                    "change_type": sel_state["change_type"],
                    "old_content": old_content_ui.value.strip(),
                    "new_content": new_content_ui.value.strip(),
                    "execute_status": "manual_record",
                }
                await on_save_callback(payload, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")
        dialog.open()

    # ------------------------------------------
    # 核心总控台：详情与流转操作
    # ------------------------------------------
    async def open_ecn_detail_dialog(ecn_id=None):
        is_new = ecn_id is None
        all_ecns = db_storage.get_item("ecn_management_data", {})

        proj_dict_mass, proj_dict_non = {"其它": {"其它": {}}}, {"其它": {"其它": {}}}
        for p, data in app.storage.general.get("project_summary", {}).items():
            parts = p.split("-")
            l1, l2 = parts[0], parts[1] if len(parts) > 1 else "其它"
            l3 = "-".join(parts[2:]) if len(parts) > 2 else "基础版"
            if data.get("state") in ECN_ALLOWED_PROJECT_STATES:
                proj_dict_mass.setdefault(l1, {}).setdefault(l2, {})[p] = l3
            else:
                proj_dict_non.setdefault(l1, {}).setdefault(l2, {})[p] = l3
        if not proj_dict_mass["其它"]["其它"]:
            del proj_dict_mass["其它"]
        if not proj_dict_non["其它"]["其它"]:
            del proj_dict_non["其它"]

        # ------------------------------------------
        # 防御性深度合并核心逻辑：告别繁琐的历史数据兼容补丁
        # ------------------------------------------
        latest_template = get_ecn_template()

        if is_new:
            if not proj_dict_mass and not proj_dict_non:
                return ui.notify("当前没有可用的受控项目。", type="warning")
            raw_data = generate_initial_ecn_data(current_user, current_role, all_ecns)
            ecn_data = merge_with_template(raw_data, latest_template)
        else:
            raw_data = all_ecns.get(ecn_id, {})
            # 此处动态自动补齐新字段，剥离废弃字段
            ecn_data = merge_with_template(raw_data, latest_template)

        local_data = copy.deepcopy(ecn_data)

        wf = local_data["workflow"]
        basic = local_data["basic_info"]
        review = local_data["review_info"]
        parts = wf.setdefault("scheme_participants", {})

        is_draft_or_reject = is_new or wf["current_state"] in [ECNState.DRAFT, ECNState.REJECTED]
        is_scheming_phase = wf["current_state"] == ECNState.ECN_SCHEMING

        # === 建立一个跨 Tab 刷新的引用桥梁 ===
        dashboard_updater = {"refresh": lambda: None}  # 初始值为一个空函数，后续会被覆盖为真正的刷新函数

        async def auto_save_review(e=None):
            if ecn_id and is_scheming_phase:
                await save_ecn_deep_item(["ecn_management_data", ecn_id, "review_info"], review)
                # 核心修复：当影响项的勾选发生改变并保存后，立即调用看板的刷新函数
                if dashboard_updater["refresh"]:
                    dashboard_updater["refresh"]()

        # ------------------- 渲染 UI -------------------
        root_dialog.clear()
        with (
            root_dialog,
            ui.card().classes("w-full h-[100vh] flex flex-col p-0 overflow-hidden bg-gray-100 -space-y-3"),
        ):
            with ui.row().classes(
                "w-full bg-white px-4 py-2 border-b border-gray-300 justify-between items-start shrink-0"
            ):
                ui.chip(
                    wf["current_state"],
                    color="orange"
                    if "中" in wf["current_state"]
                    else "red"
                    if wf["current_state"] == ECNState.REJECTED
                    else "blue",
                ).props("outline size=sm")
                with ui.column().classes("gap-0 items-center"):
                    ui.label("工程变更单").classes("text-2xl font-black text-gray-800 tracking-widest")
                    ui.label(f"{local_data['ecn_id']}").classes("text-lg font-mono font-bold text-gray-700")
                ui.button(icon="close", on_click=root_dialog.close).props("flat round dense").classes("ml-15")

            # ui.tabs: NiceGUI框架用于创建选项卡导航容器的类
            with ui.tabs().classes("w-full shrink-0 bg-white") as tabs:
                tab_ecr = ui.tab("1. ECR-申请", icon="assignment")
                tab_impact = ui.tab("2. ECN-影响", icon="fact_check")
                tab_scheme = ui.tab("3. ECN-方案", icon="design_services")
                tab_exec = ui.tab("4. ECN-执行", icon="assignment_turned_in")
                tab_workflow = ui.tab("审批记录", icon="timeline")

            is_ecr_editable = is_new or (
                basic.get("applicant") == current_user
                and wf.get("current_state") in [ECNState.DRAFT, ECNState.REJECTED]
            )

            with ui.tab_panels(tabs, value=tab_ecr).classes("w-full flex-1 min-h-0 p-2 md:p-4"):
                # --- [TAB 1] ECR 申请表单 ---
                with ui.tab_panel(tab_ecr).classes("p-0 bg-transparent"):
                    with ui.column().classes(
                        "gap-0 p-0 bg-white pdf-border shadow-sm w-full max-w-[1500px] mx-auto h-auto"
                    ):
                        ui.label("ECR-申请").classes(
                            "text-lg font-bold bg-blue-100 text-blue-900 w-full p-1 pdf-border-b text-center tracking-wider"
                        )

                        with ui.grid().classes(
                            "w-full grid-cols-2 md:grid-cols-5 gap-2 p-2 pdf-border-b bg-gray-50 items-center"
                        ):
                            ui.input("申请部门", value=basic["applicant_dept"]).props(
                                "outlined dense readonly bg-gray-100"
                            ).classes("w-full")
                            ui.input("申请人", value=basic["applicant"]).props(
                                "outlined dense readonly bg-gray-100"
                            ).classes("w-full")
                            ui.input("申请日期", value=basic["apply_date"].split(" ")[0]).props(
                                "outlined dense readonly bg-gray-100"
                            ).classes("w-full")
                            ui.input("需求日期(可选)").bind_value(basic, "requirement_date").props(
                                f"outlined dense {'readonly bg-gray-100' if not is_ecr_editable else 'bg-white'}"
                            ).classes("w-full")
                            ui.input("文件编号", value=basic["file_no"]).props(
                                "outlined dense readonly bg-gray-100"
                            ).classes("w-full")

                        with ui.row().classes("w-full p-2 pdf-border-b items-center gap-2 hover:bg-gray-50"):
                            ui.label("变更性质:").classes("font-bold text-gray-700 w-20 shrink-0")
                            with ui.row().classes("gap-6 items-center flex-1"):
                                ui.radio(["永久变更", "临时变更"]).bind_value(basic, "nature").props(
                                    f"inline {'disable' if not is_ecr_editable else ''}"
                                )
                                if basic.get("nature") == "临时变更":
                                    ui.input("涉及ERP系统单号为:").bind_value(basic, "erp_no").props(
                                        f"outlined dense {'readonly' if not is_ecr_editable else ''}"
                                    ).classes("flex-1 max-w-[300px]")

                        with ui.row().classes("w-full p-2 pdf-border-b items-start gap-2 hover:bg-gray-50"):
                            ui.label("变更原因:").classes("font-bold text-gray-700 w-20 shrink-0 pt-1")
                            with ui.row().classes("gap-x-4 gap-y-2 flex-1"):
                                # 动态读取配置
                                for reason_key in ECN_SCHEMA_CONFIG["reasons"]:
                                    ui.checkbox(reason_key).bind_value(basic["reasons"], reason_key).props(
                                        f"{'disable' if not is_ecr_editable else ''}"
                                    )

                                # bind_visibility_from: NiceGUI框架函数，将组件可见性与字典键值绑定，实现动态隐藏
                                ui.input("其他说明").bind_value(basic, "other_reason_desc").bind_visibility_from(
                                    basic["reasons"], "其他"
                                ).props(f"outlined dense {'readonly' if not is_ecr_editable else ''}").classes(
                                    "w-full mt-2 transition-all duration-300"
                                )

                        with ui.row().classes("w-full p-2 pdf-border-b items-start gap-2 hover:bg-gray-50"):
                            ui.label("变更对象:").classes("font-bold text-gray-700 w-20 shrink-0 pt-1")
                            with ui.column().classes("flex-1 gap-2"):
                                if is_ecr_editable:
                                    proj_sel_state = {"l1": None, "l2": None, "l3": None}
                                    with ui.row().classes("w-full items-center gap-2"):
                                        sel_l1 = (
                                            ui.select(
                                                options=list(proj_dict_mass.keys()),
                                                label="大系列",
                                                on_change=lambda e: [
                                                    proj_sel_state.update(l1=e.value),
                                                    sel_l2.set_options(
                                                        list(proj_dict_mass.get(e.value, {}).keys()) if e.value else []
                                                    ),
                                                    sel_l2.set_value(None),
                                                    sel_l3.set_options({}),
                                                    sel_l3.set_value(None),
                                                ],
                                            )
                                            .classes("flex-grow")
                                            .props("dense outlined bg-white")
                                        )
                                        sel_l2 = (
                                            ui.select(
                                                options=[],
                                                label="小系列",
                                                on_change=lambda e: [
                                                    proj_sel_state.update(l2=e.value),
                                                    sel_l3.set_options(
                                                        proj_dict_mass[proj_sel_state["l1"]][e.value]
                                                        if proj_sel_state["l1"] and e.value
                                                        else {}
                                                    ),
                                                    sel_l3.set_value(None),
                                                ],
                                            )
                                            .classes("flex-grow")
                                            .props("dense outlined bg-white")
                                        )
                                        sel_l3 = (
                                            ui.select(
                                                options={},
                                                label="具体型号",
                                                on_change=lambda e: proj_sel_state.update(l3=e.value),
                                            )
                                            .classes("flex-grow")
                                            .props("dense outlined bg-white")
                                        )

                                        def add_proj():
                                            target = proj_sel_state.get("l3")
                                            if target and target not in local_data["target_projects"]:
                                                local_data["target_projects"].append(target)
                                                render_proj_chips()
                                            elif not target:
                                                ui.notify("请先选择具体型号后再添加", type="warning")
                                            else:
                                                ui.notify("该项目已在变更对象列表中", type="info")

                                        ui.button("添加", on_click=add_proj).props(
                                            f"outline color=primary dense {'disable' if not is_ecr_editable else ''}"
                                        )

                                proj_chip_container = ui.row().classes("w-full gap-2 mt-1")

                                def render_proj_chips():
                                    proj_chip_container.clear()
                                    with proj_chip_container:
                                        if not local_data["target_projects"]:
                                            ui.label("尚未添加变更对象 (项目)").classes(
                                                "text-xs text-red-400 italic mt-1"
                                            )
                                        for p in local_data["target_projects"]:
                                            with ui.chip(color="primary", text_color="white").classes(
                                                "gap-1 items-center"
                                            ):
                                                ui.label(p)
                                                if is_ecr_editable:
                                                    ui.icon("cancel", size="xs").classes(
                                                        "cursor-pointer hover:text-red-300 ml-1"
                                                    ).on(
                                                        "click",
                                                        lambda e, proj=p: [
                                                            local_data["target_projects"].remove(proj),
                                                            render_proj_chips(),
                                                        ],
                                                    )

                                render_proj_chips()

                        with ui.row().classes("w-full p-2 pdf-border-b items-start gap-2 hover:bg-gray-50"):
                            ui.label("变更要求:").classes("font-bold text-gray-700 w-20 shrink-0 pt-1")
                            with ui.column().classes("flex-1 gap-2"):
                                if is_ecr_editable:
                                    with ui.row().classes("w-full gap-2 mb-2 items-center"):
                                        req_input = (
                                            ui.input("输入具体的变更要求...")
                                            .props(
                                                f"dense outlined bg-white {'readonly' if not is_ecr_editable else ''}"
                                            )
                                            .classes("flex-grow")
                                        )

                                        def add_req():
                                            val = req_input.value
                                            if val and val.strip():
                                                local_data["basic_info"]["requirements"].append(
                                                    {
                                                        "idx": len(local_data["basic_info"]["requirements"]) + 1,
                                                        "content": val.strip(),
                                                    }
                                                )
                                                req_input.set_value("")
                                                render_reqs()
                                            else:
                                                ui.notify("变更要求不能为空", type="warning")

                                        ui.button("添加条目", on_click=add_req).props("dense color=primary")

                                req_container = ui.column().classes("w-full gap-1")

                                def render_reqs():
                                    req_container.clear()
                                    with req_container:
                                        if not local_data["basic_info"]["requirements"]:
                                            ui.label("尚未填写具体的变更要求").classes("text-xs text-red-400 italic")
                                        for req in local_data["basic_info"]["requirements"]:
                                            with ui.row().classes(
                                                "w-full items-center gap-2 border-b border-dashed pb-1 group"
                                            ):
                                                ui.label(f"{req['idx']}.").classes("font-bold text-gray-600")
                                                ui.label(req["content"]).classes(
                                                    "text-sm text-gray-800 flex-1 break-all"
                                                )
                                                if is_ecr_editable:
                                                    ui.icon("close", size="sm").classes(
                                                        "cursor-pointer text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                                                    ).on(
                                                        "click",
                                                        lambda e, r=req: [
                                                            local_data["basic_info"]["requirements"].remove(r),
                                                            [
                                                                req.update(idx=i + 1)
                                                                for i, req in enumerate(
                                                                    local_data["basic_info"]["requirements"]
                                                                )
                                                            ],
                                                            render_reqs(),
                                                        ],
                                                    )

                                render_reqs()

                        with ui.row().classes("w-full p-2 items-start gap-2 hover:bg-gray-50"):
                            ui.label("原因说明:").classes("font-bold text-gray-700 w-20 shrink-0")
                            ui.textarea(placeholder="详细说明变更的原因及背景 (必填)...").bind_value(
                                basic, "reason_desc"
                            ).classes("w-full flex-1").props(
                                f"outlined auto-grow {'readonly bg-gray-100' if not is_ecr_editable else 'bg-white'}"
                            )

                # --- [TAB 2] ECN 影响表单 ---
                with ui.tab_panel(tab_impact).classes(
                    "gap-0 p-0 max-w-[1500px] mx-auto overflow-y-scroll overflow-x-hidden"
                ):
                    if wf["current_phase"] == "ECR_PHASE" and not is_new:
                        ui.label("当前处于 ECR 申请阶段，ECN 影响将在评审通过后由工程师协同填写。").classes(
                            "text-gray-500 m-8 text-center bg-white p-2 border rounded"
                        )
                    elif is_new:
                        ui.label("请先完成 ECR 申请并发起流程。").classes(
                            "text-gray-500 m-8 text-center bg-white p-2 border rounded"
                        )
                    else:
                        with ui.card().classes("w-full p-0 pdf-border bg-white shadow-sm"):
                            ui.label("ECN-影响").classes(
                                "text-lg font-bold bg-indigo-100 text-indigo-900 w-full p-1 pdf-border-b text-center tracking-wider"
                            )

                            with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50"):
                                ui.label("变更涉及产品:").classes("font-bold text-gray-700")
                                with ui.column().classes("gap-3 ml-4 w-full"):
                                    with ui.row().classes("items-start gap-2"):
                                        ui.label("ECR申请涵盖项目:").classes(
                                            "text-xs font-bold text-gray-500 w-36 pt-1"
                                        )
                                        with ui.row().classes("gap-1"):
                                            for p in local_data["target_projects"]:
                                                ui.chip(p, color="grey", text_color="white").props("dense")
                                            if not local_data["target_projects"]:
                                                ui.label("无").classes("text-xs text-gray-400")

                                    def render_expanded_proj(
                                        target_list, label_text, proj_dict_source, color="primary"
                                    ):
                                        with ui.row().classes("items-start gap-2"):
                                            ui.label(label_text).classes("text-xs font-bold text-gray-500 w-36 pt-2")
                                            with ui.column().classes("gap-1"):
                                                if is_scheming_phase:
                                                    ps = {"l1": None, "l2": None, "l3": None}
                                                    with ui.row().classes("items-center gap-2"):
                                                        s1 = (
                                                            ui.select(
                                                                options=list(proj_dict_source.keys()),
                                                                on_change=lambda e: [
                                                                    ps.update(l1=e.value),
                                                                    s2.set_options(
                                                                        list(proj_dict_source.get(e.value, {}).keys())
                                                                        if e.value
                                                                        else []
                                                                    ),
                                                                    s2.set_value(None),
                                                                    s3.set_options({}),
                                                                    s3.set_value(None),
                                                                ],
                                                            )
                                                            .props("dense outlined bg-white")
                                                            .classes("w-28")
                                                        )
                                                        s2 = (
                                                            ui.select(
                                                                options=[],
                                                                on_change=lambda e: [
                                                                    ps.update(l2=e.value),
                                                                    s3.set_options(
                                                                        proj_dict_source[ps["l1"]][e.value]
                                                                        if ps["l1"] and e.value
                                                                        else {}
                                                                    ),
                                                                    s3.set_value(None),
                                                                ],
                                                            )
                                                            .props("dense outlined bg-white")
                                                            .classes("w-28")
                                                        )
                                                        s3 = (
                                                            ui.select(
                                                                options={}, on_change=lambda e: ps.update(l3=e.value)
                                                            )
                                                            .props("dense outlined bg-white")
                                                            .classes("w-32")
                                                        )

                                                        def add_exp_proj():
                                                            if (
                                                                ps["l3"]
                                                                and ps["l3"] not in target_list
                                                                and ps["l3"] not in local_data["target_projects"]
                                                            ):
                                                                target_list.append(ps["l3"])
                                                                render_chips()
                                                                if is_scheming_phase:
                                                                    ui.timer(0.1, auto_save_review, once=True)
                                                            else:
                                                                ui.notify("未选择、已存在或已被ECR涵盖", type="warning")

                                                        ui.button(icon="add", on_click=add_exp_proj).props(
                                                            f"outline dense {'disable' if not is_scheming_phase else ''}"
                                                        ).classes("mt-0")

                                                chip_container = ui.row().classes("gap-1")

                                                def render_chips():
                                                    chip_container.clear()
                                                    with chip_container:
                                                        if not target_list:
                                                            ui.label("未扩大").classes("text-xs text-gray-400 mt-1")
                                                        for p in target_list:
                                                            with ui.chip(p, color=color, text_color="white").props(
                                                                "dense"
                                                            ):
                                                                if is_scheming_phase:
                                                                    ui.icon("close", size="xs").classes(
                                                                        "cursor-pointer ml-1"
                                                                    ).on(
                                                                        "click",
                                                                        lambda e, proj=p: [
                                                                            target_list.remove(proj),
                                                                            render_chips(),
                                                                            ui.timer(0.1, auto_save_review, once=True),
                                                                        ],
                                                                    )

                                                render_chips()

                                    render_expanded_proj(
                                        review["expanded_projects_mass"],
                                        "扩大影响 (试产/量产):",
                                        proj_dict_mass,
                                        color="blue",
                                    )
                                    render_expanded_proj(
                                        review["expanded_projects_non_mass"],
                                        "扩大影响 (非试产/量产):",
                                        proj_dict_non,
                                        color="teal",
                                    )

                            with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50"):
                                ui.label("相关影响 (范围告知):").classes("font-bold text-gray-700")
                                with ui.grid().classes(
                                    "w-full grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-2 gap-y-1 ml-4 items-center"
                                ):
                                    # 动态读取配置遍历
                                    for imp_key in ECN_SCHEMA_CONFIG["impact_dimensions"]:
                                        ui.checkbox(imp_key).bind_value(review["impacts"], imp_key).props(
                                            f"{'disable' if not is_scheming_phase else ''} dense"
                                        ).on_value_change(auto_save_review)

                            with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50"):
                                ui.label("变更涉及资料 (必出方案):").classes("font-bold text-gray-700")
                                with ui.grid().classes(
                                    "w-full grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-2 gap-y-1 ml-4 p-1 max-w-[900px]"
                                ):
                                    # 动态读取配置遍历
                                    for doc_key in ECN_SCHEMA_CONFIG["document_types"]:
                                        ui.checkbox(doc_key).bind_value(review["involved_docs"], doc_key).props(
                                            f"{'disable' if not is_scheming_phase else ''} dense"
                                        ).on_value_change(auto_save_review)

                                # bind_visibility_from: 实现“其它”项仅在勾选后显示
                                ui.input("其它:").bind_value(review, "other_docs_desc").bind_visibility_from(
                                    review["involved_docs"], "其它"
                                ).props(
                                    f"outlined dense {'readonly bg-gray-100' if not is_scheming_phase else 'bg-white'}"
                                ).classes("w-full ml-4 mt-2 max-w-[500px] transition-all duration-300").on(
                                    "blur", auto_save_review
                                )

                            # 优化点：父级增加 overflow-hidden 防止非预期的横向滚动条
                            with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50 overflow-hidden"):
                                ui.label("变更涉及物料:").classes("font-bold text-gray-700")

                                # 彻底重构的物料表格，解决行级对齐容错率低的问题
                                with ui.column().classes("w-full overflow-x-auto scrollbar-hide pl-4 gap-0"):
                                    with ui.grid(columns=6).classes(
                                        "w-full min-w-[550px] grid-cols-[100px_1fr_1fr_1fr_1fr_1fr] items-center p-1 max-w-[800px] border-b border-gray-300"
                                    ):
                                        ui.label("物料类别").classes("font-bold text-gray-600 pb-1 text-center")
                                        for a in ECN_SCHEMA_CONFIG["material_actions"]:
                                            ui.label(a).classes("font-bold text-gray-600 text-center pb-1")

                                    # 为每一个物料类别单独创建 Grid 行，加注 hover 背景色
                                    for mat_key in ECN_SCHEMA_CONFIG["material_categories"]:
                                        with ui.grid(columns=6).classes(
                                            "w-full min-w-[550px] grid-cols-[100px_1fr_1fr_1fr_1fr_1fr] items-center p-1 max-w-[800px] hover:bg-blue-100 transition-colors duration-150 rounded"
                                        ):
                                            ui.label(mat_key).classes("text-sm font-bold text-gray-700 text-right pr-4")
                                            for act in ECN_SCHEMA_CONFIG["material_actions"]:
                                                with ui.row().classes("justify-center w-full"):
                                                    ui.checkbox("").bind_value(
                                                        review["involved_materials"][mat_key], act
                                                    ).props(
                                                        f"{'disable' if not is_scheming_phase else ''} dense"
                                                    ).on_value_change(auto_save_review)

                            # with ui.grid(columns=1).classes(
                            #     "w-full grid-cols-1 md:grid-cols-3 pdf-border-b bg-gray-50"
                            # ):
                            #     with ui.column().classes("p-2 pdf-border-r gap-1 hover:bg-white"):
                            #         ui.label("SOP:").classes("font-bold text-gray-700")
                            #         ui.radio(["无影响", "更新SOP"]).bind_value(review, "sop_impact").props(
                            #             f"{'disable' if not is_scheming_phase else ''} dense inline"
                            #         ).on_value_change(auto_save_review)
                            #     with ui.column().classes("p-2 pdf-border-r gap-1 hover:bg-white"):
                            #         ui.label("治具:").classes("font-bold text-gray-700")
                            #         ui.radio(["无影响", "新做治具", "修改治具"]).bind_value(
                            #             review, "fixture_impact"
                            #         ).props(
                            #             f"{'disable' if not is_scheming_phase else ''} dense inline"
                            #         ).on_value_change(auto_save_review)
                            #     with ui.column().classes("p-2 gap-1 hover:bg-white"):
                            #         ui.label("工具:").classes("font-bold text-gray-700")
                            #         ui.radio(["无影响", "新购工具", "其它"]).bind_value(review, "tool_impact").props(
                            #             f"{'disable' if not is_scheming_phase else ''} dense inline"
                            #         ).on_value_change(auto_save_review)

                # --- [TAB 3] ECN 方案表单 ---
                with ui.tab_panel(tab_scheme).classes("gap-0 p-0 max-w-[1500px] mx-auto overflow-y-scroll"):
                    if wf["current_phase"] == "ECR_PHASE" and not is_new:
                        ui.label("当前处于 ECR 申请阶段，ECN 方案将在评审通过后由工程师协同填写。").classes(
                            "text-gray-500 m-8 text-center bg-white p-2 border rounded"
                        )
                    elif is_new:
                        ui.label("请先完成 ECR 申请并发起流程。").classes(
                            "text-gray-500 m-8 text-center bg-white p-2 border rounded"
                        )
                    else:
                        with ui.card().classes("w-full p-0 pdf-border bg-white shadow-sm"):
                            ui.label("ECN-方案").classes(
                                "text-lg font-bold bg-blue-100 text-blue-900 w-full p-1 pdf-border-b text-center tracking-wider"
                            )

                            with ui.column().classes("w-full p-2 gap-3 bg-blue-50/30"):
                                with ui.column().classes("w-full p-2 gap-3 bg-blue-50/30"):
                                    # ==========================================
                                    # 新增：方案覆盖率与影响项监控看板
                                    # ==========================================
                                    coverage_container = ui.column().classes("w-full p-0 m-0")

                                    def render_coverage_dashboard():
                                        coverage_container.clear()
                                        with coverage_container:
                                            # 计算要求项
                                            req_docs = set([k for k, v in review.get("involved_docs", {}).items() if v])
                                            req_mats = set(
                                                [
                                                    f"{mat}-{act}"
                                                    for mat, actions in review.get("involved_materials", {}).items()
                                                    if isinstance(actions, dict)
                                                    for act, val in actions.items()
                                                    if val
                                                ]
                                            )

                                            # 计算已提供的方案项
                                            prov_docs = set()
                                            prov_mats = set()
                                            for item in local_data.get("change_items", []):
                                                prov_docs.update(item.get("linked_docs", []))
                                                prov_mats.update(item.get("linked_materials", []))

                                            missing_docs = req_docs - prov_docs
                                            missing_mats = req_mats - prov_mats

                                            # 渲染看板卡片 (单列纯净版)
                                            with ui.card().classes(
                                                "w-full bg-orange-50/70 border border-orange-200 shadow-sm p-3 gap-2"
                                            ):
                                                with ui.row().classes(
                                                    "items-center gap-2 border-b border-orange-200 pb-2 w-full"
                                                ):
                                                    ui.icon("rule", color="orange-8").classes("text-lg")
                                                    ui.label("方案完整性自检与提醒").classes(
                                                        "font-bold text-orange-900 text-sm tracking-wide"
                                                    )

                                                # 取消了 grid，直接使用单列纵向布局
                                                with ui.column().classes("w-full gap-1 mt-1"):
                                                    ui.label("强制交付物覆盖率自检:").classes(
                                                        "text-[10px] font-bold text-gray-500 mb-1"
                                                    )

                                                    if missing_docs:
                                                        ui.label(f"✖ 缺少资料方案: {', '.join(missing_docs)}").classes(
                                                            "text-xs text-red-600 font-bold"
                                                        )
                                                    elif req_docs:
                                                        ui.label("✔ 资料方案已全覆盖").classes(
                                                            "text-xs text-green-600 font-bold"
                                                        )

                                                    if missing_mats:
                                                        ui.label(f"✖ 缺少物料方案: {', '.join(missing_mats)}").classes(
                                                            "text-xs text-red-600 font-bold"
                                                        )
                                                    elif req_mats:
                                                        ui.label("✔ 物料变更方案已全覆盖").classes(
                                                            "text-xs text-green-600 font-bold"
                                                        )

                                                    if not req_docs and not req_mats:
                                                        ui.label("前方未勾选资料或物料变更").classes(
                                                            "text-xs text-gray-400"
                                                        )

                                    # === 核心修复：将渲染函数挂载到上方定义的字典中 ===
                                    dashboard_updater["refresh"] = render_coverage_dashboard
                                    render_coverage_dashboard()
                                # ==========================================
                                with ui.row().classes("w-full justify-between items-center"):
                                    ui.label("产品设计与工艺变更方案明细").classes("font-bold text-gray-800 text-lg")
                                    if is_scheming_phase and any(
                                        role in current_role for role in ECN_SCHEME_WRITER_ROLES
                                    ):
                                        with ui.row().classes("gap-2"):
                                            ui.button(
                                                "添加概述修改方案",
                                                icon="auto_fix_high",
                                                on_click=lambda: open_overview_change_dialog(
                                                    local_data, current_user, handle_save_item
                                                ),
                                            ).props(
                                                f"color=primary outline dense {'disable' if not is_scheming_phase else ''}"
                                            )
                                            ui.button(
                                                "添加文本描述方案",
                                                icon="text_snippet",
                                                on_click=lambda: open_text_change_dialog(
                                                    local_data, current_user, handle_save_item
                                                ),
                                            ).props(
                                                f"color=secondary outline dense {'disable' if not is_scheming_phase else ''}"
                                            )

                                with ui.row().classes(
                                    "w-full p-2 bg-white rounded border border-gray-200 items-center justify-between"
                                ):
                                    with ui.row().classes("gap-2 items-center"):
                                        ui.label("提供人员确认状态").classes("text-sm font-bold text-gray-600")
                                        parts_container = ui.row().classes("gap-1")

                                        def render_parts():
                                            parts_container.clear()
                                            with parts_container:
                                                if not parts:
                                                    ui.label("暂无").classes("text-xs text-gray-400 mt-1")
                                                for p, status in parts.items():
                                                    ui.chip(
                                                        f"{p}: {'已确认' if status == 'confirmed' else '编写中'}",
                                                        color="green" if status == "confirmed" else "orange",
                                                        icon="check_circle" if status == "confirmed" else "edit",
                                                    ).props("size=sm").classes("text-white")

                                        render_parts()

                                    my_action_container = ui.row()

                                    def render_my_actions():
                                        my_action_container.clear()
                                        with my_action_container:
                                            if is_scheming_phase and any(
                                                role in current_role for role in ECN_SCHEME_WRITER_ROLES
                                            ):
                                                cur_status = parts.get(current_user)
                                                if cur_status == "editing" or not cur_status:
                                                    ui.button(
                                                        "确认完成我的方案",
                                                        icon="done_all",
                                                        on_click=lambda: toggle_part_status("confirmed"),
                                                    ).props("color=green outline dense")
                                                elif cur_status == "confirmed":
                                                    ui.button(
                                                        "重新开启编辑",
                                                        icon="lock_open",
                                                        on_click=lambda: toggle_part_status("editing"),
                                                    ).props("color=orange outline dense")

                                    render_my_actions()

                                    async def toggle_part_status(new_status):
                                        parts[current_user] = new_status
                                        await save_ecn_deep_item(
                                            [
                                                "ecn_management_data",
                                                local_data["ecn_id"],
                                                "workflow",
                                                "scheme_participants",
                                            ],
                                            parts,
                                        )
                                        render_parts()
                                        render_my_actions()
                                        render_items()  # 状态切换后，必须通知下方的方案列表重新渲染，以更新编辑/删除按钮的显示状态

                                item_container = ui.column().classes("w-full gap-3")

                                async def handle_save_item(item_data, is_edit=False):
                                    """
                                    保存方案
                                    """
                                    if is_edit:
                                        for idx, e_item in enumerate(local_data["change_items"]):
                                            if e_item["item_id"] == item_data["item_id"]:
                                                local_data["change_items"][idx] = item_data
                                                break
                                    else:
                                        local_data["change_items"].append(item_data)
                                    parts[current_user] = "editing"
                                    await save_ecn_deep_item(
                                        [
                                            "ecn_management_data",
                                            local_data["ecn_id"],
                                            "workflow",
                                            "scheme_participants",
                                        ],
                                        parts,
                                    )
                                    await save_ecn_deep_item(
                                        ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                        local_data["change_items"],
                                    )
                                    render_parts()
                                    render_my_actions()
                                    render_items()
                                    render_coverage_dashboard()  # 同时更新覆盖率看板状态

                                def get_item_projects(item):
                                    projects = item.get("projects")
                                    if projects:
                                        return [p for p in projects if p]
                                    return [item.get("project")] if item.get("project") else []

                                def render_items():
                                    item_container.clear()
                                    with item_container:
                                        if not local_data["change_items"]:
                                            ui.label("暂未添加具体的方案条目").classes(
                                                "text-sm text-gray-400 m-auto mt-4"
                                            )
                                            return

                                        # --- 核心修改：数据按类型预处理分组，并绑定全局序号 ---
                                        grouped_items = {}
                                        for global_idx, item in enumerate(local_data["change_items"]):
                                            i_type = item.get("type", "unknown")
                                            if i_type not in grouped_items:
                                                grouped_items[i_type] = []
                                            grouped_items[i_type].append((global_idx, item))

                                        # 定义分组 UI 的映射配置
                                        group_configs = {
                                            "overview_update": {
                                                "title": "概述数据变更方案",
                                                "icon": "auto_fix_high",
                                                "color": "blue",
                                            },
                                            "text_desc": {
                                                "title": "文本/工艺变更方案",
                                                "icon": "text_snippet",
                                                "color": "teal",
                                            },
                                        }

                                        # 遍历分组，生成折叠面板
                                        for g_type, items_in_group in grouped_items.items():
                                            cfg = group_configs.get(
                                                g_type, {"title": "其它变更方案", "icon": "list", "color": "grey"}
                                            )

                                            # ui.expansion: NiceGUI框架中用于创建可折叠/展开面板的组件。
                                            # 未传入 value=True 参数，故面板默认为收起状态。
                                            with (
                                                ui.expansion(
                                                    f"{cfg['title']} (共 {len(items_in_group)} 项)", icon=cfg["icon"]
                                                )
                                                .classes(
                                                    f"w-full bg-white border border-{cfg['color']}-200 rounded shadow-sm mb-2"
                                                )
                                                .props(
                                                    f'header-class="text-{cfg["color"]}-900 font-bold bg-{cfg["color"]}-50"'
                                                )
                                            ):
                                                with ui.column().classes("w-full p-3 gap-3"):
                                                    # 在折叠面板内部渲染具体的卡片，解包全局序号 (idx) 和项目数据 (item)
                                                    for idx, item in items_in_group:
                                                        with ui.card().classes(
                                                            "w-full p-0 shadow-sm border border-gray-200 relative"
                                                        ):
                                                            with ui.row().classes(
                                                                "w-full bg-gray-100 p-2 justify-between items-center"
                                                            ):
                                                                with ui.row().classes("gap-2 items-center flex-wrap"):
                                                                    ui.badge(str(idx + 1), color="grey-7")
                                                                    ui.badge(
                                                                        "概述修改"
                                                                        if item["type"] == "overview_update"
                                                                        else f"文本/工艺: {item.get('change_type', '')}",
                                                                        color="blue"
                                                                        if item["type"] == "overview_update"
                                                                        else "teal",
                                                                    )
                                                                    ui.label(item["author"]).classes(
                                                                        "text-xs text-white bg-cyan-500 px-1 rounded"
                                                                    )
                                                                    if item.get("req_idxs"):
                                                                        ui.label(
                                                                            f"解决要求: {', '.join(map(str, item['req_idxs']))}"
                                                                        ).classes(
                                                                            "text-xs text-white bg-lime-500 px-1 rounded"
                                                                        )
                                                                    if item.get("linked_docs"):
                                                                        ui.label(
                                                                            f"对应勾选文档: {', '.join(item['linked_docs'])}"
                                                                        ).classes(
                                                                            "text-xs text-white bg-orange-500 px-1 rounded"
                                                                        )
                                                                    if item.get("linked_materials"):
                                                                        ui.label(
                                                                            f"对应勾选物料: {', '.join(item['linked_materials'])}"
                                                                        ).classes(
                                                                            "text-xs font-bold text-white bg-red-400 px-1 rounded"
                                                                        )

                                                                if (
                                                                    is_scheming_phase
                                                                    and item["author"] == current_user
                                                                    and parts.get(current_user) != "confirmed"
                                                                ):
                                                                    with ui.row().classes(
                                                                        "absolute top-1 right-1 gap-1"
                                                                    ):
                                                                        ui.button(
                                                                            icon="edit",
                                                                            on_click=lambda _, i=item: (
                                                                                open_overview_change_dialog(
                                                                                    local_data,
                                                                                    current_user,
                                                                                    handle_save_item,
                                                                                    i,
                                                                                )
                                                                                if i["type"] == "overview_update"
                                                                                else open_text_change_dialog(
                                                                                    local_data,
                                                                                    current_user,
                                                                                    handle_save_item,
                                                                                    i,
                                                                                )
                                                                            ),
                                                                        ).props("flat round text-color=blue size=sm")
                                                                        ui.button(
                                                                            icon="delete",
                                                                            on_click=lambda _, i=item: remove_item(i),
                                                                        ).props("flat round text-color=red size=sm")

                                                            with ui.column().classes("w-full p-3 gap-1 bg-white"):
                                                                if item["type"] == "overview_update":
                                                                    item_projects = get_item_projects(item)
                                                                    item_label = item.get("label", "")
                                                                    item_title = (
                                                                        app.storage.general.get(
                                                                            "over_config_data_flat", {}
                                                                        )
                                                                        .get(item_label, {})
                                                                        .get("title", item_label)
                                                                    )
                                                                    ui.label(
                                                                        f"【{', '.join(item_projects)} - {item.get('role')} - {item_title}】"
                                                                    ).classes("text-xs font-bold text-blue-900")
                                                                    with ui.row().classes("w-full items-start gap-2"):
                                                                        project_states = item.get("project_states", {})

                                                                        if project_states:
                                                                            with ui.column().classes(
                                                                                "gap-1 bg-gray-50 p-2 rounded max-h-[150px] overflow-y-auto border border-dashed border-gray-200 shrink-0 min-w-[150px]"
                                                                            ):
                                                                                for (
                                                                                    p,
                                                                                    p_state,
                                                                                ) in project_states.items():
                                                                                    if p_state.get("action") == "add":
                                                                                        ui.label(
                                                                                            f"[{p}] 将全新添加"
                                                                                        ).classes(
                                                                                            "text-[10px] text-orange-500 font-bold bg-orange-50 px-1 rounded"
                                                                                        )
                                                                                    else:
                                                                                        old_content = p_state.get(
                                                                                            "old_data", {}
                                                                                        ).get("content", "无")
                                                                                        ui.label(
                                                                                            f"[{p}] {old_content}"
                                                                                        ).classes(
                                                                                            "text-[10px] text-gray-500 line-through break-all"
                                                                                        )
                                                                        else:
                                                                            ui.label(
                                                                                item.get("old_data", {}).get(
                                                                                    "content", "无"
                                                                                )
                                                                            ).classes(
                                                                                "text-sm text-gray-500 line-through bg-gray-50 p-1 rounded break-all"
                                                                            )

                                                                        ui.icon("arrow_forward", color="gray").classes(
                                                                            "mt-2 shrink-0"
                                                                        )
                                                                        new_d = item.get("new_data", {})
                                                                        if (
                                                                            item.get("old_data", {}).get("type")
                                                                            == "test"
                                                                        ):
                                                                            with ui.column().classes(
                                                                                "bg-green-50 p-2 rounded gap-0"
                                                                            ):
                                                                                ui.label(
                                                                                    new_d.get("content", "")
                                                                                ).classes(
                                                                                    "text-sm font-bold text-green-700 break-all mb-1"
                                                                                )
                                                                                ui.label(
                                                                                    f"性质: {new_d.get('test_select_data', {}).get('test_nature_select', '')}"
                                                                                ).classes("text-xs text-green-700")
                                                                                ui.label(
                                                                                    f"状态: {new_d.get('test_select_data', {}).get('state_select', '')}"
                                                                                ).classes("text-xs text-green-700")
                                                                                ui.label(
                                                                                    f"节点: {new_d.get('test_select_data', {}).get('node_select', '')}"
                                                                                ).classes("text-xs text-green-700")
                                                                                ui.label(
                                                                                    f"工具: {new_d.get('test_select_data', {}).get('instrument_select', '')}"
                                                                                ).classes("text-xs text-green-700")
                                                                        else:
                                                                            ui.label(new_d.get("content", "")).classes(
                                                                                "text-sm font-bold text-green-700 bg-green-50 p-1 rounded break-all"
                                                                            )
                                                                else:
                                                                    with ui.grid(columns=2).classes("w-full gap-2"):
                                                                        ui.label(item.get("old_content", "")).classes(
                                                                            "text-sm text-gray-500 bg-gray-50 p-2 rounded w-full border border-dashed line-through break-all"
                                                                        )
                                                                        ui.label(item.get("new_content", "")).classes(
                                                                            "text-sm text-gray-800 bg-blue-50 p-2 rounded w-full border border-solid border-blue-200 break-all"
                                                                        )

                                async def remove_item(item_to_remove):
                                    """
                                    删除方案
                                    """
                                    local_data["change_items"].remove(item_to_remove)
                                    author = item_to_remove.get("author")
                                    if author and not any(
                                        existing_item.get("author") == author
                                        for existing_item in local_data["change_items"]
                                    ):
                                        parts.pop(author, None)
                                        await save_ecn_deep_item(
                                            [
                                                "ecn_management_data",
                                                local_data["ecn_id"],
                                                "workflow",
                                                "scheme_participants",
                                            ],
                                            parts,
                                        )
                                    await save_ecn_deep_item(
                                        ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                        local_data["change_items"],
                                    )
                                    render_parts()
                                    render_my_actions()
                                    render_items()
                                    render_coverage_dashboard()  # 同时更新覆盖率看板状态

                                render_items()

                # --- [TAB 4] ECN 执行与试产 ---
                with ui.tab_panel(tab_exec).classes(
                    "gap-4 p-0 max-w-[1500px] mx-auto  overflow-y-scroll overflow-x-hidden"
                ):
                    if wf["current_state"] in [ECNState.DRAFT, ECNState.ECR_REVIEWING, ECNState.REJECTED]:
                        ui.label("当前尚未进入执行环节。").classes(
                            "text-gray-500 m-8 text-center bg-white p-2 border rounded"
                        )
                    else:
                        is_exec_phase = wf["current_state"] in [ECNState.ECN_EXECUTING, ECNState.PENDING_FINAL_EXECUTE]
                        exec_info = local_data.setdefault("execution_info", get_ecn_template()["execution_info"])
                        with ui.card().classes("w-full p-0 pdf-border bg-white shadow-sm"):
                            ui.label("ECN-执行 & 试产").classes(
                                "text-lg font-bold bg-green-100 text-green-900 w-full p-1 pdf-border-b text-center tracking-wider"
                            )
                            with ui.row().classes("w-full p-3 pdf-border-b items-start gap-6 hover:bg-gray-50"):
                                ui.label("追溯等级:").classes("font-bold text-gray-700 w-20 pt-1")
                                with ui.row().classes("gap-x-6 gap-y-2 flex-1 flex-wrap"):
                                    ui.radio(
                                        [
                                            "无影响",
                                            "追溯至文件",
                                            "追溯至供应商存量",
                                            "追溯至零件/返修/在线",
                                            "追溯至半成品/返修/在线",
                                            "追溯至成品/返修/在线",
                                            "追溯至在途/客户",
                                        ]
                                    ).bind_value(exec_info, "traceability_level").props(
                                        f"{'disable' if not is_exec_phase else ''} inline"
                                    )
                            with ui.row().classes("w-full p-3 pdf-border-b items-center gap-6 bg-gray-50"):
                                ui.label("处理措施:").classes("font-bold text-gray-700 w-20")
                                with ui.row().classes("gap-6 flex-1"):
                                    ui.checkbox("报废").bind_value(exec_info["handling_measures"], "报废").props(
                                        f"{'disable' if not is_exec_phase else ''}"
                                    )
                                    ui.checkbox("返工").bind_value(exec_info["handling_measures"], "返工").props(
                                        f"{'disable' if not is_exec_phase else ''}"
                                    )
                            with ui.row().classes("w-full p-3 items-center gap-6 hover:bg-gray-50"):
                                ui.label("试产结论:").classes("font-bold text-gray-700 w-20")
                                with ui.row().classes("gap-6 flex-1"):
                                    ui.radio(
                                        [
                                            "无需试产,变更完成",
                                            "试产通过,变更完成",
                                            "试产条件通过,变更内容再完善",
                                            "试产不通过,重新试产",
                                        ]
                                    ).bind_value(exec_info, "trial_conclusion").props(
                                        f"{'disable' if not is_exec_phase else ''} inline"
                                    )

                # --- [TAB 4] 审批流转记录 ---
                with ui.tab_panel(tab_workflow).classes("gap-4 p-4 max-w-[1000px] mx-auto bg-white rounded border"):
                    workflow_container = ui.column().classes("w-full")

                    def render_workflow_tab():
                        workflow_container.clear()
                        with workflow_container:
                            if is_new:
                                ui.label("暂无审批记录，请先发起申请。").classes(
                                    "text-gray-500 mt-4 text-center w-full"
                                )
                            else:
                                if wf["pending_roles"]:
                                    pending_list = [r for r in wf["pending_roles"] if not wf["step_approvals"].get(r)]
                                    approved_list = [r for r in wf["pending_roles"] if wf["step_approvals"].get(r)]
                                    with ui.card().classes(
                                        "w-full bg-blue-50/50 shadow-sm mb-4 border border-blue-100 p-3"
                                    ):
                                        if pending_list:
                                            ui.label(f"▶ 当前节点等待审批: {', '.join(pending_list)}").classes(
                                                "text-orange-600 font-bold text-sm"
                                            )
                                        if approved_list:
                                            ui.label(f"▶ 当前节点已同意: {', '.join(approved_list)}").classes(
                                                "text-green-600 text-sm mt-1"
                                            )

                                # ui.timeline: NiceGUI框架用于展示时间线或流转步骤的类
                                with ui.timeline(color="secondary"):
                                    for log in local_data.get("approval_log", []):
                                        icon_map = {
                                            "同意": "check_circle",
                                            "驳回": "cancel",
                                            "执行变更": "play_circle",
                                            "发起申请": "send",
                                            "发起方案评审": "fact_check",
                                        }
                                        color_map = {
                                            "同意": "green",
                                            "驳回": "red",
                                            "执行变更": "blue",
                                            "发起申请": "orange",
                                            "发起方案评审": "purple",
                                        }
                                        ui.timeline_entry(
                                            f"{log['user']} ({log['role']}) - {log['action']}",
                                            subtitle=log["time"],
                                            icon=icon_map.get(log["action"], "info"),
                                            color=color_map.get(log["action"], "grey"),
                                        ).classes("text-sm")
                                        if log.get("note"):
                                            ui.label(f"批注: {log['note']}").classes(
                                                "text-xs text-gray-500 ml-8 -mt-2 mb-2"
                                            )

                    render_workflow_tab()

            # ------------------------------------------
            # 底部操作栏及各类事件触发器
            # ------------------------------------------
            with ui.row().classes(
                "w-full bg-white p-4 border-t border-gray-300 justify-end items-center shrink-0 gap-4 shadow-[0_-5px_15px_rgba(0,0,0,0.05)]"
            ):
                if is_draft_or_reject:
                    if basic["applicant"] == current_user or is_new:
                        ui.button("保存为草稿", on_click=lambda: execute_db_action("save_draft")).props("color=grey-7")
                        ui.button("发起/重新发起 ECR", on_click=lambda: execute_db_action("submit_ecr")).props(
                            "color=primary"
                        )
                else:
                    is_pending_user = current_role in wf["pending_roles"]
                    if wf["current_state"] == ECNState.ECR_REVIEWING and basic["applicant"] == current_user:
                        ui.button("撤回修改", icon="undo", on_click=lambda: execute_db_action("withdraw")).props(
                            "color=orange"
                        )
                        ui.button("作废", icon="delete_forever", on_click=lambda: execute_db_action("cancel")).props(
                            "color=red"
                        )
                    if wf["current_state"] == ECNState.PENDING_FINAL_EXECUTE and "研发经理" in current_role:
                        ui.button(
                            "驳回至影响/方案阶段", color="red", on_click=lambda: execute_db_action("reject", note="")
                        )
                        ui.button(
                            "确认各部已就绪，立刻执行数据变更并归档",
                            icon="warning",
                            on_click=lambda: execute_db_action("final_execute"),
                        ).props("color=red")
                    elif is_scheming_phase and any(r in current_role for r in ECN_SCHEME_INITIATOR_ROLES):
                        all_confirmed = len(parts) > 0 and all(s == "confirmed" for s in parts.values())
                        btn = ui.button(
                            "发起 ECN 方案评审", on_click=lambda: execute_db_action("initiate_scheme_review")
                        ).props(f"color=purple {'disabled' if not all_confirmed else ''}")
                        if not all_confirmed:
                            btn.tooltip("需要所有提供方案的人员点击'确认完成'后方可发起")
                    elif is_pending_user and wf["current_state"] not in [
                        ECNState.CLOSED,
                        ECNState.CANCEL,
                        ECNState.REJECTED,
                        ECNState.ECN_SCHEMING,
                    ]:
                        note_input = ui.input("审批意见 (选填)").props("dense outlined").classes("w-64")
                        ui.button(
                            "驳回", color="red", on_click=lambda: execute_db_action("reject", note=note_input.value)
                        )
                        ui.button(
                            "同意", color="green", on_click=lambda: execute_db_action("approve", note=note_input.value)
                        )

            # ------------------------------------------
            # 提取的数据库与流转控制逻辑中心
            # ------------------------------------------
            async def execute_db_action(action_type, note=""):
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if action_type == "submit_ecr":
                    if not basic.get("nature"):
                        return ui.notify("请选择变更性质", type="warning")
                    if not any(basic.get("reasons", {}).values()):
                        return ui.notify("请至少勾选一项变更原因", type="warning")
                    if basic.get("reasons", {}).get("其他") and not basic.get("other_reason_desc", "").strip():
                        return ui.notify("请填写其他说明", type="warning")
                    if not local_data.get("target_projects"):
                        return ui.notify("请至少添加一个变更对象", type="warning")
                    if not basic.get("requirements"):
                        return ui.notify("请至少填写一条变更要求", type="warning")
                    if not basic.get("reason_desc", "").strip():
                        return ui.notify("请填写原因说明", type="warning")

                    basic["title"] = (
                        f"{','.join(local_data['target_projects'][:2])}等 - {'/'.join([k for k, v in basic['reasons'].items() if v])}变更"
                    )
                    wf["current_state"] = ECNState.ECR_REVIEWING
                    wf["current_phase"] = "ECR_PHASE"
                    wf["route_type"] = "SALES_INITIATED" if "销售" in current_role else "RD_INITIATED"
                    wf["current_step_index"] = 0
                    wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECR_PHASE"][wf["route_type"]][0]
                    wf["step_approvals"] = {}
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "发起申请", "time": now_str}
                    )

                elif action_type == "withdraw":
                    wf["current_state"], wf["pending_roles"], wf["step_approvals"] = ECNState.DRAFT, [], {}
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "撤回修改", "time": now_str}
                    )

                elif action_type == "cancel":
                    wf["current_state"], wf["pending_roles"], wf["step_approvals"] = ECNState.CANCEL, [], {}
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "作废变更", "time": now_str}
                    )

                elif action_type == "initiate_scheme_review":
                    req_docs = set([k for k, v in review["involved_docs"].items() if v])
                    req_mats = set(
                        [
                            f"{mat}-{act}"
                            for mat, acts in review["involved_materials"].items()
                            if isinstance(acts, dict)
                            for act, val in acts.items()
                            if val
                        ]
                    )
                    prov_docs, prov_mats = set(), set()
                    for item in local_data.get("change_items", []):
                        prov_docs.update(item.get("linked_docs", []))
                        prov_mats.update(item.get("linked_materials", []))
                    if (req_docs - prov_docs) or (req_mats - prov_mats):
                        msg = ["【系统拦截】评审区勾选项缺少方案关联："]
                        if req_docs - prov_docs:
                            msg.append(f"▶ 遗漏文档: {', '.join(req_docs - prov_docs)}")
                        if req_mats - prov_mats:
                            msg.append(f"▶ 遗漏物料: {', '.join(req_mats - prov_mats)}")
                        return ui.notify("\n".join(msg), type="negative", multi_line=True)

                    wf["current_state"], wf["current_phase"], wf["current_step_index"] = (
                        ECNState.ECN_REVIEWING,
                        "ECN_SCHEME_REVIEW_PHASE",
                        0,
                    )
                    wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_SCHEME_REVIEW_PHASE"][0]
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "发起方案评审", "time": now_str}
                    )

                elif action_type in ["approve", "reject"]:
                    act_name = "同意" if action_type == "approve" else "驳回"
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": act_name, "note": note, "time": now_str}
                    )
                    if action_type == "reject":
                        if wf["current_phase"] == "ECR_PHASE":
                            wf["current_state"], wf["pending_roles"] = ECNState.REJECTED, []
                        else:
                            wf["current_phase"], wf["current_state"], wf["pending_roles"] = (
                                "ECN_SCHEME_PHASE",
                                ECNState.ECN_SCHEMING,
                                [],
                            )
                            for u in wf.setdefault("scheme_participants", {}):
                                wf["scheme_participants"][u] = "editing"
                    else:
                        wf["step_approvals"][current_role] = True
                        if all(wf["step_approvals"].get(r, False) for r in wf["pending_roles"]):
                            wf["current_step_index"] += 1
                            wf["step_approvals"] = {}
                            route = (
                                ECN_WORKFLOW_ROUTES[wf["current_phase"]][wf["route_type"]]
                                if wf["current_phase"] == "ECR_PHASE"
                                else ECN_WORKFLOW_ROUTES[wf["current_phase"]]
                            )
                            if wf["current_step_index"] >= len(route):
                                if wf["current_phase"] == "ECR_PHASE":
                                    wf["current_phase"], wf["current_state"], wf["pending_roles"] = (
                                        "ECN_SCHEME_PHASE",
                                        ECNState.ECN_SCHEMING,
                                        [],
                                    )
                                else:
                                    wf["current_phase"], wf["current_state"], wf["current_step_index"] = (
                                        "ECN_EXECUTION_PHASE",
                                        ECNState.ECN_EXECUTING,
                                        0,
                                    )
                                    wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_EXECUTION_PHASE"][0]
                            else:
                                wf["pending_roles"] = route[wf["current_step_index"]]
                                if "研发经理_EXECUTE" in wf["pending_roles"]:
                                    wf["current_state"] = ECNState.PENDING_FINAL_EXECUTE

                elif action_type == "final_execute":
                    try:
                        # --- 优化点 2 收尾：对执行列表进行拓扑排序，保证基准列先执行 ---
                        def execution_sort_key(item):
                            if item.get("type") == "overview_update":
                                # 强制让 first_col_label 相同的条目排在最前面执行 (权值为 0)
                                return 0 if item.get("label") == item.get("first_col_label") else 1
                            return 2

                        sorted_items = sorted(local_data["change_items"], key=execution_sort_key)
                        pending_id_to_row_id = {}  # 虚拟暂存 ID 到真实 row_id 的映射表

                        # 遍历经过排序的 sorted_items 而不是 local_data["change_items"]
                        for item in sorted_items:
                            if item["type"] == "overview_update":
                                processing_type = item.get("config_processing_type", "text")
                                icon_map = {
                                    "file": "attachment",
                                    "search": "saved_search",
                                    "svn": "saved_search",
                                    "image": "image",
                                    "video": "play_circle",
                                }
                                new_icon = icon_map.get(processing_type, None)

                                project_states = item.get("project_states", {})
                                if not project_states:
                                    target_projects = item.get("projects") or [item.get("project")]
                                    project_states = {
                                        p: {"action": "update", "chip_id": item.get("chip_id")}
                                        for p in target_projects
                                        if p
                                    }

                                updated = False

                                # 数据工厂函数：统一生成新的 Chip 模板
                                def create_new_chip_template(proj, author, processing_type, new_icon, new_data):
                                    req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(proj, "1.0")
                                    new_activ_dic = {
                                        f"{i}.0": (f"{i}.0" == req_max_ver)
                                        for i in range(1, int(float(req_max_ver)) + 1)
                                    }

                                    new_chip = {
                                        "id": str(uuid.uuid4()),
                                        "role": item["role"],
                                        "type": processing_type,
                                        "icon": new_icon,
                                        "enabled": True,
                                        "bg_color": "bg-light-blue-1",
                                        "content": new_data.get("content", ""),
                                        "notes": f"依据: {local_data['ecn_id']} 执行",
                                        "creator": author,
                                        "req_ver": req_max_ver,
                                        "select_activ_dic": new_activ_dic,
                                        "timestamp": {
                                            now_str: {
                                                "creator": author,
                                                "select_activ_dic": copy.deepcopy(new_activ_dic),
                                            }
                                        },
                                    }
                                    # 注入特有属性
                                    if "test_select_data" in new_data:
                                        new_chip["test_select_data"] = copy.deepcopy(new_data["test_select_data"])
                                    if "file_type" in new_data:
                                        new_chip["file_type"] = new_data["file_type"]
                                    if "url_path" in new_data:
                                        new_chip["url_path"] = new_data["url_path"]
                                    if "warehouse" in new_data:
                                        new_chip["warehouse"] = new_data["warehouse"]
                                    return new_chip, req_max_ver

                                for project, p_state in project_states.items():
                                    action = p_state.get("action")
                                    chip_id = p_state.get("chip_id")
                                    anchor_row_id = p_state.get("anchor_row_id")

                                    if action == "update" and chip_id:
                                        path = [f"{project}_over_data", item["label"], chip_id]
                                        old_chip = db_storage.get_deep_item(path)

                                        if old_chip:
                                            # 生成新节点（借用工厂）
                                            new_chip, req_max_ver = create_new_chip_template(
                                                project,
                                                item.get("author", current_user),
                                                processing_type,
                                                new_icon,
                                                item.get("new_data", {}),
                                            )
                                            new_chip["row_id"] = old_chip.get("row_id")  # 严格继承旧 row_id

                                            # 处理旧数据的部分继承与失活
                                            new_chip["select_activ_dic"] = copy.deepcopy(
                                                old_chip.get("select_activ_dic", {})
                                            )
                                            new_chip["select_activ_dic"][req_max_ver] = True
                                            new_chip["timestamp"][now_str]["select_activ_dic"] = copy.deepcopy(
                                                new_chip["select_activ_dic"]
                                            )

                                            deactivated_chip = copy.deepcopy(old_chip)
                                            deactivated_chip.setdefault("select_activ_dic", {})[req_max_ver] = False
                                            deactivated_chip["enabled"] = False
                                            deactivated_chip["bg_color"] = "bg-grey-5"
                                            deactivated_chip["icon"] = "block"
                                            deactivated_chip.setdefault("timestamp", {})[now_str] = {
                                                "creator": f"ECN自动执行 ({local_data['ecn_id']})",
                                                "select_activ_dic": copy.deepcopy(deactivated_chip["select_activ_dic"]),
                                            }

                                            # 写入失活旧节点与新生节点
                                            await save_ecn_deep_item(path, deactivated_chip)
                                            await save_ecn_deep_item(
                                                [f"{project}_over_data", item["label"], new_chip["id"]], new_chip
                                            )
                                            updated = True

                                    elif action == "add":
                                        new_chip, _ = create_new_chip_template(
                                            project,
                                            item.get("author", current_user),
                                            processing_type,
                                            new_icon,
                                            item.get("new_data", {}),
                                        )

                                        is_first_col = item["label"] == item.get("first_col_label", "")
                                        if is_first_col:
                                            # 如果是基准列，生成真实 UUID，并存入映射表供后续列使用
                                            new_row_id = str(uuid.uuid4())
                                            new_chip["row_id"] = new_row_id
                                            pending_id_to_row_id[item["item_id"]] = new_row_id
                                        else:
                                            # 如果是后续列，解析虚拟锚点
                                            if anchor_row_id and str(anchor_row_id).startswith("PENDING_NEW_"):
                                                temp_item_id = anchor_row_id.replace("PENDING_NEW_", "")
                                                # 从映射表中获取刚刚生成的真实 row_id
                                                new_chip["row_id"] = pending_id_to_row_id.get(
                                                    temp_item_id, str(uuid.uuid4())
                                                )
                                            else:
                                                new_chip["row_id"] = anchor_row_id

                                        await save_ecn_deep_item(
                                            [f"{project}_over_data", item["label"], new_chip["id"]], new_chip
                                        )
                                        updated = True

                                if updated:
                                    item["execute_status"] = "success"

                        wf["current_state"], wf["pending_roles"] = ECNState.CLOSED, []
                        local_data["approval_log"].append(
                            {"user": current_user, "role": current_role, "action": "执行变更", "time": now_str}
                        )
                    except Exception as e:
                        logger.error(f"执行ECN分裂变更失败: {e}", exc_info=True)
                        return ui.notify(f"执行失败: {e}", type="negative")

                await save_ecn_deep_item(["ecn_management_data", local_data["ecn_id"]], local_data)
                ui.notify("操作成功！", type="positive")
                root_dialog.close()
                refresh_list()

            # --- 协同同步定时器 ---
            async def sync_schemes():
                """
                协同同步方案编写阶段的核心函数，定期从数据库拉取最新数据并对比当前本地数据，智能更新界面以反映其他用户的修改
                """
                if ecn_id:
                    # copy.deepcopy: Python标准库函数，用于递归复制对象，防止内存引用导致的数据污染
                    fresh = db_storage.get_deep_item(["ecn_management_data", ecn_id])
                    if not fresh:
                        return

                    # 1. 同步工作流状态
                    fresh_wf = fresh.get("workflow", {})
                    if (
                        fresh_wf.get("current_state") != wf["current_state"]
                        or fresh_wf.get("pending_roles") != wf["pending_roles"]
                    ):
                        wf["current_state"] = fresh_wf.get("current_state")
                        wf["pending_roles"] = fresh_wf.get("pending_roles")
                        wf["step_approvals"] = fresh_wf.get("step_approvals", {})
                        local_data["approval_log"] = copy.deepcopy(fresh.get("approval_log", []))
                        render_workflow_tab()  # 触发刷新流转页面
                        ui.notify("后台流转状态已更新，已为您同步。", type="info")

                    # 2. 同步方案内容 (仅在方案编写阶段需要动态重绘卡片)
                    if wf["current_state"] == ECNState.ECN_SCHEMING:
                        if (
                            str(fresh.get("change_items", [])) != str(local_data["change_items"])
                            or fresh["workflow"].get("scheme_participants", {}) != parts
                        ):
                            local_data["change_items"].clear()
                            local_data["change_items"].extend(copy.deepcopy(fresh.get("change_items", [])))
                            parts.clear()
                            parts.update(copy.deepcopy(fresh["workflow"].get("scheme_participants", {})))
                            render_parts()
                            render_my_actions()
                            render_items()
                            render_coverage_dashboard()  # 同时更新覆盖率看板状态

                        fresh_rev = fresh.get("review_info", {})
                        if fresh_rev:
                            review["impacts"].update(fresh_rev.get("impacts", {}))
                            review["involved_docs"].update(fresh_rev.get("involved_docs", {}))
                            for mat, acts in fresh_rev.get("involved_materials", {}).items():
                                if mat in review["involved_materials"] and isinstance(acts, dict):
                                    review["involved_materials"][mat].update(acts)
                            review["sop_impact"] = fresh_rev.get("sop_impact", "无影响")
                            review["fixture_impact"] = fresh_rev.get("fixture_impact", "无影响")
                            review["tool_impact"] = fresh_rev.get("tool_impact", "无影响")

            if wf["current_state"] == ECNState.ECN_SCHEMING and not is_new:
                # ui.timer: NiceGUI框架用于周期性执行函数的定时器类，此处用于实现多人协同数据同步
                sync_timer = ui.timer(3.0, sync_schemes)
                root_dialog.on("close", sync_timer.cancel)

        root_dialog.open()

    # ==========================================
    # 管理员功能：删除确认与执行
    # ==========================================
    async def confirm_delete(ecn_id):
        dialog.clear()
        with dialog, ui.card().classes("p-6"):
            ui.label("删除确认 (仅管理员)").classes("text-xl font-bold text-red-600 border-b pb-2 mb-4 w-full")
            ui.label(f"您确定要永久删除 ECN 单号【{ecn_id}】吗？")
            ui.label("该操作将清除所有的表单与审批流转记录，且不可恢复！").classes("text-sm text-gray-500 mt-2")
            with ui.row().classes("w-full justify-end mt-6 gap-3"):
                ui.button("取消", on_click=dialog.close).props("outline color=grey")

                async def do_delete():
                    all_ecns = db_storage.get_item("ecn_management_data", {})
                    if ecn_id in all_ecns:
                        del all_ecns[ecn_id]
                        await save_ecn_root_item("ecn_management_data", all_ecns)
                        ui.notify(f"单号 {ecn_id} 已被彻底删除", type="positive")
                        refresh_list()
                    dialog.close()

                ui.button("确认删除", color="red", on_click=do_delete)
        dialog.open()

    # ==========================================
    # 主页面 UI (头部与列表总览)
    # ==========================================
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-600 h-14 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("工程变更管理系统 (ECN)").classes(
            "text-white text-xl font-bold absolute left-1/2 transform -translate-x-1/2"
        )
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {current_user}")
                ui.separator()
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # ==========================================
    # 优化点 1：主页面列表的静默轮询与刷新机制 (终极 O(1) 性能版)
    # ==========================================
    # 这里的 hash 变量名我们改叫 version_stamp，更符合语意
    last_ecn_state_tracker = {"version_stamp": 0.0}

    def check_and_refresh_list():
        # 极限性能：不遍历、不拼接、不哈希。直接拿全局时间戳对比！
        current_stamp = db_storage.get_item("ecn_global_version_stamp", 0.0)
        # 判断时间戳是否发生改变
        if last_ecn_state_tracker["version_stamp"] != 0.0 and current_stamp != last_ecn_state_tracker["version_stamp"]:
            last_ecn_state_tracker["version_stamp"] = current_stamp
            refresh_list()
        elif last_ecn_state_tracker["version_stamp"] == 0.0:
            # 首次加载时记录初始时间戳
            last_ecn_state_tracker["version_stamp"] = current_stamp

    # ui.timer: NiceGUI第三方Web框架中用于周期性执行异步或同步函数的类
    ui.timer(5.0, check_and_refresh_list)
    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.row().classes("w-full justify-between items-center bg-white p-4 shadow-sm rounded-md"):
            with ui.row().classes("gap-4"):
                ui.input("搜索单号/项目/申请人").props("dense outlined").bind_value(
                    page_state, "search_keyword"
                ).classes("w-64")
                ui.select(
                    [
                        "全部",
                        ECNState.DRAFT,
                        ECNState.ECR_REVIEWING,
                        ECNState.ECN_SCHEMING,
                        ECNState.ECN_REVIEWING,
                        ECNState.ECN_EXECUTING,
                        ECNState.PENDING_FINAL_EXECUTE,
                        ECNState.CLOSED,
                        ECNState.CANCEL,
                        ECNState.REJECTED,
                    ],
                    label="状态筛选",
                ).props("dense outlined").bind_value(page_state, "filter_state").classes("w-40")
                ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("color=primary outline")
            ui.button("新建 ECR 申请", icon="add_box", on_click=lambda: open_ecn_detail_dialog()).props("color=red-7")
        with ui.element("div").classes("w-full h-full overflow-y-auto overflow-x-hidden p-4 md:p-6"):
            # with ui.column().classes("w-full p-4 h-[calc(100vh-4rem)] bg-gray-100"):
            list_container = ui.column().classes("w-full mt-4 gap-3 flex-grow overflow-y-auto")

            def refresh_list():
                list_container.clear()
                ALL_ECNS = db_storage.get_item("ecn_management_data", {})
                kw = page_state["search_keyword"].lower()
                f_state = page_state["filter_state"]

                sorted_ecns = sorted(ALL_ECNS.values(), key=lambda x: x["basic_info"]["apply_date"], reverse=True)

                with list_container:
                    if not sorted_ecns:
                        return ui.label("暂无工程变更记录").classes("text-gray-500 m-auto mt-10")

                    for ecn in sorted_ecns:
                        if (
                            kw
                            and kw not in ecn["ecn_id"].lower()
                            and kw not in str(ecn.get("target_projects", "")).lower()
                            and kw not in ecn["basic_info"]["applicant"].lower()
                        ):
                            continue
                        if f_state != "全部" and ecn["workflow"]["current_state"] != f_state:
                            continue

                        with ui.card().classes(
                            "w-full flex flex-row justify-between items-center p-4 bg-blue-50 hover:bg-amber-50 transition-colors cursor-pointer border-l-4 border-blue-500 shadow-sm relative"
                        ) as card:
                            card.on("click", lambda _, e_id=ecn["ecn_id"]: open_ecn_detail_dialog(e_id))

                            if current_user.lower() == "admin":
                                ui.button(icon="delete", color="red").props("flat round dense").classes(
                                    "absolute top-1 right-1 z-10"
                                ).on("click.stop", lambda e, e_id=ecn["ecn_id"]: confirm_delete(e_id)).tooltip(
                                    "永久删除此数据 (管理员专用)"
                                )

                            with ui.column().classes("gap-1"):
                                with ui.row().classes("items-center gap-3"):
                                    ui.label(ecn["ecn_id"]).classes("font-mono font-bold text-gray-800 text-lg")
                                    ui.badge(
                                        ecn["workflow"]["current_state"],
                                        color="red"
                                        if ecn["workflow"]["current_state"] == ECNState.REJECTED
                                        else "orange"
                                        if "中" in ecn["workflow"]["current_state"]
                                        else "green"
                                        if "完成" in ecn["workflow"]["current_state"]
                                        else "grey",
                                    ).props("outline")

                                    current_state = ecn["workflow"]["current_state"]
                                    pending_roles = ecn["workflow"].get("pending_roles", [])

                                    if pending_roles and current_state not in [
                                        ECNState.DRAFT,
                                        ECNState.CLOSED,
                                        ECNState.CANCEL,
                                        ECNState.REJECTED,
                                    ]:
                                        ui.label(f"等待审批: {', '.join(pending_roles)}").classes(
                                            "text-xs font-bold text-orange-600 bg-orange-100 px-2 py-0.5 rounded"
                                        )
                                    elif current_state == ECNState.ECN_SCHEMING:
                                        # 1. 检查人员确认状态
                                        unconfirmed = [
                                            p
                                            for p, status in ecn["workflow"].get("scheme_participants", {}).items()
                                            if status != "confirmed"
                                        ]

                                        # 2. 检查强制交付物覆盖率
                                        review_info = ecn.get("review_info", {})
                                        req_docs = set(
                                            [k for k, v in review_info.get("involved_docs", {}).items() if v]
                                        )
                                        req_mats = set(
                                            [
                                                f"{mat}-{act}"
                                                for mat, actions in review_info.get("involved_materials", {}).items()
                                                if isinstance(actions, dict)
                                                for act, val in actions.items()
                                                if val
                                            ]
                                        )

                                        prov_docs, prov_mats = set(), set()
                                        for item in ecn.get("change_items", []):
                                            prov_docs.update(item.get("linked_docs", []))
                                            prov_mats.update(item.get("linked_materials", []))

                                        missing_docs = req_docs - prov_docs
                                        missing_mats = req_mats - prov_mats
                                        has_missing_deliverables = bool(missing_docs or missing_mats)

                                        # 3. 综合判断并渲染状态标签
                                        if unconfirmed:
                                            ui.label(f"等待完成方案编写: {', '.join(unconfirmed)}").classes(
                                                "text-xs font-bold text-purple-600 bg-purple-100 px-2 py-0.5 rounded"
                                            )
                                        elif has_missing_deliverables:
                                            # 核心防呆：人员都点完了确认，但系统查出仍有漏交的强制项
                                            miss_text = []
                                            if missing_docs:
                                                miss_text.append("资料")
                                            if missing_mats:
                                                miss_text.append("物料")
                                            ui.label(f"尚缺{'、'.join(miss_text)}方案，待补充").classes(
                                                "text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded"
                                            )
                                        elif ecn["workflow"].get("scheme_participants"):
                                            ui.label("方案已齐，待发起评审").classes(
                                                "text-xs font-bold text-green-600 bg-green-100 px-2 py-0.5 rounded"
                                            )

                                ui.label(
                                    ecn["basic_info"].get(
                                        "title", f"涉及项目: {', '.join(ecn.get('target_projects', []))}"
                                    )
                                ).classes("text-sm text-gray-800 font-bold")

                            with ui.column().classes("items-end gap-1"):
                                ui.label(f"申请人: {ecn['basic_info']['applicant']}").classes("text-sm text-gray-600")
                                ui.label(ecn["basic_info"]["apply_date"]).classes("text-xs text-gray-400 font-mono")

                                is_pending = (current_role in ecn["workflow"]["pending_roles"]) or (
                                    ecn["workflow"]["current_state"] == ECNState.REJECTED
                                    and ecn["basic_info"]["applicant"] == current_user
                                )

                                is_scheming = (
                                    ecn["workflow"]["current_state"] == ECNState.ECN_SCHEMING
                                    and any(r in current_role for r in ECN_SCHEME_WRITER_ROLES)
                                    and ecn["workflow"].get("scheme_participants", {}).get(current_user) != "confirmed"
                                )
                                if is_pending or is_scheming:
                                    # ui.chip: NiceGUI框架中用于渲染小标签/徽章的类
                                    ui.chip("待处理", icon="notifications_active", color="red").props(
                                        "dense outline size=sm"
                                    )

            refresh_list()
