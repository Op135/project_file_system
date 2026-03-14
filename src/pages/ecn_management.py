# -*- encoding: utf-8 -*-
import copy
import logging
import uuid
from datetime import datetime

from nicegui import app, ui

from .. import db_storage
from ..config import (
    ECN_ALLOWED_PROJECT_STATES,
    ECN_SCHEME_INITIATOR_ROLES,
    ECN_SCHEME_WRITER_ROLES,
    ECN_WORKFLOW_ROUTES,
    IMG_DIR,
    PRESET_AVATARS,
    ECNState,
)
from ..utils import get_cache_busted_path, logout

logger = logging.getLogger(__name__)


# --- 辅助：自动生成 ECN 单号 ---
def generate_ecn_id(all_ecns: dict) -> str:
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
    next_num_str = str(max_count + 1).zfill(2)
    return f"{prefix}{next_num_str}"


# --- 辅助：数据模型生成 ---
def generate_initial_ecn_data(applicant: str, all_ecns: dict) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 查找所有记录在案的当天ECN的编号，自动在最大序号上加1，成为当前新ECN的编号
    ecn_id = generate_ecn_id(all_ecns)
    return {
        "ecn_id": ecn_id,
        "basic_info": {
            "title": "",
            "nature": "永久变更",
            "reason_type": "需求更改",
            "reason_desc": "",
            "requirements": [],  # [{"idx": 1, "content": "..."}]
            "applicant": applicant,
            "apply_date": now_str,
        },
        "target_projects": [],
        "change_items": [],  # [{"item_id":..., "type":..., "req_idxs": [1,2], "author": "user", ...}]
        "workflow": {
            "current_state": ECNState.DRAFT,
            "current_phase": "ECR_PHASE",
            "current_step_index": 0,
            "route_type": "",
            "pending_roles": [],
            "step_approvals": {},
            "scheme_participants": {},  # {"张三": "editing", "李四": "confirmed"}
        },
        "approval_log": [],
        "timestamp": {now_str: f"由 {applicant} 创建草稿"},
    }


@ui.page("/ecn_management")
async def ecn_management_page():
    ui.add_head_html("""
        <style>
            .q-dialog__inner--minimized>div {
                    max-width: 1000px;
                }
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

    if db_storage.get_item("ecn_management_data") is None:
        await db_storage.set_item("ecn_management_data", {})

    page_state = {"search_keyword": "", "filter_state": "全部"}

    # ==========================================
    # 弹窗 1：概述变更项添加 / 编辑 (深度对齐底层数据)
    # ==========================================
    def open_add_overview_change_dialog(ecn_data, on_save_callback, edit_item={}):
        dialog = ui.dialog().props("persistent")
        is_edit = edit_item != {}

        sel_state = {
            "project": edit_item["project"] if is_edit else None,
            "role": edit_item["role"] if is_edit else None,
            "label": edit_item["label"] if is_edit else None,
            "chip_id": edit_item["chip_id"] if is_edit else None,
            "old_data": edit_item.get("old_data", {}) if is_edit else {},
            "new_data": edit_item.get("new_data", {}) if is_edit else {},
            "req_idxs": edit_item["req_idxs"] if is_edit else [],
        }

        target_projects = ecn_data.get("target_projects", [])
        roles = list(app.storage.general.get("over_config_data", {}).keys())
        req_options = {
            req["idx"]: f"[{req['idx']}] {req['content'][:15]}..." for req in ecn_data["basic_info"]["requirements"]
        }

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

        with dialog, ui.card().classes("w-[800px] max-w-full flex flex-col"):
            ui.label("修改概述数据变更方案" if is_edit else "添加概述数据变更方案").classes(
                "text-lg font-bold text-blue-900 shrink-0"
            )

            with ui.column().classes("w-full gap-2 flex-1 min-h-0 overflow-y-auto"):
                ui.select(options=req_options, multiple=True, label="关联解决的要求序号 (支持多选)").classes(
                    "w-full"
                ).bind_value(sel_state, "req_idxs")

                # 顶部选择区域 (编辑模式下锁定目标以防错乱)
                with ui.grid(columns=2).classes("w-full gap-2"):
                    sel_proj = (
                        ui.select(
                            options=target_projects,
                            label="1. 目标项目",
                            value=sel_state["project"],
                            on_change=lambda e: (
                                [
                                    sel_state.update(project=e.value),
                                    sel_chip.set_options(get_chips(e.value, sel_state["label"])),
                                    sel_chip.set_value(None),
                                ]
                                if not is_edit
                                else None
                            ),
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )

                    sel_role = (
                        ui.select(
                            options=roles,
                            label="2. 技术维度",
                            value=sel_state["role"],
                            on_change=lambda e: (
                                [
                                    sel_state.update(role=e.value),
                                    sel_label.set_options(get_labels(e.value)),
                                    sel_label.set_value(None),
                                ]
                                if not is_edit
                                else None
                            ),
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )

                    sel_label = (
                        ui.select(
                            options=get_labels(sel_state["role"]) if is_edit else {},
                            label="3. 具体参数",
                            value=sel_state["label"],
                            on_change=lambda e: (
                                [
                                    sel_state.update(label=e.value),
                                    sel_chip.set_options(get_chips(sel_state["project"], e.value)),
                                    sel_chip.set_value(None),
                                ]
                                if not is_edit
                                else None
                            ),
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )

                    sel_chip = (
                        ui.select(
                            options=get_chips(sel_state["project"], sel_state["label"]) if is_edit else {},
                            label="4. 原数据",
                            value=sel_state["chip_id"],
                        )
                        .classes("w-full")
                        .props(f"readonly={is_edit}")
                    )

                # 动态表单容器
                dynamic_form_container = ui.column().classes("w-full gap-2 mt-2")

                def render_dynamic_form():
                    dynamic_form_container.clear()
                    if not sel_state["old_data"]:
                        return

                    chip_type = sel_state["old_data"].get("type", "text")

                    with dynamic_form_container:
                        ui.label(f"检测到底层数据类型为: {chip_type.upper()}").classes(
                            "text-xs font-bold text-teal-700 bg-teal-50 px-2 py-1 rounded"
                        )

                        with ui.grid(columns=2).classes("w-full gap-4"):
                            # 左侧：原数据展示
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

                            # 右侧：新方案填写
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
                                elif chip_type in ["file", "image", "video", "search", "svn"]:
                                    ui.input("新文件名/新引用").bind_value(sel_state["new_data"], "content").classes(
                                        "w-full"
                                    ).props("outlined")

                                ui.textarea("修改原因/注释").bind_value(sel_state["new_data"], "notes").classes(
                                    "w-full mt-2"
                                ).props("outlined auto-grow rows=1")

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

            if not is_edit:
                sel_chip.on_value_change(on_chip_change)
            else:
                render_dynamic_form()

            async def save_item():
                if (
                    not all([sel_state["project"], sel_state["label"], sel_state["chip_id"]])
                    or not sel_state["new_data"].get("content", "").strip()
                ):
                    ui.notify("请完善必填信息", type="warning")
                    return

                item_data = {
                    "item_id": edit_item["item_id"] if is_edit else str(uuid.uuid4()),
                    "type": "overview_update",
                    "author": current_user,
                    "req_idxs": sel_state["req_idxs"],
                    "project": sel_state["project"],
                    "role": sel_state["role"],
                    "label": sel_state["label"],
                    "chip_id": sel_state["chip_id"],
                    "old_data": copy.deepcopy(sel_state["old_data"]),
                    "new_data": copy.deepcopy(sel_state["new_data"]),
                    "execute_status": "pending",
                }
                await on_save_callback(item_data, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4 shrink-0"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")
        dialog.open()

    # ==========================================
    # 弹窗 2：其他文本描述方案添加 / 编辑 (双列布局)
    # ==========================================
    def open_add_text_change_dialog(ecn_data, on_save_callback, edit_item={}):
        dialog = ui.dialog().props("persistent")
        is_edit = edit_item != {}
        sel_state = {
            "req_idxs": edit_item["req_idxs"] if is_edit else [],
            "change_type": edit_item["change_type"] if is_edit else "物料变更",
        }
        req_options = {
            req["idx"]: f"[{req['idx']}] {req['content'][:15]}..." for req in ecn_data["basic_info"]["requirements"]
        }

        with dialog, ui.card().classes("w-[800px] max-w-full"):
            ui.label("修改补充说明方案" if is_edit else "添加补充说明方案").classes("text-lg font-bold text-blue-900")

            with ui.row().classes("w-full gap-4"):
                ui.select(options=req_options, multiple=True, label="关联解决的要求序号").classes("flex-1").bind_value(
                    sel_state, "req_idxs"
                )
                ui.select(["物料变更", "图纸更新", "工艺调整", "SOP修改", "其它"], label="方案分类").classes(
                    "w-48"
                ).bind_value(sel_state, "change_type")

            with ui.grid(columns=2).classes("w-full gap-4 mt-2"):
                old_content_ui = (
                    ui.textarea(label="现状 / 原内容 (必填)", value=edit_item.get("old_content", "") if is_edit else "")
                    .classes("w-full")
                    .props("outlined auto-grow rows=4")
                )

                new_content_ui = (
                    ui.textarea(
                        label="变更方案 / 新内容 (必填)", value=edit_item.get("new_content", "") if is_edit else ""
                    )
                    .classes("w-full")
                    .props("outlined auto-grow rows=4 bg-blue-50")
                )

            async def save_item():
                if not old_content_ui.value.strip() or not new_content_ui.value.strip():
                    ui.notify("原内容与新内容均不能为空", type="warning")
                    return
                item_data = {
                    "item_id": edit_item["item_id"] if is_edit else str(uuid.uuid4()),
                    "type": "text_desc",
                    "author": current_user,
                    "req_idxs": sel_state["req_idxs"],
                    "change_type": sel_state["change_type"],
                    "old_content": old_content_ui.value.strip(),
                    "new_content": new_content_ui.value.strip(),
                    "execute_status": "manual_record",
                }
                await on_save_callback(item_data, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")
        dialog.open()

    # ==========================================
    # 弹窗 3：ECN 详情与操作主控台
    # ==========================================
    async def open_ecn_detail_dialog(ecn_id=None):
        dialog = ui.dialog().classes("w-full max-w-[95vw]")
        is_new = ecn_id is None
        all_ecns = db_storage.get_item("ecn_management_data", {})

        # 1. 项目三级级联
        proj_dict = {"其它": {"其它": {}}}
        for p, data in app.storage.general.get("project_summary", {}).items():
            if data.get("state") in ECN_ALLOWED_PROJECT_STATES:
                parts = p.split("-")
                if len(parts) >= 2:
                    l1, l2, l3 = parts[0], parts[1], "-".join(parts[2:]) if len(parts) > 2 else "基础版"
                    proj_dict.setdefault(l1, {}).setdefault(l2, {})[p] = l3
                else:
                    proj_dict["其它"]["其它"][p] = p
        if not proj_dict["其它"]["其它"]:
            del proj_dict["其它"]

        if is_new:
            if not proj_dict:
                ui.notify("当前没有处于【试产】或【量产】阶段的项目。", type="warning")
                return
            ecn_data = generate_initial_ecn_data(current_user, all_ecns)
        else:
            ecn_data = all_ecns.get(ecn_id)

        local_data = copy.deepcopy(ecn_data)
        is_draft_or_reject = is_new or local_data["workflow"]["current_state"] in [ECNState.DRAFT, ECNState.REJECTED]
        wf = local_data["workflow"]

        with dialog, ui.card().classes("w-full h-[85vh] flex flex-col p-0 overflow-hidden"):
            # Header
            with ui.row().classes(
                "w-full bg-blue-50 p-4 border-b border-blue-200 justify-between items-center shrink-0"
            ):
                ui.label("新建 ECR 申请" if is_new else f"工程变更单: {local_data['ecn_id']}").classes(
                    "text-xl font-bold text-blue-900"
                )
                ui.chip(
                    wf["current_state"],
                    color="orange"
                    if "中" in wf["current_state"]
                    else "red"
                    if wf["current_state"] == ECNState.REJECTED
                    else "blue",
                ).props("outline")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")

            with ui.tabs().classes("w-full shrink-0") as tabs:
                tab_ecr = ui.tab("ECR 申请信息", icon="assignment")
                tab_ecn = ui.tab("ECN 方案明细", icon="engineering")
                tab_workflow = ui.tab("审批流转记录", icon="timeline")

            # 核心内容区
            with ui.tab_panels(tabs, value=tab_ecr).classes("w-full flex-1 min-h-0 overflow-y-auto bg-gray-50/30 p-4"):
                # ------ Tab 1: ECR 申请信息 ------
                with ui.tab_panel(tab_ecr).classes("gap-4"):
                    with ui.card().classes("w-full shadow-sm shrink-0"):
                        ui.label("基础申请信息").classes("font-bold text-gray-700 border-b w-full pb-1 mb-2")
                        with ui.grid(columns=2).classes("w-full gap-4"):
                            ui.input("变更单标题 (简述)", value=local_data["basic_info"]["title"]).classes(
                                "w-full"
                            ).bind_value(local_data["basic_info"], "title").props(f"readonly={not is_draft_or_reject}")
                            ui.select(
                                ["永久变更", "临时变更"], label="变更性质", value=local_data["basic_info"]["nature"]
                            ).classes("w-full").bind_value(local_data["basic_info"], "nature").props(
                                f"readonly={not is_draft_or_reject}"
                            )

                            with ui.column().classes("w-full col-span-2 gap-1"):
                                if is_draft_or_reject:
                                    proj_sel_state = {"l1": None, "l2": None, "l3": None}
                                    with ui.row().classes("w-full items-center gap-2"):
                                        sel_l1 = ui.select(
                                            options=list(proj_dict.keys()),
                                            label="大系列",
                                            on_change=lambda e: [
                                                proj_sel_state.update(l1=e.value),
                                                sel_l2.set_options(
                                                    list(proj_dict.get(e.value, {}).keys()) if e.value else []
                                                ),
                                                sel_l2.set_value(None),
                                                sel_l3.set_options({}),
                                                sel_l3.set_value(None),
                                                sel_l2.update(),
                                                sel_l3.update(),
                                            ],
                                        ).classes("flex-grow")
                                        sel_l2 = ui.select(
                                            options=[],
                                            label="小系列",
                                            on_change=lambda e: [
                                                proj_sel_state.update(l2=e.value),
                                                sel_l3.set_options(
                                                    proj_dict[proj_sel_state["l1"]][e.value]
                                                    if proj_sel_state["l1"] and e.value
                                                    else {}
                                                ),
                                                sel_l3.set_value(None),
                                                sel_l3.update(),
                                            ],
                                        ).classes("flex-grow")
                                        sel_l3 = ui.select(
                                            options={},
                                            label="衍生/具体型号",
                                            on_change=lambda e: proj_sel_state.update(l3=e.value),
                                        ).classes("flex-grow")

                                        def add_proj():
                                            p = proj_sel_state["l3"]
                                            if p and p not in local_data["target_projects"]:
                                                local_data["target_projects"].append(p)
                                                render_proj_chips()
                                            sel_l3.value = None

                                        ui.button("添加项目", icon="add", on_click=add_proj).props(
                                            "outline color=primary"
                                        )

                                proj_chip_container = ui.row().classes(
                                    "w-full gap-2 mt-1 min-h-[32px] p-2 bg-gray-100 rounded border border-dashed"
                                )

                                def render_proj_chips():
                                    proj_chip_container.clear()
                                    with proj_chip_container:
                                        if not local_data["target_projects"]:
                                            ui.label("尚未添加项目").classes("text-xs text-gray-400 italic mt-1")
                                        for p in local_data["target_projects"]:
                                            with ui.chip(color="primary", text_color="white").classes(
                                                "gap-1 items-center"
                                            ):
                                                ui.label(p)
                                                if is_draft_or_reject:
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

                            ui.select(
                                ["需求更改", "设计改善", "工艺调整", "物料替换", "其它"],
                                label="变更原因",
                                value=local_data["basic_info"]["reason_type"],
                            ).classes("w-full").bind_value(local_data["basic_info"], "reason_type").props(
                                f"readonly={not is_draft_or_reject}"
                            )
                            ui.textarea("原因详细说明", value=local_data["basic_info"]["reason_desc"]).classes(
                                "w-full col-span-2"
                            ).bind_value(local_data["basic_info"], "reason_desc").props(
                                f"readonly={not is_draft_or_reject} auto-grow rows=2"
                            )

                    with ui.card().classes("w-full shadow-sm mt-2 shrink-0"):
                        ui.label("变更要求 (自动分配序号)").classes("font-bold text-gray-700 border-b w-full pb-1 mb-2")
                        if is_draft_or_reject:
                            with ui.row().classes("w-full gap-2 mb-2 items-center"):
                                req_input = (
                                    ui.input("输入具体的变更要求...").props("dense outlined").classes("flex-grow")
                                )

                                def add_req():
                                    if req_input.value.strip():
                                        local_data["basic_info"]["requirements"].append(
                                            {
                                                "idx": len(local_data["basic_info"]["requirements"]) + 1,
                                                "content": req_input.value.strip(),
                                            }
                                        )
                                        req_input.value = ""
                                        render_reqs()

                                ui.button("添加", on_click=add_req, icon="add").props("dense color=primary")

                        req_container = ui.column().classes("w-full gap-1 p-2 bg-gray-50 rounded")

                        def render_reqs():
                            req_container.clear()
                            with req_container:
                                if not local_data["basic_info"]["requirements"]:
                                    ui.label("尚未填写任何要求").classes("text-sm text-gray-400")
                                for req in local_data["basic_info"]["requirements"]:
                                    with ui.row().classes(
                                        "w-full items-start gap-2 bg-white p-2 border rounded shadow-sm relative"
                                    ):
                                        ui.badge(str(req["idx"]), color="blue-grey-6")
                                        ui.label(req["content"]).classes("text-sm text-gray-800 break-all pr-6")
                                        if is_draft_or_reject:
                                            ui.button(
                                                icon="close",
                                                on_click=lambda r=req: [
                                                    local_data["basic_info"]["requirements"].remove(r),
                                                    [
                                                        req.update(idx=i + 1)
                                                        for i, req in enumerate(
                                                            local_data["basic_info"]["requirements"]
                                                        )
                                                    ],
                                                    render_reqs(),
                                                ],
                                            ).props("flat round dense size=xs color=red").classes(
                                                "absolute top-1 right-1"
                                            )

                        render_reqs()

                # ------ Tab 2: ECN 方案明细 (协同编辑区) ------
                with ui.tab_panel(tab_ecn).classes("gap-4 p-0 bg-gray-100"):
                    if wf["current_phase"] == "ECR_PHASE" and not is_new:
                        ui.label("当前处于 ECR 申请阶段，ECN 方案将在评审通过后由工程师协同填写。").classes(
                            "text-gray-500 m-8 text-center"
                        )
                    elif is_new:
                        ui.label("请先完成 ECR 申请并发起流程。").classes("text-gray-500 m-8 text-center")
                    else:
                        is_scheming_phase = wf["current_state"] == ECNState.ECN_SCHEMING
                        parts = wf.setdefault("scheme_participants", {})

                        # 顶部状态与操作栏
                        with ui.row().classes(
                            "w-full bg-white p-3 border-b items-center justify-between shadow-sm sticky top-0 z-10"
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.label("方案参与者状态:").classes("text-sm font-bold text-gray-600")
                                parts_container = ui.row().classes("gap-1")

                                def render_parts():
                                    parts_container.clear()
                                    with parts_container:
                                        if not parts:
                                            ui.label("暂无人员提供方案").classes("text-xs text-gray-400 italic mt-1")
                                        for p, status in parts.items():
                                            ui.chip(
                                                f"{p}: {'已确认' if status == 'confirmed' else '编写中'}",
                                                color="green" if status == "confirmed" else "orange",
                                                icon="check_circle" if status == "confirmed" else "edit",
                                            ).props("size=sm")

                                render_parts()

                            # 个人协同按钮
                            if is_scheming_phase and any(role in current_role for role in ECN_SCHEME_WRITER_ROLES):

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

                                my_action_container = ui.row()

                                def render_my_actions():
                                    my_action_container.clear()
                                    with my_action_container:
                                        cur_status = parts.get(current_user)
                                        if cur_status == "editing":
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

                        # 统一保存回调逻辑
                        async def handle_save_item(item_data, is_edit=False):
                            if is_edit:
                                for idx, existing_item in enumerate(local_data["change_items"]):
                                    if existing_item["item_id"] == item_data["item_id"]:
                                        local_data["change_items"][idx] = item_data
                                        break
                            else:
                                local_data["change_items"].append(item_data)

                            parts[current_user] = "editing"
                            await db_storage.set_deep_item(
                                ["ecn_management_data", local_data["ecn_id"], "workflow", "scheme_participants"], parts
                            )
                            await db_storage.set_deep_item(
                                ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                local_data["change_items"],
                            )
                            render_parts()
                            render_my_actions()
                            render_items()

                        def open_edit_dialog(item_to_edit):
                            if item_to_edit["type"] == "overview_update":
                                open_add_overview_change_dialog(local_data, handle_save_item, edit_item=item_to_edit)
                            else:
                                open_add_text_change_dialog(local_data, handle_save_item, edit_item=item_to_edit)

                        # 方案列表与添加区域
                        with ui.column().classes("w-full p-4 gap-3"):
                            if (
                                is_scheming_phase
                                and parts.get(current_user) != "confirmed"
                                and any(role in current_role for role in ECN_SCHEME_WRITER_ROLES)
                            ):
                                with ui.row().classes("w-full gap-3 mb-2"):
                                    ui.button(
                                        "添加概述自动修改方案",
                                        icon="auto_fix_high",
                                        on_click=lambda: open_add_overview_change_dialog(local_data, handle_save_item),
                                    ).props("color=primary outline")
                                    ui.button(
                                        "添加文本描述方案 (物料/工艺等)",
                                        icon="text_snippet",
                                        on_click=lambda: open_add_text_change_dialog(local_data, handle_save_item),
                                    ).props("color=secondary outline")

                            item_container = ui.column().classes("w-full gap-3")

                            def render_items():
                                item_container.clear()
                                with item_container:
                                    if not local_data["change_items"]:
                                        ui.label("暂未添加具体的方案条目").classes("text-sm text-gray-400 m-auto mt-4")
                                    for idx, item in enumerate(local_data["change_items"]):
                                        with ui.card().classes("w-full p-0 shadow-sm border border-gray-200 relative"):
                                            # 条目 Header
                                            with ui.row().classes(
                                                "w-full bg-gray-100 p-2 justify-between items-center"
                                            ):
                                                with ui.row().classes("gap-2 items-center"):
                                                    ui.badge(str(idx + 1), color="grey-7")
                                                    ui.badge(
                                                        item["type"] == "overview_update"
                                                        and "概述自动修改"
                                                        or "人工文本描述",
                                                        color="blue" if item["type"] == "overview_update" else "teal",
                                                    )
                                                    if item.get("req_idxs"):
                                                        ui.label(
                                                            f"关联要求: {', '.join(map(str, item['req_idxs']))}"
                                                        ).classes(
                                                            "text-xs font-bold text-amber-800 bg-amber-100 px-1 rounded"
                                                        )
                                                ui.label(f"作者: {item['author']}").classes("text-xs text-gray-500")

                                                # 操作权限：草稿期 作者自己可以编辑和删除 (前提是自己还没点确认)
                                                can_edit = (
                                                    is_scheming_phase
                                                    and item["author"] == current_user
                                                    and parts.get(current_user) != "confirmed"
                                                )
                                                if can_edit:
                                                    with ui.row().classes("absolute top-1 right-1 gap-1"):
                                                        ui.button(
                                                            icon="edit", on_click=lambda i=item: open_edit_dialog(i)
                                                        ).props("flat round text-color=blue size=sm")
                                                        ui.button(
                                                            icon="delete", on_click=lambda i=item: remove_item(i)
                                                        ).props("flat round text-color=red size=sm")

                                            # 条目 Body (双列表现)
                                            with ui.column().classes("w-full p-3 gap-1"):
                                                if item["type"] == "overview_update":
                                                    ui.label(
                                                        f"【{item['project']} - {item['role']} - {item['label']}】"
                                                    ).classes("text-xs font-bold text-blue-900")
                                                    with ui.row().classes("w-full items-center gap-2"):
                                                        ui.label(item.get("old_data", {}).get("content", "")).classes(
                                                            "text-sm text-gray-500 line-through bg-gray-50 p-1 rounded break-all"
                                                        )
                                                        ui.icon("arrow_forward", color="gray")
                                                        ui.label(item.get("new_data", {}).get("content", "")).classes(
                                                            "text-sm font-bold text-green-700 bg-green-50 p-1 rounded break-all"
                                                        )
                                                else:
                                                    ui.label(f"分类: {item['change_type']}").classes(
                                                        "text-xs font-bold text-teal-900"
                                                    )
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

                            # 异步刷新定时器
                            async def sync_schemes():
                                if local_data["workflow"]["current_state"] == ECNState.ECN_SCHEMING and ecn_id:
                                    fresh_data = db_storage.get_deep_item(["ecn_management_data", ecn_id])
                                    if fresh_data:
                                        fresh_items = fresh_data.get("change_items", [])
                                        fresh_parts = fresh_data["workflow"].get("scheme_participants", {})
                                        if str(fresh_items) != str(local_data["change_items"]) or fresh_parts != parts:
                                            local_data["change_items"] = copy.deepcopy(fresh_items)
                                            parts.clear()
                                            parts.update(copy.deepcopy(fresh_parts))
                                            render_parts()
                                            render_my_actions()
                                            render_items()

                            sync_timer = ui.timer(3.0, sync_schemes)
                            dialog.on("close", sync_timer.cancel)

                # ------ Tab 3: 审批流转记录 ------
                with ui.tab_panel(tab_workflow).classes("gap-4"):
                    if is_new:
                        ui.label("暂无审批记录，请先发起申请。").classes("text-gray-500 mt-4 text-center w-full")
                    else:
                        with ui.column().classes("w-full"):
                            # 待办预示区
                            if wf["pending_roles"]:
                                pending_list = [r for r in wf["pending_roles"] if not wf["step_approvals"].get(r)]
                                approved_list = [r for r in wf["pending_roles"] if wf["step_approvals"].get(r)]
                                with ui.card().classes("w-full bg-blue-50/50 shadow-sm mb-4 border border-blue-100"):
                                    if pending_list:
                                        ui.label(f"▶ 当前节点等待审批: {', '.join(pending_list)}").classes(
                                            "text-orange-600 font-bold text-sm"
                                        )
                                    if approved_list:
                                        ui.label(f"▶ 当前节点已同意: {', '.join(approved_list)}").classes(
                                            "text-green-600 text-sm mt-1"
                                        )

                            # 历史记录树
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

            # Footer / Action Area
            with ui.row().classes(
                "w-full bg-white p-3 border-t border-gray-200 justify-end items-center shrink-0 gap-3 shadow-[0_-2px_10px_rgba(0,0,0,0.05)]"
            ):
                if is_draft_or_reject:
                    if local_data["basic_info"]["applicant"] == current_user or is_new:
                        ui.button("保存为草稿", on_click=lambda: save_ecn(local_data, action="save_draft")).props(
                            "outline color=grey-7"
                        )
                        ui.button(
                            "发起/重新发起 ECR", on_click=lambda: save_ecn(local_data, action="submit_ecr")
                        ).props("color=primary")
                else:
                    is_pending_user = current_role in wf["pending_roles"]

                    if wf["current_state"] == ECNState.PENDING_FINAL_EXECUTE and "研发经理" in current_role:
                        ui.button(
                            "驳回至方案阶段", color="red", on_click=lambda: process_approval(local_data, "驳回", "")
                        ).props("outline")
                        ui.button(
                            "确认各部已就绪，立刻执行数据变更",
                            icon="warning",
                            on_click=lambda: execute_ecn_data(local_data),
                        ).props("color=red")

                    elif wf["current_state"] == ECNState.ECN_SCHEMING and any(
                        r in current_role for r in ECN_SCHEME_INITIATOR_ROLES
                    ):
                        all_confirmed = len(wf.get("scheme_participants", {})) > 0 and all(
                            s == "confirmed" for s in wf.get("scheme_participants", {}).values()
                        )
                        btn = ui.button("发起 ECN 方案评审", on_click=lambda: initiate_scheme_review(local_data)).props(
                            f"color=purple {'disabled' if not all_confirmed else ''}"
                        )
                        if not all_confirmed:
                            btn.tooltip("需要所有提供方案的人员点击'确认完成'后方可发起")

                    elif is_pending_user and wf["current_state"] not in [
                        ECNState.CLOSED,
                        ECNState.REJECTED,
                        ECNState.ECN_SCHEMING,
                    ]:
                        note_input = ui.input("审批意见 (选填)").props("dense outlined").classes("w-64")
                        ui.button(
                            "驳回", color="red", on_click=lambda: process_approval(local_data, "驳回", note_input.value)
                        ).props("outline")
                        ui.button(
                            "同意",
                            color="green",
                            on_click=lambda: process_approval(local_data, "同意", note_input.value),
                        )

        # --- 数据操作逻辑 ---
        async def save_ecn(data_to_save, action):
            if not data_to_save["basic_info"]["title"].strip():
                return ui.notify("请填写变更单标题", type="warning")
            if not data_to_save["target_projects"]:
                return ui.notify("至少添加一个受影响项目", type="warning")
            if not data_to_save["basic_info"]["requirements"]:
                return ui.notify("请至少填写一条变更要求", type="warning")

            if action == "submit_ecr":
                data_to_save["workflow"]["current_state"] = ECNState.ECR_REVIEWING
                data_to_save["workflow"]["current_phase"] = "ECR_PHASE"
                data_to_save["workflow"]["route_type"] = "SALES_INITIATED" if "销售" in current_role else "RD_INITIATED"
                data_to_save["workflow"]["current_step_index"] = 0
                data_to_save["workflow"]["pending_roles"] = ECN_WORKFLOW_ROUTES["ECR_PHASE"][
                    data_to_save["workflow"]["route_type"]
                ][0]
                data_to_save["workflow"]["step_approvals"] = {}
                data_to_save["approval_log"].append(
                    {
                        "user": current_user,
                        "role": current_role,
                        "action": "发起申请",
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

            await db_storage.set_deep_item(["ecn_management_data", data_to_save["ecn_id"]], data_to_save)
            ui.notify("操作成功！", type="positive")
            dialog.close()
            refresh_list()

        async def initiate_scheme_review(data_to_save):
            wf = data_to_save["workflow"]
            wf["current_state"] = ECNState.ECN_REVIEWING
            wf["current_phase"] = "ECN_SCHEME_REVIEW_PHASE"
            wf["current_step_index"] = 0
            wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_SCHEME_REVIEW_PHASE"][0]
            data_to_save["approval_log"].append(
                {
                    "user": current_user,
                    "role": current_role,
                    "action": "发起方案评审",
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            await db_storage.set_deep_item(["ecn_management_data", data_to_save["ecn_id"]], data_to_save)
            ui.notify("已进入方案评审环节", type="positive")
            dialog.close()
            refresh_list()

        async def process_approval(data_to_save, action, note):
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data_to_save["approval_log"].append(
                {"user": current_user, "role": current_role, "action": action, "note": note, "time": now_str}
            )
            wf = data_to_save["workflow"]

            if action == "驳回":
                if wf["current_phase"] == "ECR_PHASE":
                    wf["current_state"] = ECNState.REJECTED
                    wf["pending_roles"] = []
                else:
                    wf["current_phase"] = "ECN_SCHEME_PHASE"
                    wf["current_state"] = ECNState.ECN_SCHEMING
                    wf["pending_roles"] = []
                    for u in wf.setdefault("scheme_participants", {}):
                        wf["scheme_participants"][u] = "editing"
            else:
                wf["step_approvals"][current_role] = True
                if all(wf["step_approvals"].get(role, False) for role in wf["pending_roles"]):
                    wf["current_step_index"] += 1
                    wf["step_approvals"] = {}

                    route_array = (
                        ECN_WORKFLOW_ROUTES[wf["current_phase"]][wf["route_type"]]
                        if wf["current_phase"] == "ECR_PHASE"
                        else ECN_WORKFLOW_ROUTES[wf["current_phase"]]
                    )

                    if wf["current_step_index"] >= len(route_array):
                        if wf["current_phase"] == "ECR_PHASE":
                            wf["current_phase"] = "ECN_SCHEME_PHASE"
                            wf["current_state"] = ECNState.ECN_SCHEMING
                            wf["pending_roles"] = []
                        elif wf["current_phase"] == "ECN_SCHEME_REVIEW_PHASE":
                            wf["current_phase"] = "ECN_EXECUTION_PHASE"
                            wf["current_state"] = ECNState.ECN_EXECUTING
                            wf["current_step_index"] = 0
                            wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_EXECUTION_PHASE"][0]
                    else:
                        next_roles = route_array[wf["current_step_index"]]
                        wf["pending_roles"] = next_roles
                        if "研发经理_EXECUTE" in next_roles:
                            wf["current_state"] = ECNState.PENDING_FINAL_EXECUTE

            await db_storage.set_deep_item(["ecn_management_data", data_to_save["ecn_id"]], data_to_save)
            ui.notify(f"已{action}", type="positive")
            dialog.close()
            refresh_list()

        # 核心修复：更新字典执行逻辑对齐底层模型
        async def execute_ecn_data(data_to_save):
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                for item in data_to_save["change_items"]:
                    if item["type"] == "overview_update":
                        path = [f"{item['project']}_over_data", item["label"], item["chip_id"]]
                        target_chip = db_storage.get_deep_item(path)
                        if target_chip:
                            # 提取修改并覆盖底层芯片数据
                            new_data = item.get("new_data", {})
                            keys_to_update = ["content", "notes", "test_select_data", "file_type", "url_path"]
                            target_chip.update({k: v for k, v in new_data.items() if k in keys_to_update})

                            target_chip.setdefault("timestamp", {})[now_str] = {
                                "creator": f"ECN自动执行 ({data_to_save['ecn_id']})",
                                "select_activ_dic": copy.deepcopy(target_chip.get("select_activ_dic", {})),
                            }
                            await db_storage.set_deep_item(path, target_chip)
                            item["execute_status"] = "success"

                data_to_save["workflow"]["current_state"] = ECNState.CLOSED
                data_to_save["workflow"]["pending_roles"] = []
                data_to_save["approval_log"].append(
                    {"user": current_user, "role": current_role, "action": "执行变更", "time": now_str}
                )
                await db_storage.set_deep_item(["ecn_management_data", data_to_save["ecn_id"]], data_to_save)

                ui.notify("系统数据已成功修改，ECN归档完毕！", type="positive", color="red")
                dialog.close()
                refresh_list()
            except Exception as e:
                logger.error(f"执行ECN变更失败: {e}", exc_info=True)
                ui.notify(f"执行失败: {e}", type="negative")

        dialog.open()

    # ==========================================
    # 主页面 UI (总览列表)
    # ==========================================
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("工程变更管理 (ECN)").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {current_user}")
                ui.separator()
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    with ui.column().classes("w-full p-4 h-[calc(100vh-4rem)] bg-gray-50"):
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
                        ECNState.REJECTED,
                    ],
                    label="状态筛选",
                ).props("dense outlined").bind_value(page_state, "filter_state").classes("w-40")
                ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("color=primary outline")

            ui.button("新建 ECR 申请", icon="add_box", on_click=lambda: open_ecn_detail_dialog()).props("color=red-7")

        list_container = ui.column().classes("w-full mt-4 gap-2 flex-grow overflow-y-auto")

        def refresh_list():
            list_container.clear()
            all_ecns = db_storage.get_item("ecn_management_data", {})
            kw = page_state["search_keyword"].lower()
            f_state = page_state["filter_state"]

            sorted_ecns = sorted(all_ecns.values(), key=lambda x: x["basic_info"]["apply_date"], reverse=True)

            with list_container:
                if not sorted_ecns:
                    return ui.label("暂无工程变更记录").classes("text-gray-500 m-auto mt-10")

                for ecn in sorted_ecns:
                    if (
                        kw
                        and kw not in ecn["ecn_id"].lower()
                        and kw not in ecn["basic_info"]["title"].lower()
                        and kw not in ecn["basic_info"]["applicant"].lower()
                    ):
                        continue
                    if f_state != "全部" and ecn["workflow"]["current_state"] != f_state:
                        continue

                    with ui.card().classes(
                        "w-full flex flex-row justify-between items-center p-3 hover:bg-blue-50 transition-colors cursor-pointer border-l-4 border-blue-500"
                    ) as card:
                        card.on("click", lambda _, e_id=ecn["ecn_id"]: open_ecn_detail_dialog(e_id))

                        with ui.column().classes("gap-1"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(ecn["ecn_id"]).classes("font-mono font-bold text-gray-800")
                                ui.badge(
                                    ecn["workflow"]["current_state"],
                                    color="red"
                                    if ecn["workflow"]["current_state"] == ECNState.REJECTED
                                    else "orange"
                                    if "中" in ecn["workflow"]["current_state"]
                                    else "green"
                                    if "完成" in ecn["workflow"]["current_state"]
                                    else "grey",
                                )
                            ui.label(ecn["basic_info"]["title"]).classes("text-lg font-bold text-blue-900")
                            ui.label(f"涉及项目: {', '.join(ecn['target_projects'])}").classes("text-xs text-gray-500")

                        with ui.column().classes("items-end gap-1"):
                            ui.label(f"申请人: {ecn['basic_info']['applicant']}").classes("text-sm text-gray-600")
                            ui.label(ecn["basic_info"]["apply_date"]).classes("text-xs text-gray-400 font-mono")

                            is_pending = current_role in ecn["workflow"]["pending_roles"]
                            is_scheming = (
                                ecn["workflow"]["current_state"] == ECNState.ECN_SCHEMING
                                and any(r in current_role for r in ECN_SCHEME_WRITER_ROLES)
                                and ecn["workflow"].get("scheme_participants", {}).get(current_user) != "confirmed"
                            )
                            if is_pending or is_scheming:
                                ui.chip("待您处理", icon="notifications_active", color="red").props(
                                    "dense outline size=sm"
                                )

        refresh_list()
