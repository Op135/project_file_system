# -*- encoding: utf-8 -*-
import copy
import os
import uuid

from nicegui import (
    app,
    ui,
)

from ... import (
    db_storage,
)
from ...custom_ui import (
    custom_upload,
)
from ...ecn_management_config import (
    ECN_DISPOSITION_MEASURES,
    ECN_DOCUMENT_CHANGE_TYPES,
    ECN_MATERIAL_CHANGE_TYPE_ADD,
    ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY,
    ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE,
    ECN_MATERIAL_CHANGE_TYPE_REPLACE,
    ECN_MATERIAL_CHANGE_TYPES,
    ECN_MATERIAL_DEFAULT_UNIT,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_LABELS,
    ECN_OVERVIEW_ACTION_UPDATE,
    ECN_OVERVIEW_CONFLICT_AUTO_CLOSE_SECONDS,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_TRACEABILITY_LEVELS,
    build_overview_validation_signature,
    collect_ecn_pending_overview_overrides,
    ecn_overview_requires_new_content,
    expand_new_material_traceability_selection,
    get_active_overview_row_contents,
    get_ecn_material_change_missing_fields,
    get_ecn_scheme_target_projects,
    is_ecn_disposition_condition_required,
    is_ecn_material_disposition_required,
    resolve_ecn_overview_parameter_config,
)


def render_association_checkboxes(title, options, state, state_key, on_selection_change=None):
    """将方案关联项直接展开为复选框，并保持列表字段的数据结构不变。"""
    entries = list(options.items()) if isinstance(options, dict) else [(option, option) for option in options]
    ui.label(title).classes("text-xs font-medium text-slate-600 mt-1")
    if not entries:
        ui.label("暂无可关联项").classes("text-xs text-slate-400 italic")
        return

    selected_values = state.setdefault(state_key, [])
    with ui.element("div").classes(
        "w-full grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-1 rounded border border-slate-200 bg-white px-2 py-1"
    ):
        for option_value, option_label in entries:

            def update_selection(e, value=option_value):
                current_values = state.setdefault(state_key, [])
                if e.value and value not in current_values:
                    current_values.append(value)
                elif not e.value and value in current_values:
                    current_values.remove(value)
                if on_selection_change:
                    on_selection_change()

            ui.checkbox(
                str(option_label),
                value=option_value in selected_values,
                on_change=update_selection,
            ).props("dense color=primary").classes("w-full text-sm items-start")


def render_traceability_checkboxes(state, state_key="traceability_levels"):
    """平铺追溯范围复选框；新勾选后级时只在该次操作中自动补选前级。"""
    selected_values = state.setdefault(state_key, [])
    selection_state = {"previous": copy.deepcopy(selected_values), "syncing": False}
    checkbox_controls = {}

    def sync_checkbox_values(values):
        selection_state["syncing"] = True
        try:
            selected_set = set(values)
            for level, checkbox in checkbox_controls.items():
                should_select = level in selected_set
                if bool(checkbox.value) != should_select:
                    checkbox.set_value(should_select)
        finally:
            selection_state["syncing"] = False

    with ui.element("div").classes(
        "w-full grid grid-cols-4 md:grid-cols-8 gap-x-4 gap-y-1 rounded border border-slate-200 bg-white px-2 py-1"
    ):
        for level in ECN_TRACEABILITY_LEVELS:

            def update_traceability(e, selected_level=level):
                if selection_state["syncing"]:
                    return
                current_values = list(state.setdefault(state_key, []))
                if e.value and selected_level not in current_values:
                    current_values.append(selected_level)
                elif not e.value and selected_level in current_values:
                    current_values.remove(selected_level)
                expanded_values = expand_new_material_traceability_selection(
                    current_values,
                    selection_state["previous"],
                )
                state[state_key] = expanded_values
                selection_state["previous"] = copy.deepcopy(expanded_values)
                sync_checkbox_values(expanded_values)

            checkbox_controls[level] = (
                ui.checkbox(
                    level,
                    value=level in selected_values,
                    on_change=update_traceability,
                )
                .props("dense color=primary")
                .classes("w-full text-sm items-start")
            )


def open_overview_change_dialog(ecn_data, current_user, on_save_callback, edit_item=None):
    dialog = ui.dialog().props("persistent")
    expected_item = copy.deepcopy(edit_item)
    new_item_id = str(uuid.uuid4())
    is_edit = edit_item is not None
    # 编辑过程必须使用隔离草稿，避免输入框通过双向绑定提前污染已保存方案。
    edit_data = copy.deepcopy(edit_item) if is_edit else {}

    traceability_levels = copy.deepcopy(edit_data.get("traceability_levels", []))
    initial_projects = copy.deepcopy(edit_data.get("projects", []))
    initial_project_states = copy.deepcopy(edit_data.get("project_states", {}))
    initial_config, initial_processing_type = resolve_ecn_overview_parameter_config(
        app.storage.general.get("over_config_data_flat", {}),
        edit_data.get("label"),
    )

    sel_state = {
        "projects": initial_projects,
        "role": edit_data.get("role"),
        "label": edit_data.get("label"),
        "project_states": initial_project_states,
        "new_data": copy.deepcopy(edit_data.get("new_data", {})) if is_edit else {},
        "req_idxs": copy.deepcopy(edit_data.get("req_idxs", [])),
        "linked_docs": copy.deepcopy(edit_data.get("linked_docs", [])),
        # 彻底废弃 linked_materials
        "config": initial_config,
        "processing_type": initial_processing_type,
        "is_valid": is_edit,
        "validated_url": edit_data.get("new_data", {}).get("url_path", ""),
        "validated_file_type": edit_data.get("new_data", {}).get("file_type", ""),
        "validated_local_file_path": edit_data.get("new_data", {}).get("local_file_path", ""),
        "first_col_label": edit_data.get("first_col_label", ""),
        "has_enabled_bool": True,
        "auto_open_warning_key": None,
        "auto_shown_warning_keys": set(),
        "validated_signature": None,
        "validated_project_files": {
            project: copy.deepcopy(project_state.get("new_file_data", {}))
            for project, project_state in initial_project_states.items()
            if isinstance(project_state, dict) and isinstance(project_state.get("new_file_data"), dict)
        },
        "traceability_levels": traceability_levels,
    }

    path_validation_types = {"search", "svn"}

    def get_current_validation_signature():
        return build_overview_validation_signature(
            sel_state["processing_type"],
            sel_state["new_data"].get("content", ""),
            sel_state["projects"],
            sel_state["role"],
            sel_state["label"],
        )

    def invalidate_path_validation():
        sel_state["is_valid"] = False
        sel_state["validated_url"] = ""
        sel_state["validated_file_type"] = ""
        sel_state["validated_local_file_path"] = ""
        sel_state["validated_signature"] = None
        sel_state["validated_project_files"] = {}
        for project_state in sel_state["project_states"].values():
            if isinstance(project_state, dict):
                project_state.pop("new_file_data", None)

    def invalidate_path_validation_if_changed(candidate_signature=None):
        """忽略 NiceGUI 对相同值的补发事件，只在已校验内容确实变化时作废。"""
        validated_signature = sel_state.get("validated_signature")
        if validated_signature is None:
            return
        current_signature = candidate_signature or get_current_validation_signature()
        if current_signature != validated_signature:
            invalidate_path_validation()

    if is_edit and sel_state["processing_type"] in path_validation_types:
        projects_requiring_new_content = [
            project
            for project in sel_state["projects"]
            if sel_state["project_states"].get(project, {}).get("action") != ECN_OVERVIEW_ACTION_DEACTIVATE
        ]
        has_all_svn_results = (
            sel_state["processing_type"] == "svn"
            and bool(projects_requiring_new_content)
            and all(
                sel_state["validated_project_files"].get(project, {}).get("url_path")
                for project in projects_requiring_new_content
            )
        )
        if sel_state["validated_url"] or has_all_svn_results:
            sel_state["validated_signature"] = get_current_validation_signature()
        else:
            sel_state["is_valid"] = False

    target_projects = get_ecn_scheme_target_projects(ecn_data)
    roles = list(app.storage.general.get("over_config_data", {}).keys())
    req_options = {req["idx"]: f"[{req['idx']}] {req['content']}" for req in ecn_data["basic_info"]["requirements"]}
    req_docs = [k for k, v in ecn_data["review_info"]["involved_docs"].items() if v]

    def get_labels(r):
        return {
            i["label"]: f"{i.get('title', '未命名')}"
            for gl in app.storage.general.get("over_config_data", {}).get(r, {}).values()
            for i in gl
        }

    def get_first_col_label(r, current_label):
        groups = app.storage.general.get("over_config_data", {}).get(r, {})
        for group_configs in groups.values():
            for cfg in group_configs:
                if cfg.get("label") == current_label:
                    return group_configs[0].get("label")
        return current_label

    def get_chips_for_project(p, ll):
        req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(p, "1.0")
        chips = {}
        raw_data = db_storage.get_deep_item([f"{p}_over_data", ll], {})
        # 遍历chip数据
        for c_id, c in raw_data.items():
            if c.get("select_activ_dic", {}).get(req_max_ver) is True:
                chips[c_id] = c.get("content", "")
        return chips

    def get_existing_cell_contents(project, label, anchor_row_id, include_all_active=False):
        """返回新增位置的当前内容及本单已暂存内容。"""
        result = []
        if include_all_active:
            for content in get_chips_for_project(project, label).values():
                entry = ("当前已有", content)
                if entry not in result:
                    result.append(entry)
        elif anchor_row_id and not str(anchor_row_id).startswith("PENDING_NEW_"):
            req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "1.0")
            raw_data = db_storage.get_deep_item([f"{project}_over_data", label], {})
            for content in get_active_overview_row_contents(raw_data, anchor_row_id, req_max_ver):
                entry = ("当前已有", content)
                if entry not in result:
                    result.append(entry)

        editing_item_id = edit_data.get("item_id")
        for change_item in ecn_data.get("change_items", []):
            if change_item.get("item_id") == editing_item_id:
                continue
            if change_item.get("type") != "overview_update" or change_item.get("label") != label:
                continue
            project_state = change_item.get("project_states", {}).get(project, {})
            if project_state.get("action") != ECN_OVERVIEW_ACTION_ADD:
                continue
            if project_state.get("anchor_row_id") != anchor_row_id:
                continue
            content = str(change_item.get("new_data", {}).get("content", "")).strip() or "（空内容）"
            if ("本单已暂存", content) not in result:
                result.append(("本单已暂存", content))
        return result

    dialog.clear()
    with dialog, ui.card().classes("w-[1000px] max-w-full max-h-[90vh] flex flex-col flex-nowrap"):
        ui.label("修改系统内资料变更方案" if is_edit else "添加系统内资料变更方案").classes(
            "text-lg font-bold text-blue-900 shrink-0"
        )

        with ui.element("div").classes("w-full flex-1 min-h-0 overflow-y-auto pr-2"):
            with ui.column().classes("w-full gap-2"):
                # === 区域 1：对应关联卡片 (仅保留资料关联) ===
                with ui.card().classes("w-full p-3 bg-gray-50 border border-gray-200 shadow-sm gap-2"):
                    ui.label("对应关联 (必填)").classes("text-xs font-bold text-indigo-700")
                    render_association_checkboxes(
                        "目标项目（必选）",
                        target_projects,
                        sel_state,
                        "projects",
                        on_selection_change=lambda: (
                            invalidate_path_validation_if_changed(),
                            build_matrix_and_sync_state(),
                        ),
                    )
                    render_association_checkboxes("对应解决的变更要求", req_options, sel_state, "req_idxs")
                    if req_docs:
                        render_association_checkboxes(
                            "对应勾选的文档/图纸项",
                            req_docs,
                            sel_state,
                            "linked_docs",
                        )

                # === 区域 2：技术维度选择 ===
                with ui.grid(columns=2).classes("w-full gap-2 mt-2 items-start"):
                    sel_role = ui.select(options=roles, label="1. 技术维度", value=sel_state["role"]).classes("w-full")
                    sel_label = ui.select(
                        options=get_labels(sel_state["role"]) if sel_state["role"] else {},
                        label="2. 具体参数",
                        value=sel_state["label"],
                    ).classes("w-full")

                with ui.card().classes("w-full p-3 mt-2 bg-slate-50 border border-slate-200 shadow-none gap-2"):
                    ui.label("追溯处置范围（选填）").classes("text-xs font-bold text-slate-700")
                    render_traceability_checkboxes(sel_state)

                # === 区域 3：多项目配置矩阵 ===
                matrix_container = (
                    ui.column()
                    .classes("w-full gap-1 mt-2 border border-blue-100 rounded bg-white p-2")
                    .style("display: none;")
                )

                def build_matrix_and_sync_state():
                    matrix_container.clear()
                    projects = sel_state["projects"] or []
                    sel_state["projects"] = projects
                    role = sel_role.value
                    label = sel_label.value
                    sel_state["has_enabled_bool"] = True

                    keys_to_remove = [p for p in sel_state["project_states"] if p not in projects]
                    for p in keys_to_remove:
                        del sel_state["project_states"][p]

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
                            ui.label("处理方式（新增 / 更换 / 失效）")
                            ui.label("绑定基准行" if not is_first_col else "")

                        for p in projects:
                            p_state = sel_state["project_states"].setdefault(
                                p,
                                {
                                    "action": ECN_OVERVIEW_ACTION_ADD,
                                    "chip_id": "NEW",
                                    "anchor_row_id": None,
                                    "old_data": {},
                                },
                            )
                            chips_options = get_chips_for_project(p, label)

                            display_options = {
                                ECN_OVERVIEW_ACTION_ADD: f"[{ECN_OVERVIEW_ACTION_LABELS['add']}] 不覆盖原数据"
                            }
                            for chip_id, content in chips_options.items():
                                display_options[f"{ECN_OVERVIEW_ACTION_UPDATE}::{chip_id}"] = (
                                    f"[{ECN_OVERVIEW_ACTION_LABELS['update']}] {content}"
                                )
                                display_options[f"{ECN_OVERVIEW_ACTION_DEACTIVATE}::{chip_id}"] = (
                                    f"[{ECN_OVERVIEW_ACTION_LABELS['deactivate']}] {content}"
                                )

                            selected_action = p_state.get("action")
                            selected_chip_id = p_state.get("chip_id")
                            current_selection = (
                                ECN_OVERVIEW_ACTION_ADD
                                if selected_action == ECN_OVERVIEW_ACTION_ADD
                                else f"{selected_action}::{selected_chip_id}"
                            )
                            if current_selection not in display_options:
                                if chips_options:
                                    p_state["chip_id"] = list(chips_options.keys())[-1]
                                    p_state["action"] = ECN_OVERVIEW_ACTION_UPDATE
                                else:
                                    p_state["chip_id"] = "NEW"
                                    p_state["action"] = ECN_OVERVIEW_ACTION_ADD
                                current_selection = (
                                    ECN_OVERVIEW_ACTION_ADD
                                    if p_state["action"] == ECN_OVERVIEW_ACTION_ADD
                                    else f"{p_state['action']}::{p_state['chip_id']}"
                                )

                            if p_state["action"] != ECN_OVERVIEW_ACTION_ADD:
                                p_state["old_data"] = db_storage.get_deep_item(
                                    [f"{p}_over_data", label, p_state["chip_id"]], {}
                                )
                            else:
                                p_state["old_data"] = {}

                            if p_state["action"] == ECN_OVERVIEW_ACTION_ADD and (
                                is_first_col or p_state.get("anchor_row_id")
                            ):
                                existing_contents = get_existing_cell_contents(
                                    p,
                                    label,
                                    p_state.get("anchor_row_id"),
                                    include_all_active=is_first_col,
                                )
                                p_state["existing_contents"] = [
                                    {"source": source, "content": content} for source, content in existing_contents
                                ]
                            else:
                                p_state["existing_contents"] = []

                            with ui.grid().classes(
                                "w-full grid-cols-[120px_1fr_1fr] items-center border-b border-dashed border-gray-200 pb-1 gap-2"
                            ):
                                ui.label(p).classes("text-sm font-bold text-gray-700 break-all pr-2")

                                def on_chip_select(e, current_p=p):
                                    val = e.value
                                    state = sel_state["project_states"][current_p]
                                    if val == ECN_OVERVIEW_ACTION_ADD:
                                        state["chip_id"] = "NEW"
                                        state["action"] = ECN_OVERVIEW_ACTION_ADD
                                        state["old_data"] = {}
                                    else:
                                        action, chip_id = str(val).split("::", 1)
                                        state["chip_id"] = chip_id
                                        state["action"] = action
                                        state["old_data"] = db_storage.get_deep_item(
                                            [f"{current_p}_over_data", sel_state["label"], chip_id], {}
                                        )
                                    if state["action"] == ECN_OVERVIEW_ACTION_ADD and not is_first_col:
                                        state["anchor_row_id"] = None
                                    elif state["action"] != ECN_OVERVIEW_ACTION_ADD:
                                        state["anchor_row_id"] = None
                                    if sel_state["processing_type"] in path_validation_types:
                                        invalidate_path_validation()
                                    else:
                                        sel_state["is_valid"] = False
                                        sel_state["validated_url"] = ""
                                    render_dynamic_form()
                                    build_matrix_and_sync_state()

                                ui.select(
                                    options=display_options,
                                    value=current_selection,
                                    on_change=on_chip_select,
                                ).props("dense outlined bg-white").classes("w-full")

                                anchor_container = ui.element("div").classes("w-full")
                                with anchor_container:
                                    if p_state["action"] == ECN_OVERVIEW_ACTION_ADD and not is_first_col:

                                        def get_chips_for_project_with_pending(proj, label_str):
                                            c_opts = get_chips_for_project(proj, label_str)
                                            for c_item in ecn_data.get("change_items", []):
                                                if (
                                                    c_item.get("type") == "overview_update"
                                                    and c_item.get("label") == label_str
                                                ):
                                                    sub_states = c_item.get("project_states", {})
                                                    if proj in sub_states:
                                                        action = sub_states[proj].get("action")
                                                        raw_content = c_item.get("new_data", {}).get(
                                                            "content", "暂无内容"
                                                        )
                                                        display_content = str(raw_content)
                                                        if len(display_content) > 50:
                                                            display_content = display_content[:50] + "..."

                                                        if action == ECN_OVERVIEW_ACTION_ADD:
                                                            virtual_id = f"PENDING_NEW_{c_item['item_id']}"
                                                            c_opts[virtual_id] = f"[本单暂存新增] {display_content}"
                                                        elif action == ECN_OVERVIEW_ACTION_UPDATE:
                                                            old_chip_id = sub_states[proj].get("chip_id")
                                                            if old_chip_id and old_chip_id in c_opts:
                                                                c_opts[old_chip_id] = (
                                                                    f"[本单暂存变更] {display_content}"
                                                                )
                                                        elif action == ECN_OVERVIEW_ACTION_DEACTIVATE:
                                                            c_opts.pop(sub_states[proj].get("chip_id"), None)
                                            return c_opts

                                        first_col_chips = get_chips_for_project_with_pending(
                                            p, sel_state["first_col_label"]
                                        )

                                        def on_anchor_select(e, current_p=p):
                                            if not e.value:
                                                sel_state["project_states"][current_p]["anchor_row_id"] = None
                                            elif str(e.value).startswith("PENDING_NEW_"):
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
                                            sel_state["auto_open_warning_key"] = (
                                                current_p,
                                                sel_state["label"],
                                                sel_state["project_states"][current_p]["anchor_row_id"],
                                            )
                                            build_matrix_and_sync_state()

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
                                            existing_contents = [
                                                (
                                                    entry.get("source", "当前已有"),
                                                    entry.get("content", ""),
                                                )
                                                for entry in p_state.get("existing_contents", [])
                                            ]
                                            with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                                                anchor_select = (
                                                    ui.select(
                                                        options=first_col_chips,
                                                        value=current_anchor_chip_id,
                                                        label="选择绑定的第一列基准行",
                                                        on_change=on_anchor_select,
                                                    )
                                                    .props("dense outlined bg-amber-50")
                                                    .classes("flex-1 min-w-0")
                                                )
                                                if existing_contents:
                                                    parameter_title = get_labels(role).get(label, label)
                                                    anchor_select.classes("border border-red-300 rounded")
                                                    warning_key = (p, label, p_state.get("anchor_row_id"))
                                                    with (
                                                        ui.button(icon="warning")
                                                        .props("flat round dense color=negative")
                                                        .classes("shrink-0")
                                                    ):
                                                        ui.tooltip("该基准行的具体参数已有数据，点击查看").classes(
                                                            "text-xs"
                                                        )
                                                        with (
                                                            ui.menu()
                                                            .props('anchor="bottom right" self="top right"')
                                                            .classes("max-w-[480px]") as warning_menu
                                                        ):
                                                            with ui.column().classes(
                                                                "min-w-[320px] max-w-[480px] gap-2 p-3 bg-red-50"
                                                            ):
                                                                ui.label(
                                                                    f"⚠ 该基准行的「{parameter_title}」已有数据"
                                                                ).classes("text-sm font-bold text-red-700")
                                                                ui.label(
                                                                    "继续新增后，同一格将出现多个数据；"
                                                                    "如业务确有需要，仍可继续保存。"
                                                                ).classes("text-xs text-red-600")
                                                                ui.separator()
                                                                for source, content in existing_contents:
                                                                    with ui.column().classes("w-full gap-0"):
                                                                        ui.label(source).classes(
                                                                            "text-[10px] font-bold text-red-500"
                                                                        )
                                                                        ui.label(content).classes(
                                                                            "text-xs text-gray-800 break-all"
                                                                        )

                                                    if (
                                                        sel_state.get("auto_open_warning_key") == warning_key
                                                        and warning_key not in sel_state["auto_shown_warning_keys"]
                                                    ):
                                                        sel_state["auto_shown_warning_keys"].add(warning_key)
                                                        sel_state["auto_open_warning_key"] = None

                                                        def auto_open_warning(menu=warning_menu):
                                                            try:
                                                                menu.open()
                                                            except RuntimeError:
                                                                return

                                                            def auto_close_warning():
                                                                try:
                                                                    menu.close()
                                                                except RuntimeError:
                                                                    pass

                                                            ui.timer(
                                                                ECN_OVERVIEW_CONFLICT_AUTO_CLOSE_SECONDS,
                                                                auto_close_warning,
                                                                once=True,
                                                            )

                                                        ui.timer(0.15, auto_open_warning, once=True)

                    render_dynamic_form()

                def on_role_change(e):
                    sel_state["role"] = e.value
                    invalidate_path_validation_if_changed()
                    sel_label.set_options(get_labels(e.value))
                    sel_label.set_value(None)
                    sel_state["label"] = None
                    sel_state["project_states"].clear()
                    build_matrix_and_sync_state()

                sel_role.on_value_change(on_role_change)

                def on_label_change(e):
                    sel_state["label"] = e.value
                    sel_state["config"], sel_state["processing_type"] = resolve_ecn_overview_parameter_config(
                        app.storage.general.get("over_config_data_flat", {}),
                        e.value,
                    )
                    if (
                        sel_state["processing_type"] in path_validation_types
                        and sel_state.get("validated_signature") is None
                    ):
                        invalidate_path_validation()
                    else:
                        invalidate_path_validation_if_changed()
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
                    requires_new_content = ecn_overview_requires_new_content(sel_state["project_states"])
                    dialog_placeholder = str(config.get("dialog_placeholder") or "")
                    if (
                        requires_new_content
                        and ptype in {"text", "test"}
                        and dialog_placeholder
                        and not str(sel_state["new_data"].get("content") or "").strip()
                    ):
                        # 与项目概述的新增控件保持一致：空内容时带入配置的格式示例。
                        sel_state["new_data"]["content"] = dialog_placeholder

                    with dynamic_form_container:
                        ui.label(f"检测到对应的业务数据类型为: {ptype.upper()}").classes(
                            "text-xs font-bold text-teal-700 bg-teal-50 px-2 py-1 rounded w-fit"
                        )

                        with ui.grid(columns=2).classes("w-full gap-4"):
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

                                        if p_state.get("action") == ECN_OVERVIEW_ACTION_ADD:
                                            ui.label("将作为全新节点添加").classes(
                                                "text-xs font-bold text-orange-500 bg-orange-50 px-1 rounded"
                                            )
                                        else:
                                            old_d = p_state.get("old_data", {})
                                            with ui.column().classes("gap-0 flex-1"):
                                                ui.label(old_d.get("content", "无")).classes(
                                                    "text-sm text-gray-700 break-all"
                                                )
                                                action_label = ECN_OVERVIEW_ACTION_LABELS.get(
                                                    p_state.get("action"),
                                                    p_state.get("action", ""),
                                                )
                                                ui.label(action_label).classes(
                                                    "text-[10px] font-semibold text-slate-500"
                                                )
                                                if ptype == "test":
                                                    old_test = old_d.get("test_select_data", {})
                                                    text_str = f"性质: {old_test.get('test_nature_select', '')} | 状态: {old_test.get('state_select', '')} | 节点: {old_test.get('node_select', '')} | 工具: {old_test.get('instrument_select', '')}"
                                                    ui.label(text_str).classes("text-[10px] text-gray-500")

                            with ui.card().classes("w-full bg-blue-50 shadow-inner p-3 border border-blue-100"):
                                ui.label("统一方案 / 新内容 (必填)" if requires_new_content else "失效说明").classes(
                                    "text-xs text-blue-700 font-bold mb-2"
                                )

                                if not requires_new_content:
                                    sel_state["is_valid"] = True
                                    ui.label("所选项目均只失效原概述，不会添加对应的新概述。").classes(
                                        "text-sm text-slate-600"
                                    )
                                elif ptype == "text":
                                    ui.textarea(
                                        label=str(config.get("dialog_label") or "新文本内容"),
                                        placeholder=dialog_placeholder,
                                    ).bind_value(sel_state["new_data"], "content").classes("w-full").props(
                                        "outlined auto-grow rows=2 bg-white"
                                    )
                                    sel_state["is_valid"] = True

                                elif ptype == "test":
                                    ui.textarea(
                                        label="新检测内容与标准",
                                        placeholder=dialog_placeholder,
                                    ).bind_value(sel_state["new_data"], "content").classes("w-full").props(
                                        "outlined auto-grow rows=2 bg-white"
                                    )
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
                                        file_name_input = (
                                            ui.input(
                                                label=str(config.get("dialog_label") or "新引用文件名"),
                                                placeholder=dialog_placeholder or "填入包括后缀的完整文件名",
                                            )
                                            .bind_value(sel_state["new_data"], "content")
                                            .props("outlined dense bg-white")
                                            .classes("flex-grow")
                                        )

                                        def on_file_name_change(e):
                                            # 不依赖双向绑定与回调的执行先后，显式记录输入框最新值。
                                            sel_state["new_data"]["content"] = e.value or ""
                                            candidate_signature = build_overview_validation_signature(
                                                ptype,
                                                sel_state["new_data"]["content"],
                                                sel_state["projects"],
                                                sel_state["role"],
                                                sel_state["label"],
                                            )
                                            invalidate_path_validation_if_changed(candidate_signature)

                                        file_name_input.on_value_change(on_file_name_change)

                                        async def validate_path():
                                            # 以当前控件值为唯一依据，避免编辑态重绘后的绑定字典滞后。
                                            val = str(file_name_input.value or "").strip()
                                            sel_state["new_data"]["content"] = val
                                            if not val:
                                                return ui.notify("请先填写文件名", type="warning")
                                            requested_signature = build_overview_validation_signature(
                                                ptype,
                                                val,
                                                sel_state["projects"],
                                                sel_state["role"],
                                                sel_state["label"],
                                            )
                                            from ...utils import validate_search_path, validate_svn_url

                                            project_results = {}
                                            local_file_path = ""
                                            if ptype == "search":
                                                primary_proj = sel_state["projects"][0] if sel_state["projects"] else ""
                                                pending_overrides = collect_ecn_pending_overview_overrides(
                                                    ecn_data.get("change_items", []),
                                                    primary_proj,
                                                    edit_data.get("item_id"),
                                                )
                                                (
                                                    is_valid,
                                                    url,
                                                    ftype,
                                                    local_file_path,
                                                    msg,
                                                ) = await validate_search_path(
                                                    val, config, sel_state["projects"], pending_overrides
                                                )
                                            else:
                                                project_errors = []
                                                projects_to_validate = [
                                                    project
                                                    for project in sel_state["projects"]
                                                    if sel_state["project_states"].get(project, {}).get("action")
                                                    != ECN_OVERVIEW_ACTION_DEACTIVATE
                                                ]
                                                exempt_projects = [
                                                    project
                                                    for project in sel_state["projects"]
                                                    if sel_state["project_states"].get(project, {}).get("action")
                                                    == ECN_OVERVIEW_ACTION_DEACTIVATE
                                                ]
                                                for project in projects_to_validate:
                                                    pending_overrides = collect_ecn_pending_overview_overrides(
                                                        ecn_data.get("change_items", []),
                                                        project,
                                                        edit_data.get("item_id"),
                                                    )
                                                    (
                                                        project_is_valid,
                                                        project_url,
                                                        project_file_type,
                                                        project_message,
                                                    ) = await validate_svn_url(
                                                        val,
                                                        config,
                                                        [project],
                                                        pending_overrides,
                                                    )
                                                    if project_is_valid:
                                                        project_state = (
                                                            app.storage.general.get("project_summary", {})
                                                            .get(project, {})
                                                            .get("state", "")
                                                        )
                                                        project_results[project] = {
                                                            "url_path": project_url,
                                                            "file_type": project_file_type,
                                                            "warehouse": config.get("state_path", {}).get(
                                                                project_state
                                                            ),
                                                        }
                                                    else:
                                                        project_errors.append(f"{project}：{project_message}")
                                                is_valid = bool(projects_to_validate) and not project_errors
                                                if is_valid:
                                                    first_result = project_results[projects_to_validate[0]]
                                                    url = first_result.get("url_path", "")
                                                    ftype = first_result.get("file_type", "")
                                                    msg = (
                                                        f"全部 {len(projects_to_validate)} 个项目的 "
                                                        "SVN 文件均校验通过！"
                                                    )
                                                    if exempt_projects:
                                                        msg += (
                                                            f"\n另有 {len(exempt_projects)} 个项目选择失效、不产生新内容，"
                                                            "无需校验：" + "、".join(exempt_projects)
                                                        )
                                                else:
                                                    url, ftype = "", ""
                                                    msg = "SVN逐项目校验未通过：\n" + "\n".join(project_errors)
                                                    if exempt_projects:
                                                        msg += (
                                                            f"\n以下 {len(exempt_projects)} 个项目选择失效、"
                                                            "不产生新内容，已免检：" + "、".join(exempt_projects)
                                                        )

                                            if requested_signature != get_current_validation_signature():
                                                invalidate_path_validation()
                                                return ui.notify(
                                                    "校验期间文件名或目标项目发生变化，请重新校验。",
                                                    type="warning",
                                                )

                                            if is_valid:
                                                sel_state["is_valid"] = True
                                                sel_state["validated_url"] = url
                                                sel_state["validated_file_type"] = ftype
                                                sel_state["validated_local_file_path"] = (
                                                    local_file_path if ptype == "search" else ""
                                                )
                                                sel_state["validated_project_files"] = (
                                                    project_results if ptype == "svn" else {}
                                                )
                                                sel_state["validated_signature"] = requested_signature
                                                ui.notify(
                                                    msg,
                                                    type="positive",
                                                    multi_line=True,
                                                )
                                            else:
                                                invalidate_path_validation()
                                                ui.notify(
                                                    msg,
                                                    type="negative",
                                                    multi_line=True,
                                                )

                                        ui.button("校验有效性", on_click=validate_path).props(
                                            "color=primary outline dense"
                                        )
                                        ui.icon("check_circle", color="green", size="sm").bind_visibility_from(
                                            sel_state, "is_valid"
                                        )

                                elif ptype in ["file", "image", "video"]:
                                    ui.label("上传新文件").classes("text-xs text-gray-500 mb-1")

                                    async def handle_upload(e):
                                        from ...config import FILES_URL_DIR, UPLOADS_DIR

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

                                    def handle_upload_removed():
                                        sel_state["new_data"]["content"] = ""
                                        invalidate_path_validation()

                                    custom_upload(
                                        on_upload=handle_upload,
                                        on_removed=handle_upload_removed,
                                    ).props("accept=*/*")
                                    ui.label().bind_text_from(
                                        sel_state["new_data"],
                                        "content",
                                        backward=lambda x: f"暂存文件: {x}" if x else "",
                                    ).classes("text-sm text-green-600 mt-1")

                                ui.label("注: 原因和记录将被系统自动接管").classes(
                                    "text-[10px] text-gray-400 mt-2 block"
                                )

                if is_edit or sel_state["projects"]:
                    build_matrix_and_sync_state()

        async def save_item():
            if not sel_state["projects"]:
                return ui.notify("请至少选择一个目标项目", type="warning")
            if not sel_state["has_enabled_bool"]:
                return ui.notify("缺少第一列的基准数据，请先为基准列添加方案！", type="warning")
            requires_new_content = ecn_overview_requires_new_content(sel_state["project_states"])
            if (
                requires_new_content
                and sel_state["processing_type"] in path_validation_types
                and sel_state.get("validated_signature") != get_current_validation_signature()
            ):
                invalidate_path_validation()
                return ui.notify("文件名或校验上下文已变化，请重新校验有效性。", type="warning")
            if requires_new_content and not sel_state["is_valid"]:
                return ui.notify("未完成文件/路径校验，或数据不合法，请先点击校验有效性。", type="warning")
            if requires_new_content and not sel_state["new_data"].get("content", "").strip():
                return ui.notify("请完善新内容", type="warning")
            has_traceability = bool(sel_state["traceability_levels"])

            is_first_col = sel_state["label"] == sel_state["first_col_label"]
            for p, p_state in sel_state["project_states"].items():
                if p_state["action"] == ECN_OVERVIEW_ACTION_ADD and not is_first_col and not p_state["anchor_row_id"]:
                    return ui.notify(f"项目 [{p}] 作为新增项，必须绑定第一列基准行！", type="warning")

            if requires_new_content and sel_state["processing_type"] in [
                "search",
                "svn",
                "file",
                "image",
                "video",
            ]:
                if sel_state["processing_type"] == "svn":
                    sel_state["new_data"].pop("url_path", None)
                    sel_state["new_data"].pop("file_type", None)
                    sel_state["new_data"].pop("warehouse", None)
                    for project, project_state in sel_state["project_states"].items():
                        project_state.pop("new_file_data", None)
                        project_file_data = sel_state["validated_project_files"].get(project)
                        if project_file_data:
                            project_state["new_file_data"] = copy.deepcopy(project_file_data)
                else:
                    sel_state["new_data"]["url_path"] = sel_state["validated_url"]
                    sel_state["new_data"]["file_type"] = sel_state["validated_file_type"]
                    if sel_state["processing_type"] == "search" and sel_state["validated_local_file_path"]:
                        sel_state["new_data"]["local_file_path"] = sel_state["validated_local_file_path"]
                    else:
                        sel_state["new_data"].pop("local_file_path", None)

            sel_state["new_data"].pop("notes", None)

            payload = {
                "item_id": edit_data.get("item_id", new_item_id),
                "type": "overview_update",
                "scheme_category": ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
                "author": current_user,
                "req_idxs": sel_state["req_idxs"],
                "linked_docs": sel_state["linked_docs"],
                "linked_materials": [],  # 彻底置空
                "projects": copy.deepcopy(sel_state["projects"]),
                "role": sel_state["role"],
                "label": sel_state["label"],
                "first_col_label": sel_state["first_col_label"],
                "project_states": copy.deepcopy(sel_state["project_states"]),
                "new_data": copy.deepcopy(sel_state["new_data"]) if requires_new_content else {},
                "config_processing_type": sel_state["processing_type"],
                "execute_status": "pending",
            }
            if has_traceability:
                payload["traceability_levels"] = copy.deepcopy(sel_state["traceability_levels"])
            if await on_save_callback(payload, is_edit, expected_item):
                dialog.close()

        with ui.row().classes("w-full justify-end mt-4 shrink-0"):
            ui.button("取消", on_click=dialog.close).props("flat color=grey")
            ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")

    dialog.on("close", dialog.delete)
    dialog.open()


def open_text_change_dialog(
    ecn_data,
    current_user,
    on_save_callback,
    edit_item=None,
    scheme_category=ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
):
    dialog = ui.dialog().props("persistent")
    expected_item = copy.deepcopy(edit_item)
    new_item_id = str(uuid.uuid4())
    is_edit = edit_item is not None
    edit_data = edit_item or {}

    if is_edit:
        scheme_category = edit_data.get("scheme_category", scheme_category)
    is_document_scheme = scheme_category == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT
    is_material_scheme = scheme_category == ECN_SCHEME_GROUP_MATERIAL
    is_optional_tracking_scheme = is_document_scheme
    traceability_levels = copy.deepcopy(edit_data.get("traceability_levels", []))
    material_change = copy.deepcopy(edit_data.get("material_change", {}))
    if not isinstance(material_change, dict):
        material_change = {}
    for unit_key in ("unit", "old_unit", "new_unit"):
        material_change.setdefault(unit_key, ECN_MATERIAL_DEFAULT_UNIT)
    initial_change_type = edit_data.get(
        "change_type",
        ECN_DOCUMENT_CHANGE_TYPES[-1] if is_document_scheme else ECN_MATERIAL_CHANGE_TYPE_ADD,
    )
    if is_document_scheme and initial_change_type not in ECN_DOCUMENT_CHANGE_TYPES:
        initial_change_type = ECN_DOCUMENT_CHANGE_TYPES[-1]
    if is_material_scheme and initial_change_type not in ECN_MATERIAL_CHANGE_TYPES:
        initial_change_type = ECN_MATERIAL_CHANGE_TYPE_ADD

    initial_file_server_path = str(edit_data.get("file_server_path") or "").strip()

    sel_state = {
        "projects": copy.deepcopy(edit_data.get("projects", [])),
        "req_idxs": edit_data.get("req_idxs", []),
        "linked_docs": edit_data.get("linked_docs", []) if is_document_scheme else [],
        "linked_materials": (
            edit_data.get("linked_materials", []) if scheme_category == ECN_SCHEME_GROUP_MATERIAL else []
        ),
        "change_type": initial_change_type,
        "material_change": material_change,
        "traceability_levels": traceability_levels,
        "disposition_measure": edit_data.get("disposition_measure") if is_material_scheme else None,
        "disposition_condition": edit_data.get("disposition_condition", ""),
        "provide_file_server_path": bool(initial_file_server_path),
        "file_server_path": initial_file_server_path,
    }

    req_options = {req["idx"]: f"[{req['idx']}] {req['content']}" for req in ecn_data["basic_info"]["requirements"]}
    req_docs = [k for k, v in ecn_data["review_info"]["involved_docs"].items() if v]
    target_projects = get_ecn_scheme_target_projects(ecn_data)
    req_mats = [
        f"{mat}-{act}"
        for mat, actions in ecn_data["review_info"]["involved_materials"].items()
        if isinstance(actions, dict)
        for act, val in actions.items()
        if val
    ]

    dialog.clear()
    with dialog, ui.card().classes("w-[900px] max-w-full"):
        dialog_title = "其它特定事项/资料变更方案" if is_document_scheme else "物料变更方案"
        ui.label(f"修改{dialog_title}" if is_edit else f"添加{dialog_title}").classes("text-lg font-bold text-blue-900")

        with ui.card().classes("w-full p-3 bg-gray-50 border border-gray-200 shadow-sm gap-2 mt-2"):
            ui.label("对应关联 (必填)").classes("text-xs font-bold text-indigo-700")
            render_association_checkboxes(
                "目标项目（必选）",
                target_projects,
                sel_state,
                "projects",
            )
            render_association_checkboxes("对应解决的变更要求", req_options, sel_state, "req_idxs")
            # 类别隔离控制显示
            if is_document_scheme and req_docs:
                render_association_checkboxes(
                    "对应勾选的文档/图纸项",
                    req_docs,
                    sel_state,
                    "linked_docs",
                )
            if scheme_category == ECN_SCHEME_GROUP_MATERIAL and req_mats:
                render_association_checkboxes(
                    "对应勾选的物料动作",
                    req_mats,
                    sel_state,
                    "linked_materials",
                )

        # 根据类别控制可用分类
        type_options = ECN_DOCUMENT_CHANGE_TYPES if is_document_scheme else list(ECN_MATERIAL_CHANGE_TYPES)
        if is_document_scheme:
            with ui.card().classes("w-full p-3 mt-4 bg-slate-50 border border-slate-200 shadow-none gap-1"):
                ui.label("方案分类（必选）").classes("text-xs font-bold text-slate-700")
                type_select = (
                    ui.radio(type_options)
                    .classes("w-full")
                    .props("inline dense color=primary")
                    .bind_value(sel_state, "change_type")
                )
        else:
            type_select = (
                ui.select(type_options, label="方案分类（必选）")
                .classes("w-56 mt-4")
                .bind_value(sel_state, "change_type")
            )

        material_form_container = ui.column().classes("w-full gap-2")
        disposition_container = ui.column().classes("w-full gap-1")

        def render_material_change_form():
            if not is_material_scheme:
                material_form_container.set_visibility(False)
                return
            material_form_container.set_visibility(True)
            material_form_container.clear()
            change_type = sel_state["change_type"]
            material_state = sel_state["material_change"]
            with material_form_container:
                with ui.card().classes("w-full p-3 bg-blue-50/50 border border-blue-200 shadow-none gap-2"):
                    ui.label(f"{change_type}物料信息").classes("text-xs font-bold text-blue-900")
                    if change_type in [ECN_MATERIAL_CHANGE_TYPE_ADD, ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE]:
                        with ui.grid(columns=3).classes("w-full gap-3"):
                            ui.input("物料名称（必填）").classes("w-full").bind_value(
                                material_state, "material_name"
                            ).props("outlined dense bg-white")
                            ui.number("用量（必填）").classes("w-full").bind_value(material_state, "quantity").props(
                                "outlined dense bg-white step=any"
                            )
                            ui.input("单位（必填）").classes("w-full").bind_value(material_state, "unit").props(
                                "outlined dense bg-white"
                            )
                    elif change_type == ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY:
                        with ui.grid(columns=4).classes("w-full gap-3"):
                            ui.input("物料名称（必填）").classes("w-full").bind_value(
                                material_state, "material_name"
                            ).props("outlined dense bg-white")
                            ui.number("改前用量（必填）").classes("w-full").bind_value(
                                material_state, "old_quantity"
                            ).props("outlined dense bg-white step=any")
                            ui.number("改后用量（必填）").classes("w-full").bind_value(
                                material_state, "new_quantity"
                            ).props("outlined dense bg-white step=any")
                            ui.input("单位（必填）").classes("w-full").bind_value(material_state, "unit").props(
                                "outlined dense bg-white"
                            )
                    elif change_type == ECN_MATERIAL_CHANGE_TYPE_REPLACE:
                        ui.label("改前物料").classes("text-[11px] font-bold text-slate-500")
                        with ui.grid(columns=3).classes("w-full gap-3"):
                            ui.input("改前物料名称（必填）").classes("w-full").bind_value(
                                material_state, "old_material_name"
                            ).props("outlined dense bg-white")
                            ui.number("改前用量（必填）").classes("w-full").bind_value(
                                material_state, "old_quantity"
                            ).props("outlined dense bg-white step=any")
                            ui.input("改前单位（必填）").classes("w-full").bind_value(material_state, "old_unit").props(
                                "outlined dense bg-white"
                            )
                        ui.label("改后物料").classes("text-[11px] font-bold text-slate-500 mt-1")
                        with ui.grid(columns=3).classes("w-full gap-3"):
                            ui.input("改后物料名称（必填）").classes("w-full").bind_value(
                                material_state, "new_material_name"
                            ).props("outlined dense bg-white")
                            ui.number("改后用量（必填）").classes("w-full").bind_value(
                                material_state, "new_quantity"
                            ).props("outlined dense bg-white step=any")
                            ui.input("改后单位（必填）").classes("w-full").bind_value(material_state, "new_unit").props(
                                "outlined dense bg-white"
                            )

        def on_change_type(e):
            sel_state["change_type"] = e.value
            if is_material_scheme and not is_ecn_material_disposition_required(e.value):
                sel_state["disposition_measure"] = None
                sel_state["disposition_condition"] = ""
            render_material_change_form()
            render_disposition_field()

        type_select.on_value_change(on_change_type)
        render_material_change_form()

        if is_material_scheme or is_optional_tracking_scheme:
            tracking_card_classes = (
                "w-full p-3 mt-2 bg-amber-50/60 border border-amber-200 shadow-none gap-2"
                if is_material_scheme
                else "w-full p-3 mt-2 bg-slate-50 border border-slate-200 shadow-none gap-2"
            )
            with ui.card().classes(tracking_card_classes):
                tracking_title = "物料追溯处置范围（必填）" if is_material_scheme else "追溯处置范围（选填）"
                ui.label(tracking_title).classes(
                    "text-xs font-bold " + ("text-amber-900" if is_material_scheme else "text-slate-700")
                )
                render_traceability_checkboxes(sel_state)

        def render_disposition_field():
            disposition_container.clear()
            if not is_material_scheme or not is_ecn_material_disposition_required(sel_state["change_type"]):
                disposition_container.set_visibility(False)
                return
            disposition_container.set_visibility(True)
            with disposition_container:
                with ui.card().classes("w-full p-3 bg-amber-50/60 border border-amber-200 shadow-none gap-1"):
                    ui.label("旧料处置措施（必填）").classes("text-xs font-bold text-amber-900")
                    disposition_select = (
                        ui.radio(ECN_DISPOSITION_MEASURES)
                        .classes("w-full")
                        .bind_value(sel_state, "disposition_measure")
                        .props("inline dense color=primary")
                    )
                    condition_container = ui.column().classes("w-full gap-0")

                    def render_disposition_condition():
                        condition_container.clear()
                        if not is_ecn_disposition_condition_required(sel_state["disposition_measure"]):
                            sel_state["disposition_condition"] = ""
                            condition_container.set_visibility(False)
                            return
                        condition_container.set_visibility(True)
                        with condition_container:
                            ui.input("具体使用条件（必填）").classes("w-full").bind_value(
                                sel_state, "disposition_condition"
                            ).props("outlined dense bg-white")

                    def on_disposition_change(e):
                        sel_state["disposition_measure"] = e.value
                        render_disposition_condition()

                    disposition_select.on_value_change(on_disposition_change)
                    render_disposition_condition()

        render_disposition_field()

        old_content_ui = None
        new_content_ui = None
        if is_document_scheme:
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

            with ui.card().classes("w-full p-3 bg-slate-50 border border-slate-200 shadow-none gap-1"):
                provide_server_path_checkbox = (
                    ui.checkbox("提供文件服务器存放路径说明（可选）")
                    .classes("text-sm text-slate-700")
                    .bind_value(sel_state, "provide_file_server_path")
                )
                server_path_container = ui.column().classes("w-full gap-1")

                def render_server_path_input():
                    server_path_container.clear()
                    if not sel_state["provide_file_server_path"]:
                        server_path_container.set_visibility(False)
                        return
                    server_path_container.set_visibility(True)
                    with server_path_container:
                        ui.input("文件服务器存放路径（必填）").classes("w-full").bind_value(
                            sel_state,
                            "file_server_path",
                        ).props("outlined dense bg-white")

                def on_provide_server_path_change(e):
                    sel_state["provide_file_server_path"] = bool(e.value)
                    render_server_path_input()

                provide_server_path_checkbox.on_value_change(on_provide_server_path_change)
                render_server_path_input()

        async def save_item():
            old_content = ""
            new_content = ""
            if not sel_state["projects"]:
                return ui.notify("请至少选择一个目标项目", type="warning")
            if is_material_scheme and not sel_state["traceability_levels"]:
                return ui.notify("请至少选择一项物料方案的追溯处置范围", type="warning")
            if (
                is_material_scheme
                and is_ecn_material_disposition_required(sel_state["change_type"])
                and not sel_state["disposition_measure"]
            ):
                return ui.notify("请选择旧料处置措施", type="warning")
            if (
                is_material_scheme
                and is_ecn_material_disposition_required(sel_state["change_type"])
                and is_ecn_disposition_condition_required(sel_state["disposition_measure"])
                and not sel_state["disposition_condition"].strip()
            ):
                return ui.notify("请填写旧料处置的具体使用条件", type="warning")
            if is_material_scheme:
                missing_fields = get_ecn_material_change_missing_fields(
                    sel_state["change_type"], sel_state["material_change"]
                )
                if missing_fields:
                    return ui.notify("请填写：" + "、".join(missing_fields), type="warning")
            else:
                assert old_content_ui is not None and new_content_ui is not None
                if not old_content_ui.value.strip() or not new_content_ui.value.strip():
                    return ui.notify("原内容与新内容均不能为空", type="warning")
                if sel_state["provide_file_server_path"] and not sel_state["file_server_path"].strip():
                    return ui.notify("请填写文件服务器存放路径", type="warning")
                old_content = old_content_ui.value.strip()
                new_content = new_content_ui.value.strip()
            payload = {
                "item_id": edit_data.get("item_id", new_item_id),
                "type": "text_desc",
                "scheme_category": scheme_category,  # 明确注入分类
                "author": current_user,
                "projects": copy.deepcopy(sel_state["projects"]),
                "req_idxs": sel_state["req_idxs"],
                "linked_docs": sel_state["linked_docs"],
                "linked_materials": sel_state["linked_materials"],
                "change_type": sel_state["change_type"],
                "execute_status": "manual_record",
            }
            if is_material_scheme or sel_state["traceability_levels"]:
                payload["traceability_levels"] = copy.deepcopy(sel_state["traceability_levels"])
            if is_material_scheme and is_ecn_material_disposition_required(sel_state["change_type"]):
                payload["disposition_measure"] = sel_state["disposition_measure"]
                if is_ecn_disposition_condition_required(sel_state["disposition_measure"]):
                    payload["disposition_condition"] = sel_state["disposition_condition"].strip()
            if is_material_scheme:
                payload["material_change"] = copy.deepcopy(sel_state["material_change"])
            else:
                payload["old_content"] = old_content
                payload["new_content"] = new_content
                if sel_state["provide_file_server_path"]:
                    payload["file_server_path"] = sel_state["file_server_path"].strip()
            if await on_save_callback(payload, is_edit, expected_item):
                dialog.close()

        with ui.row().classes("w-full justify-end mt-4"):
            ui.button("取消", on_click=dialog.close).props("flat color=grey")
            ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")
    dialog.on("close", dialog.delete)
    dialog.open()
