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
    # 弹窗 1：概述变更项添加
    # ==========================================
    def open_add_overview_change_dialog(ecn_data, on_save_callback):
        dialog = ui.dialog().props("persistent")
        sel_state = {
            "project": None,
            "role": None,
            "label": None,
            "chip_id": None,
            "old_content": "",
            "new_content": "",
            "req_idxs": [],
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

        def get_chips(p, l):
            return {
                c_id: c.get("content", "")[:30] + "..."
                for c_id, c in db_storage.get_deep_item([f"{p}_over_data", l], {}).items()
                if c.get("enabled")
            }

        with dialog, ui.card().classes("w-[600px] max-w-full flex flex-col"):
            ui.label("添加概述数据变更方案").classes("text-lg font-bold text-blue-900 shrink-0")

            with ui.column().classes("w-full gap-2 flex-1 min-h-0 overflow-y-auto"):
                ui.select(options=req_options, multiple=True, label="关联解决的要求序号 (支持多选)").classes(
                    "w-full"
                ).bind_value(sel_state, "req_idxs")
                with ui.grid(columns=2).classes("w-full gap-2"):
                    sel_proj = ui.select(
                        options=target_projects,
                        label="1. 目标项目",
                        on_change=lambda e: [
                            sel_state.update(project=e.value),
                            sel_chip.set_options(get_chips(e.value, sel_state["label"])),
                            sel_chip.set_value(None),
                        ],
                    ).classes("w-full")
                    sel_role = ui.select(
                        options=roles,
                        label="2. 技术维度",
                        on_change=lambda e: [
                            sel_state.update(role=e.value),
                            sel_label.set_options(get_labels(e.value)),
                            sel_label.set_value(None),
                        ],
                    ).classes("w-full")
                    sel_label = ui.select(
                        options={},
                        label="3. 具体参数",
                        on_change=lambda e: [
                            sel_state.update(label=e.value),
                            sel_chip.set_options(get_chips(sel_state["project"], e.value)),
                            sel_chip.set_value(None),
                        ],
                    ).classes("w-full")
                    sel_chip = ui.select(options={}, label="4. 原数据").classes("w-full")

                with ui.card().classes("w-full bg-gray-50 shadow-inner mt-2 shrink-0"):
                    ui.label("原内容：").classes("text-xs text-gray-500 font-bold")
                    old_text_ui = ui.label("请先选择上方数据...").classes("text-sm text-gray-700 mb-2 break-all")
                    new_text_ui = (
                        ui.textarea(label="变更后的新内容 (必填)").classes("w-full").props("outlined auto-grow")
                    )

            def on_chip_change(e):
                sel_state["chip_id"] = e.value
                if all([sel_state["project"], sel_state["label"], sel_state["chip_id"]]):
                    sel_state["old_content"] = db_storage.get_deep_item(
                        [f"{sel_state['project']}_over_data", sel_state["label"], sel_state["chip_id"]], {}
                    ).get("content", "")
                    old_text_ui.set_text(sel_state["old_content"])

            sel_chip.on_value_change(on_chip_change)

            def save_item():
                if (
                    not all([sel_state["project"], sel_state["label"], sel_state["chip_id"]])
                    or not new_text_ui.value.strip()
                ):
                    ui.notify("请完善必填信息", type="warning")
                    return
                on_save_callback(
                    {
                        "item_id": str(uuid.uuid4()),
                        "type": "overview_update",
                        "author": current_user,
                        "req_idxs": sel_state["req_idxs"],
                        "project": sel_state["project"],
                        "role": sel_state["role"],
                        "label": sel_state["label"],
                        "chip_id": sel_state["chip_id"],
                        "old_content": sel_state["old_content"],
                        "new_content": new_text_ui.value.strip(),
                        "execute_status": "pending",
                    }
                )
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4 shrink-0"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认添加", on_click=save_item).props("color=primary")
        dialog.open()

    # ==========================================
    # 弹窗 2：其他文本描述方案添加
    # ==========================================
    def open_add_text_change_dialog(ecn_data, on_save_callback):
        dialog = ui.dialog().props("persistent")
        sel_state = {"req_idxs": [], "change_type": "物料变更", "content": ""}
        req_options = {
            req["idx"]: f"[{req['idx']}] {req['content'][:15]}..." for req in ecn_data["basic_info"]["requirements"]
        }

        with dialog, ui.card().classes("w-[500px]"):
            ui.label("添加补充说明方案 (物料/图纸/工艺等)").classes("text-lg font-bold text-blue-900")
            ui.select(options=req_options, multiple=True, label="关联解决的要求序号").classes("w-full").bind_value(
                sel_state, "req_idxs"
            )
            ui.select(["物料变更", "图纸更新", "工艺调整", "SOP修改", "其它"], label="方案分类").classes(
                "w-full"
            ).bind_value(sel_state, "change_type")
            content_ui = ui.textarea(label="方案详细描述 (必填)").classes("w-full").props("outlined auto-grow rows=4")

            def save_item():
                if not content_ui.value.strip():
                    ui.notify("描述不能为空", type="warning")
                    return
                on_save_callback(
                    {
                        "item_id": str(uuid.uuid4()),
                        "type": "text_desc",
                        "author": current_user,
                        "req_idxs": sel_state["req_idxs"],
                        "change_type": sel_state["change_type"],
                        "content": content_ui.value.strip(),
                        "execute_status": "manual_record",
                    }
                )
                dialog.close()

            with ui.row().classes("w-full justify-end mt-2"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认添加", on_click=save_item).props("color=primary")
        dialog.open()

    # ==========================================
    # 弹窗 3：ECN 详情与操作主控台
    # ==========================================
    async def open_ecn_detail_dialog(ecn_id=None):
        dialog = ui.dialog().classes("w-[1000px] max-w-[95vw]")
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
                                    # 实时静默保存个人状态，防止丢失
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
                                        on_click=lambda: open_add_overview_change_dialog(local_data, handle_add_item),
                                    ).props("color=primary outline")
                                    ui.button(
                                        "添加文本描述方案 (物料/工艺等)",
                                        icon="text_snippet",
                                        on_click=lambda: open_add_text_change_dialog(local_data, handle_add_item),
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

                                                # 删除权限：草稿期 作者自己可以删 (前提是自己还没点确认)
                                                can_del = (
                                                    is_scheming_phase
                                                    and item["author"] == current_user
                                                    and parts.get(current_user) != "confirmed"
                                                )
                                                if can_del:
                                                    ui.button(
                                                        icon="delete", on_click=lambda i=item: remove_item(i)
                                                    ).props("flat round text-color=red size=sm").classes(
                                                        "absolute top-1 right-1"
                                                    )

                                            # 条目 Body
                                            with ui.column().classes("w-full p-3 gap-1"):
                                                if item["type"] == "overview_update":
                                                    ui.label(
                                                        f"【{item['project']} - {item['role']} - {item['label']}】"
                                                    ).classes("text-xs font-bold text-blue-900")
                                                    with ui.row().classes("w-full items-center gap-2"):
                                                        ui.label(item["old_content"]).classes(
                                                            "text-sm text-gray-500 line-through bg-gray-50 p-1 rounded"
                                                        )
                                                        ui.icon("arrow_forward", color="gray")
                                                        ui.label(item["new_content"]).classes(
                                                            "text-sm font-bold text-green-700 bg-green-50 p-1 rounded"
                                                        )
                                                else:
                                                    ui.label(f"分类: {item['change_type']}").classes(
                                                        "text-xs font-bold text-teal-900"
                                                    )
                                                    ui.label(item["content"]).classes(
                                                        "text-sm text-gray-800 whitespace-pre-wrap bg-gray-50 p-2 rounded w-full border border-dashed"
                                                    )

                            async def handle_add_item(new_item):
                                local_data["change_items"].append(new_item)
                                # 只要添加了方案，自动加入参与者池并标记为 editing
                                parts[current_user] = "editing"
                                # 同步到数据库确保协作不丢失
                                await db_storage.set_deep_item(
                                    ["ecn_management_data", local_data["ecn_id"], "workflow", "scheme_participants"],
                                    parts,
                                )
                                await db_storage.set_deep_item(
                                    ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                    local_data["change_items"],
                                )
                                render_parts()
                                render_my_actions()
                                render_items()

                            async def remove_item(item_to_remove):
                                local_data["change_items"].remove(item_to_remove)
                                await db_storage.set_deep_item(
                                    ["ecn_management_data", local_data["ecn_id"], "change_items"],
                                    local_data["change_items"],
                                )
                                render_items()

                            render_items()

                            # 异步刷新定时器 (仅在编辑阶段拉取别人的更新)
                            async def sync_schemes():
                                if local_data["workflow"]["current_state"] == ECNState.ECN_SCHEMING and ecn_id:
                                    fresh_data = db_storage.get_deep_item(["ecn_management_data", ecn_id])
                                    if fresh_data:
                                        # 仅同步方案项和参与者状态，不覆盖当前用户可能在 Tab1 填的东西
                                        fresh_items = fresh_data.get("change_items", [])
                                        fresh_parts = fresh_data["workflow"].get("scheme_participants", {})
                                        # 简单比对长度或内容判断是否需刷新 UI
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

                    # 场景 1：终极执行 (研发经理最后一步)
                    if wf["current_state"] == ECNState.PENDING_FINAL_EXECUTE and "研发经理" in current_role:
                        ui.button(
                            "驳回至方案阶段", color="red", on_click=lambda: process_approval(local_data, "驳回", "")
                        ).props("outline")
                        ui.button(
                            "确认各部已就绪，立刻执行数据变更",
                            icon="warning",
                            on_click=lambda: execute_ecn_data(local_data),
                        ).props("color=red")

                    # 场景 2：发起方案评审 (由总控人员在大家都 Confirm 后发起)
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

                    # 场景 3：常规并行审批
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
                # 异常流：降级与重开
                if wf["current_phase"] == "ECR_PHASE":
                    wf["current_state"] = ECNState.REJECTED
                    wf["pending_roles"] = []
                else:
                    # 方案评审或执行被驳回，打回方案编写阶段
                    wf["current_phase"] = "ECN_SCHEME_PHASE"  # 虚拟的编辑态
                    wf["current_state"] = ECNState.ECN_SCHEMING
                    wf["pending_roles"] = []
                    # 强制所有人重新确认
                    for u in wf.setdefault("scheme_participants", {}):
                        wf["scheme_participants"][u] = "editing"
            else:
                # 正常流：节点推进
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
                            wf["pending_roles"] = []  # 等待总控点击发起评审
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

        async def execute_ecn_data(data_to_save):
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                for item in data_to_save["change_items"]:
                    if item["type"] == "overview_update":
                        path = [f"{item['project']}_over_data", item["label"], item["chip_id"]]
                        target_chip = db_storage.get_deep_item(path)
                        if target_chip:
                            target_chip["content"] = item["new_content"]
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

            if current_role in ["销售", "销售总监", "研发", "研发经理"]:
                ui.button("新建 ECR 申请", icon="add_box", on_click=lambda: open_ecn_detail_dialog()).props(
                    "color=red-7"
                )

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
