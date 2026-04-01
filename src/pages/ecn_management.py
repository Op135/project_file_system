# -*- encoding: utf-8 -*-
import copy  # copy: Python标准库，用于创建对象的副本
import logging
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
    IMG_DIR,
    PRESET_AVATARS,
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
            "sop_impact": "无影响",
            "fixture_impact": "无影响",
            "tool_impact": "无影响",
            "tool_impact_desc": "",
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
# 主路由页面定义
# ==========================================
# @ui.page: NiceGUI框架的路由装饰器，用于定义页面路径
@ui.page("/ecn_management")
async def ecn_management_page():
    ui.add_head_html("""
        <style>
            .q-dialog__inner--minimized>div { max-width: 1200px; }
            .pdf-border { border: 1px solid #cbd5e1; }
            .pdf-border-b { border-bottom: 1px solid #cbd5e1; }
            .pdf-border-r { border-right: 1px solid #cbd5e1; }
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
    root_dialog = ui.dialog().classes("w-full max-w-[95vw]")

    # ==========================================
    # 独立解耦弹窗 1：底层数据变更方案设计
    # ==========================================
    def open_overview_change_dialog(ecn_data, current_user, on_save_callback, edit_item=None):
        is_edit = edit_item is not None
        edit_data = edit_item or {}

        sel_state = {
            "project": edit_data.get("project"),
            "role": edit_data.get("role"),
            "label": edit_data.get("label"),
            "chip_id": edit_data.get("chip_id"),
            "old_data": edit_data.get("old_data", {}) if is_edit else {},
            "new_data": edit_data.get("new_data", {}) if is_edit else {},
            "req_idxs": edit_data.get("req_idxs", []),
            "linked_docs": edit_data.get("linked_docs", []),
            "linked_materials": edit_data.get("linked_materials", []),
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
                i["label"]: f"{i.get('title', '未命名')} ({i['label']})"
                for gl in app.storage.general.get("over_config_data", {}).get(r, {}).values()
                for i in gl
            }

        def get_chips(p, ll):
            return {
                c_id: c.get("content", "")[:30] + "..."
                for c_id, c in db_storage.get_deep_item([f"{p}_over_data", ll], {}).items()
                if c.get("enabled")
            }

        dialog.clear()
        with dialog, ui.card().classes("w-[900px] max-w-full flex flex-col"):
            ui.label("修改概述数据变更方案" if is_edit else "添加概述数据变更方案").classes(
                "text-lg font-bold text-blue-900 shrink-0"
            )
            with ui.column().classes("w-full gap-2 flex-1 min-h-0 overflow-y-auto pr-2"):
                with ui.card().classes("w-full p-3 bg-gray-50 border border-gray-200 shadow-sm gap-2"):
                    ui.label("对应关联 (必填)").classes("text-xs font-bold text-indigo-700")
                    # bind_value: NiceGUI框架实现前后端数据双向绑定的核心方法
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

                def on_proj_change(e):
                    if is_edit:
                        return
                    sel_state["project"] = e.value
                    sel_chip.set_options(get_chips(e.value, sel_state["label"]))
                    sel_chip.set_value(None)

                def on_role_change(e):
                    if is_edit:
                        return
                    sel_state["role"] = e.value
                    sel_label.set_options(get_labels(e.value))
                    sel_label.set_value(None)

                def on_label_change(e):
                    if is_edit:
                        return
                    sel_state["label"] = e.value
                    sel_chip.set_options(get_chips(sel_state["project"], e.value))
                    sel_chip.set_value(None)

                def on_chip_change(e):
                    if is_edit:
                        return
                    sel_state["chip_id"] = e.value
                    if all([sel_state["project"], sel_state["label"], sel_state["chip_id"]]):
                        old_chip_data = db_storage.get_deep_item(
                            [f"{sel_state['project']}_over_data", sel_state["label"], sel_state["chip_id"]], {}
                        )
                        sel_state["old_data"] = copy.deepcopy(old_chip_data)
                        sel_state["new_data"] = copy.deepcopy(old_chip_data)
                        render_dynamic_form()

                with ui.grid(columns=2).classes("w-full gap-2 mt-2"):
                    sel_proj = (
                        ui.select(
                            options=target_projects,
                            label="1. 目标项目",
                            value=sel_state["project"],
                            on_change=on_proj_change,
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )
                    sel_role = (
                        ui.select(options=roles, label="2. 技术维度", value=sel_state["role"], on_change=on_role_change)
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )
                    sel_label = (
                        ui.select(
                            options=get_labels(sel_state["role"]) if is_edit else {},
                            label="3. 具体参数",
                            value=sel_state["label"],
                            on_change=on_label_change,
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )
                    sel_chip = (
                        ui.select(
                            options=get_chips(sel_state["project"], sel_state["label"]) if is_edit else {},
                            label="4. 原数据",
                            value=sel_state["chip_id"],
                            on_change=on_chip_change,
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )

                dynamic_form_container = ui.column().classes("w-full gap-2 mt-2")

                def render_dynamic_form():
                    dynamic_form_container.clear()
                    if not sel_state["old_data"]:
                        return

                    chip_type = sel_state["old_data"].get("type", "text")
                    with dynamic_form_container:
                        ui.label(f"检测到概述数据类型为: {chip_type.upper()}").classes(
                            "text-xs font-bold text-teal-700 bg-teal-50 px-2 py-1 rounded"
                        )
                        with ui.grid(columns=2).classes("w-full gap-4"):
                            with ui.card().classes("w-full bg-gray-50 shadow-inner p-3"):
                                ui.label("现状 / 原内容").classes("text-xs text-gray-500 font-bold mb-2")
                                ui.label(sel_state["old_data"].get("content", "无")).classes(
                                    "text-sm text-gray-700 break-all"
                                )
                                if chip_type == "test":
                                    old_test = sel_state["old_data"].get("test_select_data", {})
                                    ui.label(f"性质: {old_test.get('test_nature_select', '')}").classes(
                                        "text-xs text-gray-500 mt-1"
                                    )
                                    ui.label(f"状态: {old_test.get('state_select', '')}").classes(
                                        "text-xs text-gray-500"
                                    )
                                    ui.label(f"节点: {old_test.get('node_select', '')}").classes(
                                        "text-xs text-gray-500"
                                    )
                                    ui.label(f"工具: {old_test.get('instrument_select', '')}").classes(
                                        "text-xs text-gray-500"
                                    )

                            with ui.card().classes("w-full bg-blue-50 shadow-inner p-3 border border-blue-100"):
                                ui.label("方案 / 新内容 (必填)").classes("text-xs text-blue-700 font-bold mb-2")
                                if chip_type == "text":
                                    ui.textarea("新文本内容").bind_value(sel_state["new_data"], "content").classes(
                                        "w-full"
                                    ).props("outlined auto-grow rows=2")
                                elif chip_type == "test":
                                    ui.textarea("新检测内容与标准").bind_value(
                                        sel_state["new_data"], "content"
                                    ).classes("w-full").props("outlined auto-grow rows=2")
                                    test_data = sel_state["new_data"].setdefault("test_select_data", {})
                                    with ui.grid(columns=2).classes("w-full gap-2 mt-2"):
                                        ui.input("测试性质").bind_value(test_data, "test_nature_select").props(
                                            "outlined dense"
                                        )
                                        ui.input("条件/状态").bind_value(test_data, "state_select").props(
                                            "outlined dense"
                                        )
                                        ui.input("节点/位置").bind_value(test_data, "node_select").props(
                                            "outlined dense"
                                        )
                                        ui.input("工具/仪器/治具").bind_value(test_data, "instrument_select").props(
                                            "outlined dense"
                                        )
                                else:
                                    ui.input("新文件名/新引用").bind_value(sel_state["new_data"], "content").classes(
                                        "w-full"
                                    ).props("outlined")

                                ui.textarea("修改原因/注释").bind_value(sel_state["new_data"], "notes").classes(
                                    "w-full mt-2"
                                ).props("outlined auto-grow rows=1")

            if is_edit:
                render_dynamic_form()

            async def save_item():
                if not sel_state["new_data"].get("content", "").strip():
                    return ui.notify("请完善新内容", type="warning")
                payload = {
                    "item_id": edit_data.get("item_id", str(uuid.uuid4())),
                    "type": "overview_update",
                    "author": current_user,
                    "req_idxs": sel_state["req_idxs"],
                    "linked_docs": sel_state["linked_docs"],
                    "linked_materials": sel_state["linked_materials"],
                    "project": sel_state["project"],
                    "role": sel_state["role"],
                    "label": sel_state["label"],
                    "chip_id": sel_state["chip_id"],
                    "old_data": copy.deepcopy(sel_state["old_data"]),
                    "new_data": copy.deepcopy(sel_state["new_data"]),
                    "execute_status": "pending",
                }
                await on_save_callback(payload, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4 shrink-0"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
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

        async def auto_save_review(e=None):
            if ecn_id and is_scheming_phase:
                await db_storage.set_deep_item(["ecn_management_data", ecn_id, "review_info"], review)

        # ------------------- 渲染 UI -------------------
        root_dialog.clear()
        with root_dialog, ui.card().classes("w-full h-[90vh] flex flex-col p-0 overflow-hidden bg-gray-100 -space-y-3"):
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
                tab_ecn = ui.tab("2. ECN-方案", icon="fact_check")
                tab_exec = ui.tab("3. ECN-执行", icon="assignment_turned_in")
                tab_workflow = ui.tab("审批记录", icon="timeline")

            is_ecr_editable = is_new or (
                basic.get("applicant") == current_user
                and wf.get("current_state") in [ECNState.DRAFT, ECNState.REJECTED]
            )

            with ui.tab_panels(tabs, value=tab_ecr).classes("w-full flex-1 min-h-0 overflow-y-auto p-2 md:p-4"):
                # --- [TAB 1] ECR 申请表单 ---
                with ui.tab_panel(tab_ecr).classes("p-0 bg-transparent"):
                    with ui.column().classes(
                        "gap-0 p-0 bg-white pdf-border shadow-sm w-full max-w-[1000px] mx-auto h-auto"
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

                # --- [TAB 2] ECN 评审表单 ---
                with ui.tab_panel(tab_ecn).classes("gap-0 p-0 max-w-[1000px] mx-auto"):
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
                            ui.label("ECN-评审").classes(
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
                                ui.label("相关影响 (方案编写工程师勾选):").classes("font-bold text-gray-700")
                                with ui.grid().classes(
                                    "w-full grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-2 gap-y-1 ml-4 items-center"
                                ):
                                    # 动态读取配置遍历
                                    for imp_key in ECN_SCHEMA_CONFIG["impact_dimensions"]:
                                        ui.checkbox(imp_key).bind_value(review["impacts"], imp_key).props(
                                            f"{'disable' if not is_scheming_phase else ''} dense"
                                        ).on_value_change(auto_save_review)

                            with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50"):
                                ui.label("变更涉及文档/图纸:").classes("font-bold text-gray-700")
                                with ui.grid().classes(
                                    "w-full grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-2 gap-y-1 ml-4 p-1 max-w-[900px]"
                                ):
                                    # 动态读取配置遍历
                                    for doc_key in ECN_SCHEMA_CONFIG["document_types"]:
                                        ui.checkbox(doc_key).bind_value(review["involved_docs"], doc_key).props(
                                            f"{'disable' if not is_scheming_phase else ''} dense"
                                        ).on_value_change(auto_save_review)

                                # bind_visibility_from: 实现“其它”项仅在勾选后显示
                                ui.input("其它文档:").bind_value(review, "other_docs_desc").bind_visibility_from(
                                    review["involved_docs"], "其它文档"
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

                            with ui.grid(columns=1).classes(
                                "w-full grid-cols-1 md:grid-cols-3 pdf-border-b bg-gray-50"
                            ):
                                with ui.column().classes("p-2 pdf-border-r gap-1 hover:bg-white"):
                                    ui.label("SOP:").classes("font-bold text-gray-700")
                                    ui.radio(["无影响", "更新SOP"]).bind_value(review, "sop_impact").props(
                                        f"{'disable' if not is_scheming_phase else ''} dense inline"
                                    ).on_value_change(auto_save_review)
                                with ui.column().classes("p-2 pdf-border-r gap-1 hover:bg-white"):
                                    ui.label("治具:").classes("font-bold text-gray-700")
                                    ui.radio(["无影响", "新做治具", "修改治具"]).bind_value(
                                        review, "fixture_impact"
                                    ).props(
                                        f"{'disable' if not is_scheming_phase else ''} dense inline"
                                    ).on_value_change(auto_save_review)
                                with ui.column().classes("p-2 gap-1 hover:bg-white"):
                                    ui.label("工具:").classes("font-bold text-gray-700")
                                    ui.radio(["无影响", "新购工具", "其它"]).bind_value(review, "tool_impact").props(
                                        f"{'disable' if not is_scheming_phase else ''} dense inline"
                                    ).on_value_change(auto_save_review)

                            with ui.column().classes("w-full p-2 gap-3 bg-blue-50/30"):
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
                                        ui.label("提供人员确认状态:").classes("text-sm font-bold text-gray-600")
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
                                                    ).props("size=sm")

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
                                        await db_storage.set_deep_item(
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

                                item_container = ui.column().classes("w-full gap-3")

                                async def handle_save_item(item_data, is_edit=False):
                                    if is_edit:
                                        for idx, e_item in enumerate(local_data["change_items"]):
                                            if e_item["item_id"] == item_data["item_id"]:
                                                local_data["change_items"][idx] = item_data
                                                break
                                    else:
                                        local_data["change_items"].append(item_data)
                                    parts[current_user] = "editing"
                                    await db_storage.set_deep_item(
                                        [
                                            "ecn_management_data",
                                            local_data["ecn_id"],
                                            "workflow",
                                            "scheme_participants",
                                        ],
                                        parts,
                                    )
                                    await db_storage.set_deep_item(
                                        ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                        local_data["change_items"],
                                    )
                                    render_parts()
                                    render_my_actions()
                                    render_items()

                                def render_items():
                                    item_container.clear()
                                    with item_container:
                                        if not local_data["change_items"]:
                                            ui.label("暂未添加具体的方案条目").classes(
                                                "text-sm text-gray-400 m-auto mt-4"
                                            )
                                        for idx, item in enumerate(local_data["change_items"]):
                                            with ui.card().classes(
                                                "w-full p-0 shadow-sm border border-gray-200 relative"
                                            ):
                                                with ui.row().classes(
                                                    "w-full bg-gray-100 p-2 justify-between items-center"
                                                ):
                                                    with ui.row().classes("gap-2 items-center flex-wrap"):
                                                        ui.badge(str(idx + 1), color="grey-7")
                                                        ui.badge(
                                                            "研发概述修改"
                                                            if item["type"] == "overview_update"
                                                            else f"文本/工艺: {item.get('change_type', '')}",
                                                            color="blue"
                                                            if item["type"] == "overview_update"
                                                            else "teal",
                                                        )
                                                        if item.get("req_idxs"):
                                                            ui.label(
                                                                f"解决要求: {', '.join(map(str, item['req_idxs']))}"
                                                            ).classes(
                                                                "text-xs font-bold text-amber-800 bg-amber-100 px-1 rounded"
                                                            )
                                                        if item.get("linked_docs"):
                                                            ui.label(
                                                                f"对应勾选文档: {', '.join(item['linked_docs'])}"
                                                            ).classes(
                                                                "text-xs font-bold text-indigo-800 bg-indigo-100 px-1 rounded"
                                                            )
                                                        if item.get("linked_materials"):
                                                            ui.label(
                                                                f"对应勾选物料: {', '.join(item['linked_materials'])}"
                                                            ).classes(
                                                                "text-xs font-bold text-pink-800 bg-pink-100 px-1 rounded"
                                                            )
                                                    ui.label(f"作者: {item['author']}").classes("text-xs text-gray-500")

                                                    if (
                                                        is_scheming_phase
                                                        and item["author"] == current_user
                                                        and parts.get(current_user) != "confirmed"
                                                    ):
                                                        with ui.row().classes("absolute top-1 right-1 gap-1"):
                                                            ui.button(
                                                                icon="edit",
                                                                on_click=lambda _, i=item: (
                                                                    open_overview_change_dialog(
                                                                        local_data, current_user, handle_save_item, i
                                                                    )
                                                                    if i["type"] == "overview_update"
                                                                    else open_text_change_dialog(
                                                                        local_data, current_user, handle_save_item, i
                                                                    )
                                                                ),
                                                            ).props("flat round text-color=blue size=sm")
                                                            ui.button(
                                                                icon="delete", on_click=lambda _, i=item: remove_item(i)
                                                            ).props("flat round text-color=red size=sm")

                                                with ui.column().classes("w-full p-3 gap-1 bg-white"):
                                                    if item["type"] == "overview_update":
                                                        ui.label(
                                                            f"【{item.get('project')} - {item.get('role')} - {item.get('label')}】"
                                                        ).classes("text-xs font-bold text-blue-900")
                                                        with ui.row().classes("w-full items-start gap-2"):
                                                            ui.label(
                                                                item.get("old_data", {}).get("content", "")
                                                            ).classes(
                                                                "text-sm text-gray-500 line-through bg-gray-50 p-1 rounded break-all"
                                                            )
                                                            ui.icon("arrow_forward", color="gray").classes("mt-1")
                                                            new_d = item.get("new_data", {})
                                                            if item.get("old_data", {}).get("type") == "test":
                                                                with ui.column().classes(
                                                                    "bg-green-50 p-2 rounded gap-0"
                                                                ):
                                                                    ui.label(new_d.get("content", "")).classes(
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
                                    local_data["change_items"].remove(item_to_remove)
                                    await db_storage.set_deep_item(
                                        ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                        local_data["change_items"],
                                    )
                                    render_items()

                                render_items()

                # --- [TAB 3] ECN 执行与试产 ---
                with ui.tab_panel(tab_exec).classes("gap-4 p-0 max-w-[1000px] mx-auto"):
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
                    if is_new:
                        ui.label("暂无审批记录，请先发起申请。").classes("text-gray-500 mt-4 text-center w-full")
                    else:
                        with ui.column().classes("w-full"):
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
                                for log in local_data["approval_log"]:
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

            # ------------------------------------------
            # 底部操作栏及各类事件触发器
            # ------------------------------------------
            with ui.row().classes(
                "w-full bg-white p-4 border-t border-gray-300 justify-end items-center shrink-0 gap-4 shadow-[0_-5px_15px_rgba(0,0,0,0.05)]"
            ):
                if is_draft_or_reject:
                    if basic["applicant"] == current_user or is_new:
                        ui.button("保存为草稿", on_click=lambda: execute_db_action("save_draft")).props(
                            "color=grey-7 size=lg"
                        )
                        ui.button("发起/重新发起 ECR", on_click=lambda: execute_db_action("submit_ecr")).props(
                            "color=primary size=lg"
                        )
                else:
                    is_pending_user = current_role in wf["pending_roles"]
                    if wf["current_state"] == ECNState.ECR_REVIEWING and basic["applicant"] == current_user:
                        ui.button("撤回修改", icon="undo", on_click=lambda: execute_db_action("withdraw")).props(
                            "color=orange size=lg"
                        )
                        ui.button("作废", icon="delete_forever", on_click=lambda: execute_db_action("cancel")).props(
                            "color=red size=lg"
                        )
                    if wf["current_state"] == ECNState.PENDING_FINAL_EXECUTE and "研发经理" in current_role:
                        ui.button(
                            "驳回至方案阶段", color="red", on_click=lambda: execute_db_action("reject", note="")
                        ).props("size=lg")
                        ui.button(
                            "确认各部已就绪，立刻执行数据变更并归档",
                            icon="warning",
                            on_click=lambda: execute_db_action("final_execute"),
                        ).props("color=red size=lg")
                    elif is_scheming_phase and any(r in current_role for r in ECN_SCHEME_INITIATOR_ROLES):
                        all_confirmed = len(parts) > 0 and all(s == "confirmed" for s in parts.values())
                        btn = ui.button(
                            "发起 ECN 方案评审", on_click=lambda: execute_db_action("initiate_scheme_review")
                        ).props(f"color=purple size=lg {'disabled' if not all_confirmed else ''}")
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
                        ).props("size=lg")
                        ui.button(
                            "同意", color="green", on_click=lambda: execute_db_action("approve", note=note_input.value)
                        ).props("size=lg")

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
                        for item in local_data["change_items"]:
                            if item["type"] == "overview_update":
                                path = [f"{item['project']}_over_data", item["label"], item["chip_id"]]
                                t_chip = db_storage.get_deep_item(path)
                                if t_chip:
                                    t_chip.update(
                                        {
                                            k: v
                                            for k, v in item.get("new_data", {}).items()
                                            if k in ["content", "notes", "test_select_data", "file_type", "url_path"]
                                        }
                                    )
                                    t_chip.setdefault("timestamp", {})[now_str] = {
                                        "creator": f"ECN自动执行 ({local_data['ecn_id']})",
                                        "select_activ_dic": copy.deepcopy(t_chip.get("select_activ_dic", {})),
                                    }
                                    await db_storage.set_deep_item(path, t_chip)
                                    item["execute_status"] = "success"
                        wf["current_state"], wf["pending_roles"] = ECNState.CLOSED, []
                        local_data["approval_log"].append(
                            {"user": current_user, "role": current_role, "action": "执行变更", "time": now_str}
                        )
                    except Exception as e:
                        logger.error(f"执行ECN变更失败: {e}", exc_info=True)
                        return ui.notify(f"执行失败: {e}", type="negative")

                await db_storage.set_deep_item(["ecn_management_data", local_data["ecn_id"]], local_data)
                ui.notify("操作成功！", type="positive")
                root_dialog.close()
                refresh_list()

            # --- 协同同步定时器 ---
            async def sync_schemes():
                if wf["current_state"] == ECNState.ECN_SCHEMING and ecn_id:
                    fresh = db_storage.get_deep_item(["ecn_management_data", ecn_id])
                    if fresh:
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

                        fresh_rev = fresh.get("review_info", {})
                        if fresh_rev:
                            for k, v in fresh_rev.get("impacts", {}).items():
                                review["impacts"][k] = v
                            for k, v in fresh_rev.get("involved_docs", {}).items():
                                review["involved_docs"][k] = v
                            for mat, acts in fresh_rev.get("involved_materials", {}).items():
                                if mat in review["involved_materials"] and isinstance(acts, dict):
                                    for act, val in acts.items():
                                        review["involved_materials"][mat][act] = val
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
                        await db_storage.set_item("ecn_management_data", all_ecns)
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

    with ui.column().classes("w-full p-4 h-[calc(100vh-4rem)] bg-gray-100"):
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
                        "w-full flex flex-row justify-between items-center p-4 hover:bg-blue-50 transition-colors cursor-pointer border-l-4 border-blue-500 shadow-sm relative"
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
                                    unconfirmed = [
                                        p
                                        for p, status in ecn["workflow"].get("scheme_participants", {}).items()
                                        if status != "confirmed"
                                    ]
                                    if unconfirmed:
                                        ui.label(f"等待方案确认: {', '.join(unconfirmed)}").classes(
                                            "text-xs font-bold text-purple-600 bg-purple-100 px-2 py-0.5 rounded"
                                        )
                                    elif ecn["workflow"].get("scheme_participants"):
                                        ui.label("方案已齐，待发起评审").classes(
                                            "text-xs font-bold text-green-600 bg-green-100 px-2 py-0.5 rounded"
                                        )

                            ui.label(
                                ecn["basic_info"].get("title", f"涉及项目: {', '.join(ecn.get('target_projects', []))}")
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
