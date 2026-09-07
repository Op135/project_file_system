# -*- encoding: utf-8 -*-
import copy
import logging
import mimetypes
import os
import ssl
import time
import uuid

import httpx
from httpx import (
    BasicAuth,
)
from nicegui import (
    app,
    ui,
)

from ...components import (
    FileThumbnail,
)
from ...config import (
    FILES_URL_DIR,
    PDF_PREVIEW_CACHE,
    SVN_PASSWORD,
    SVN_USERNAME,
    UPLOADS_DIR,
)
from ...ecn_access import (
    can_view_ecn_scheme_non_image_file,
)
from ...ecn_management_config import (
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_ITEM_STATUS_NORMAL,
    ECN_ITEM_STATUS_REVISED_CONFIRMED,
    ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_LABELS,
    ECN_PARTICIPANT_STATUS_CONFIG,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_SCHEME_GROUP_UNKNOWN,
    classify_ecn_change_item,
    get_ecn_material_change_display,
    get_ecn_overview_project_new_data,
    get_ecn_scheme_coverage,
    is_ecn_disposition_condition_required,
    is_ecn_material_disposition_required,
    resolve_ecn_overview_parameter_config,
)
from .actions import (
    edit_scheme,
    set_participant_status,
)
from .scheme_dialogs import (
    open_overview_change_dialog,
    open_text_change_dialog,
)

logger = logging.getLogger(__name__)


def build_scheme_panel(
    tab_scheme,
    wf,
    is_new,
    local_data,
    current_user,
    current_role,
    participants,
    is_scheming_phase,
    is_scheme_writer,
    dashboard_updater,
):
    render_parts = render_my_actions = render_items = render_coverage_dashboard = lambda: None

    def apply_scheme_result(record):
        local_data["change_items"] = copy.deepcopy(record["change_items"])
        participants.clear()
        participants.update(record["workflow"].get("scheme_participants", {}))
        render_parts()
        render_my_actions()
        render_items()
        render_coverage_dashboard()

    # --- [TAB 3] ECN 方案表单 ---
    with ui.tab_panel(tab_scheme).classes("gap-0 p-0 w-full mx-auto overflow-y-scroll"):
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
                        # 方案覆盖率与影响项监控看板
                        # ==========================================
                        coverage_container = ui.column().classes("w-full p-0 m-0")

                        def render_coverage_dashboard():
                            coverage_container.clear()
                            with coverage_container:
                                coverage = get_ecn_scheme_coverage(local_data)
                                req_requirements = coverage["required_requirements"]
                                req_docs = coverage["required_docs"]
                                req_mats = coverage["required_materials"]
                                missing_requirements = coverage["missing_requirements"]
                                missing_docs = coverage["missing_docs"]
                                missing_mats = coverage["missing_materials"]
                                incomplete_material_schemes = coverage["incomplete_material_schemes"]

                                # 渲染看板卡片 (单列纯净版)
                                with ui.card().classes(
                                    "w-full bg-orange-50/70 border border-orange-200 shadow-sm p-3 gap-2"
                                ):
                                    with ui.row().classes("items-center gap-2 border-b border-orange-200 pb-2 w-full"):
                                        ui.icon("rule", color="orange-8").classes("text-lg")
                                        ui.label("方案完整性自检与提醒").classes(
                                            "font-bold text-orange-900 text-sm tracking-wide"
                                        )

                                    # 取消了 grid，直接使用单列纵向布局
                                    with ui.column().classes("w-full gap-1 mt-1"):
                                        ui.label("变更要求及ECN影响项方案覆盖率自检:").classes(
                                            "text-[10px] font-bold text-gray-500 mb-1"
                                        )

                                        if missing_requirements:
                                            ui.label(
                                                "✖ 未关联变更要求: "
                                                + ", ".join(
                                                    f"要求 {idx}"
                                                    for idx in sorted(
                                                        missing_requirements,
                                                        key=lambda value: (
                                                            int(value) if str(value).isdigit() else float("inf"),
                                                            str(value),
                                                        ),
                                                    )
                                                )
                                            ).classes("text-xs text-red-600 font-bold")
                                        elif req_requirements:
                                            ui.label("✔ 所有变更要求均有方案关联").classes(
                                                "text-xs text-green-600 font-bold"
                                            )

                                        if missing_docs:
                                            ui.label(f"✖ 缺少资料方案: {', '.join(missing_docs)}").classes(
                                                "text-xs text-red-600 font-bold"
                                            )
                                        elif req_docs:
                                            ui.label("✔ 资料方案已全覆盖").classes("text-xs text-green-600 font-bold")

                                        if missing_mats:
                                            ui.label(f"✖ 缺少物料方案: {', '.join(missing_mats)}").classes(
                                                "text-xs text-red-600 font-bold"
                                            )
                                        elif req_mats:
                                            ui.label("✔ 物料变更方案已全覆盖").classes(
                                                "text-xs text-green-600 font-bold"
                                            )

                                        if incomplete_material_schemes:
                                            ui.label(
                                                "✖ 物料方案未配置追溯处置范围或适用的旧料处置措施: "
                                                + ", ".join(sorted(incomplete_material_schemes))
                                            ).classes("text-xs text-red-600 font-bold")

                                        if not req_requirements and not req_docs and not req_mats:
                                            ui.label("暂无需要检查的变更要求、资料或物料").classes(
                                                "text-xs text-gray-400"
                                            )

                        # 将渲染函数挂载到上方定义的字典中，以便借助自动刷新机制在数据变更时调用，同步更新覆盖率看板
                        dashboard_updater["refresh"] = render_coverage_dashboard
                        render_coverage_dashboard()
                    # 替换按钮渲染部分
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.label("产品工程变更方案明细").classes("font-bold text-gray-800 text-lg")
                        # 在方案可编辑阶段，且用户具有方案编写权限的前提下，才显示添加方案的按钮
                        if is_scheme_writer:
                            with ui.row().classes("gap-2 flex-wrap justify-end"):
                                ui.button(
                                    "添加系统内资料变更方案",
                                    icon="view_list",
                                    on_click=lambda: open_overview_change_dialog(
                                        local_data, current_user, handle_save_item
                                    ),
                                ).props(f"color=indigo outline dense {'disable' if not is_scheming_phase else ''}")

                                ui.button(
                                    "添加其它特定事项/资料变更方案",
                                    icon="article",
                                    on_click=lambda: open_text_change_dialog(
                                        local_data,
                                        current_user,
                                        handle_save_item,
                                        scheme_category=ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
                                    ),
                                ).props(f"color=primary outline dense {'disable' if not is_scheming_phase else ''}")

                                ui.button(
                                    "添加物料变更方案",
                                    icon="inventory",
                                    on_click=lambda: open_text_change_dialog(
                                        local_data,
                                        current_user,
                                        handle_save_item,
                                        scheme_category=ECN_SCHEME_GROUP_MATERIAL,
                                    ),
                                ).props(f"color=secondary outline dense {'disable' if not is_scheming_phase else ''}")

                    with ui.row().classes(
                        "w-full p-2 bg-white rounded border border-gray-200 items-center justify-between"
                    ):
                        with ui.row().classes("gap-2 items-center"):
                            ui.label("方案编写人员确认状态").classes("text-sm font-bold text-gray-600")
                            # 显示方案编写处于什么状态
                            parts_container = ui.row().classes("gap-1")

                            def render_parts():
                                parts_container.clear()
                                with parts_container:
                                    if not participants:
                                        ui.label("暂无人员参与").classes("text-xs text-gray-400 mt-1")
                                    for p, status in participants.items():
                                        status_info = ECN_PARTICIPANT_STATUS_CONFIG.get(
                                            status,
                                            ECN_PARTICIPANT_STATUS_CONFIG[ECN_PARTICIPANT_STATUS_EDITING],
                                        )
                                        ui.chip(
                                            f"{p}: {status_info['label']}",
                                            color=status_info["color"],
                                            icon=status_info["icon"],
                                        ).props("size=sm").classes("text-white")

                            render_parts()

                        # 方案编写不同状态提供不同按钮交互，且只有方案编写者才有权限操作
                        my_action_container = ui.row()

                        def render_my_actions():
                            my_action_container.clear()
                            with my_action_container:
                                if is_scheme_writer:
                                    cur_status = participants.get(current_user)
                                    if cur_status in [
                                        ECN_PARTICIPANT_STATUS_EDITING,
                                        ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
                                        None,
                                    ]:
                                        ui.button(
                                            "重新确认我的方案"
                                            if cur_status == ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
                                            else "确认完成我的方案",
                                            icon="done_all",
                                            on_click=lambda: toggle_part_status(ECN_PARTICIPANT_STATUS_CONFIRMED),
                                        ).props("color=green outline dense")
                                    elif cur_status == ECN_PARTICIPANT_STATUS_CONFIRMED:
                                        ui.button(
                                            "重新开启编辑",
                                            icon="lock_open",
                                            on_click=lambda: toggle_part_status(ECN_PARTICIPANT_STATUS_EDITING),
                                        ).props("color=orange outline dense")

                        render_my_actions()

                        # 切换参与者状态的显示与数据库对应状态数据
                        async def toggle_part_status(new_status):
                            result = await set_participant_status(
                                local_data["ecn_id"], copy.deepcopy(local_data), current_user, current_role, new_status
                            )
                            if not result.ok or result.record is None:
                                ui.notify(result.message, type="warning")
                                return
                            apply_scheme_result(result.record)

                    # 方案内容显示列
                    item_container = ui.column().classes("w-full gap-3")
                    scheme_group_expansion_state = {
                        ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT: True,
                        ECN_SCHEME_GROUP_ORDINARY_DOCUMENT: True,
                        ECN_SCHEME_GROUP_MATERIAL: True,
                        ECN_SCHEME_GROUP_UNKNOWN: True,
                    }

                    async def handle_save_item(item_data, is_edit=False, expected_item=None):
                        result = await edit_scheme(
                            local_data["ecn_id"],
                            copy.deepcopy(local_data),
                            copy.deepcopy(item_data),
                            expected_item if is_edit else None,
                            current_user,
                            current_role,
                        )
                        if not result.ok or result.record is None:
                            ui.notify(result.message, type="warning")
                            return False
                        apply_scheme_result(result.record)
                        return True

                    def get_item_projects(item):
                        return [project for project in item.get("projects", []) if project]

                    # --- 替换列表渲染分组部分 ---
                    def render_items():
                        """按资料分类渲染舒适型对比表格。"""
                        item_container.clear()
                        with item_container:
                            change_items = local_data.get("change_items", [])
                            if not change_items:
                                ui.label("暂未添加具体的方案条目").classes("text-sm text-slate-400 m-auto mt-4")
                                return

                            grouped_items = {
                                ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT: [],
                                ECN_SCHEME_GROUP_ORDINARY_DOCUMENT: [],
                                ECN_SCHEME_GROUP_MATERIAL: [],
                                ECN_SCHEME_GROUP_UNKNOWN: [],
                            }
                            for global_idx, item in enumerate(change_items):
                                grouped_items[classify_ecn_change_item(item)].append((global_idx, item))

                            group_configs = {
                                ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT: (
                                    "系统内资料变更方案",
                                    "view_list",
                                ),
                                ECN_SCHEME_GROUP_ORDINARY_DOCUMENT: (
                                    "其它特定事项/资料变更方案",
                                    "article",
                                ),
                                ECN_SCHEME_GROUP_MATERIAL: (
                                    "物料变更方案",
                                    "inventory",
                                ),
                                ECN_SCHEME_GROUP_UNKNOWN: (
                                    "未识别方案",
                                    "list",
                                ),
                            }
                            table_grid_style = (
                                "display:grid;"
                                "grid-template-columns:60px minmax(190px,.72fr) "
                                "minmax(120px,.42fr) 72px "
                                "minmax(210px,1.1fr) minmax(230px,1.1fr) "
                                "minmax(90px,.35fr) minmax(90px,.35fr) "
                                "minmax(110px,.35fr) minmax(200px,.6fr) "
                                "80px 120px 80px;"
                            )

                            def table_status_view(item):
                                review_status = item.get("review_status", ECN_ITEM_STATUS_NORMAL)
                                if review_status == ECN_ITEM_STATUS_NEEDS_IMPROVEMENT:
                                    return "待改进", "warning_amber", "text-red-600", False
                                if review_status == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION:
                                    return "待重新确认", "schedule", "text-amber-600", False
                                if review_status == ECN_ITEM_STATUS_REVISED_CONFIRMED:
                                    return (
                                        "已整改确认",
                                        "task_alt",
                                        "text-cyan-600",
                                        True,
                                    )

                                participant_status = participants.get(item.get("author"))
                                status_info = ECN_PARTICIPANT_STATUS_CONFIG.get(
                                    participant_status,
                                    ECN_PARTICIPANT_STATUS_CONFIG.get(ECN_PARTICIPANT_STATUS_EDITING, {}),
                                )
                                if participant_status == ECN_PARTICIPANT_STATUS_CONFIRMED:
                                    return (
                                        status_info.get("label", "确认完成方案"),
                                        "check_circle",
                                        "text-green-700",
                                        True,
                                    )
                                if participant_status == ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION:
                                    # 只有被驳回的具体方案显示整改状态，作者的其它方案保持有效。
                                    return (
                                        "方案有效",
                                        "check_circle_outline",
                                        "text-slate-500",
                                        True,
                                    )
                                return (
                                    status_info.get("label", "编写中"),
                                    "more_horiz",
                                    "text-slate-500",
                                    False,
                                )

                            def table_item_title(item):
                                if item.get("type") == "overview_update":
                                    label = item.get("label", "")
                                    title = (
                                        app.storage.general.get("over_config_data_flat", {})
                                        .get(label, {})
                                        .get("title", label)
                                    )
                                    return f"{item.get('role') or '概述参数'} · {title}"
                                return item.get("change_type") or "资料变更"

                            def render_table_projects(item):
                                project_states = item.get("project_states", {})
                                if item.get("type") == "overview_update" and project_states:
                                    with ui.column().classes("w-full gap-0 self-stretch"):
                                        for project in project_states:
                                            with ui.element("div").classes(
                                                "w-full min-h-[64px] px-1 flex items-center "
                                                "justify-center border-b border-slate-100 last:border-b-0"
                                            ):
                                                ui.label(project).classes(
                                                    "text-sm font-semibold text-blue-950 text-center "
                                                    "break-all leading-tight"
                                                )
                                    return
                                projects = get_item_projects(item)
                                if projects:
                                    for project in projects:
                                        ui.label(project).classes("text-sm text-blue-950 leading-tight")
                                else:
                                    ui.label("未指定项目").classes("text-sm font-bold text-slate-500")

                            def render_table_parameter(item):
                                ui.label(table_item_title(item)).classes("text-sm text-slate-800 break-all")

                            def get_scheme_tracking_values(item):
                                traceability_levels = item.get("traceability_levels", [])
                                disposition_measure = item.get("disposition_measure")
                                return traceability_levels, disposition_measure

                            def render_table_traceability(item):
                                traceability_levels, _ = get_scheme_tracking_values(item)
                                if traceability_levels:
                                    for level in traceability_levels:
                                        ui.label(level).classes("text-xs text-slate-700 break-all")
                                elif classify_ecn_change_item(item) == ECN_SCHEME_GROUP_MATERIAL:
                                    ui.label("未配置").classes("text-xs text-red-500")
                                else:
                                    ui.label("—").classes("text-sm text-slate-400")

                            def render_table_disposition(item):
                                if classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
                                    ui.label("—").classes("text-sm text-slate-400")
                                    return
                                if not is_ecn_material_disposition_required(item.get("change_type")):
                                    ui.label("—").classes("text-sm text-slate-400")
                                    return
                                _, disposition_measure = get_scheme_tracking_values(item)
                                if disposition_measure:
                                    disposition_color = {
                                        "报废": "text-red-700",
                                        "返工": "text-orange-600",
                                        "有条件用完止": "text-amber-600",
                                    }.get(disposition_measure, "text-slate-700")
                                    ui.label(disposition_measure).classes(
                                        f"text-sm font-semibold {disposition_color} break-all"
                                    )
                                    disposition_condition = str(item.get("disposition_condition") or "").strip()
                                    if disposition_condition:
                                        ui.label(f"条件：{disposition_condition}").classes(
                                            "text-xs text-slate-500 break-all"
                                        )
                                    elif is_ecn_disposition_condition_required(disposition_measure):
                                        ui.label("条件：未填写").classes("text-xs text-red-500 break-all")
                                elif classify_ecn_change_item(item) == ECN_SCHEME_GROUP_MATERIAL:
                                    ui.label("未配置").classes("text-sm text-red-500")
                                else:
                                    ui.label("—").classes("text-sm text-slate-400")

                            def render_table_requirements(item):
                                requirement_indexes = item.get("req_idxs", [])
                                if requirement_indexes:
                                    for requirement_idx in requirement_indexes:
                                        ui.label(f"要求 {requirement_idx}").classes("text-sm text-slate-700 break-all")
                                else:
                                    ui.label("—").classes("text-sm text-amber-500 font-medium")

                            def render_table_impacts(item):
                                linked_docs = item.get("linked_docs", [])
                                linked_materials = item.get("linked_materials", [])
                                if linked_docs:
                                    for document in linked_docs:
                                        ui.label(document).classes("text-sm text-slate-700 break-all")
                                if linked_materials:
                                    for material in linked_materials:
                                        ui.label(material).classes("text-sm text-slate-700 break-all")
                                if not linked_docs and not linked_materials:
                                    ui.label("—").classes("text-sm text-amber-500")

                            overview_existing_data_dialog = ui.dialog()

                            def open_overview_existing_data_dialog(project, item, entries):
                                overview_existing_data_dialog.clear()
                                with (
                                    overview_existing_data_dialog,
                                    ui.card().classes("w-[720px] max-w-full max-h-[80vh] p-0 gap-0"),
                                ):
                                    with ui.row().classes(
                                        "w-full px-4 py-3 items-center justify-between "
                                        "border-b border-slate-200 bg-slate-50"
                                    ):
                                        with ui.column().classes("gap-0 min-w-0"):
                                            ui.label(f"{project} · 当前及本单暂存数据").classes(
                                                "text-base font-bold text-slate-800"
                                            )
                                            ui.label(table_item_title(item)).classes("text-xs text-slate-500 break-all")
                                        ui.button(
                                            icon="close",
                                            on_click=overview_existing_data_dialog.close,
                                        ).props("flat round dense color=blue-grey-7")
                                    with ui.column().classes("w-full gap-2 p-4 overflow-y-auto"):
                                        for index, entry in enumerate(entries, start=1):
                                            with ui.card().classes(
                                                "w-full p-3 gap-1 shadow-none border border-slate-200"
                                            ):
                                                ui.label(f"{index}. {entry.get('source', '当前已有')}").classes(
                                                    "text-xs font-bold text-slate-500"
                                                )
                                                ui.label(str(entry.get("content") or "（空内容）")).classes(
                                                    "text-sm text-slate-800 break-all whitespace-pre-line"
                                                )
                                    with ui.row().classes("w-full px-4 py-3 border-t border-slate-200 justify-end"):
                                        ui.button(
                                            "关闭",
                                            on_click=overview_existing_data_dialog.close,
                                        ).props("flat color=blue-grey-7")
                                overview_existing_data_dialog.open()

                            def render_overview_subrow():
                                return ui.element("div").classes(
                                    "w-full min-h-[64px] px-1 flex flex-col justify-center "
                                    "border-b border-slate-100 last:border-b-0 min-w-0"
                                )

                            def render_table_action(item):
                                project_states = item.get("project_states", {})
                                if item.get("type") == "overview_update" and project_states:
                                    with ui.column().classes("w-full gap-0 self-stretch"):
                                        for project_state in project_states.values():
                                            with render_overview_subrow():
                                                action = project_state.get("action")
                                                action_label = ECN_OVERVIEW_ACTION_LABELS.get(
                                                    action,
                                                    str(action or "—"),
                                                )
                                                action_class = {
                                                    ECN_OVERVIEW_ACTION_ADD: "text-blue-700 bg-blue-50",
                                                    ECN_OVERVIEW_ACTION_DEACTIVATE: "text-red-700 bg-red-50",
                                                }.get(action, "text-amber-700 bg-amber-50")
                                                ui.label(action_label).classes(
                                                    f"text-xs font-bold rounded px-2 py-0.5 self-center {action_class}"
                                                )
                                    return
                                action_label = (
                                    item.get("change_type")
                                    if classify_ecn_change_item(item) == ECN_SCHEME_GROUP_MATERIAL
                                    else "变更"
                                )
                                ui.label(action_label or "—").classes(
                                    "text-xs font-semibold text-slate-600 text-center break-all"
                                )

                            async def fetch_ecn_svn_file(file_url, file_name):
                                """使用系统配置的 SVN 账号读取文件，供ECN审核查看或下载。"""
                                ui.notify(
                                    f"正在从 SVN 读取 {file_name}...",
                                    type="info",
                                    timeout=2000,
                                )
                                ssl_context = ssl.create_default_context()
                                ssl_context.check_hostname = False
                                ssl_context.verify_mode = ssl.CERT_NONE
                                auth = BasicAuth(SVN_USERNAME, SVN_PASSWORD) if SVN_USERNAME and SVN_PASSWORD else None
                                try:
                                    async with httpx.AsyncClient(
                                        follow_redirects=True,
                                        verify=ssl_context,
                                        auth=auth,
                                        trust_env=False,
                                    ) as client:
                                        response = await client.get(file_url, timeout=60)
                                    if response.status_code >= 400:
                                        ui.notify(
                                            f"SVN 文件读取失败：HTTP {response.status_code}",
                                            type="negative",
                                        )
                                        return None
                                    return response.content
                                except Exception as exc:
                                    logger.error(
                                        "ECN读取SVN文件失败：%s",
                                        file_url,
                                        exc_info=True,
                                    )
                                    ui.notify(f"SVN 文件读取失败：{exc}", type="negative")
                                    return None

                            async def open_or_download_overview_file(
                                file_url,
                                file_name,
                                file_type,
                                local_file_path,
                                is_remote_svn,
                            ):
                                normalized_type = str(file_type or "").split(";", 1)[0].lower()
                                is_pdf = normalized_type == "application/pdf" or file_name.lower().endswith(".pdf")
                                if is_remote_svn:
                                    file_content = await fetch_ecn_svn_file(file_url, file_name)
                                    if file_content is None:
                                        return
                                    if is_pdf:
                                        client = ui.context.client
                                        cache_key = f"{client.id}-{uuid.uuid4()}"
                                        PDF_PREVIEW_CACHE[cache_key] = file_content

                                        def cleanup_pdf_cache(key=cache_key):
                                            PDF_PREVIEW_CACHE.pop(key, None)

                                        client.on_disconnect(cleanup_pdf_cache)
                                        ui.run_javascript(
                                            f'window.open("/view/svn_pdf?id={cache_key}&v={int(time.time())}", "_blank");'
                                        )
                                    else:
                                        ui.download(file_content, file_name)
                                    return

                                if is_pdf:
                                    ui.navigate.to(file_url, new_tab=True)
                                elif os.path.isfile(local_file_path):
                                    ui.download(local_file_path, file_name)
                                else:
                                    ui.download(file_url, file_name)

                            def render_overview_file_content(
                                item,
                                file_data,
                                display_label,
                                result_note="",
                                project="",
                            ):
                                """图片用缩略图，其它文件用可点击文件名展示。"""
                                if not isinstance(file_data, dict):
                                    return False
                                processing_type = str(file_data.get("type") or item.get("config_processing_type") or "")
                                if processing_type not in {"file", "image", "video", "search", "svn"}:
                                    return False

                                file_name = str(file_data.get("content") or "").strip()
                                if not file_name:
                                    return False
                                config, _ = resolve_ecn_overview_parameter_config(
                                    app.storage.general.get("over_config_data_flat", {}),
                                    item.get("label"),
                                )
                                upload_path = str(config.get("upload_path") or UPLOADS_DIR)
                                file_url = str(file_data.get("url_path") or f"{FILES_URL_DIR}/{file_name}")
                                stored_local_file_path = str(file_data.get("local_file_path") or "")
                                local_file_path = stored_local_file_path or os.path.join(
                                    upload_path,
                                    file_name,
                                )
                                file_type = str(
                                    file_data.get("file_type")
                                    or mimetypes.guess_type(file_name)[0]
                                    or ("image/*" if processing_type == "image" else "application/octet-stream")
                                )
                                is_remote_svn = processing_type == "svn" and file_url.startswith(
                                    ("http://", "https://")
                                )

                                def file_tooltip(*parts):
                                    return "\n".join(str(part) for part in parts if str(part or "").strip())

                                file_text_color = "text-slate-700" if display_label == "旧" else "text-slate-900"
                                is_uploaded_image = processing_type in {"file", "image"} and (
                                    processing_type == "image" or file_type.startswith("image/")
                                )
                                if not is_uploaded_image:
                                    can_view_file = can_view_ecn_scheme_non_image_file(
                                        item,
                                        current_role,
                                        current_user,
                                        app.storage.general.get("over_config_data_flat", {}),
                                    )
                                    if not can_view_file:
                                        with (
                                            ui.row()
                                            .classes(
                                                "w-full items-center gap-1 flex-nowrap min-w-0 "
                                                "text-slate-400 cursor-not-allowed"
                                            )
                                            .tooltip(
                                                file_tooltip(
                                                    "当前角色无文件查看或下载权限",
                                                    result_note,
                                                )
                                            )
                                        ):
                                            ui.icon("lock", size="xs").classes("shrink-0")
                                            ui.label(file_name).classes("text-sm font-semibold break-all min-w-0")
                                        return True

                                if processing_type == "search" and (
                                    not stored_local_file_path or not os.path.isfile(stored_local_file_path)
                                ):
                                    search_result_container = ui.row().classes(
                                        "w-full items-center gap-1 text-slate-400"
                                    )
                                    with search_result_container:
                                        ui.spinner(size="xs")
                                        ui.label(f"正在检查 {file_name}").classes("text-xs min-w-0")

                                    async def resolve_search_file():
                                        from ...utils import validate_search_path

                                        (
                                            is_valid,
                                            resolved_url,
                                            resolved_file_type,
                                            resolved_local_path,
                                            message,
                                        ) = await validate_search_path(
                                            file_name,
                                            config,
                                            [project] if project else [],
                                        )
                                        search_result_container.clear()
                                        with search_result_container:
                                            if is_valid and os.path.isfile(resolved_local_path):
                                                resolved_data = copy.deepcopy(file_data)
                                                resolved_data.update(
                                                    {
                                                        "url_path": resolved_url,
                                                        "file_type": resolved_file_type,
                                                        "local_file_path": resolved_local_path,
                                                    }
                                                )
                                                render_overview_file_content(
                                                    item,
                                                    resolved_data,
                                                    display_label,
                                                    result_note,
                                                    project,
                                                )
                                            else:
                                                with (
                                                    ui.row()
                                                    .classes("w-full items-center gap-1 text-slate-400")
                                                    .tooltip(
                                                        file_tooltip(
                                                            file_name,
                                                            message or "文件不存在",
                                                            result_note,
                                                        )
                                                    )
                                                ):
                                                    ui.icon("link_off", size="xs")
                                                    ui.label(f"{file_name}（文件不存在）").classes("text-xs min-w-0")

                                    ui.timer(0.01, resolve_search_file, once=True)
                                    return True

                                if not is_remote_svn and not os.path.isfile(local_file_path):
                                    with (
                                        ui.row()
                                        .classes("w-full items-center gap-1 text-slate-400")
                                        .tooltip(
                                            file_tooltip(
                                                file_name,
                                                "文件不存在",
                                                result_note,
                                            )
                                        )
                                    ):
                                        ui.icon(
                                            "image_not_supported" if processing_type == "image" else "link_off",
                                            size="xs",
                                        )
                                        ui.label(f"{file_name}（文件不存在）").classes("text-xs  min-w-0")
                                    return True
                                if not is_remote_svn:
                                    try:
                                        app.add_static_file(
                                            local_file=local_file_path,
                                            url_path=file_url,
                                        )
                                    except Exception:
                                        logger.debug(
                                            "ECN方案文件静态路由可能已注册：%s",
                                            file_url,
                                            exc_info=True,
                                        )

                                if is_uploaded_image:
                                    with ui.row().classes("w-full items-center gap-2 flex-nowrap min-w-0"):
                                        FileThumbnail(
                                            file_url=file_url,
                                            file_type=file_type,
                                            file_name_suffix=file_name,
                                            file_lab=(f"ecn-{item.get('item_id', '')}-{display_label}-{file_name}"),
                                            display_lab=display_label,
                                            parents_h=8,
                                            delet_lab=False,
                                            local_file_path=local_file_path,
                                        )
                                        ui.label(file_name).classes(
                                            f"text-sm font-semibold {file_text_color} break-all min-w-0 flex-1"
                                        ).tooltip(file_tooltip(file_name, result_note))
                                    return True

                                is_pdf = file_type.split(";", 1)[0].lower() == "application/pdf" or (
                                    file_name.lower().endswith(".pdf")
                                )

                                async def handle_file_click():
                                    await open_or_download_overview_file(
                                        file_url,
                                        file_name,
                                        file_type,
                                        local_file_path,
                                        is_remote_svn,
                                    )

                                file_link = (
                                    ui.row()
                                    .classes(
                                        "w-full items-center gap-1 flex-nowrap min-w-0 "
                                        f"cursor-pointer {file_text_color}"
                                    )
                                    .on("click", handle_file_click)
                                )
                                if result_note:
                                    file_link.tooltip(result_note)
                                with file_link:
                                    ui.icon(
                                        "picture_as_pdf" if is_pdf else "attach_file",
                                        size="xs",
                                    ).classes("shrink-0")
                                    ui.label(file_name).classes(
                                        "text-sm font-semibold break-all underline-offset-2 hover:underline min-w-0"
                                    )
                                return True

                            def render_overview_current_content(item, project, project_state):
                                action = project_state.get("action")
                                if action != ECN_OVERVIEW_ACTION_ADD:
                                    old_data = project_state.get("old_data", {})
                                    if render_overview_file_content(
                                        item,
                                        old_data,
                                        "旧",
                                        project=project,
                                    ):
                                        return
                                    old_text = str(old_data.get("content") or "无")
                                    ui.label(old_text).classes("w-full text-sm font-semibold text-slate-700 ").tooltip(
                                        old_text
                                    )
                                    return

                                entries = [
                                    entry
                                    for entry in project_state.get("existing_contents", [])
                                    if isinstance(entry, dict)
                                ]
                                current_entries = [entry for entry in entries if entry.get("source") == "当前已有"]
                                pending_entries = [entry for entry in entries if entry.get("source") != "当前已有"]
                                if not entries:
                                    ui.label("当前无内容").classes("text-sm text-slate-400")
                                    return
                                if not current_entries:
                                    ui.label("当前无内容").classes("text-sm text-slate-400")
                                    ui.button(
                                        f"本单另有 {len(pending_entries)} 条待新增 · 查看",
                                        on_click=lambda _=None, p=project, current_item=item, all_entries=copy.deepcopy(entries): (
                                            open_overview_existing_data_dialog(
                                                p,
                                                current_item,
                                                all_entries,
                                            )
                                        ),
                                    ).props("flat dense no-caps color=primary").classes("text-[11px] self-start -ml-2")
                                    return
                                current_contents = [
                                    str(entry.get("content") or "（空内容）") for entry in current_entries
                                ]
                                tooltip_text = "\n".join(
                                    f"{index}. {content}" for index, content in enumerate(current_contents, start=1)
                                )
                                ui.label("存在现有内容").classes(
                                    "w-full text-sm font-normal text-slate-400 cursor-help"
                                ).tooltip(tooltip_text)

                            def render_table_old_value(item):
                                if classify_ecn_change_item(item) == ECN_SCHEME_GROUP_MATERIAL:
                                    old_value, _ = get_ecn_material_change_display(item)
                                    ui.label(old_value or "无").classes(
                                        "text-sm font-bold text-slate-800 break-all whitespace-pre-line"
                                    )
                                    return
                                if item.get("type") != "overview_update":
                                    ui.label(item.get("old_content", "")).classes(
                                        "text-sm font-bold text-slate-800 break-all"
                                    )
                                    return

                                project_states = item.get("project_states", {})
                                if project_states:
                                    with ui.column().classes("w-full gap-0 self-stretch"):
                                        for project, project_state in project_states.items():
                                            with render_overview_subrow():
                                                render_overview_current_content(
                                                    item,
                                                    project,
                                                    project_state,
                                                )
                                else:
                                    ui.label(item.get("old_data", {}).get("content", "无")).classes(
                                        "text-sm font-bold text-slate-600 break-all"
                                    )

                            def render_table_new_value(item):
                                if classify_ecn_change_item(item) == ECN_SCHEME_GROUP_MATERIAL:
                                    _, new_value = get_ecn_material_change_display(item)
                                    ui.label(new_value or "无").classes(
                                        "text-sm font-semibold text-slate-900 break-all whitespace-pre-line"
                                    )
                                    return
                                if item.get("type") != "overview_update":
                                    with ui.row().classes("w-full items-center gap-1 flex-nowrap min-w-0"):
                                        ui.label(item.get("new_content", "")).classes(
                                            "text-sm font-semibold text-slate-900 break-all min-w-0 flex-1"
                                        )
                                        file_server_path = str(item.get("file_server_path") or "").strip()
                                        if file_server_path:
                                            ui.icon("folder_open", size="xs").classes(
                                                "shrink-0 text-slate-400 cursor-help"
                                            ).tooltip(f"文件服务器存放路径：\n{file_server_path}")
                                    return

                                new_data = item.get("new_data", {})
                                project_states = item.get("project_states", {})
                                new_content = str(new_data.get("content") or "（未填写）")
                                if project_states:
                                    with ui.column().classes("w-full gap-0 self-stretch"):
                                        for project, project_state in project_states.items():
                                            with render_overview_subrow():
                                                action = project_state.get("action")
                                                project_new_data = get_ecn_overview_project_new_data(
                                                    new_data,
                                                    project_state,
                                                )
                                                if action == ECN_OVERVIEW_ACTION_DEACTIVATE:
                                                    ui.label("—").classes(
                                                        "text-sm font-semibold text-slate-400 cursor-help"
                                                    ).tooltip("原内容失效；不生成新内容")
                                                else:
                                                    result_note = (
                                                        (
                                                            "现有内容保留"
                                                            if any(
                                                                isinstance(entry, dict)
                                                                and entry.get("source") == "当前已有"
                                                                for entry in project_state.get("existing_contents", [])
                                                            )
                                                            else "当前无内容，将生成新内容"
                                                        )
                                                        if action == ECN_OVERVIEW_ACTION_ADD
                                                        else "原内容失效"
                                                    )
                                                    is_file_content = str(
                                                        new_data.get("type") or item.get("config_processing_type") or ""
                                                    ) in {
                                                        "file",
                                                        "image",
                                                        "video",
                                                        "search",
                                                        "svn",
                                                    } and bool(str(new_data.get("content") or "").strip())
                                                    if is_file_content:
                                                        render_overview_file_content(
                                                            item,
                                                            project_new_data,
                                                            "新",
                                                            result_note,
                                                            project,
                                                        )
                                                    else:
                                                        ui.label(new_content).classes(
                                                            "w-full text-sm font-semibold text-slate-900 cursor-help"
                                                        ).tooltip(result_note)
                                    return
                                ui.label(new_content).classes("text-sm font-semibold text-slate-900 break-all")

                            rejection_history_dialog = ui.dialog()

                            def get_rejection_history(item):
                                history = item.get("rejection_history", [])
                                return (
                                    [copy.deepcopy(record) for record in history if isinstance(record, dict)]
                                    if isinstance(history, list)
                                    else []
                                )

                            def snapshot_projects(snapshot):
                                projects = snapshot.get("projects", [])
                                return ", ".join(str(project) for project in projects) or "未指定"

                            def snapshot_old_content(snapshot):
                                if classify_ecn_change_item(snapshot) == ECN_SCHEME_GROUP_MATERIAL:
                                    return get_ecn_material_change_display(snapshot)[0] or "无"
                                if snapshot.get("type") != "overview_update":
                                    return str(snapshot.get("old_content") or "无")
                                project_states = snapshot.get("project_states", {})
                                if isinstance(project_states, dict) and project_states:
                                    values = []
                                    for project, state in project_states.items():
                                        if not isinstance(state, dict):
                                            continue
                                        action = state.get("action")
                                        if action == ECN_OVERVIEW_ACTION_ADD:
                                            entries = state.get("existing_contents", [])
                                            current_count = sum(
                                                1 for entry in entries if entry.get("source") == "当前已有"
                                            )
                                            summary = f"{project}：" + (
                                                f"当前已有 {current_count} 条" if current_count else "当前无内容"
                                            )
                                            preview = [
                                                f"{entry.get('source', '当前已有')}："
                                                f"{entry.get('content') or '（空内容）'}"
                                                for entry in entries[:2]
                                            ]
                                            if preview:
                                                summary += "：" + "；".join(preview)
                                            if len(entries) > 2:
                                                summary += f"；另有 {len(entries) - 2} 条"
                                            values.append(summary)
                                        else:
                                            content = state.get("old_data", {}).get("content", "无")
                                            values.append(f"{project}：{content}")
                                    if values:
                                        return "\n".join(values)
                                return str(snapshot.get("old_data", {}).get("content", "无"))

                            def snapshot_new_content(snapshot):
                                if classify_ecn_change_item(snapshot) == ECN_SCHEME_GROUP_MATERIAL:
                                    return get_ecn_material_change_display(snapshot)[1] or "无"
                                if snapshot.get("type") == "overview_update":
                                    project_states = snapshot.get("project_states", {})
                                    if isinstance(project_states, dict) and project_states:
                                        new_content = str(snapshot.get("new_data", {}).get("content") or "（未填写）")
                                        results = []
                                        for project, state in project_states.items():
                                            if not isinstance(state, dict):
                                                continue
                                            action = state.get("action")
                                            if action == ECN_OVERVIEW_ACTION_ADD:
                                                has_current_content = any(
                                                    isinstance(entry, dict) and entry.get("source") == "当前已有"
                                                    for entry in state.get("existing_contents", [])
                                                )
                                                results.append(
                                                    f"{project}：新增 {new_content}；"
                                                    + (
                                                        "现有内容保留"
                                                        if has_current_content
                                                        else "当前无内容，将生成新内容"
                                                    )
                                                )
                                            elif action == ECN_OVERVIEW_ACTION_DEACTIVATE:
                                                results.append(f"{project}：原内容失效；不生成新内容")
                                            else:
                                                results.append(f"{project}：更换为 {new_content}；原内容失效")
                                        if results:
                                            return "\n".join(results)
                                    return "未记录执行结果"
                                return str(snapshot.get("new_content") or "无")

                            def snapshot_requirements(snapshot):
                                requirement_indexes = snapshot.get("req_idxs", [])
                                return (
                                    ", ".join(f"要求 {requirement_idx}" for requirement_idx in requirement_indexes)
                                    if requirement_indexes
                                    else "未关联"
                                )

                            def snapshot_impacts(snapshot):
                                impacts = []
                                linked_docs = snapshot.get("linked_docs", [])
                                linked_materials = snapshot.get("linked_materials", [])
                                if linked_docs:
                                    impacts.append("资料：" + ", ".join(map(str, linked_docs)))
                                if linked_materials:
                                    impacts.append("物料：" + ", ".join(map(str, linked_materials)))
                                return "\n".join(impacts) or "未关联"

                            def render_scheme_snapshot(title, snapshot, accent_classes):
                                with ui.card().classes(f"w-full p-3 gap-2 shadow-none border {accent_classes}"):
                                    ui.label(title).classes("text-sm font-bold text-blue-950")
                                    if not isinstance(snapshot, dict) or not snapshot:
                                        ui.label("该历史记录未保存方案内容快照").classes(
                                            "text-xs text-slate-400 italic"
                                        )
                                        return
                                    snapshot_fields = [
                                        ("项目", snapshot_projects(snapshot)),
                                        ("变更对象", table_item_title(snapshot)),
                                        ("对应变更要求", snapshot_requirements(snapshot)),
                                        ("对应影响勾选", snapshot_impacts(snapshot)),
                                        ("当前内容", snapshot_old_content(snapshot)),
                                        ("执行后结果", snapshot_new_content(snapshot)),
                                    ]
                                    snapshot_is_material = (
                                        classify_ecn_change_item(snapshot) == ECN_SCHEME_GROUP_MATERIAL
                                    )
                                    snapshot_has_tracking = bool(snapshot.get("traceability_levels"))
                                    if snapshot_is_material or snapshot_has_tracking:
                                        snapshot_traceability_levels = snapshot.get("traceability_levels", [])
                                        snapshot_disposition_measure = snapshot.get("disposition_measure")
                                        snapshot_fields.append(
                                            (
                                                "追溯处置范围（多选）",
                                                ", ".join(map(str, snapshot_traceability_levels)) or "未配置",
                                            )
                                        )
                                        if snapshot_is_material and is_ecn_material_disposition_required(
                                            snapshot.get("change_type")
                                        ):
                                            snapshot_disposition_text = snapshot_disposition_measure or "未配置"
                                            snapshot_condition = str(
                                                snapshot.get("disposition_condition") or ""
                                            ).strip()
                                            if snapshot_condition:
                                                snapshot_disposition_text += f"\n条件：{snapshot_condition}"
                                            elif is_ecn_disposition_condition_required(snapshot_disposition_measure):
                                                snapshot_disposition_text += "\n条件：未填写"
                                            snapshot_fields.append(
                                                (
                                                    "旧料处置措施",
                                                    snapshot_disposition_text,
                                                )
                                            )
                                    with ui.grid(columns=2).classes("w-full gap-x-4 gap-y-2"):
                                        for field_label, field_value in snapshot_fields:
                                            with ui.column().classes("gap-0 min-w-0"):
                                                ui.label(field_label).classes("text-[10px] font-bold text-slate-400")
                                                ui.label(field_value).classes(
                                                    "text-xs text-slate-700 break-all whitespace-pre-wrap"
                                                )

                            def open_rejection_history_dialog(global_idx, item):
                                records = get_rejection_history(item)
                                if not records:
                                    return ui.notify("该方案暂无驳回记录", type="info")

                                rejection_history_dialog.clear()
                                with (
                                    rejection_history_dialog,
                                    ui.card().classes("w-[1100px] max-w-full max-h-[90vh] p-0 gap-0 overflow-hidden"),
                                ):
                                    with ui.row().classes(
                                        "w-full px-5 py-3 bg-slate-100 border-b border-slate-200 "
                                        "items-center justify-between shrink-0"
                                    ):
                                        with ui.column().classes("gap-0 min-w-0"):
                                            ui.label(f"方案 #{global_idx + 1:02d} · 驳回记录").classes(
                                                "text-lg font-bold text-blue-950"
                                            )
                                            ui.label(table_item_title(item)).classes("text-xs text-slate-500 break-all")
                                        ui.button(
                                            icon="close",
                                            on_click=rejection_history_dialog.close,
                                        ).props("flat round dense text-color=blue-grey-7")

                                    with ui.column().classes("w-full p-4 gap-3 overflow-y-auto"):
                                        for reverse_idx, record in enumerate(reversed(records), start=1):
                                            is_latest = reverse_idx == 1
                                            with ui.card().classes(
                                                "w-full p-3 gap-2 shadow-none border "
                                                + (
                                                    "border-red-200 bg-red-50"
                                                    if is_latest
                                                    else "border-slate-200 bg-white"
                                                )
                                            ):
                                                with ui.row().classes("w-full items-center justify-between gap-2"):
                                                    ui.label(
                                                        "最近一次驳回"
                                                        if is_latest
                                                        else f"历史驳回 {len(records) - reverse_idx + 1}"
                                                    ).classes(
                                                        "text-xs font-bold "
                                                        + ("text-red-700" if is_latest else "text-slate-600")
                                                    )
                                                    ui.label(record.get("time") or "时间未记录").classes(
                                                        "text-xs text-slate-500"
                                                    )
                                                ui.label(record.get("note") or "未填写驳回意见").classes(
                                                    "text-sm text-slate-800 break-all whitespace-pre-wrap"
                                                )
                                                ui.label(
                                                    f"审核人：{record.get('reviewer') or '未记录'}"
                                                    + (
                                                        f"（{record.get('reviewer_role')}）"
                                                        if record.get("reviewer_role")
                                                        else ""
                                                    )
                                                ).classes("text-xs text-slate-500")
                                                with ui.grid(columns=2).classes("w-full gap-3 mt-1 items-stretch"):
                                                    render_scheme_snapshot(
                                                        "改进前方案",
                                                        record.get("before_snapshot", {}),
                                                        "border-red-200 bg-red-50/40",
                                                    )
                                                    render_scheme_snapshot(
                                                        "改进后方案",
                                                        record.get("after_snapshot", {}),
                                                        "border-green-200 bg-green-50/40",
                                                    )

                                    with ui.row().classes(
                                        "w-full px-4 py-3 border-t border-slate-200 justify-end shrink-0"
                                    ):
                                        ui.button("关闭", on_click=rejection_history_dialog.close).props(
                                            "flat color=blue-grey-7"
                                        )
                                rejection_history_dialog.open()

                            def render_table_item(global_idx, item, display_row_idx):
                                status_label, status_icon, status_class, _ = table_status_view(item)
                                review_status = item.get("review_status", ECN_ITEM_STATUS_NORMAL)
                                row_background = "bg-amber-50/50" if display_row_idx % 2 == 0 else "bg-blue-50/50"
                                row_accent = (
                                    "border-l-red-500"
                                    if review_status == ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
                                    else "border-l-amber-400"
                                    if review_status == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
                                    else "border-l-transparent"
                                )
                                # 控制每行内容与顺序
                                with ui.column().classes(f"w-full gap-0 border-l {row_accent}"):
                                    with (
                                        ui.element("div")
                                        .classes(
                                            "w-full min-h-[50px] border-b border-slate-300 "
                                            f"{row_background} hover:bg-slate-100 "
                                            "items-stretch transition-colors duration-100"
                                        )
                                        .style(table_grid_style)
                                    ):
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex items-center justify-center"
                                        ):
                                            ui.label(f"#{global_idx + 1:02d}").classes(
                                                "text-sm font-bold text-slate-500"
                                            )
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex flex-col items-center justify-center min-w-0"
                                        ):
                                            render_table_parameter(item)
                                        with ui.element("div").classes(
                                            "px-1 py-0 border-l border-slate-200 flex flex-col items-center justify-center"
                                        ):
                                            render_table_projects(item)
                                        with ui.element("div").classes(
                                            "px-1 py-0 border-l border-slate-200 flex flex-col items-center justify-center min-w-0"
                                        ):
                                            render_table_action(item)
                                        with ui.element("div").classes(
                                            "px-1 py-0 border-l border-slate-200 flex flex-col justify-center min-w-0"
                                        ):
                                            render_table_old_value(item)
                                        with ui.element("div").classes(
                                            "px-1 py-0 border-l border-slate-200 flex flex-col justify-center min-w-0"
                                        ):
                                            render_table_new_value(item)
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex flex-col items-center justify-center min-w-0"
                                        ):
                                            render_table_traceability(item)
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex flex-col items-center justify-center min-w-0"
                                        ):
                                            render_table_disposition(item)
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex flex-col items-center justify-center min-w-0"
                                        ):
                                            render_table_requirements(item)
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex flex-col items-center justify-center min-w-0"
                                        ):
                                            render_table_impacts(item)
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex items-center justify-center"
                                        ):
                                            author = str(item.get("author") or "未知")
                                            ui.label(author).classes("text-sm text-slate-700 text-center break-all")
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-slate-200 flex items-center justify-center"
                                        ):
                                            with ui.row().classes(f"items-center justify-center gap-1 {status_class}"):
                                                ui.icon(status_icon).classes("text-lg")
                                                ui.label(status_label).classes("text-xs font-bold text-center")
                                        with ui.element("div").classes(
                                            "px-2 py-1 border-l border-r border-slate-200 flex items-center justify-center"
                                        ):
                                            can_edit_item = (
                                                is_scheming_phase
                                                and item.get("author") == current_user
                                                and participants.get(current_user) != ECN_PARTICIPANT_STATUS_CONFIRMED
                                            )
                                            has_rejection_history = bool(get_rejection_history(item))
                                            if can_edit_item or has_rejection_history:
                                                with ui.row().classes("gap-0 flex-nowrap"):
                                                    if has_rejection_history:
                                                        ui.button(
                                                            icon="history",
                                                            on_click=lambda _, idx=global_idx, i=item: (
                                                                open_rejection_history_dialog(idx, i)
                                                            ),
                                                        ).props(
                                                            "flat round dense text-color=blue-grey-7 size=sm"
                                                        ).tooltip("查看驳回记录")
                                                    if can_edit_item:
                                                        ui.button(
                                                            icon="edit",
                                                            on_click=lambda _, i=item: (
                                                                open_overview_change_dialog(
                                                                    local_data,
                                                                    current_user,
                                                                    handle_save_item,
                                                                    i,
                                                                )
                                                                if i.get("type") == "overview_update"
                                                                else open_text_change_dialog(
                                                                    local_data,
                                                                    current_user,
                                                                    handle_save_item,
                                                                    i,
                                                                    i.get(
                                                                        "scheme_category",
                                                                        ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
                                                                    ),
                                                                )
                                                            ),
                                                        ).props(
                                                            "flat round dense text-color=blue-grey-7 size=sm"
                                                        ).tooltip("编辑方案")
                                                        ui.button(
                                                            icon="delete_outline",
                                                            on_click=lambda _, i=item: remove_item(i),
                                                        ).props("flat round dense text-color=red-5 size=sm").tooltip(
                                                            "删除方案"
                                                        )
                                            else:
                                                ui.icon("more_horiz").classes("text-slate-300")

                            # 控制折叠栏顺序
                            for group_type in [
                                ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
                                ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
                                ECN_SCHEME_GROUP_MATERIAL,
                                ECN_SCHEME_GROUP_UNKNOWN,
                            ]:
                                items_in_group = grouped_items[group_type]
                                if not items_in_group:
                                    continue

                                group_title, group_icon = group_configs[group_type]
                                completed = sum(1 for _, item in items_in_group if table_status_view(item)[3])
                                pending = len(items_in_group) - completed
                                group_expansion = (
                                    ui.expansion(
                                        f"{group_title}  {len(items_in_group)} 项",
                                        caption=f"{completed} 已完成 · {pending} 待处理",
                                        icon=group_icon,
                                        value=scheme_group_expansion_state[group_type],
                                    )
                                    .classes("w-full bg-white border border-slate-200 rounded-lg mb-2 overflow-hidden")
                                    .props('header-class="text-blue-950 text-base font-bold bg-slate-300"')
                                )
                                group_expansion.on_value_change(
                                    lambda e, group=group_type: scheme_group_expansion_state.__setitem__(
                                        group,
                                        bool(e.value),
                                    )
                                )
                                with group_expansion:
                                    with ui.element("div").classes("w-full overflow-x-auto"):
                                        with ui.column().classes("w-full gap-0"):
                                            with (
                                                ui.element("div")
                                                .classes(
                                                    "w-full min-h-[42px] bg-slate-100 border-y "
                                                    "border-slate-200 items-stretch"
                                                )
                                                .style(table_grid_style)
                                            ):
                                                # 控制表头内容及顺序
                                                for header, extra_classes in [
                                                    ("编号", "justify-center"),
                                                    ("变更对象/类别", "justify-center"),
                                                    ("项目", "justify-center"),
                                                    ("动作", "justify-center"),
                                                    ("当前内容", ""),
                                                    ("执行后结果", ""),
                                                    ("追溯处置范围", "justify-center"),
                                                    ("旧料处置措施", "justify-center"),
                                                    ("对应变更要求", "justify-center"),
                                                    ("对应影响勾选项", "justify-center"),
                                                    ("编制", "justify-center"),
                                                    ("方案状态", "justify-center"),
                                                    ("操作", "justify-center border-r"),
                                                ]:
                                                    with ui.element("div").classes(
                                                        "px-2 py-1 border-l border-slate-300 "
                                                        f"flex items-center {extra_classes}"
                                                    ):
                                                        ui.label(header).classes("text-sm font-bold text-slate-600")
                                            for display_row_idx, (global_idx, item) in enumerate(items_in_group):
                                                render_table_item(
                                                    global_idx,
                                                    item,
                                                    display_row_idx,
                                                )

                    async def remove_item(item_to_remove):
                        result = await edit_scheme(
                            local_data["ecn_id"],
                            copy.deepcopy(local_data),
                            None,
                            copy.deepcopy(item_to_remove),
                            current_user,
                            current_role,
                            delete=True,
                        )
                        if not result.ok or result.record is None:
                            ui.notify(result.message, type="warning")
                            return
                        apply_scheme_result(result.record)

                    render_items()

    return render_parts, render_my_actions, render_items, render_coverage_dashboard
