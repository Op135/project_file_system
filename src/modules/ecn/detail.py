# -*- encoding: utf-8 -*-
import asyncio
import copy
import uuid
from datetime import (
    datetime,
)

from nicegui import (
    app,
    ui,
)
from nicegui.client import Client

from ... import (
    db_storage,
)
from ...custom_ui import custom_upload
from ...config import (
    ECN_ALLOWED_PROJECT_STATES,
    ECN_SCHEMA_CONFIG,
    ECNState,
)
from ...ecn_access import (
    build_ecn_access_snapshot,
    can_approve_ecn_validation_report,
    can_classify_ecn_level_before_scheme_review,
    can_classify_ecn_level_during_ecr,
    can_create_ecn_request,
    can_edit_ecn_impact,
    can_edit_ecn_material_codes,
    can_edit_ecn_scheme,
    can_execute_ecn_assistant_stage,
    can_reassign_ecn_approval,
    can_submit_ecn_scheme_review,
    can_designate_ecn_validation_report,
    can_view_ecn_validation_report,
    can_verify_ecn_execution,
    can_view_ecn,
)
from ...ecn_management_config import (
    ECN_ATTACHMENT_CONFIG,
    ECN_VERSION_KEY,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_REQUIRE_REJECTED_ITEM_SELECTION,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_SCHEME_GROUP_UNKNOWN,
    ECN_LEVEL_LABELS,
    get_ecn_level_code,
    get_ecn_level_label,
    classify_ecn_change_item,
    get_ecn_material_change_display,
    get_ecn_pending_approval_roles,
    is_ecn_scheme_ready_for_review,
)
from ...ecn_workflow import (
    ECN_ECR_ASSIGNMENT_KEY,
    ECN_SCHEME_ASSIGNMENT_KEY,
    is_ecr_assigned_approver,
    is_scheme_assigned_approver,
)
from .actions import (
    execute_action,
    save_review,
    set_ecn_level,
)
from .editing import (
    sync_review_snapshot,
)
from .execution_panel import (
    build_execution_panel,
)
from .models import (
    generate_initial_ecn_data,
)
from .scheme_panel import (
    build_scheme_panel,
)
from .approval_reassignment_ui import open_approval_reassignment_dialog
from .task_labels import humanize_ecn_log_action
from .attachment_preview import attachment_kind, issue_staged_attachment_preview_url
from .attachment_ui import render_ecn_ecr_attachments, open_staged_ecn_attachment_file
from .attachments import cleanup_staged, stage_upload


def sync_detail_scheme_snapshot(local_data: dict, participants: dict, fresh: dict) -> bool:
    """同步详情页方案与参与人快照，返回方案区域是否发生变化。"""
    fresh_items = fresh.get("change_items", [])
    fresh_items = fresh_items if isinstance(fresh_items, list) else []
    fresh_workflow = fresh.get("workflow", {})
    fresh_workflow = fresh_workflow if isinstance(fresh_workflow, dict) else {}
    fresh_participants = fresh_workflow.get("scheme_participants", {})
    fresh_participants = fresh_participants if isinstance(fresh_participants, dict) else {}
    changed = fresh_items != local_data.get("change_items", []) or fresh_participants != participants
    if not changed:
        return False
    local_data["change_items"] = copy.deepcopy(fresh_items)
    participants.clear()
    participants.update(copy.deepcopy(fresh_participants))
    return True


async def open_ecn_detail_dialog(ecn_id=None, *, current_user, current_role, refresh_list):
    access_snapshot = build_ecn_access_snapshot()
    if not can_view_ecn(current_role, current_user, access_snapshot=access_snapshot):
        ui.notify("当前用户没有查看ECN工程变更的权限", type="warning")
        return
    root_dialog = ui.dialog().props("maximized persistent")
    can_create_request = can_create_ecn_request(current_role, current_user, access_snapshot=access_snapshot)
    can_edit_impact = can_edit_ecn_impact(current_role, current_user, access_snapshot=access_snapshot)
    can_edit_scheme = can_edit_ecn_scheme(current_role, current_user, access_snapshot=access_snapshot)
    can_edit_material_codes = can_edit_ecn_material_codes(
        current_role,
        current_user,
        access_snapshot=access_snapshot,
    )
    can_submit_scheme_review = can_submit_ecn_scheme_review(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_execute_assistant = can_execute_ecn_assistant_stage(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_reassign_approval = can_reassign_ecn_approval(
        current_role,
        current_user,
        access_snapshot=access_snapshot,
    )
    can_classify_level_ecr = can_classify_ecn_level_during_ecr(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_classify_level_scheme = can_classify_ecn_level_before_scheme_review(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_designate_validation = can_designate_ecn_validation_report(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_view_validation = can_view_ecn_validation_report(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_approve_validation = can_approve_ecn_validation_report(
        current_role, current_user, access_snapshot=access_snapshot
    )
    can_review_execution = can_verify_ecn_execution(
        current_role, current_user, access_snapshot=access_snapshot
    )
    is_new = ecn_id is None
    if is_new and not can_create_request:
        return ui.notify("当前用户没有新建ECR申请的权限", type="warning")
    all_ecns = db_storage.get_item("ecn_management_data", {})

    # 数据结构为：{"RFFM":{"1519":{"RFFM-1519-A":"A"}}}
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

    if is_new:
        if not proj_dict_mass and not proj_dict_non:
            return ui.notify("当前没有可供变更的转产项目。", type="warning")
        ecn_data = generate_initial_ecn_data(
            current_user,
            current_role,
            all_ecns,
            user_service=getattr(app.state, "user_service", None),
        )
    else:
        ecn_data = all_ecns.get(ecn_id)
        if not isinstance(ecn_data, dict):
            ui.notify("单据已不存在，请刷新列表。", type="warning")
            return

    local_data = copy.deepcopy(ecn_data)
    detail_version_tracker = {
        "stamp": db_storage.get_item(ECN_VERSION_KEY, 0.0) if ecn_id else 0.0
    }
    form_baseline = copy.deepcopy(ecn_data)
    review_baseline = copy.deepcopy(ecn_data["review_info"])
    review_save_lock = asyncio.Lock()
    action_busy = {"value": False}

    wf = local_data["workflow"]
    basic = local_data["basic_info"]
    basic.setdefault("attachments", [])
    ecr_upload_busy = {"count": 0}
    ecr_dialog_closed = {"value": False}
    if is_new:
        def cleanup_new_ecr_uploads() -> None:
            ecr_dialog_closed["value"] = True
            cleanup_staged(basic.get("attachments", []))

        root_dialog.on("close", cleanup_new_ecr_uploads)
    review = local_data["review_info"]
    participants = wf.setdefault("scheme_participants", {})

    is_draft_or_reject = is_new or wf["current_state"] in [ECNState.DRAFT, ECNState.REJECTED]
    # 是否处于编写方案阶段
    is_scheming_phase = wf["current_state"] == ECNState.ECN_SCHEMING
    # 影响评估与方案编写是两个独立权限，避免为了填写方案而放开全部影响范围。
    is_impact_editor = is_scheming_phase and can_edit_impact
    is_scheme_writer = is_scheming_phase and can_edit_scheme
    level_code = get_ecn_level_code(local_data)
    level_chip_color = {
        "simple": "green-7",
        "general": "indigo-6",
        "complex": "deep-orange-7",
    }.get(level_code, "indigo-6")

    level_timing = (
        "ecr_review"
        if wf.get("current_state") == ECNState.ECR_REVIEWING
        and wf.get("current_phase") == "ECR_PHASE"
        and can_classify_level_ecr
        else "before_scheme_review"
        if is_scheming_phase and can_classify_level_scheme
        else ""
    )

    def open_level_dialog() -> None:
        if not level_timing:
            return
        level_dialog = ui.dialog().props("persistent")
        level_state = {"code": get_ecn_level_code(local_data)}
        with level_dialog, ui.card().classes("w-[500px] max-w-[94vw] p-5 gap-3"):
            ui.label("判定 ECN 等级").classes("text-lg font-bold text-blue-950")
            ui.label(
                "未判定的ECN按一般等级运行。简单等级将使用独立的精简审批流程；"
                "复杂等级在发起方案评审前还须完成指定方案的验证报告审批。"
            ).classes("text-sm text-slate-600 leading-relaxed")
            ui.radio(ECN_LEVEL_LABELS).bind_value(level_state, "code").props(
                "inline color=primary"
            ).classes("w-full")

            async def submit_level() -> None:
                result = await set_ecn_level(
                    str(local_data.get("ecn_id") or ""),
                    copy.deepcopy(local_data),
                    str(level_state["code"]),
                    level_timing,
                    current_user,
                    current_role,
                )
                if not result.ok or result.record is None:
                    ui.notify(result.message, type="warning", multi_line=True)
                    return
                level_dialog.close()
                root_dialog.close()
                refresh_list()
                ui.notify(
                    f"ECN等级已判定为：{get_ecn_level_label(result.record)}",
                    type="positive",
                )

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("取消", on_click=level_dialog.close).props("flat color=grey")
                ui.button("保存等级", icon="save", on_click=submit_level).props("color=primary")
        level_dialog.on("close", level_dialog.delete)
        level_dialog.open()

    # === 建立一个跨 Tab 刷新的引用桥梁 ===
    dashboard_updater = {"refresh": lambda: None}  # 初始值为一个空函数，后续会被覆盖为真正的刷新函数

    def record_impact_change(field, target, action, before, after):
        if not is_impact_editor:
            return
        review.setdefault("impact_change_log", []).append(
            {
                "event_id": str(uuid.uuid4()),
                "user": current_user,
                "role": current_role,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "field": field,
                "target": str(target),
                "action": action,
                "before": copy.deepcopy(before),
                "after": copy.deepcopy(after),
            }
        )

    async def auto_save_review(e=None):
        if not ecn_id or not is_impact_editor:
            return
        async with review_save_lock:
            submitted = copy.deepcopy(review)
            if submitted == review_baseline:
                return
            expected = copy.deepcopy(local_data)
            result = await save_review(
                ecn_id, expected, copy.deepcopy(review_baseline), submitted, current_user, current_role
            )
            if result.ok and result.record is not None:
                # 只承认这次提交的值；await 期间继续输入的内容仍保留为下一次增量。
                review_baseline.clear()
                review_baseline.update(copy.deepcopy(submitted))
                sync_review_snapshot(review, review_baseline, result.record["review_info"])
                local_data["workflow"]["impact_handlers"] = result.record["workflow"].get("impact_handlers", [])
            else:
                ui.notify(result.message, type="warning", multi_line=True)
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
            with ui.row().classes("items-center gap-2"):
                ui.chip(
                    wf["current_state"],
                    color="orange"
                    if "中" in wf["current_state"]
                    else "red"
                    if wf["current_state"] == ECNState.REJECTED
                    else "blue",
                ).props("outline size=base")
            with ui.column().classes("gap-0 items-center"):
                ui.label("工程变更单").classes("text-2xl font-black text-gray-800 tracking-widest")
                with ui.row().classes("items-center justify-center gap-2"):
                    ui.label(local_data["ecn_id"] or "新建 ECR（保存后生成编号）").classes(
                        "text-lg font-mono font-bold text-gray-700"
                    )
                    if not is_new:
                        ui.chip(
                            f"ECN等级：{get_ecn_level_label(local_data)}",
                            icon="speed",
                            color=level_chip_color,
                        ).props("dense square text-color=white").classes(
                            "font-bold shadow-sm"
                        )
            ui.button(icon="close", on_click=root_dialog.close).props("flat round dense").classes("ml-15")

        # ui.tabs: NiceGUI框架用于创建选项卡导航容器的类
        with ui.tabs().classes("w-full shrink-0 bg-white") as tabs:
            tab_ecr = ui.tab("1. ECR-申请", icon="assignment")
            tab_impact = ui.tab("2. ECN-影响", icon="fact_check")
            tab_scheme = ui.tab("3. ECN-方案", icon="design_services")
            tab_exec = ui.tab("4. ECN-执行", icon="assignment_turned_in")
            tab_workflow = ui.tab("审批记录", icon="timeline")

        open_scheme_review_tab = bool(
            wf.get("current_state") == ECNState.ECN_REVIEWING
            and wf.get("current_phase") == "ECN_SCHEME_REVIEW_PHASE"
            and is_scheme_assigned_approver(local_data, current_user)
        )
        initial_tab = tab_scheme if open_scheme_review_tab else tab_ecr

        # 当前用户是否为ECN申请人，且处于草稿或驳回待编辑状态
        is_ecr_editable = is_new or (
            basic.get("applicant") == current_user and wf.get("current_state") in [ECNState.DRAFT, ECNState.REJECTED]
        )

        with ui.tab_panels(tabs, value=initial_tab).classes("w-full flex-1 min-h-0 p-2 md:p-4"):
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
                        ui.input("文件编号", value=basic["file_no"] or "保存后生成").props(
                            "outlined dense readonly bg-gray-100"
                        ).classes("w-full")

                    with ui.row().classes("w-full p-2 pdf-border-b items-center gap-2 hover:bg-gray-50"):
                        ui.label("变更性质:").classes("font-bold text-gray-700 w-20 shrink-0")
                        with ui.row().classes("gap-6 items-center flex-1"):
                            ui.radio(ECN_SCHEMA_CONFIG["change_natures"]).bind_value(basic, "nature").props(
                                f"inline {'disable' if not is_ecr_editable else ''}"
                            )
                            if (
                                len(ECN_SCHEMA_CONFIG["change_natures"]) > 1
                                and basic.get("nature") == ECN_SCHEMA_CONFIG["change_natures"][1]
                            ):
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
                            # ECR可编辑时，才显示项目选择选框
                            if is_ecr_editable:
                                proj_sel_state = {"l1": None, "l2": None, "l3": None}
                                with ui.row().classes("w-full items-center gap-2"):
                                    (
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

                                    # 添加目标项目为ECN变更对象，更新目标项目chip行，并记录到字典里
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

                            # 显示ECN申请时选定的目标项目chip
                            def render_proj_chips():
                                proj_chip_container.clear()
                                with proj_chip_container:
                                    if not local_data["target_projects"]:
                                        ui.label("尚未添加变更对象 (项目)").classes("text-xs text-red-400 italic mt-1")
                                    # 如果有目标项目，生成它们的chip，并在可编辑状态下添加删除功能，删除后会重新调用自己，进行刷新
                                    for p in local_data["target_projects"]:
                                        with ui.chip(color="primary", text_color="white").classes("gap-1 items-center"):
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

                            # 初始化显示ECN目标项目chip
                            render_proj_chips()

                    with ui.row().classes("w-full p-2 pdf-border-b items-start gap-2 hover:bg-gray-50"):
                        ui.label("变更要求:").classes("font-bold text-gray-700 w-20 shrink-0 pt-1")
                        with ui.column().classes("flex-1 gap-2"):
                            # 只有ECR处于可编辑状态下，才显示要求输入框
                            if is_ecr_editable:
                                with ui.row().classes("w-full gap-2 mb-2 items-center"):
                                    req_input = (
                                        ui.input("输入具体的变更要求", placeholder="单行输入，不用加序号。")
                                        .props(f"dense outlined bg-white {'readonly' if not is_ecr_editable else ''}")
                                        .classes("flex-grow")
                                    )

                                    # 添加变更要求用户填写内容chip，记录到字典里，刷新chip标签显示
                                    def add_req():
                                        val = req_input.value
                                        if val and val.strip():
                                            local_data["basic_info"]["requirements"].append(
                                                {
                                                    "idx": len(local_data["basic_info"]["requirements"]) + 1,
                                                    "content": val.strip(),
                                                }
                                            )
                                            req_input.set_value("")  # 清空输入框
                                            render_reqs()  # 刷新显示变更要求的chip列表
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
                                            ui.label(req["content"]).classes("text-sm text-gray-800 flex-1 break-all")
                                            if is_ecr_editable:
                                                ui.icon("close", size="sm").classes(
                                                    "cursor-pointer text-red-500 opacity-0 group-hover:opacity-100 transition-opacity"
                                                ).on(
                                                    "click",
                                                    lambda e, r=req: [
                                                        local_data["basic_info"]["requirements"].remove(r),
                                                        # 删除要求后，重新根据顺序更新索引编号
                                                        # 如果以后ECR评审后可回退重新编辑，则这里有问题，需要固定不更新
                                                        [
                                                            req.update(idx=i + 1)
                                                            for i, req in enumerate(
                                                                local_data["basic_info"]["requirements"]
                                                            )
                                                        ],
                                                        render_reqs(),  # 调用自己刷新显示
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

                    with ui.row().classes("w-full p-2 items-start gap-2 pdf-border-b") as ecr_attachment_row:
                        ui.label("申请附件:").classes("font-bold text-gray-700 w-20 shrink-0 pt-1")
                        with ui.column().classes("flex-1 min-w-0 gap-1"):
                            if is_new:
                                pending_layout = ui.element("div").classes(
                                    "w-full grid grid-cols-1 md:grid-cols-2 gap-5 items-start"
                                )
                                with pending_layout, ui.column().classes("w-full min-w-0 gap-2"):
                                    ui.label("待保存附件（点击文件名预览或下载）").classes(
                                        "text-xs font-semibold text-slate-600"
                                    )
                                    pending_list = ui.column().classes("w-full gap-3 max-h-[45vh] overflow-y-auto")

                                def render_pending_files() -> None:
                                    pending_list.clear()
                                    with pending_list:
                                        if not basic["attachments"]:
                                            ui.label("暂无附件").classes("text-xs text-slate-400 py-2")
                                        for attachment in basic["attachments"]:
                                            with ui.row().classes(
                                                "w-full min-h-[56px] items-center gap-2 rounded-lg "
                                                "border border-slate-200 bg-white px-3 py-2"
                                            ):
                                                if attachment_kind(str(attachment.get("name") or "")) == "image":
                                                    ui.image(issue_staged_attachment_preview_url(
                                                        attachment, current_user, current_role,
                                                    )).classes("w-10 h-10 object-cover rounded cursor-pointer shrink-0").on(
                                                        "click", lambda _, entry=attachment: open_staged_ecn_attachment_file(
                                                            entry, current_user, current_role,
                                                        )
                                                    )
                                                else:
                                                    ui.icon("attach_file", size="xs").classes("text-slate-500")
                                                ui.label(f"{attachment.get('name', '附件')} · 待保存").classes(
                                                    "text-xs text-indigo-700 break-all cursor-pointer hover:underline"
                                                ).on("click", lambda _, entry=attachment: open_staged_ecn_attachment_file(
                                                    entry, current_user, current_role,
                                                ))

                                async def upload_new_ecr_file(event) -> None:
                                    ecr_upload_busy["count"] += 1
                                    try:
                                        staged = await stage_upload(event.file, current_user)
                                    except Exception as exc:
                                        ui.notify(f"附件上传失败：{exc}", type="negative")
                                        return
                                    finally:
                                        ecr_upload_busy["count"] -= 1
                                    if ecr_dialog_closed["value"]:
                                        cleanup_staged([staged])
                                        return
                                    basic["attachments"].append(staged)
                                    render_pending_files()
                                    ui.notify("附件已暂存，保存或提交 ECR 后归档", type="positive")

                                def remove_new_ecr_file(event) -> None:
                                    removed = []
                                    for file_info in event.files:
                                        match = next(
                                            (
                                                entry for entry in basic["attachments"]
                                                if entry not in removed
                                                and entry.get("name") == file_info.get("name")
                                                and (
                                                    file_info.get("size") is None
                                                    or entry.get("size") == file_info.get("size")
                                                )
                                            ),
                                            None,
                                        )
                                        if match is not None:
                                            removed.append(match)
                                    cleanup_staged(removed)
                                    basic["attachments"] = [entry for entry in basic["attachments"] if entry not in removed]
                                    render_pending_files()

                                with pending_layout, ui.column().classes("w-full min-w-0 gap-2"):
                                    ui.label("添加申请附件（可选）").classes(
                                        "text-xs font-semibold text-slate-600"
                                    )
                                    custom_upload(
                                        multiple=True,
                                        max_file_size=int(ECN_ATTACHMENT_CONFIG["max_file_size_mb"]) * 1024 * 1024,
                                        on_upload=upload_new_ecr_file,
                                        on_removed=remove_new_ecr_file,
                                    ).props("accept=*/*")
                                render_pending_files()
                            else:
                                render_ecn_ecr_attachments(
                                    str(ecn_id), current_user, current_role,
                                    can_upload=is_ecr_editable and basic["applicant"] == current_user,
                                )
                    if not is_ecr_editable and not basic.get("attachments"):
                        ecr_attachment_row.set_visibility(False)

            # --- [TAB 2] ECN 影响表单 ---
            with ui.tab_panel(tab_impact).classes(
                "gap-0 p-0 max-w-[1000px] mx-auto overflow-y-scroll overflow-x-hidden"
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
                                    ui.label("ECR申请涵盖项目:").classes("text-xs font-bold text-gray-500 w-36 pt-1")
                                    with ui.row().classes("gap-1"):
                                        for p in local_data["target_projects"]:
                                            ui.chip(p, color="grey", text_color="white").props("dense")
                                        if not local_data["target_projects"]:
                                            ui.label("无").classes("text-xs text-gray-400")

                                # 方案编写阶段，才显示扩大影响的选项，且只有方案编写者角色才有权限修改，任何变更都会自动保存评审信息
                                def render_expanded_proj(
                                    target_list,
                                    field_name,
                                    label_text,
                                    proj_dict_source,
                                    color="primary",
                                ):
                                    """
                                    target_list: 扩大影响选择的项目
                                    label_text：标签文本
                                    proj_dict_source：用于生成选项的项目数据源
                                    color： chip颜色
                                    """
                                    with ui.row().classes("items-start gap-2"):
                                        ui.label(label_text).classes("text-xs font-bold text-gray-500 w-36 pt-2")
                                        with ui.column().classes("gap-1"):
                                            # 处于方案编写阶段，才生成选择项目的扩大选框给用户用
                                            if is_scheming_phase:
                                                ps = {"l1": None, "l2": None, "l3": None}
                                                with ui.row().classes("items-center gap-2"):
                                                    (
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
                                                        ui.select(options={}, on_change=lambda e: ps.update(l3=e.value))
                                                        .props("dense outlined bg-white")
                                                        .classes("w-32")
                                                    )

                                                    def add_exp_proj():
                                                        if (
                                                            ps["l3"]
                                                            and ps["l3"]
                                                            not in target_list  # select如果传入的时字典，则字典value是显示文本，key才是选项返回值
                                                            and ps["l3"] not in local_data["target_projects"]
                                                        ):
                                                            target_list.append(ps["l3"])
                                                            record_impact_change(
                                                                field_name,
                                                                ps["l3"],
                                                                "add",
                                                                False,
                                                                True,
                                                            )
                                                            render_chips()
                                                            # 方案编写阶段，任何扩大影响的变更都需要自动保存评审信息，确保数据一致性和实时更新看板监控
                                                            if is_scheming_phase:
                                                                ui.timer(0.1, auto_save_review, once=True)
                                                        else:
                                                            ui.notify("未选择、已存在或已被ECR涵盖", type="warning")

                                                    ui.button(icon="add", on_click=add_exp_proj).props(
                                                        f"outline dense {'disable' if not is_impact_editor else ''}"
                                                    ).classes("mt-0")

                                            chip_container = ui.row().classes("gap-1")

                                            def render_chips():
                                                chip_container.clear()
                                                with chip_container:
                                                    if not target_list:
                                                        ui.label("未扩大").classes("text-xs text-gray-400 mt-1")
                                                    for p in target_list:
                                                        with ui.chip(p, color=color, text_color="white").props("dense"):
                                                            if is_impact_editor:

                                                                def remove_expanded_project(
                                                                    _=None,
                                                                    project=p,
                                                                ):
                                                                    if project not in target_list:
                                                                        return
                                                                    target_list.remove(project)
                                                                    record_impact_change(
                                                                        field_name,
                                                                        project,
                                                                        "remove",
                                                                        True,
                                                                        False,
                                                                    )
                                                                    render_chips()
                                                                    ui.timer(
                                                                        0.1,
                                                                        auto_save_review,
                                                                        once=True,
                                                                    )

                                                                ui.icon("close", size="xs").classes(
                                                                    "cursor-pointer ml-1"
                                                                ).on(
                                                                    "click",
                                                                    remove_expanded_project,
                                                                )

                                            render_chips()

                                render_expanded_proj(
                                    review["expanded_projects_mass"],
                                    "expanded_projects_mass",
                                    "扩大影响 (试产/量产):",
                                    proj_dict_mass,
                                    color="blue",
                                )
                                render_expanded_proj(
                                    review["expanded_projects_non_mass"],
                                    "expanded_projects_non_mass",
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

                                    async def on_impact_change(e, impact_key=imp_key):
                                        selected = bool(e.value)
                                        record_impact_change(
                                            "impacts",
                                            impact_key,
                                            "check" if selected else "uncheck",
                                            not selected,
                                            selected,
                                        )
                                        await auto_save_review(e)

                                    ui.checkbox(imp_key).bind_value(review["impacts"], imp_key).props(
                                        f"{'disable' if not is_impact_editor else ''} dense"
                                    ).on_value_change(on_impact_change)

                        with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50"):
                            ui.label("变更涉及资料 (必出方案):").classes("font-bold text-gray-700")
                            with ui.grid().classes(
                                "w-full grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-2 gap-y-1 ml-4 items-center"
                            ):
                                # 动态读取配置遍历
                                for doc_key in ECN_SCHEMA_CONFIG["document_types"]:
                                    ui.checkbox(doc_key).bind_value(review["involved_docs"], doc_key).props(
                                        f"{'disable' if not is_impact_editor else ''} dense"
                                    ).on_value_change(auto_save_review)

                            # bind_visibility_from: 实现“其它”项仅在勾选后显示
                            ui.input("其它:").bind_value(review, "other_docs_desc").bind_visibility_from(
                                review["involved_docs"], "其它"
                            ).props(
                                f"outlined dense {'readonly bg-gray-100' if not is_impact_editor else 'bg-white'}"
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
                                                    f"{'disable' if not is_impact_editor else ''} dense"
                                                ).on_value_change(auto_save_review)

            # 方案和执行页控件最多，延后到用户首次打开相应页签时再创建，避免拖慢详情弹窗和其它点击。
            with ui.tab_panel(tab_scheme).classes("gap-0 p-0 w-full mx-auto overflow-y-scroll"):
                scheme_panel_host = ui.column().classes("w-full gap-0")
            with (
                ui.tab_panel(tab_exec)
                .props("id=ecn-execution-tab-panel")
                .classes("gap-4 p-2 mx-auto overflow-y-auto overflow-x-hidden")
            ):
                execution_panel_host = ui.column().classes("w-full gap-4")

            render_parts = render_my_actions = render_items = render_coverage_dashboard = lambda: None

            def render_execution_tab():
                return None

            material_task_controls = {}
            execution_container = execution_panel_host

            def refresh_material_execution_controls(item_ids: list[str] | None = None) -> None:
                del item_ids
                return None

            async def capture_execution_scroll_state(event_client: Client) -> dict[str, float]:
                del event_client
                return {}

            async def restore_execution_scroll_state(
                event_client: Client,
                scroll_state: dict[str, float],
            ) -> None:
                del event_client, scroll_state
                return None

            lazy_panels = {"scheme": False, "execution": False}
            lazy_panel_queued = {"scheme": False, "execution": False}

            def handle_material_code_saved() -> None:
                if lazy_panels["execution"]:
                    render_execution_tab()
                render_workflow_tab()
                refresh_list()

            def load_scheme_panel():
                nonlocal render_parts, render_my_actions, render_items, render_coverage_dashboard
                if lazy_panels["scheme"]:
                    return
                lazy_panel_queued["scheme"] = False
                lazy_panels["scheme"] = True
                scheme_panel_host.clear()
                (
                    render_parts,
                    render_my_actions,
                    render_items,
                    render_coverage_dashboard,
                ) = build_scheme_panel(
                    tab_scheme,
                    wf,
                    is_new,
                    local_data,
                    current_user,
                    current_role,
                    participants,
                    is_scheming_phase,
                    is_scheme_writer,
                    can_edit_material_codes,
                    can_designate_validation,
                    can_view_validation,
                    can_approve_validation,
                    handle_material_code_saved,
                    dashboard_updater,
                    panel_container=scheme_panel_host,
                )

            def load_execution_panel():
                nonlocal render_execution_tab, material_task_controls
                nonlocal capture_execution_scroll_state, execution_container
                nonlocal refresh_material_execution_controls, restore_execution_scroll_state
                if lazy_panels["execution"]:
                    return
                lazy_panel_queued["execution"] = False
                lazy_panels["execution"] = True
                execution_panel_host.clear()
                (
                    render_execution_tab,
                    material_task_controls,
                    capture_execution_scroll_state,
                    execution_container,
                    refresh_material_execution_controls,
                    restore_execution_scroll_state,
                ) = build_execution_panel(
                    tab_exec,
                    local_data,
                    wf,
                    current_user,
                    current_role,
                    can_execute_assistant,
                    can_review_execution,
                    refresh_list,
                    panel_container=execution_panel_host,
                )

            def queue_lazy_panel(panel_name: str):
                if lazy_panels[panel_name] or lazy_panel_queued[panel_name]:
                    return
                lazy_panel_queued[panel_name] = True
                host = scheme_panel_host if panel_name == "scheme" else execution_panel_host
                host.clear()
                with host:
                    with ui.row().classes("w-full items-center justify-center gap-2 py-8 text-slate-500"):
                        ui.spinner(size="sm")
                        ui.label("正在加载页签内容…").classes("text-sm")
                loader = load_scheme_panel if panel_name == "scheme" else load_execution_panel
                ui.timer(0.01, loader, once=True)

            tab_scheme.on("click", lambda: queue_lazy_panel("scheme"))
            tab_exec.on("click", lambda: queue_lazy_panel("execution"))
            if open_scheme_review_tab:
                queue_lazy_panel("scheme")

            # --- [TAB 4] 审批流转记录 ---
            with ui.tab_panel(tab_workflow).classes("p-2 md:p-3 bg-transparent h-full min-h-0 overflow-hidden"):
                with ui.card().classes(
                    "w-full h-full min-h-0 max-w-[1100px] mx-auto p-3 gap-2 "
                    "bg-white border border-slate-200 shadow-sm overflow-hidden"
                ):
                    workflow_container = ui.column().classes("w-full h-full min-h-0 gap-2 overflow-hidden")

                def render_workflow_tab():
                    workflow_container.clear()
                    with workflow_container:
                        if is_new:
                            ui.label("暂无审批记录，请先发起申请。").classes("text-gray-500 mt-4 text-center w-full")
                        else:
                            assignment_key = (
                                ECN_ECR_ASSIGNMENT_KEY
                                if wf.get("current_phase") == "ECR_PHASE"
                                else ECN_SCHEME_ASSIGNMENT_KEY
                                if wf.get("current_phase") == "ECN_SCHEME_REVIEW_PHASE"
                                else ""
                            )
                            assignment = wf.get(assignment_key, {}) if assignment_key else {}
                            nodes = assignment.get("nodes", []) if isinstance(assignment, dict) else []

                            def apply_reassignment(updated_record: dict) -> None:
                                updated_workflow = updated_record.get("workflow", {})
                                if isinstance(updated_workflow, dict):
                                    wf.clear()
                                    wf.update(copy.deepcopy(updated_workflow))
                                    local_data["workflow"] = wf
                                local_data["approval_log"] = copy.deepcopy(
                                    updated_record.get("approval_log", [])
                                )
                                render_workflow_tab()

                            if wf["pending_roles"]:
                                pending_list = get_ecn_pending_approval_roles(wf)
                                approved_list = [role for role in wf["pending_roles"] if role not in pending_list]
                                with ui.card().classes(
                                    "w-full shrink-0 bg-blue-50/50 shadow-none border border-blue-100 px-3 py-2 gap-0.5"
                                ):
                                    if pending_list:
                                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                                            ui.icon("schedule", size="xs").classes("text-orange-500")
                                            ui.label("当前节点等待审批").classes("text-xs font-bold text-slate-600")
                                            ui.label("、".join(pending_list)).classes(
                                                "text-sm font-semibold text-orange-700"
                                            )
                                    if approved_list:
                                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                                            ui.icon("check_circle", size="xs").classes("text-green-600")
                                            ui.label("当前节点已同意").classes("text-xs font-bold text-slate-600")
                                            ui.label("、".join(approved_list)).classes(
                                                "text-sm font-medium text-green-700"
                                            )

                            if (
                                can_reassign_approval
                                and isinstance(assignment, dict)
                                and assignment.get("status") == "pending"
                                and isinstance(nodes, list)
                            ):
                                current_node_index = int(assignment.get("current_node_index", 0))
                                with ui.card().classes(
                                    "w-full shrink-0 shadow-none border border-slate-200 px-3 py-2 gap-1"
                                ):
                                    with ui.row().classes("w-full items-center justify-between"):
                                        ui.label("审批节点审核人").classes("text-sm font-bold text-slate-700")
                                        ui.label("可调整当前及后续节点").classes("text-xs text-slate-400")
                                    for node_index, node in enumerate(nodes):
                                        if (
                                            not isinstance(node, dict)
                                            or node_index < current_node_index
                                            or node.get("status") == "completed"
                                        ):
                                            continue
                                        approved = {
                                            str(value)
                                            for value in node.get("approved_usernames", [])
                                            if str(value)
                                        }
                                        assignees = [
                                            str(value)
                                            for value in node.get("assignee_usernames", [])
                                            if str(value) and str(value) not in approved
                                        ]
                                        if not assignees:
                                            continue
                                        with ui.row().classes(
                                            "w-full items-center gap-2 py-1 border-t border-slate-100 first:border-t-0"
                                        ):
                                            ui.label(
                                                f"节点{node_index + 1} · {node.get('name') or '审批'}"
                                            ).classes("w-44 shrink-0 text-xs font-semibold text-slate-600")
                                            with ui.row().classes("flex-1 items-center gap-2 flex-wrap"):
                                                for assignee in assignees:
                                                    with ui.row().classes(
                                                        "items-center gap-1 rounded bg-slate-50 border border-slate-200 px-2 py-1"
                                                    ):
                                                        ui.label(assignee).classes("text-xs text-slate-700")
                                                        ui.button(
                                                            "移交",
                                                            icon="swap_horiz",
                                                            on_click=lambda _, key=assignment_key, index=node_index, snapshot=node, source=assignee: (
                                                                open_approval_reassignment_dialog(
                                                                    str(local_data.get("ecn_id") or ""),
                                                                    key,
                                                                    index,
                                                                    snapshot,
                                                                    source,
                                                                    current_user,
                                                                    apply_reassignment,
                                                                )
                                                            ),
                                                        ).props("flat dense size=xs color=primary")

                            approval_logs = local_data.get("approval_log", [])
                            if not approval_logs:
                                with ui.column().classes(
                                    "w-full flex-1 min-h-0 items-center justify-center "
                                    "rounded border border-dashed border-slate-200"
                                ):
                                    ui.icon("history", size="md").classes("text-slate-300")
                                    ui.label("暂无审批记录").classes("text-sm text-slate-400")
                                return

                            icon_map = {
                                "同意": "check",
                                "驳回": "close",
                                "执行变更": "play_arrow",
                                "发起申请": "send",
                                "发起方案评审": "fact_check",
                            }
                            action_classes = {
                                "同意": "text-green-700 bg-green-50 border-green-200",
                                "驳回": "text-red-700 bg-red-50 border-red-200",
                                "执行变更": "text-blue-700 bg-blue-50 border-blue-200",
                                "发起申请": "text-orange-700 bg-orange-50 border-orange-200",
                                "发起方案评审": "text-purple-700 bg-purple-50 border-purple-200",
                            }
                            with ui.column().classes(
                                "w-full flex-1 min-h-0 gap-0 overflow-y-auto overscroll-contain "
                                "rounded border border-slate-200 bg-white"
                            ):
                                for log_index, log in enumerate(approval_logs):
                                    action = humanize_ecn_log_action(local_data, log.get("action"))
                                    action_class = action_classes.get(
                                        action,
                                        "text-slate-700 bg-slate-50 border-slate-200",
                                    )
                                    row_background = "bg-white" if log_index % 2 == 0 else "bg-slate-50/60"
                                    with ui.row().classes(
                                        f"w-full items-start gap-2 px-3 py-2 flex-nowrap border-b "
                                        f"border-slate-100 last:border-b-0 {row_background}"
                                    ):
                                        with ui.element("div").classes(
                                            f"w-7 h-7 shrink-0 rounded-full border flex items-center "
                                            f"justify-center {action_class}"
                                        ):
                                            ui.icon(icon_map.get(action, "info"), size="xs")
                                        with ui.column().classes("flex-1 min-w-0 gap-0.5"):
                                            with ui.row().classes(
                                                "w-full items-center justify-between gap-x-3 gap-y-0 flex-wrap"
                                            ):
                                                with ui.row().classes("items-center gap-2 min-w-0 flex-wrap"):
                                                    ui.label(action).classes(
                                                        f"text-xs font-bold rounded border px-1.5 py-0.5 {action_class}"
                                                    )
                                                    ui.label(
                                                        f"{log.get('user') or '未知用户'}"
                                                        f"（{log.get('role') or '未知角色'}）"
                                                    ).classes("text-sm font-medium text-slate-800 break-all")
                                                ui.label(str(log.get("time") or "时间未记录")).classes(
                                                    "text-xs font-mono text-slate-500 shrink-0"
                                                )
                                            note = str(log.get("note") or "").strip()
                                            if note:
                                                ui.label(f"意见：{note}").classes(
                                                    "text-xs text-slate-600 break-all whitespace-pre-wrap"
                                                )

                render_workflow_tab()

        reject_scheme_dialog = ui.dialog().props("persistent")

        def open_scheme_reject_dialog(note=""):
            reject_scheme_dialog.clear()
            selected_state = {"item_ids": []}
            item_options = {}
            for index, item in enumerate(local_data.get("change_items", []), start=1):
                item_id = item.get("item_id")
                if item_id in [None, ""]:
                    continue
                item_group = classify_ecn_change_item(item)
                group_label = {
                    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT: "其它特定事项/资料",
                    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT: "系统内资料",
                    ECN_SCHEME_GROUP_MATERIAL: "物料",
                    ECN_SCHEME_GROUP_UNKNOWN: "未识别",
                }[item_group]
                if item.get("type") == "overview_update":
                    content = item.get("new_data", {}).get("content", "")
                    if not content and any(
                        state.get("action") == ECN_OVERVIEW_ACTION_DEACTIVATE
                        for state in item.get("project_states", {}).values()
                    ):
                        content = "仅失效原概述"
                elif item_group == ECN_SCHEME_GROUP_MATERIAL:
                    content = get_ecn_material_change_display(item)[1]
                else:
                    content = item.get("new_content", "")
                item_options[item_id] = (
                    f"#{index} [{group_label}] {item.get('author', '未知作者')} - {str(content)[:60]}"
                )

            if not item_options:
                return ui.notify("当前没有可供驳回的具体方案。", type="warning")

            with reject_scheme_dialog, ui.card().classes("w-[760px] max-w-full p-5 gap-3"):
                ui.label("驳回 ECN 方案").classes("text-xl font-bold text-red-700")
                ui.label("请选择需要改进的具体方案。只有所选方案及其作者会被退回整改。").classes(
                    "text-sm text-gray-600"
                )
                ui.select(
                    options=item_options,
                    multiple=True,
                    label="被驳回方案（必选）",
                ).bind_value(selected_state, "item_ids").props(
                    'outlined use-chips options-dense behavior="menu" '
                    'menu-anchor="bottom left" menu-self="top left" '
                    'popup-content-style="max-height: 280px"'
                ).classes("w-full")
                reject_note = (
                    ui.textarea(
                        "驳回意见",
                        value=note,
                        placeholder="说明所选方案需要改进的内容……",
                    )
                    .props("outlined auto-grow rows=3")
                    .classes("w-full")
                )

                async def submit_scheme_reject():
                    if ECN_REQUIRE_REJECTED_ITEM_SELECTION and not selected_state["item_ids"]:
                        return ui.notify("请至少选择一个需要改进的方案。", type="warning")
                    reject_scheme_dialog.close()
                    await execute_db_action(
                        "reject",
                        note=(reject_note.value or "").strip(),
                        rejected_item_ids=list(selected_state["item_ids"]),
                    )

                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("取消", on_click=reject_scheme_dialog.close).props("flat color=grey")
                    ui.button("确认驳回所选方案", on_click=submit_scheme_reject).props("color=red")
            reject_scheme_dialog.open()

        # ------------------------------------------
        # 底部操作栏及各类事件触发器
        # ------------------------------------------
        with ui.row().classes(
            "w-full bg-white p-4 border-t border-gray-300 justify-end items-center shrink-0 gap-4 shadow-[0_-5px_15px_rgba(0,0,0,0.05)]"
        ):
            if level_timing:
                ui.button("判定等级", icon="rule", on_click=open_level_dialog).props(
                    "outline color=indigo no-caps"
                )
            if is_draft_or_reject:
                if can_create_request and (basic["applicant"] == current_user or is_new):
                    ui.button("保存为草稿", on_click=lambda: execute_db_action("save_draft")).props("color=grey-7")
                    ui.button("发起 ECR", on_click=lambda: execute_db_action("submit_ecr")).props("color=primary")
            else:
                current_phase = wf.get("current_phase")
                if current_phase == "ECR_PHASE":
                    is_pending_user = is_ecr_assigned_approver(local_data, current_user)
                elif current_phase == "ECN_SCHEME_REVIEW_PHASE":
                    is_pending_user = is_scheme_assigned_approver(local_data, current_user)
                else:
                    is_pending_user = False
                if wf["current_state"] == ECNState.ECR_REVIEWING and basic["applicant"] == current_user:
                    ui.button("撤回修改", icon="undo", on_click=lambda: execute_db_action("withdraw")).props(
                        "color=orange"
                    )
                    ui.button("作废", icon="delete_forever", on_click=lambda: execute_db_action("cancel")).props(
                        "color=red"
                    )
                if is_scheming_phase and can_submit_scheme_review:
                    ui.button("发起 ECN 方案评审", on_click=lambda: execute_db_action("initiate_scheme_review")).props(
                        "color=purple"
                    ).bind_enabled_from(
                        local_data, "workflow", backward=lambda _: is_ecn_scheme_ready_for_review(local_data)
                    ).tooltip("需要所有参与人确认完成，且变更要求、资料与物料均有完整方案")
                elif is_pending_user and wf["current_state"] not in [
                    ECNState.CLOSED,
                    ECNState.CANCEL,
                    ECNState.REJECTED,
                    ECNState.ECN_SCHEMING,
                ]:
                    if wf["current_state"] == ECNState.ECN_REVIEWING:
                        ui.button(
                            "驳回 ECN 方案",
                            color="red",
                            on_click=open_scheme_reject_dialog,
                        )
                        ui.button(
                            "通过 ECN 方案",
                            color="green",
                            on_click=lambda: execute_db_action("approve"),
                        )
                    else:
                        note_input = ui.input("审批意见 (选填)").props("dense outlined").classes("w-64")
                        if wf.get("current_phase") == "ECR_PHASE":
                            ui.button(
                                "驳回 ECR 申请",
                                color="red",
                                on_click=lambda: execute_db_action("reject", note=note_input.value),
                            )
                        else:
                            ui.button(
                                "驳回",
                                color="red",
                                on_click=lambda: open_scheme_reject_dialog(note_input.value),
                            )
                        ui.button(
                            "同意 ECR 申请"
                            if wf.get("current_phase") == "ECR_PHASE"
                            else "通过 ECN 方案",
                            color="green",
                            on_click=lambda: execute_db_action("approve", note=note_input.value),
                        )

        # ------------------------------------------
        # 提取的数据库与流转控制逻辑中心
        # ------------------------------------------
        async def execute_db_action(action_type, note="", rejected_item_ids=None):
            if is_new and ecr_upload_busy["count"]:
                ui.notify("附件仍在上传，请等待上传完成后再保存", type="warning")
                return
            if action_busy["value"]:
                return
            action_busy["value"] = True
            succeeded = False
            try:
                result = await execute_action(
                    copy.deepcopy(local_data),
                    form_baseline,
                    action_type,
                    current_user,
                    current_role,
                    is_new=is_new,
                    note=note,
                    rejected_ids=rejected_item_ids,
                    project_sales=app.storage.general.get("project_sale", {}),
                )
                if result.ok and result.record is not None:
                    succeeded = True
                    ui.notify(f"操作成功：{result.record['ecn_id']}", type="positive")
                    root_dialog.close()
                    refresh_list()
                else:
                    ui.notify(result.message, type="warning", multi_line=True)
            finally:
                if not succeeded:
                    action_busy["value"] = False

        # --- 协同同步定时器 ---
        async def sync_schemes():
            """
            协同同步方案编写阶段的核心函数，定期从数据库拉取最新数据并对比当前本地数据，智能更新界面以反映其他用户的修改
            """
            if ecn_id:
                current_stamp = db_storage.get_item(ECN_VERSION_KEY, 0.0)
                if current_stamp == detail_version_tracker["stamp"]:
                    return
                detail_version_tracker["stamp"] = current_stamp
                # copy.deepcopy: Python标准库函数，用于递归复制对象，防止内存引用导致的数据污染
                fresh = db_storage.get_deep_item(["ecn_management_data", ecn_id])
                if not fresh:
                    return

                # 1. 同步工作流状态
                fresh_wf = fresh.get("workflow", {})
                was_current_role_pending = current_user in get_ecn_pending_approval_roles(wf)
                level_changed = (
                    fresh_wf.get("ecn_level") != wf.get("ecn_level")
                    or fresh_wf.get("ecn_level_decisions", []) != wf.get("ecn_level_decisions", [])
                )
                if (
                    fresh_wf.get("current_state") != wf["current_state"]
                    or fresh_wf.get("pending_roles") != wf["pending_roles"]
                    or fresh_wf.get("current_phase") != wf.get("current_phase")
                    or fresh_wf.get("current_step_index") != wf.get("current_step_index")
                    or fresh_wf.get("approval_round") != wf.get("approval_round")
                    or fresh_wf.get("step_approvals", {}) != wf.get("step_approvals", {})
                    or fresh_wf.get("ecr_workflow_assignment", {}) != wf.get("ecr_workflow_assignment", {})
                    or fresh_wf.get("scheme_workflow_assignment", {}) != wf.get("scheme_workflow_assignment", {})
                    or level_changed
                ):
                    wf["approval_round"] = fresh_wf.get("approval_round")
                    wf["current_state"] = fresh_wf.get("current_state")
                    wf["current_phase"] = fresh_wf.get("current_phase")
                    wf["current_step_index"] = fresh_wf.get("current_step_index", 0)
                    wf["pending_roles"] = copy.deepcopy(fresh_wf.get("pending_roles", []))
                    wf["step_approvals"] = copy.deepcopy(fresh_wf.get("step_approvals", {}))
                    wf["ecr_workflow_assignment"] = copy.deepcopy(fresh_wf.get("ecr_workflow_assignment", {}))
                    wf["scheme_workflow_assignment"] = copy.deepcopy(fresh_wf.get("scheme_workflow_assignment", {}))
                    wf["ecn_level"] = fresh_wf.get("ecn_level", "")
                    wf["ecn_level_decisions"] = copy.deepcopy(fresh_wf.get("ecn_level_decisions", []))
                    local_data["approval_log"] = copy.deepcopy(fresh.get("approval_log", []))
                    render_workflow_tab()  # 触发刷新流转页面
                    if level_changed:
                        root_dialog.close()
                        refresh_list()
                        ui.notify("ECN等级已由其他页面调整，请重新打开详情。", type="info")
                        return
                    current_identity_still_pending = current_user in get_ecn_pending_approval_roles(wf)
                    if was_current_role_pending and not current_identity_still_pending:
                        root_dialog.close()
                        refresh_list()
                        ui.notify("当前角色的审批已完成，待办状态已同步。", type="info")
                        return
                    ui.notify("后台流转状态已更新，已为您同步。", type="info")

                # 2. 任何阶段都同步方案快照。否则从方案阶段切到执行阶段时，已打开的方案页会停留在旧行数。
                change_items_changed = fresh.get("change_items", []) != local_data.get("change_items", [])
                if sync_detail_scheme_snapshot(local_data, participants, fresh):
                    render_parts()
                    render_my_actions()
                    render_items()
                    render_coverage_dashboard()

                if wf["current_state"] == ECNState.ECN_SCHEMING:
                    fresh_rev = fresh.get("review_info", {})
                    if fresh_rev:
                        sync_review_snapshot(review, review_baseline, fresh_rev)
                        render_coverage_dashboard()

                # 执行阶段允许多人按各自责任项协同确认，详情页需同步其他用户的勾选和阶段推进。
                fresh_execution_info = fresh.get("execution_info", {})
                if isinstance(fresh_execution_info, dict) and fresh_execution_info != local_data.get(
                    "execution_info", {}
                ):
                    previous_execution_info = local_data.get("execution_info", {})
                    previous_material = (
                        previous_execution_info.get("material_confirmations", {})
                        if isinstance(previous_execution_info, dict)
                        else {}
                    )
                    fresh_material = fresh_execution_info.get("material_confirmations", {})
                    previous_non_material = {
                        key: value
                        for key, value in previous_execution_info.items()
                        if key != "material_confirmations"
                    } if isinstance(previous_execution_info, dict) else {}
                    fresh_non_material = {
                        key: value
                        for key, value in fresh_execution_info.items()
                        if key != "material_confirmations"
                    }
                    can_update_controls_only = (
                        isinstance(previous_execution_info, dict)
                        and previous_execution_info.get("stage") == ECN_EXECUTION_STAGE_MATERIAL
                        and fresh_execution_info.get("stage") == ECN_EXECUTION_STAGE_MATERIAL
                        and isinstance(previous_material, dict)
                        and isinstance(fresh_material, dict)
                        and previous_non_material == fresh_non_material
                        and bool(material_task_controls)
                        and not change_items_changed
                    )
                    changed_material_ids = [
                        str(item_id)
                        for item_id in set(previous_material) | set(fresh_material)
                        if previous_material.get(item_id) != fresh_material.get(item_id)
                    ]
                    scroll_state = (
                        {}
                        if can_update_controls_only
                        else await capture_execution_scroll_state(execution_container.client)
                    )
                    local_data["execution_info"] = copy.deepcopy(fresh_execution_info)
                    local_data["change_items"] = copy.deepcopy(fresh.get("change_items", []))
                    local_data["approval_log"] = copy.deepcopy(fresh.get("approval_log", []))
                    if can_update_controls_only:
                        refresh_material_execution_controls(changed_material_ids)
                    else:
                        render_execution_tab()
                    render_workflow_tab()
                    if not can_update_controls_only:
                        await restore_execution_scroll_state(execution_container.client, scroll_state)

        if wf["current_state"] in [
            ECNState.ECR_REVIEWING,
            ECNState.ECN_SCHEMING,
            ECNState.ECN_REVIEWING,
            ECNState.MATERIAL_CODE_PENDING,
            ECNState.ECN_EXECUTING,
            ECNState.CLOSED,
        ] and not is_new:
            sync_timer = ui.timer(3.0, sync_schemes)
            root_dialog.on("close", sync_timer.cancel)

        def recheck_view_permission() -> None:
            if can_view_ecn(
                current_role,
                current_user,
                user_service=app.state.user_service,
            ):
                return
            ui.notify("您的ECN查看权限已被停用，当前窗口已关闭。", type="warning")
            root_dialog.close()

        permission_timer = ui.timer(15.0, recheck_view_permission)
        root_dialog.on("close", permission_timer.cancel)

    root_dialog.on("close", refresh_list)
    root_dialog.on("close", root_dialog.delete)
    root_dialog.open()
