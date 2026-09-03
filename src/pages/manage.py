# -*- encoding: utf-8 -*-
import copy
import json
import logging
import os
from datetime import datetime

from nicegui import app, run, ui
from nicegui.events import UploadEventArguments

from ..access_control import can
from ..approval_workflow import (
    APPROVAL_WORKFLOW_EVENTS,
    APPROVER_STRATEGY_NAMES,
    get_approval_workflow_editor_nodes,
    import_design_knowledge_legacy_workflows,
    import_ecn_legacy_workflows,
    import_project_overview_legacy_workflows,
    import_sample_issue_legacy_workflows,
    resolve_approval_workflow,
)
from ..config import BASE_DIR, IMG_DIR, PRESET_AVATARS
from ..identity_codes import STABLE_CODE_HINT, normalize_stable_code, validate_stable_code
from ..overview_permission_mapping import (
    OVERVIEW_ITEM_PERMISSION_PREFIX,
    build_overview_position_permission_plan,
    collect_legacy_overview_role_usage,
    load_overview_permission_source,
    normalize_overview_role_position_mapping,
)
from ..requirement_overview_impact import (
    REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY,
    RequirementOverviewImpactConfigError,
    load_requirement_overview_impact_config,
    save_requirement_overview_impact_config,
)
from ..utils import (
    get_cache_busted_path,
    get_temp_config_service,
    logout,
    online_users,
    project_summary_update,
    project_table_update_config_update,
    setup_global_activity_tracking,
    sync_current_user_role,
    updata_overview_config,
    update_config_service,
    update_users_data,
)
from ..wecom_service import load_wecom_contacts_cache, sync_wecom_contacts

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/manage")
def manage_page():
    current_user = app.storage.user.get("current_user")
    if not current_user:
        ui.navigate.to("/login")
        return
    current_role = sync_current_user_role()
    # 分阶段迁移期间保留 admin 紧急入口，避免错误授权导致管理端锁死。
    if current_user != "admin" and not can(
        app.state.user_service,
        current_user,
        "system.manage",
        legacy_role=current_role,
        legacy_allowed_roles=["admin"],
    ):
        ui.navigate.to("/main")  # 如果不是管理员，跳转到主界面
        return

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

    def attach_stable_code_check(code_input, existing_codes_provider, entity_name):
        """为新建编码输入框增加格式提示和实时查重。"""
        status_label = ui.label().classes("text-xs text-gray-500 -mt-2 mb-1")

        def get_error():
            normalized = normalize_stable_code(code_input.value)
            format_error = validate_stable_code(normalized)
            if format_error:
                return format_error
            existing_codes = {
                normalize_stable_code(value)
                for value in existing_codes_provider()
                if normalize_stable_code(value)
            }
            if normalized in existing_codes:
                return f"{entity_name}编码已存在：{normalized}"
            return ""

        def refresh_status():
            normalized = normalize_stable_code(code_input.value)
            if not normalized:
                status_label.set_text(f"格式：{STABLE_CODE_HINT}；输入大写会自动保存为小写")
                status_label.classes(
                    remove="text-red-600 text-green-600",
                    add="text-gray-500",
                )
                return
            error = get_error()
            if error:
                status_label.set_text(error)
                status_label.classes(
                    remove="text-gray-500 text-green-600",
                    add="text-red-600",
                )
            else:
                status_label.set_text(f"编码可用，将保存为：{normalized}")
                status_label.classes(
                    remove="text-gray-500 text-red-600",
                    add="text-green-600",
                )

        code_input.on_value_change(lambda _event: refresh_status())
        refresh_status()
        return get_error

    # --- 定义备份处理函数 ---
    async def handle_manual_backup():
        # 1. 获取 main.py 中初始化的管理器实例
        manager = getattr(app.state, "backup_manager", None)

        if not manager:
            ui.notify("错误：备份服务未初始化，请检查服务器启动日志", type="negative")
            return

        # 2. 显示正在处理的提示 (spinner=True)
        notification = ui.notification("正在执行全量备份 (JSON + SQLite)...", timeout=None, spinner=True)

        try:
            # 3. 调用安全备份方法 (注意要用 await)
            # 传入触发类型 "MANUAL_ADMIN" 以便在日志中区分
            await manager.run_safe_backup("MANUAL_ADMIN")

            # 4. 成功反馈
            notification.dismiss()  # 关闭加载提示
            logger.info("成功备份了数据库文件。")
            ui.notify("备份成功！文件已保存至 backups 目录", type="positive", icon="check_circle")

        except Exception as e:
            # 5. 失败反馈
            notification.dismiss()
            logger.error(f"备份数据库文件失败：{e}")
            ui.notify(f"备份失败: {str(e)}", type="negative")

    # --- 关联影响编辑模块 (最终修复版：解决 .disable() 赋值为 None 的问题) ---
    def open_impact_editor():
        config_path = os.path.join(BASE_DIR, "overview_config.json")

        # 容器
        config_data = {}
        item_map = {}
        label_to_key_map = {}

        # 1. 读取数据并建立索引
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)

            for role, groups in config_data.items():
                for group_name, chip_data_li in groups.items():
                    for chip_data in chip_data_li:
                        if isinstance(chip_data, dict):
                            item_map[chip_data.get("title")] = chip_data
                            if "label" in chip_data:
                                label_to_key_map[chip_data["label"]] = chip_data.get("title")
                            if "impact_list" not in chip_data:
                                chip_data["impact_list"] = []

        except Exception as e:
            ui.notify(f"读取配置文件失败: {e}", type="negative")
            return

        # 2. UI 布局与逻辑
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-6xl h-auto p-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("配置项关联影响编辑器").classes("text-xl font-bold")
                ui.icon("close", size="sm").classes("cursor-pointer").on("click", dialog.close)

            ui.separator().classes("mb-4")

            # 使用 Grid 布局将界面分为左右两部分
            with ui.grid(columns=2).classes("w-full gap-8"):
                # === 左侧：选择主控项 (Source) ===
                with ui.column().classes("w-full border p-4 rounded bg-blue-50"):
                    ui.label("1. 选择主配置项 (发起影响)").classes("font-bold text-blue-900")

                    # 修正：先定义变量，不要直接链式调用 .disable()，否则变量会变成 None
                    main_cat = ui.select(options=list(config_data.keys()), label="分类", with_input=True).classes(
                        "w-full"
                    )

                    main_group = ui.select(options=[], label="分组", with_input=True).classes("w-full")
                    main_group.disable()  # 分行写禁用

                    main_item = ui.select(options=[], label="具体配置项", with_input=True).classes("w-full")
                    main_item.disable()  # 分行写禁用

                # === 右侧：选择受控项 (Target) ===
                with ui.column().classes("w-full border p-4 rounded bg-green-50"):
                    ui.label("2. 选择受影响项 (被影响对象)").classes("font-bold text-green-900")

                    target_cat = ui.select(options=list(config_data.keys()), label="分类", with_input=True).classes(
                        "w-full"
                    )

                    target_group = ui.select(options=[], label="分组", with_input=True).classes("w-full")
                    target_group.disable()  # 分行写禁用

                    target_item = ui.select(options=[], label="具体配置项", with_input=True).classes("w-full")
                    target_item.disable()  # 分行写禁用

                    add_btn = ui.button("添加关联 ↓", icon="add_link").classes("w-full bg-green-700 text-white mt-2")
                    add_btn.disable()  # 分行写禁用

            # === 下方：显示列表 ===
            ui.label("当前主控项已关联的影响列表:").classes("text-sm text-gray-600 mt-6 font-bold")
            impact_display_container = ui.row().classes(
                "w-full min-h-12 border border-dashed border-gray-400 rounded p-2 bg-gray-50 gap-2"
            )

            # --- 逻辑处理函数 ---

            def refresh_chips():
                """刷新下方的标签列表"""
                impact_display_container.clear()

                # 此时 main_item 绝对是 ui.select 对象，不会是 None
                current_key = main_item.value

                if not current_key or current_key not in item_map:
                    with impact_display_container:
                        ui.label("请先在左侧选择完整的主配置项").classes("text-gray-400 italic")
                    return

                impact_list = item_map[current_key].get("impact_list", [])

                if not impact_list:
                    with impact_display_container:
                        ui.label("暂无关联项").classes("text-gray-400 italic")
                    return

                for label_val in impact_list:
                    display_text = label_to_key_map.get(label_val, label_val)
                    with impact_display_container:
                        ui.chip(display_text, removable=True, color="primary").on(
                            "remove", lambda lv=label_val: remove_impact(lv)
                        )

            def remove_impact(label_val):
                current_key = main_item.value
                if current_key and current_key in item_map:
                    try:
                        item_map[current_key]["impact_list"].remove(label_val)
                        refresh_chips()
                    except ValueError:
                        pass

            def do_add_link():
                m_key = main_item.value
                t_key = target_item.value

                if not m_key or not t_key:
                    return

                if m_key == t_key:
                    ui.notify("不能关联自己！", type="warning")
                    return

                t_node = item_map.get(t_key)
                if not t_node or "label" not in t_node:
                    ui.notify(f"错误：'{t_key}' 缺少 label 字段，无法关联", type="negative")
                    return

                target_label = t_node["label"]
                current_list = item_map[m_key]["impact_list"]

                if target_label in current_list:
                    ui.notify("该项已存在，请勿重复添加", type="warning")
                    return

                current_list.append(target_label)
                refresh_chips()
                ui.notify(f"已关联: {t_key}", type="positive")

            async def save_to_file():
                try:
                    with open(config_path, "w", encoding="utf-8") as f:
                        json.dump(config_data, f, ensure_ascii=False, indent=4)
                    ui.notify("所有更改已保存到文件！", type="positive")
                    dialog.close()
                except Exception as e:
                    ui.notify(f"保存失败: {e}", type="negative")

            # --- 事件联动逻辑 ---

            # === 左侧联动 (Main) ===
            def on_main_cat_change(e):
                cat = e.value
                main_group.value = None
                main_item.value = None

                if cat and cat in config_data:
                    groups = list(config_data[cat].keys())
                    main_group.options = groups
                    main_group.enable()
                else:
                    main_group.options = []
                    main_group.disable()

                main_item.disable()
                impact_display_container.clear()
                main_group.update()
                main_item.update()

            def on_main_group_change(e):
                grp = e.value
                cat = main_cat.value
                main_item.value = None

                if cat and grp and grp in config_data.get(cat, {}):
                    items = [dic.get("title", "未知标题") for dic in config_data[cat][grp]]
                    main_item.options = items
                    main_item.enable()
                else:
                    main_item.options = []
                    main_item.disable()

                impact_display_container.clear()
                main_item.update()

            def on_main_item_change(e):
                refresh_chips()

            # === 右侧联动 (Target) ===
            def on_target_cat_change(e):
                cat = e.value
                target_group.value = None
                target_item.value = None

                if cat and cat in config_data:
                    groups = list(config_data[cat].keys())
                    target_group.options = groups
                    target_group.enable()
                else:
                    target_group.options = []
                    target_group.disable()

                target_item.disable()
                add_btn.disable()
                target_group.update()
                target_item.update()
                add_btn.update()

            def on_target_group_change(e):
                grp = e.value
                cat = target_cat.value
                target_item.value = None

                if cat and grp and grp in config_data.get(cat, {}):
                    items = [dic.get("title", "未知标题") for dic in config_data[cat][grp]]
                    target_item.options = items
                    target_item.enable()
                else:
                    target_item.options = []
                    target_item.disable()

                add_btn.disable()
                target_item.update()
                add_btn.update()

            def on_target_item_change(e):
                if e.value:
                    add_btn.enable()
                else:
                    add_btn.disable()
                add_btn.update()

            # --- 绑定事件 ---
            main_cat.on_value_change(on_main_cat_change)
            main_group.on_value_change(on_main_group_change)
            main_item.on_value_change(on_main_item_change)

            target_cat.on_value_change(on_target_cat_change)
            target_group.on_value_change(on_target_group_change)
            target_item.on_value_change(on_target_item_change)

            add_btn.on_click(do_add_link)

            # --- 底部按钮区 ---
            with ui.row().classes("w-full justify-end mt-4 gap-4"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("保存并退出", on_click=save_to_file).props("icon=save color=blue")

        dialog.open()

    def open_requirement_overview_impact_editor():
        """编辑需求 node_id 与概述 label 的影响映射，并同步文件和运行时内存。"""
        over_config_flat = app.storage.general.get("over_config_data_flat", {})
        if not isinstance(over_config_flat, dict) or not over_config_flat:
            ui.notify("概述配置内存尚未加载，请先更新概述配置", type="negative")
            return

        valid_overview_labels = {str(label) for label in over_config_flat if label}
        memory_config = app.storage.general.get(REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY, {})
        source_name = "当前审批内存"
        try:
            source_config = load_requirement_overview_impact_config(
                memory_config if isinstance(memory_config, dict) else {},
                valid_overview_labels=valid_overview_labels,
            )
        except RequirementOverviewImpactConfigError as memory_exc:
            try:
                source_config = load_requirement_overview_impact_config(
                    valid_overview_labels=valid_overview_labels,
                )
                source_name = "配置文件（当前审批内存无效）"
            except RequirementOverviewImpactConfigError as file_exc:
                ui.notify(
                    f"影响配置无法编辑：内存错误：{memory_exc}；文件错误：{file_exc}",
                    type="negative",
                    multi_line=True,
                )
                return

        working_impacts = copy.deepcopy(source_config["node_impacts"])
        runtime_requirement_config = getattr(app.state, "init_config_data", {})
        raw_requirement_nodes = (
            runtime_requirement_config.get("data", {}) if isinstance(runtime_requirement_config, dict) else {}
        )
        requirement_nodes = raw_requirement_nodes if isinstance(raw_requirement_nodes, dict) else {}

        def node_sort_key(node_id):
            node_id_text = str(node_id)
            try:
                return 0, float(node_id_text), node_id_text
            except ValueError:
                return 1, 0.0, node_id_text

        def node_display(node_id):
            node = requirement_nodes.get(str(node_id), {})
            guide_content = node.get("guide_content", "") if isinstance(node, dict) else ""
            guide_text = str(guide_content).strip()
            if len(guide_text) > 70:
                guide_text = f"{guide_text[:70]}…"
            return f"{node_id} ｜ {guide_text or '当前需求内存中无说明'}"

        all_node_ids = {str(node_id) for node_id in requirement_nodes}
        all_node_ids.update(working_impacts)
        node_options = {node_id: node_display(node_id) for node_id in sorted(all_node_ids, key=node_sort_key)}

        overview_options = {}
        for label, item in sorted(
            over_config_flat.items(),
            key=lambda entry: (
                str(entry[1].get("role", "")) if isinstance(entry[1], dict) else "",
                str(entry[1].get("group_name", "")) if isinstance(entry[1], dict) else "",
                str(entry[1].get("title", entry[0])) if isinstance(entry[1], dict) else str(entry[0]),
            ),
        ):
            if not label:
                continue
            item_data = item if isinstance(item, dict) else {}
            title = str(item_data.get("title") or label)
            group_name = str(item_data.get("group_name") or "未分组")
            role = str(item_data.get("role") or "未分配角色")
            overview_options[str(label)] = f"{title} ｜ {group_name} ｜ {role} [{label}]"

        selected_node_id = next(iter(node_options), None)

        with ui.dialog() as dialog, ui.card().classes("w-[95vw] max-w-7xl h-[90vh] p-4 flex flex-col no-wrap"):
            with ui.row().classes("w-full items-center justify-between shrink-0"):
                with ui.column().classes("gap-0"):
                    ui.label("需求变动 → 概述待定影响配置").classes("text-xl font-bold")
                    ui.label(f"编辑来源：{source_name}；保存后立即供后续需求审批使用").classes("text-xs text-gray-500")
                ui.icon("close", size="sm").classes("cursor-pointer").on("click", dialog.close)

            ui.separator().classes("shrink-0")

            with ui.row().classes("w-full items-center gap-4 shrink-0"):
                policy_select = ui.radio(
                    options={
                        "all_overviews": "未配置 node_id：所有概述转待定（兼容/安全）",
                        "block": "未配置 node_id：阻止审批通过",
                    },
                    value=source_config["unmapped_policy"],
                ).props("inline")
                summary_label = ui.label().classes("ml-auto text-sm font-bold text-blue-700")

            with ui.grid(columns=2).classes("w-full flex-grow min-h-0 gap-4"):
                with ui.card().classes("w-full h-full min-h-0 p-3 flex flex-col no-wrap bg-blue-50"):
                    ui.label("编辑单个需求节点").classes("font-bold text-blue-900 shrink-0")
                    node_select = ui.select(
                        options=node_options,
                        value=selected_node_id,
                        label="需求节点（来自 app.state.init_config_data）",
                        with_input=True,
                    ).classes("w-full shrink-0")
                    impact_select = (
                        ui.select(
                            options=overview_options,
                            value=list(working_impacts.get(selected_node_id, [])) if selected_node_id else [],
                            label="会被置为待定的概述项",
                            multiple=True,
                            with_input=True,
                        )
                        .props("use-chips options-dense")
                        .classes("w-full")
                    )
                    ui.label("空列表表示该需求变动不影响任何概述；删除映射则会触发上方的“未配置”策略。").classes(
                        "text-xs text-gray-600"
                    )

                    with ui.row().classes("w-full gap-2 shrink-0"):
                        ui.button(
                            "全选概述",
                            on_click=lambda: impact_select.set_value(list(overview_options)),
                        ).props("flat dense icon=done_all")
                        ui.button(
                            "清空影响",
                            on_click=lambda: impact_select.set_value([]),
                        ).props("flat dense icon=clear_all")

                    with ui.row().classes("w-full gap-2 shrink-0"):
                        apply_button = ui.button("应用到草稿", icon="playlist_add_check").props("color=primary")
                        delete_button = ui.button("删除该映射", icon="delete").props("color=negative outline")

                with ui.card().classes("w-full h-full min-h-0 p-3 flex flex-col no-wrap"):
                    ui.label("当前草稿映射").classes("font-bold shrink-0")
                    mapping_container = ui.column().classes(
                        "w-full flex-grow min-h-0 overflow-y-auto gap-2 border rounded p-2 bg-gray-50"
                    )

            def load_selected_mapping():
                node_id = str(node_select.value or "").strip()
                impact_select.set_value(list(working_impacts.get(node_id, [])))
                delete_button.set_enabled(node_id in working_impacts)

            def edit_mapping(node_id):
                node_select.set_value(node_id)
                load_selected_mapping()

            def remove_mapping(node_id=None):
                target_node_id = str(node_id or node_select.value or "").strip()
                if not target_node_id or target_node_id not in working_impacts:
                    ui.notify("该节点当前没有显式映射", type="warning")
                    return
                working_impacts.pop(target_node_id)
                load_selected_mapping()
                render_mapping_list()
                ui.notify(f"已从草稿删除 node_id={target_node_id}，尚未保存", type="warning")

            def render_mapping_list():
                mapping_container.clear()
                summary_label.set_text(f"已显式配置 {len(working_impacts)} / {len(node_options)} 个需求节点")
                with mapping_container:
                    if not working_impacts:
                        ui.label("暂无显式映射").classes("text-gray-400 italic")
                        return
                    for node_id in sorted(working_impacts, key=node_sort_key):
                        labels = working_impacts[node_id]
                        with ui.expansion(
                            f"{node_display(node_id)}　（影响 {len(labels)} 项）",
                            icon="account_tree",
                        ).classes("w-full bg-white border rounded"):
                            if labels:
                                title_list = [overview_options.get(label, label) for label in labels]
                                ui.label("\n".join(title_list)).classes("text-xs whitespace-pre-line")
                            else:
                                ui.label("已显式配置为空：该节点变动不影响概述").classes("text-xs text-green-700")
                            with ui.row().classes("gap-2"):
                                ui.button(
                                    "编辑",
                                    on_click=lambda target=node_id: edit_mapping(target),
                                ).props("flat dense icon=edit")
                                ui.button(
                                    "从草稿删除",
                                    on_click=lambda target=node_id: remove_mapping(target),
                                ).props("flat dense color=negative icon=delete")

            def apply_current_mapping():
                node_id = str(node_select.value or "").strip()
                if not node_id:
                    ui.notify("请先选择需求节点", type="warning")
                    return
                selected_labels = impact_select.value if isinstance(impact_select.value, list) else []
                working_impacts[node_id] = [
                    label for label in overview_options if label in {str(value) for value in selected_labels}
                ]
                render_mapping_list()
                delete_button.enable()
                ui.notify(f"node_id={node_id} 已应用到草稿", type="positive")

            def save_all_changes(event):
                event.sender.disable()
                try:
                    normalized = save_requirement_overview_impact_config(
                        {
                            "schema_version": 1,
                            "unmapped_policy": policy_select.value,
                            "node_impacts": working_impacts,
                        },
                        valid_overview_labels=valid_overview_labels,
                        storage=app.storage.general,
                    )
                    working_impacts.clear()
                    working_impacts.update(copy.deepcopy(normalized["node_impacts"]))
                    logger.info(
                        "管理员 %s 更新需求与概述影响配置：node_id=%s，未配置策略=%s",
                        current_user,
                        len(working_impacts),
                        normalized["unmapped_policy"],
                    )
                    render_mapping_list()
                    ui.notify("配置文件与审批内存已同步更新，无需重启服务", type="positive")
                except RequirementOverviewImpactConfigError as exc:
                    logger.exception("管理员保存需求与概述影响配置失败")
                    ui.notify(f"保存失败，文件与内存已保持原状态：{exc}", type="negative", multi_line=True)
                finally:
                    event.sender.enable()

            node_select.on_value_change(lambda _: load_selected_mapping())
            apply_button.on_click(apply_current_mapping)
            delete_button.on_click(lambda: remove_mapping())
            load_selected_mapping()
            render_mapping_list()

            with ui.row().classes("w-full justify-end gap-3 shrink-0 pt-2"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("保存文件并同步内存", on_click=save_all_changes).props("icon=save color=primary")

        dialog.open()

    def open_user_migration_dialog():
        """迁移当前机器上的用户工作簿，不对其中密码作任何预设。"""
        user_svc = app.state.user_service
        mode_text = "身份数据库" if user_svc.storage_mode == "database" else "旧版 Excel"

        with ui.dialog() as migration_dialog, ui.card().classes("w-[42rem] max-w-[95vw] p-6"):
            ui.label("一键迁移用户到身份数据库").classes("text-xl font-bold")
            ui.label(f"当前模式：{mode_text}").classes("text-sm text-gray-600")
            ui.label(
                "系统读取当前部署机器上的 data/users.xlsx，将其中实际密码转换为不可逆哈希。"
                "不会写回或删除 Excel；迁移前会额外生成备份。"
            ).classes("text-sm leading-6")
            ui.label(
                "普通重复执行只补充新用户、同步兼容角色，不覆盖数据库中已有密码。因此本地测试密码不会被带到服务器。"
            ).classes("text-sm text-blue-800 bg-blue-50 rounded p-3")
            result_label = ui.label().classes("text-sm whitespace-pre-line")

            async def run_migration(event):
                event.sender.disable()
                notice = ui.notification("正在迁移并计算密码哈希...", timeout=None, spinner=True)
                try:
                    result = await run.io_bound(user_svc.migrate_legacy_users)
                    app.state.users_data = user_svc.load_users()
                    result_label.set_text(
                        f"迁移完成：总计 {result.total} 人；新增 {result.imported}；"
                        f"更新 {result.updated}；未变化 {result.unchanged}。\n"
                        f"Excel 备份：{result.backup_path or '未生成'}"
                    )
                    ui.notify("用户已切换到身份数据库。", type="positive")
                except Exception as exc:
                    logger.exception("用户迁移失败")
                    result_label.set_text(f"迁移失败：{exc}")
                    ui.notify(f"用户迁移失败：{exc}", type="negative", multi_line=True)
                finally:
                    notice.dismiss()
                    event.sender.enable()

            with ui.row().classes("w-full justify-end gap-3"):
                ui.button("关闭", on_click=migration_dialog.close).props("flat")
                ui.button("开始安全迁移", on_click=run_migration).props("color=primary icon=database")
        migration_dialog.open()

    def open_organization_management_dialog():
        user_svc = app.state.user_service
        if user_svc.storage_mode != "database":
            ui.notify("请先执行用户一键迁移，再维护组织架构。", type="warning")
            return

        with (
            ui.dialog().props("maximized") as org_dialog,
            ui.card().classes("w-full h-full p-5 flex flex-col no-wrap bg-gray-50"),
        ):
            with ui.row().classes("w-full items-center justify-between shrink-0"):
                with ui.column().classes("gap-0"):
                    ui.label("组织架构与岗位字典").classes("text-xl font-bold")
                    ui.label("部门上下级用于审批上交；岗位只描述任职，不直接等同于权限。").classes(
                        "text-xs text-gray-500"
                    )
                ui.button(icon="close", on_click=org_dialog.close).props("flat round dense")
            ui.separator()

            with ui.grid(columns=2).classes("w-full flex-grow min-h-0 gap-5"):
                with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("部门层级").classes("text-lg font-bold")
                        with ui.row().classes("gap-2"):
                            import_button = ui.button("导入企业微信部门", icon="cloud_download").props(
                                "outline color=teal"
                            )
                            add_org_button = ui.button("新增部门", icon="add").props("color=primary")
                    org_container = ui.column().classes("w-full flex-grow min-h-0 overflow-y-auto gap-1")

                with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("岗位字典").classes("text-lg font-bold")
                        with ui.row().classes("gap-2"):
                            import_position_button = ui.button(
                                "导入企业微信职务",
                                icon="cloud_download",
                            ).props("outline color=teal")
                            add_position_button = ui.button("新增岗位", icon="add").props("color=primary")
                    position_container = ui.column().classes("w-full flex-grow min-h-0 overflow-y-auto gap-1")

            expanded_position_departments: set[str] = set()

            def remember_position_department(event, department_key: str):
                """记录当前弹窗内已展开的岗位部门，首次打开时集合为空。"""
                if event.value:
                    expanded_position_departments.add(department_key)
                else:
                    expanded_position_departments.discard(department_key)

            def render_org_units():
                org_container.clear()
                with org_container:
                    units = user_svc.list_org_units()
                    if not units:
                        ui.label("尚无部门，可手工新增或从企业微信通讯录导入。").classes("text-gray-500 p-4")
                    unit_by_id = {
                        str(item["org_unit_id"]): item for item in units
                    }
                    children_by_parent: dict[str, list[dict]] = {}
                    root_units: list[dict] = []
                    for item in units:
                        parent_id = str(item.get("parent_org_unit_id") or "")
                        if parent_id and parent_id in unit_by_id:
                            children_by_parent.setdefault(parent_id, []).append(item)
                        else:
                            root_units.append(item)
                    rendered_unit_ids: set[str] = set()

                    def render_org_branch(item, depth=0):
                        """递归展示部门层级，异常循环数据只展示一次。"""
                        department_key = str(item["org_unit_id"])
                        if department_key in rendered_unit_ids:
                            return
                        rendered_unit_ids.add(department_key)
                        with ui.row().classes("w-full items-center border-b p-2 hover:bg-blue-50"):
                            if depth:
                                ui.icon("subdirectory_arrow_right", size="18px").classes(
                                    "text-blue-400"
                                ).style(f"margin-left: {depth * 18}px")
                            else:
                                ui.icon("account_tree", size="18px").classes("text-blue-600")
                            with ui.column().classes("gap-0 flex-grow"):
                                ui.label(item.get("name", "")).classes("font-semibold")
                                ui.label(
                                    f"编码：{item.get('code', '')} ｜ "
                                    f"上级：{item.get('parent_name') or '根节点'} ｜ "
                                    f"企业微信ID：{item.get('wecom_department_id') or '-'} ｜ "
                                    f"来源：{'企业微信' if item.get('source') == 'wecom' else '系统手工'}"
                                    f"{'（本地编辑保护）' if item.get('manual_override') else ''}"
                                ).classes("text-xs text-gray-500")
                            ui.button(
                                "编辑",
                                on_click=lambda current=item: open_org_form(current),
                            ).props("flat dense color=primary")
                        for child in children_by_parent.get(department_key, []):
                            render_org_branch(child, depth + 1)

                    for item in root_units:
                        render_org_branch(item)
                    for item in units:
                        if str(item["org_unit_id"]) not in rendered_unit_ids:
                            render_org_branch(item)

            def open_org_form(current=None):
                current = current or {}
                all_units = user_svc.list_org_units()
                parent_options = {
                    item["org_unit_id"]: item["name"]
                    for item in all_units
                    if item["org_unit_id"] != current.get("org_unit_id")
                }
                with ui.dialog() as form_dialog, ui.card().classes("w-[32rem] max-w-[95vw] p-6"):
                    ui.label("编辑部门" if current else "新增部门").classes("text-lg font-bold")
                    code_input = ui.input(
                        "稳定编码（保存后不可修改）",
                        value=current.get("code", ""),
                    ).props("debounce=250").classes("w-full")
                    code_check = None
                    if current:
                        code_input.disable()
                    else:
                        code_check = attach_stable_code_check(
                            code_input,
                            lambda: [item.get("code", "") for item in user_svc.list_org_units()],
                            "部门",
                        )
                    name_input = ui.input("部门名称", value=current.get("name", "")).classes("w-full")
                    parent_select = ui.select(
                        parent_options,
                        value=current.get("parent_org_unit_id"),
                        label="上级部门",
                        clearable=True,
                    ).classes("w-full")
                    wecom_input = ui.input(
                        "企业微信部门ID（可空）",
                        value=current.get("wecom_department_id", ""),
                    ).classes("w-full")
                    order_input = ui.number(
                        "排序",
                        value=current.get("sort_order", 0),
                        precision=0,
                    ).classes("w-full")

                    def save_org():
                        if code_check:
                            error = code_check()
                            if error:
                                ui.notify(error, type="warning")
                                return
                        try:
                            user_svc.save_org_unit(
                                code=normalize_stable_code(code_input.value),
                                name=name_input.value,
                                parent_org_unit_id=parent_select.value,
                                wecom_department_id=wecom_input.value,
                                sort_order=int(order_input.value or 0),
                                reject_existing=not bool(current),
                            )
                            render_org_units()
                            form_dialog.close()
                            ui.notify("部门已保存。", type="positive")
                        except Exception as exc:
                            ui.notify(f"部门保存失败：{exc}", type="negative")

                    with ui.row().classes("w-full justify-end gap-3"):
                        ui.button("取消", on_click=form_dialog.close).props("flat")
                        ui.button("保存", on_click=save_org).props("color=primary")
                form_dialog.open()

            def render_positions():
                position_container.clear()
                with position_container:
                    positions = user_svc.list_positions()
                    units = user_svc.list_org_units()
                    if not positions:
                        ui.label("尚无岗位，请先建立岗位字典。").classes("text-gray-500 p-4")

                    def render_position_item(item):
                        with ui.row().classes("w-full items-center border-b p-2 hover:bg-blue-50"):
                            with ui.column().classes("gap-0 flex-grow"):
                                ui.label(item.get("name", "")).classes("font-semibold")
                                ui.label(
                                    f"编码：{item.get('code', '')} ｜ 职级：{item.get('level', 0)} ｜ "
                                    f"默认权限：{len(item.get('permission_codes', []))} 项 ｜ "
                                    f"来源：{'企业微信' if item.get('source') == 'wecom' else '系统手工'}"
                                    f"{'（本地编辑保护）' if item.get('manual_override') else ''}"
                                ).classes("text-xs text-gray-500")
                            ui.button(
                                "编辑",
                                on_click=lambda current=item: open_position_form(current),
                            ).props("flat dense color=primary")

                    unit_by_id = {
                        str(unit["org_unit_id"]): unit for unit in units
                    }
                    children_by_parent: dict[str, list[dict]] = {}
                    root_units: list[dict] = []
                    for unit in units:
                        parent_id = str(unit.get("parent_org_unit_id") or "")
                        if parent_id and parent_id in unit_by_id:
                            children_by_parent.setdefault(parent_id, []).append(unit)
                        else:
                            root_units.append(unit)
                    positions_by_unit: dict[str, list[dict]] = {}
                    for position in positions:
                        for org_unit_id in position.get("org_unit_ids", []):
                            positions_by_unit.setdefault(str(org_unit_id), []).append(position)
                    branch_visibility_cache: dict[str, bool] = {}

                    def department_branch_has_positions(department_key, visiting=None):
                        """只保留自身或下级实际包含岗位的部门分支。"""
                        if department_key in branch_visibility_cache:
                            return branch_visibility_cache[department_key]
                        visiting = set(visiting or ())
                        if department_key in visiting:
                            return bool(positions_by_unit.get(department_key))
                        visiting.add(department_key)
                        visible = bool(positions_by_unit.get(department_key)) or any(
                            department_branch_has_positions(
                                str(child["org_unit_id"]),
                                visiting,
                            )
                            for child in children_by_parent.get(department_key, [])
                        )
                        branch_visibility_cache[department_key] = visible
                        return visible

                    rendered_unit_ids: set[str] = set()

                    def render_position_department_branch(unit, depth=0):
                        """按照组织树递归展示各部门的岗位。"""
                        department_key = str(unit["org_unit_id"])
                        if department_key in rendered_unit_ids:
                            return
                        rendered_unit_ids.add(department_key)
                        scoped_positions = positions_by_unit.get(department_key, [])
                        child_units = [
                            child
                            for child in children_by_parent.get(department_key, [])
                            if department_branch_has_positions(str(child["org_unit_id"]))
                        ]
                        if not scoped_positions and not child_units:
                            return
                        child_summary = (
                            f" / {len(child_units)} 个下级部门" if child_units else ""
                        )
                        indentation = "ml-3" if depth else ""
                        with ui.expansion(
                            f"{unit['name']}（{len(scoped_positions)} 个岗位{child_summary}）",
                            icon="account_tree" if child_units else "business",
                            value=department_key in expanded_position_departments,
                        ).classes(
                            "w-full bg-blue-50 border border-blue-100 rounded mt-2 "
                            f"{indentation}"
                        ) as expansion:
                            for item in scoped_positions:
                                render_position_item(item)
                            for child_unit in child_units:
                                render_position_department_branch(child_unit, depth + 1)
                        expansion.on_value_change(
                            lambda event, key=department_key: remember_position_department(
                                event,
                                key,
                            )
                        )

                    for unit in root_units:
                        render_position_department_branch(unit)
                    for unit in units:
                        if str(unit["org_unit_id"]) not in rendered_unit_ids:
                            render_position_department_branch(unit)
                    unscoped_positions = [
                        item for item in positions if not item.get("org_unit_ids")
                    ]
                    if unscoped_positions:
                        unscoped_key = "__unscoped__"
                        with ui.expansion(
                            f"未划分部门（{len(unscoped_positions)} 个岗位）",
                            icon="help_outline",
                            value=unscoped_key in expanded_position_departments,
                        ).classes("w-full bg-orange-50 border border-orange-100 rounded mt-2") as expansion:
                            for item in unscoped_positions:
                                render_position_item(item)
                        expansion.on_value_change(
                            lambda event, key=unscoped_key: remember_position_department(event, key)
                        )

            def open_position_form(current=None):
                current = current or {}
                units = user_svc.list_org_units()
                if not units:
                    ui.notify("请先建立部门，再维护岗位。", type="warning")
                    return
                department_options = {
                    item["org_unit_id"]: item["name"] for item in units
                }
                with ui.dialog() as form_dialog, ui.card().classes("w-[30rem] max-w-[95vw] p-6"):
                    ui.label("编辑岗位" if current else "新增岗位").classes("text-lg font-bold")
                    code_input = ui.input(
                        "稳定编码（保存后不可修改）",
                        value=current.get("code", ""),
                    ).props("debounce=250").classes("w-full")
                    code_check = None
                    if current:
                        code_input.disable()
                    else:
                        code_check = attach_stable_code_check(
                            code_input,
                            lambda: [item.get("code", "") for item in user_svc.list_positions()],
                            "岗位",
                        )
                    name_input = ui.input("岗位名称", value=current.get("name", "")).classes("w-full")
                    department_select = ui.select(
                        department_options,
                        value=current.get("org_unit_ids", []),
                        label="适用部门（可多选）",
                        multiple=True,
                        with_input=True,
                    ).props("use-chips").classes("w-full")
                    with ui.row().classes("w-full items-center gap-1"):
                        ui.label(
                            "岗位会在所选部门下显示；同一岗位跨部门共用默认权限。"
                        ).classes("text-xs text-gray-500")
                        ui.icon("warning_amber", color="orange", size="xs").tooltip(
                            "调整已有员工使用的岗位部门时，请按顺序操作："
                            "①岗位暂时同时保留原部门和新部门；"
                            "②到“用户与微信账号管理”将相关员工的主部门迁到新部门，岗位保持不变；"
                            "③确认所有在职员工迁移完成后，再从岗位适用部门中移除原部门。"
                        )
                    level_input = ui.number(
                        "职级数字",
                        value=current.get("level", 0),
                        precision=0,
                    ).classes("w-full")

                    def save_position():
                        if code_check:
                            error = code_check()
                            if error:
                                ui.notify(error, type="warning")
                                return
                        if not department_select.value:
                            ui.notify("请至少选择一个适用部门。", type="warning")
                            return
                        try:
                            user_svc.save_position(
                                code=normalize_stable_code(code_input.value),
                                name=name_input.value,
                                level=int(level_input.value or 0),
                                org_unit_ids=department_select.value,
                                reject_existing=not bool(current),
                            )
                            render_positions()
                            form_dialog.close()
                            ui.notify("岗位已保存。", type="positive")
                        except Exception as exc:
                            ui.notify(f"岗位保存失败：{exc}", type="negative")

                    def request_delete_position():
                        """删除前先给出明确确认，最终占用检查仍由数据库事务执行。"""
                        if not current:
                            return
                        member_count = int(current.get("member_count", 0) or 0)
                        if member_count:
                            ui.notify(
                                f"该岗位仍有 {member_count} 名用户任职，请先调整用户岗位。",
                                type="warning",
                            )
                            return
                        with ui.dialog() as confirm_dialog, ui.card().classes(
                            "w-[30rem] max-w-[95vw] p-6"
                        ):
                            ui.label("确认删除岗位").classes("text-lg font-bold text-red-700")
                            ui.label(
                                f"将删除岗位“{current.get('name', '')}”及其部门归属和默认权限。"
                            ).classes("text-sm")
                            ui.label(
                                "系统会在删除瞬间再次检查用户任职；企业微信中仍存在的职务以后重新导入时可能再次生成岗位。"
                            ).classes("text-xs text-gray-500 leading-5")

                            def confirm_delete():
                                try:
                                    user_svc.delete_position(
                                        current["position_id"],
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    ui.notify(
                                        f"岗位删除失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    return
                                ui.notify("岗位已删除。", type="positive")
                                confirm_dialog.close()
                                form_dialog.close()
                                render_positions()

                            with ui.row().classes("w-full justify-end gap-3"):
                                ui.button("取消", on_click=confirm_dialog.close).props("flat")
                                ui.button(
                                    "确认删除",
                                    on_click=confirm_delete,
                                    icon="delete_forever",
                                ).props("color=negative")
                        confirm_dialog.open()

                    with ui.row().classes("w-full justify-end gap-3"):
                        if current:
                            ui.button(
                                "删除岗位",
                                on_click=request_delete_position,
                                icon="delete",
                            ).props("outline color=negative")
                            ui.space()
                        ui.button("取消", on_click=form_dialog.close).props("flat")
                        ui.button("保存", on_click=save_position).props("color=primary")
                form_dialog.open()

            def import_wecom_org():
                cache_data = load_wecom_contacts_cache()
                departments = [
                    item
                    for item in cache_data.get("departments", [])
                    if str(item.get("id", "")).strip()
                ]
                if not departments:
                    ui.notify("企业微信通讯录缓存中没有部门，请先同步通讯录。", type="warning")
                    return
                department_by_id = {
                    str(item["id"]): item for item in departments
                }
                children_by_parent: dict[str, list[dict]] = {}
                root_departments: list[dict] = []
                for item in departments:
                    parent_id = str(item.get("parentid") or "")
                    if parent_id and parent_id in department_by_id:
                        children_by_parent.setdefault(parent_id, []).append(item)
                    else:
                        root_departments.append(item)
                existing_department_ids = {
                    str(item.get("wecom_department_id"))
                    for item in user_svc.list_org_units()
                    if str(item.get("wecom_department_id") or "")
                }
                selected_department_ids: set[str] = set()
                department_checkboxes = {}

                with ui.dialog() as selection_dialog, ui.card().classes(
                    "w-[64rem] max-w-[95vw] h-[82vh] p-5 flex flex-col no-wrap"
                ):
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        with ui.column().classes("gap-0"):
                            ui.label("选择要导入的企业微信部门").classes("text-xl font-bold")
                            ui.label(
                                "默认不选择；重复导入已存在部门时仍遵循本地编辑保护。"
                            ).classes("text-xs text-gray-500")
                        ui.button(icon="close", on_click=selection_dialog.close).props(
                            "flat round dense"
                        )
                    with ui.row().classes("w-full items-center gap-2 shrink-0"):
                        selected_count_label = ui.label("已选择 0 个部门").classes(
                            "text-sm font-semibold text-blue-700"
                        )
                        ui.space()
                        select_all_button = ui.button("全选", icon="done_all").props(
                            "flat dense"
                        )
                        clear_all_button = ui.button("清空", icon="clear_all").props(
                            "flat dense"
                        )
                    department_selection_container = ui.column().classes(
                        "w-full flex-grow min-h-0 overflow-y-auto gap-0 border rounded p-2 bg-gray-50"
                    )

                    def update_department_selection(department_id, checked):
                        if checked:
                            selected_department_ids.add(department_id)
                        else:
                            selected_department_ids.discard(department_id)
                        selected_count_label.set_text(
                            f"已选择 {len(selected_department_ids)} 个部门"
                        )

                    with department_selection_container:
                        rendered_department_ids: set[str] = set()

                        def render_department_choice(item, depth=0):
                            department_id = str(item["id"])
                            if department_id in rendered_department_ids:
                                return
                            rendered_department_ids.add(department_id)
                            with ui.row().classes(
                                "w-full items-center border-b border-gray-200 py-1"
                            ):
                                checkbox = ui.checkbox(
                                    str(item.get("name") or f"企业微信部门 {department_id}"),
                                    value=False,
                                ).style(f"margin-left: {depth * 22}px")
                                department_checkboxes[department_id] = checkbox
                                checkbox.on_value_change(
                                    lambda event, current_id=department_id: (
                                        update_department_selection(current_id, event.value)
                                    )
                                )
                                ui.label(f"ID：{department_id}").classes(
                                    "text-xs text-gray-400"
                                )
                                if department_id in existing_department_ids:
                                    ui.chip("系统中已存在", color="positive").props("dense outline")
                            for child in children_by_parent.get(department_id, []):
                                render_department_choice(child, depth + 1)

                        for item in root_departments:
                            render_department_choice(item)
                        for item in departments:
                            if str(item["id"]) not in rendered_department_ids:
                                render_department_choice(item)

                    def set_all_departments(checked):
                        selected_department_ids.clear()
                        if checked:
                            selected_department_ids.update(department_checkboxes)
                        for checkbox in department_checkboxes.values():
                            checkbox.set_value(checked)
                        selected_count_label.set_text(
                            f"已选择 {len(selected_department_ids)} 个部门"
                        )

                    def import_selected_departments():
                        if not selected_department_ids:
                            ui.notify("请至少勾选一个部门。", type="warning")
                            return
                        missing_parents = []
                        for department_id in selected_department_ids:
                            department = department_by_id[department_id]
                            parent_id = str(department.get("parentid") or "")
                            if (
                                parent_id
                                and parent_id != "0"
                                and parent_id in department_by_id
                                and parent_id not in selected_department_ids
                                and parent_id not in existing_department_ids
                            ):
                                missing_parents.append(
                                    f"{department.get('name', department_id)} → "
                                    f"{department_by_id[parent_id].get('name', parent_id)}"
                                )
                        if missing_parents:
                            ui.notify(
                                "以下下级部门的上级尚未导入，请同时勾选上级部门：\n"
                                + "\n".join(missing_parents[:10]),
                                type="warning",
                                multi_line=True,
                            )
                            return
                        selected_departments = [
                            item
                            for item in departments
                            if str(item["id"]) in selected_department_ids
                        ]
                        try:
                            inserted, updated = user_svc.import_wecom_departments(
                                selected_departments
                            )
                        except Exception as exc:
                            ui.notify(
                                f"部门导入失败：{exc}",
                                type="negative",
                                multi_line=True,
                            )
                            return
                        ui.notify(
                            f"所选部门导入完成：新增 {inserted}，更新 {updated}。",
                            type="positive",
                        )
                        selection_dialog.close()
                        render_org_units()
                        render_positions()

                    select_all_button.on_click(lambda: set_all_departments(True))
                    clear_all_button.on_click(lambda: set_all_departments(False))
                    with ui.row().classes("w-full justify-end gap-3 shrink-0"):
                        ui.button("取消", on_click=selection_dialog.close).props("flat")
                        ui.button(
                            "导入已勾选部门",
                            on_click=import_selected_departments,
                            icon="cloud_download",
                        ).props("color=primary")
                selection_dialog.open()

            def import_wecom_position_catalog():
                cache_data = load_wecom_contacts_cache()
                contacts = [
                    item
                    for item in cache_data.get("contacts", [])
                    if item.get("is_active", True) and str(item.get("position", "")).strip()
                ]
                if not contacts:
                    ui.notify("企业微信通讯录缓存中没有有效职务，请先同步通讯录。", type="warning")
                    return
                department_names = {
                    str(item.get("id", "")): str(item.get("name", ""))
                    for item in cache_data.get("departments", [])
                }
                position_summaries: dict[str, dict] = {}
                for contact in contacts:
                    position_name = str(contact.get("position", "")).strip()
                    summary = position_summaries.setdefault(
                        position_name,
                        {"member_count": 0, "department_names": set(), "department_ids": set()},
                    )
                    summary["member_count"] += 1
                    contact_department_ids = {
                        str(value)
                        for value in contact.get("department_ids", [])
                        if str(value)
                    }
                    main_department_id = str(contact.get("main_department_id", "")).strip()
                    if main_department_id:
                        contact_department_ids.add(main_department_id)
                    summary["department_ids"].update(contact_department_ids)
                    summary["department_names"].update(
                        department_names.get(value, value)
                        for value in contact_department_ids
                    )
                existing_positions = user_svc.list_positions()
                existing_external_names = {
                    str(item.get("external_name_snapshot", "")).strip()
                    for item in existing_positions
                    if str(item.get("external_name_snapshot", "")).strip()
                }
                existing_position_names = {
                    str(item.get("name", "")).strip() for item in existing_positions
                }
                imported_department_ids = {
                    str(item.get("wecom_department_id", "")).strip()
                    for item in user_svc.list_org_units()
                    if str(item.get("wecom_department_id", "")).strip()
                }
                selected_position_names: set[str] = set()
                position_checkboxes = {}

                with ui.dialog() as selection_dialog, ui.card().classes(
                    "w-[64rem] max-w-[95vw] h-[82vh] p-5 flex flex-col no-wrap"
                ):
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        with ui.column().classes("gap-0"):
                            ui.label("选择要导入的企业微信岗位").classes("text-xl font-bold")
                            ui.label(
                                "默认不选择；岗位只关联已经导入系统的企业微信部门。"
                            ).classes("text-xs text-gray-500")
                        ui.button(icon="close", on_click=selection_dialog.close).props(
                            "flat round dense"
                        )
                    with ui.row().classes("w-full items-center gap-2 shrink-0"):
                        selected_count_label = ui.label("已选择 0 个岗位").classes(
                            "text-sm font-semibold text-blue-700"
                        )
                        ui.space()
                        select_all_button = ui.button("全选", icon="done_all").props(
                            "flat dense"
                        )
                        clear_all_button = ui.button("清空", icon="clear_all").props(
                            "flat dense"
                        )
                    position_selection_container = ui.column().classes(
                        "w-full flex-grow min-h-0 overflow-y-auto gap-0 border rounded p-2 bg-gray-50"
                    )

                    def update_position_selection(position_name, checked):
                        if checked:
                            selected_position_names.add(position_name)
                        else:
                            selected_position_names.discard(position_name)
                        selected_count_label.set_text(
                            f"已选择 {len(selected_position_names)} 个岗位"
                        )

                    with position_selection_container:
                        for position_name in sorted(
                            position_summaries,
                            key=lambda value: value.casefold(),
                        ):
                            summary = position_summaries[position_name]
                            with ui.row().classes(
                                "w-full items-center border-b border-gray-200 py-2"
                            ):
                                checkbox = ui.checkbox(position_name, value=False).classes(
                                    "min-w-[14rem]"
                                )
                                position_checkboxes[position_name] = checkbox
                                checkbox.on_value_change(
                                    lambda event, current_name=position_name: (
                                        update_position_selection(current_name, event.value)
                                    )
                                )
                                ui.label(
                                    f"{summary['member_count']} 人 ｜ "
                                    f"{'、'.join(sorted(summary['department_names'])) or '未提供部门'}"
                                ).classes("text-xs text-gray-500 flex-grow")
                                if (
                                    position_name in existing_external_names
                                    or position_name in existing_position_names
                                ):
                                    ui.chip("系统中已存在", color="positive").props("dense outline")

                    def set_all_positions(checked):
                        selected_position_names.clear()
                        if checked:
                            selected_position_names.update(position_checkboxes)
                        for checkbox in position_checkboxes.values():
                            checkbox.set_value(checked)
                        selected_count_label.set_text(
                            f"已选择 {len(selected_position_names)} 个岗位"
                        )

                    def import_selected_positions():
                        if not selected_position_names:
                            ui.notify("请至少勾选一个岗位。", type="warning")
                            return
                        selected_contacts = [
                            contact
                            for contact in contacts
                            if str(contact.get("position", "")).strip()
                            in selected_position_names
                        ]
                        unscoped_positions = [
                            position_name
                            for position_name in selected_position_names
                            if not (
                                position_summaries[position_name]["department_ids"]
                                & imported_department_ids
                            )
                        ]
                        try:
                            inserted, updated = user_svc.import_wecom_positions(
                                selected_contacts
                            )
                        except Exception as exc:
                            ui.notify(
                                f"岗位导入失败：{exc}",
                                type="negative",
                                multi_line=True,
                            )
                            return
                        message = f"所选岗位导入完成：新增 {inserted}，已存在 {updated}。"
                        if unscoped_positions:
                            message += (
                                "\n以下岗位没有已导入的所属部门，暂归入未划分部门："
                                + "、".join(sorted(unscoped_positions))
                            )
                        ui.notify(
                            message,
                            type="warning" if unscoped_positions else "positive",
                            multi_line=True,
                        )
                        selection_dialog.close()
                        render_positions()

                    select_all_button.on_click(lambda: set_all_positions(True))
                    clear_all_button.on_click(lambda: set_all_positions(False))
                    with ui.row().classes("w-full justify-end gap-3 shrink-0"):
                        ui.button("取消", on_click=selection_dialog.close).props("flat")
                        ui.button(
                            "导入已勾选岗位",
                            on_click=import_selected_positions,
                            icon="cloud_download",
                        ).props("color=primary")
                selection_dialog.open()

            add_org_button.on_click(lambda: open_org_form())
            add_position_button.on_click(lambda: open_position_form())
            import_button.on_click(import_wecom_org)
            import_position_button.on_click(import_wecom_position_catalog)
            render_org_units()
            render_positions()
        org_dialog.open()

    def open_security_role_management_dialog():
        """统一配置岗位权限、附加权限组和审批流程。"""
        user_svc = app.state.user_service
        if user_svc.storage_mode != "database":
            ui.notify("请先执行用户一键迁移，再维护岗位与权限。", type="warning")
            return
        try:
            user_svc.sync_permission_catalog(strict_overview=True)
        except Exception as exc:
            ui.notify(f"权限目录初始化失败：{exc}", type="negative", multi_line=True)
            return

        with (
            ui.dialog().props("maximized") as role_dialog,
            ui.card().classes("w-full h-full p-5 flex flex-col no-wrap bg-gray-50"),
        ):
            with ui.row().classes("w-full items-center justify-between shrink-0"):
                with ui.column().classes("gap-0"):
                    ui.label("岗位、权限与审批流程").classes("text-xl font-bold")
                    ui.label(
                        "岗位提供日常权限；附加权限组处理特殊授权；审批流程把业务单据分派给具体人员。"
                    ).classes("text-xs text-gray-500")
                ui.button(icon="close", on_click=role_dialog.close).props("flat round dense")
            ui.separator()

            def render_permission_tables(permissions, selected_codes):
                """按业务模块和子分类渲染权限表格，概述项按 label 合并查看/维护两列。"""
                checkbox_by_code = {}
                top_modules = list(
                    dict.fromkeys(str(item.get("module", "")).split(" · ", 1)[0] for item in permissions)
                )
                for top_module in top_modules:
                    module_permissions = [
                        item
                        for item in permissions
                        if str(item.get("module", "")).split(" · ", 1)[0] == top_module
                    ]
                    with ui.expansion(top_module, value=False, icon="folder").classes(
                        "w-full border rounded bg-white"
                    ):
                        exact_modules = list(dict.fromkeys(str(item.get("module", "")) for item in module_permissions))
                        for exact_module in exact_modules:
                            current_permissions = [
                                item for item in module_permissions if str(item.get("module", "")) == exact_module
                            ]
                            category = exact_module.removeprefix(f"{top_module} · ")
                            if category != exact_module:
                                ui.label(category.replace(" · ", " / ")).classes(
                                    "w-full text-sm font-semibold text-blue-800 bg-blue-50 rounded px-2 py-1"
                                )

                            overview_items = {}
                            ordinary_permissions = []
                            for permission in current_permissions:
                                code = str(permission.get("code", ""))
                                if code.startswith("project_overview.item.") and code.endswith((".view", ".edit")):
                                    action = code.rsplit(".", 1)[-1]
                                    label = code[len("project_overview.item.") : -(len(action) + 1)]
                                    overview_items.setdefault(label, {})[action] = permission
                                else:
                                    ordinary_permissions.append(permission)

                            if overview_items:
                                with ui.grid(columns=12).classes("w-full gap-x-2 gap-y-1 items-center text-sm"):
                                    ui.label("概述项").classes("col-span-5 font-semibold text-gray-600")
                                    ui.label("label").classes("col-span-4 font-semibold text-gray-600")
                                    ui.label("查看").classes("col-span-1 text-center font-semibold text-gray-600")
                                    ui.label("维护").classes("col-span-2 text-center font-semibold text-gray-600")
                                    for label, actions in overview_items.items():
                                        title_permission = actions.get("view") or actions.get("edit") or {}
                                        title = str(title_permission.get("name", "")).split(" — ", 1)[-1]
                                        ui.label(title).classes("col-span-5 truncate").tooltip(title)
                                        ui.label(label).classes("col-span-4 text-xs text-gray-500 font-mono truncate").tooltip(label)
                                        for action, column_class in (("view", "col-span-1"), ("edit", "col-span-2")):
                                            permission = actions.get(action)
                                            if permission:
                                                checkbox = ui.checkbox(
                                                    value=permission["code"] in selected_codes
                                                ).classes(f"{column_class} justify-self-center")
                                                checkbox.tooltip(
                                                    f"{permission['code']}\n{permission.get('description', '')}"
                                                )
                                                checkbox_by_code[permission["code"]] = checkbox
                                            else:
                                                ui.label("—").classes(f"{column_class} text-center text-gray-300")

                            if ordinary_permissions:
                                with ui.grid(columns=12).classes("w-full gap-x-2 gap-y-1 items-center text-sm"):
                                    ui.label("权限项").classes("col-span-5 font-semibold text-gray-600")
                                    ui.label("稳定编码").classes("col-span-5 font-semibold text-gray-600")
                                    ui.label("授权").classes("col-span-2 text-center font-semibold text-gray-600")
                                    for permission in ordinary_permissions:
                                        ui.label(permission["name"]).classes("col-span-5 truncate").tooltip(
                                            permission.get("description", "")
                                        )
                                        ui.label(permission["code"]).classes(
                                            "col-span-5 text-xs text-gray-500 font-mono truncate"
                                        ).tooltip(permission["code"])
                                        checkbox = ui.checkbox(
                                            value=permission["code"] in selected_codes
                                        ).classes("col-span-2 justify-self-center")
                                        checkbox.tooltip(permission.get("description", ""))
                                        checkbox_by_code[permission["code"]] = checkbox
                return checkbox_by_code

            def open_overview_permission_mapping_dialog():
                """按旧 JSON 角色与新岗位的映射批量整理概述 label 权限。"""
                try:
                    overview_config = load_overview_permission_source(
                        os.path.join(BASE_DIR, "overview_config.json")
                    )
                except Exception as exc:
                    ui.notify(f"无法读取概述权限来源：{exc}", type="negative", multi_line=True)
                    return

                positions = [
                    position
                    for position in user_svc.list_positions()
                    if position.get("status", "active") == "active"
                ]
                if not positions:
                    ui.notify("尚无可用岗位，请先维护岗位字典。", type="warning")
                    return
                units = user_svc.list_org_units()
                unit_names = {
                    str(unit["org_unit_id"]): str(unit["name"])
                    for unit in units
                }

                def position_label(position):
                    department_names = [
                        unit_names[org_unit_id]
                        for org_unit_id in map(str, position.get("org_unit_ids", []))
                        if org_unit_id in unit_names
                    ]
                    department_text = " / ".join(department_names) or "未划分部门"
                    return f"{department_text} · {position['name']}（{position['code']}）"

                position_by_id = {
                    str(position["position_id"]): position for position in positions
                }
                position_options = {
                    str(position["position_id"]): position_label(position)
                    for position in sorted(positions, key=lambda item: position_label(item).casefold())
                }
                role_usage = collect_legacy_overview_role_usage(overview_config)
                valid_roles = {str(item["role"]) for item in role_usage}
                stored_mapping = normalize_overview_role_position_mapping(
                    user_svc.get_overview_role_position_mapping(),
                    valid_roles=valid_roles,
                    valid_position_ids=set(position_by_id),
                )
                mapping_state = copy.deepcopy(stored_mapping)
                previous_managed_permissions = (
                    user_svc.get_overview_role_position_managed_permissions()
                )
                previous_target_ids = {
                    position_id
                    for position_ids in stored_mapping.values()
                    for position_id in position_ids
                } | set(previous_managed_permissions)

                with (
                    ui.dialog().props("maximized") as mapping_dialog,
                    ui.card().classes("w-full h-full p-5 flex flex-col no-wrap bg-gray-50"),
                ):
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        with ui.column().classes("gap-0"):
                            ui.label("项目概述权限批量映射").classes("text-xl font-bold")
                            ui.label(
                                "把旧 JSON 的角色名称映射到一个或多个系统岗位，再按每个 label 的读写规则自动整理。"
                            ).classes("text-xs text-gray-500")
                        ui.button(icon="close", on_click=mapping_dialog.close).props("flat round dense")

                    ui.label(
                        "自动整理只调整本工具生成的 project_overview.item.* 权限；管理员手工补充的概述权限，以及工具、"
                        "系统管理、审批和其他模块权限都不变。维护权限本身已包含查看能力。取消既有映射后再次整理，"
                        "只会清除该映射原先自动生成的概述项权限。"
                    ).classes("w-full text-sm text-blue-900 bg-blue-50 border border-blue-200 rounded p-3 shrink-0")

                    with ui.row().classes("w-full items-end gap-3 shrink-0"):
                        role_search = ui.input("搜索旧角色", placeholder="例如：质量").props(
                            "clearable debounce=250"
                        ).classes("w-72")
                        only_mapped = ui.switch("只看已映射", value=False)
                        mapping_summary = ui.label().classes("text-sm text-gray-600")

                    mapping_container = ui.column().classes(
                        "w-full flex-grow min-h-0 overflow-y-auto gap-2 bg-white border rounded p-3"
                    )
                    preview_container = ui.column().classes("w-full shrink-0 gap-1")

                    def update_mapping(role, value):
                        """更新弹窗内尚未提交的多岗位映射。"""
                        values = value if isinstance(value, list) else ([value] if value else [])
                        normalized = [
                            str(position_id)
                            for position_id in values
                            if str(position_id) in position_by_id
                        ]
                        if normalized:
                            mapping_state[role] = list(dict.fromkeys(normalized))
                        else:
                            mapping_state.pop(role, None)
                        refresh_mapping_summary()

                    def refresh_mapping_summary():
                        mapped_roles = sum(bool(mapping_state.get(role)) for role in valid_roles)
                        target_positions = {
                            position_id
                            for position_ids in mapping_state.values()
                            for position_id in position_ids
                        }
                        mapping_summary.set_text(
                            f"已映射 {mapped_roles}/{len(valid_roles)} 个旧角色，涉及 {len(target_positions)} 个岗位"
                        )

                    def render_mapping_rows():
                        query = str(role_search.value or "").strip().casefold()
                        mapping_container.clear()
                        with mapping_container:
                            with ui.grid(columns=12).classes(
                                "w-full gap-x-3 gap-y-2 items-center text-sm"
                            ):
                                ui.label("旧 JSON 角色").classes("col-span-3 font-semibold text-gray-600")
                                ui.label("覆盖项数").classes("col-span-2 font-semibold text-gray-600")
                                ui.label("映射到系统岗位（可多选）").classes(
                                    "col-span-7 font-semibold text-gray-600"
                                )
                                visible_count = 0
                                for usage in role_usage:
                                    role = str(usage["role"])
                                    if query and query not in role.casefold():
                                        continue
                                    if only_mapped.value and not mapping_state.get(role):
                                        continue
                                    visible_count += 1
                                    ui.label(role).classes("col-span-3 font-medium")
                                    ui.label(
                                        f"查看 {usage['view_count']} / 维护 {usage['edit_count']}"
                                    ).classes("col-span-2 text-xs text-gray-500")
                                    selector = ui.select(
                                        position_options,
                                        value=mapping_state.get(role, []),
                                        multiple=True,
                                        label="选择岗位",
                                    ).props("use-chips clearable with-input options-dense").classes(
                                        "col-span-7 w-full"
                                    )
                                    selector.on_value_change(
                                        lambda event, role_name=role: update_mapping(
                                            role_name,
                                            event.value,
                                        )
                                    )
                                if not visible_count:
                                    ui.label("没有符合条件的旧角色。待映射角色请保留在旧 JSON 的权限字段中。") \
                                        .classes("col-span-12 text-gray-500 p-4 text-center")

                    def calculate_preview():
                        normalized_mapping = normalize_overview_role_position_mapping(
                            mapping_state,
                            valid_roles=valid_roles,
                            valid_position_ids=set(position_by_id),
                        )
                        permission_plan = build_overview_position_permission_plan(
                            overview_config,
                            normalized_mapping,
                        )
                        current_target_ids = {
                            position_id
                            for position_ids in normalized_mapping.values()
                            for position_id in position_ids
                        }
                        affected_ids = previous_target_ids | current_target_ids
                        rows = []
                        for position_id in sorted(
                            affected_ids,
                            key=lambda value: position_options.get(value, value).casefold(),
                        ):
                            current_codes = user_svc.get_position_permission_codes(position_id)
                            current_overview = {
                                code for code in current_codes if code.startswith(OVERVIEW_ITEM_PERMISSION_PREFIX)
                            }
                            desired = permission_plan.get(position_id, set())
                            previous_managed = previous_managed_permissions.get(position_id, set())
                            organized_result = (current_overview - previous_managed) | desired
                            rows.append(
                                {
                                    "position_id": position_id,
                                    "name": position_options.get(position_id, position_id),
                                    "desired": len(organized_result),
                                    "add": len(organized_result - current_overview),
                                    "remove": len(current_overview - organized_result),
                                }
                            )
                        return normalized_mapping, permission_plan, affected_ids, rows

                    def render_preview():
                        _mapping, _plan, _affected, rows = calculate_preview()
                        preview_container.clear()
                        with preview_container:
                            if not rows:
                                ui.label("当前没有映射目标，不会修改任何岗位权限。") \
                                    .classes("text-sm text-orange-700")
                                return
                            total_add = sum(row["add"] for row in rows)
                            total_remove = sum(row["remove"] for row in rows)
                            ui.label(
                                f"预览：涉及 {len(rows)} 个岗位，新增 {total_add} 项，移除 {total_remove} 项概述权限。"
                            ).classes("text-sm font-semibold")
                            with ui.row().classes("w-full gap-3 overflow-x-auto no-wrap"):
                                for row in rows:
                                    ui.label(
                                        f"{row['name']}：整理后 {row['desired']}，+{row['add']} / -{row['remove']}"
                                    ).classes("text-xs bg-white border rounded px-2 py-1 whitespace-nowrap")

                    def confirm_apply_mapping():
                        normalized_mapping, permission_plan, affected_ids, rows = calculate_preview()
                        if not affected_ids:
                            ui.notify("当前没有需要整理的岗位。", type="warning")
                            return
                        total_add = sum(row["add"] for row in rows)
                        total_remove = sum(row["remove"] for row in rows)
                        with ui.dialog() as confirm_dialog, ui.card().classes("w-[34rem] max-w-[95vw] p-5"):
                            ui.label("确认自动整理概述权限？").classes("text-lg font-bold")
                            ui.label(
                                f"将处理 {len(affected_ids)} 个岗位：新增 {total_add} 项、移除 {total_remove} 项。"
                            ).classes("text-sm")
                            if total_remove:
                                ui.label(
                                    "移除项仅限本工具上次自动生成的概述项权限，手工附加权限会保留。"
                                ).classes("text-sm text-orange-700 bg-orange-50 rounded p-2")

                            def apply_mapping():
                                try:
                                    count = user_svc.apply_overview_role_position_mapping(
                                        normalized_mapping,
                                        permission_plan,
                                        affected_position_ids=affected_ids,
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    ui.notify(f"概述权限自动整理失败：{exc}", type="negative", multi_line=True)
                                    return
                                confirm_dialog.close()
                                mapping_dialog.close()
                                render_permission_positions()
                                render_position_permissions()
                                ui.notify(f"已按映射整理 {count} 个岗位的概述权限。", type="positive")

                            with ui.row().classes("w-full justify-end"):
                                ui.button("取消", on_click=confirm_dialog.close).props("flat")
                                ui.button("确认并自动整理", icon="auto_fix_high", on_click=apply_mapping).props(
                                    "color=primary"
                                )
                        confirm_dialog.open()

                    role_search.on_value_change(lambda _event: render_mapping_rows())
                    only_mapped.on_value_change(lambda _event: render_mapping_rows())
                    with ui.row().classes("w-full justify-end shrink-0"):
                        ui.button("刷新预览", icon="preview", on_click=render_preview).props("outline")
                        ui.button(
                            "自动整理概述权限",
                            icon="auto_fix_high",
                            on_click=confirm_apply_mapping,
                        ).props("color=primary")
                    refresh_mapping_summary()
                    render_mapping_rows()
                    render_preview()
                mapping_dialog.open()

            with ui.tabs().classes("w-full shrink-0") as tabs:
                position_tab = ui.tab("岗位默认权限", icon="badge")
                role_tab = ui.tab("附加权限组", icon="admin_panel_settings")
                user_tab = ui.tab("用户附加授权", icon="how_to_reg")
                workflow_tab = ui.tab("审批流程", icon="account_tree")

            with ui.tab_panels(tabs, value=position_tab).classes(
                "w-full flex-grow min-h-0 bg-transparent p-0"
            ):
                with ui.tab_panel(position_tab).classes("w-full h-full p-0 pt-3"):
                    with ui.grid(columns=2).classes("w-full h-full min-h-0 gap-4"):
                        with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                            with ui.row().classes("w-full items-center justify-between shrink-0"):
                                with ui.column().classes("gap-0"):
                                    ui.label("岗位字典").classes("text-lg font-bold")
                                    ui.label(
                                        "员工绑定主岗位后自动继承这里配置的权限。"
                                    ).classes("text-xs text-gray-500")
                                ui.button(
                                    "概述权限批量映射",
                                    icon="account_tree",
                                    on_click=open_overview_permission_mapping_dialog,
                                ).props("outline color=primary")
                            position_list_container = ui.column().classes(
                                "w-full flex-grow min-h-0 overflow-y-auto gap-1 pt-2"
                            )
                        with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                            position_permission_container = ui.column().classes(
                                "w-full flex-grow min-h-0 overflow-y-auto gap-3"
                            )

                    position_state = {"selected_position_id": None}
                    expanded_permission_departments: set[str] = set()

                    def remember_permission_department(event, department_key: str):
                        """保留权限列表当前展开部门，同时保证首次进入全部收起。"""
                        if event.value:
                            expanded_permission_departments.add(department_key)
                        else:
                            expanded_permission_departments.discard(department_key)

                    def select_permission_position(position_id):
                        position_state["selected_position_id"] = position_id
                        render_permission_positions()
                        render_position_permissions()

                    def render_permission_positions():
                        positions = user_svc.list_positions()
                        units = user_svc.list_org_units()
                        selected_id = position_state["selected_position_id"]
                        if selected_id not in {item["position_id"] for item in positions}:
                            position_state["selected_position_id"] = (
                                positions[0]["position_id"] if positions else None
                            )
                        position_list_container.clear()
                        with position_list_container:
                            if not positions:
                                ui.label("尚无岗位，请先在组织架构中建立岗位字典。").classes(
                                    "text-gray-500 p-4"
                                )
                            def render_permission_position_item(position):
                                selected = (
                                    position["position_id"] == position_state["selected_position_id"]
                                )
                                background = (
                                    "bg-blue-50 border-blue-400"
                                    if selected
                                    else "bg-white border-gray-200"
                                )
                                with ui.row().classes(
                                    f"w-full items-center border rounded p-3 cursor-pointer {background}"
                                ).on(
                                    "click",
                                    lambda _event, position_id=position["position_id"]: (
                                        select_permission_position(position_id)
                                    ),
                                ):
                                    with ui.column().classes("gap-0 flex-grow"):
                                        ui.label(position["name"]).classes("font-semibold")
                                        ui.label(
                                            f"{position['code']} ｜ 职级 {position.get('level', 0)}"
                                        ).classes("text-xs text-gray-500")
                                    ui.label(
                                        f"{len(position.get('permission_codes', []))} 项权限 / "
                                        f"{position.get('member_count', 0)} 人"
                                    ).classes("text-xs text-gray-500")

                            unit_by_id = {
                                str(unit["org_unit_id"]): unit for unit in units
                            }
                            children_by_parent: dict[str, list[dict]] = {}
                            root_units: list[dict] = []
                            for unit in units:
                                parent_id = str(unit.get("parent_org_unit_id") or "")
                                if parent_id and parent_id in unit_by_id:
                                    children_by_parent.setdefault(parent_id, []).append(unit)
                                else:
                                    root_units.append(unit)
                            positions_by_unit: dict[str, list[dict]] = {}
                            for position in positions:
                                for org_unit_id in position.get("org_unit_ids", []):
                                    positions_by_unit.setdefault(str(org_unit_id), []).append(position)
                            branch_visibility_cache: dict[str, bool] = {}

                            def department_branch_has_positions(department_key, visiting=None):
                                """判断部门自身或任一下级部门是否配置了岗位。"""
                                if department_key in branch_visibility_cache:
                                    return branch_visibility_cache[department_key]
                                visiting = set(visiting or ())
                                if department_key in visiting:
                                    return bool(positions_by_unit.get(department_key))
                                visiting.add(department_key)
                                visible = bool(positions_by_unit.get(department_key)) or any(
                                    department_branch_has_positions(
                                        str(child["org_unit_id"]),
                                        visiting,
                                    )
                                    for child in children_by_parent.get(department_key, [])
                                )
                                branch_visibility_cache[department_key] = visible
                                return visible

                            rendered_unit_ids: set[str] = set()

                            def render_department_branch(unit, depth=0):
                                """按部门父子关系递归展示岗位分组。"""
                                department_key = str(unit["org_unit_id"])
                                if department_key in rendered_unit_ids:
                                    return
                                rendered_unit_ids.add(department_key)
                                scoped_positions = positions_by_unit.get(department_key, [])
                                child_units = [
                                    child
                                    for child in children_by_parent.get(department_key, [])
                                    if department_branch_has_positions(
                                        str(child["org_unit_id"])
                                    )
                                ]
                                if not scoped_positions and not child_units:
                                    return
                                child_summary = (
                                    f" / {len(child_units)} 个下级部门" if child_units else ""
                                )
                                indentation = "ml-3" if depth else ""
                                with ui.expansion(
                                    f"{unit['name']}（{len(scoped_positions)} 个岗位{child_summary}）",
                                    icon="account_tree" if child_units else "business",
                                    value=department_key in expanded_permission_departments,
                                ).classes(
                                    "w-full bg-blue-50 border border-blue-100 rounded mt-2 "
                                    f"{indentation}"
                                ) as expansion:
                                    for position in scoped_positions:
                                        render_permission_position_item(position)
                                    for child_unit in child_units:
                                        render_department_branch(child_unit, depth + 1)
                                expansion.on_value_change(
                                    lambda event, key=department_key: remember_permission_department(
                                        event,
                                        key,
                                    )
                                )

                            for unit in root_units:
                                render_department_branch(unit)
                            # 异常的循环层级或孤立数据仍要显示，避免岗位在管理界面彻底消失。
                            for unit in units:
                                if str(unit["org_unit_id"]) not in rendered_unit_ids:
                                    render_department_branch(unit)
                            unscoped_positions = [
                                position
                                for position in positions
                                if not position.get("org_unit_ids")
                            ]
                            if unscoped_positions:
                                unscoped_key = "__unscoped__"
                                with ui.expansion(
                                    f"未划分部门（{len(unscoped_positions)} 个岗位）",
                                    icon="help_outline",
                                    value=unscoped_key in expanded_permission_departments,
                                ).classes(
                                    "w-full bg-orange-50 border border-orange-100 rounded mt-2"
                                ) as expansion:
                                    for position in unscoped_positions:
                                        render_permission_position_item(position)
                                expansion.on_value_change(
                                    lambda event, key=unscoped_key: remember_permission_department(
                                        event,
                                        key,
                                    )
                                )

                    def render_position_permissions():
                        positions = user_svc.list_positions()
                        permissions = user_svc.list_permissions()
                        selected = next(
                            (
                                position
                                for position in positions
                                if position["position_id"]
                                == position_state["selected_position_id"]
                            ),
                            None,
                        )
                        position_permission_container.clear()
                        with position_permission_container:
                            if not selected:
                                ui.label("请选择一个岗位。").classes("text-gray-500 p-4")
                                return

                            def open_copy_position_permissions_dialog():
                                """把另一岗位当前的整套默认权限复制到所选岗位。"""
                                source_positions = sorted(
                                    (
                                        position
                                        for position in user_svc.list_positions()
                                        if position["position_id"] != selected["position_id"]
                                    ),
                                    key=lambda position: (
                                        str(position.get("name", "")).casefold(),
                                        str(position.get("code", "")).casefold(),
                                    ),
                                )
                                if not source_positions:
                                    ui.notify("没有其他岗位可供复制。", type="warning")
                                    return
                                source_by_id = {
                                    str(position["position_id"]): position
                                    for position in source_positions
                                }
                                source_options = {
                                    position_id: (
                                        f"{position.get('name', '')} ｜ "
                                        f"{'、'.join(position.get('org_names', [])) or '未划分部门'} ｜ "
                                        f"{len(position.get('permission_codes', []))} 项权限"
                                    )
                                    for position_id, position in source_by_id.items()
                                }
                                with ui.dialog() as copy_dialog, ui.card().classes(
                                    "w-[38rem] max-w-[95vw] p-6 gap-4"
                                ):
                                    with ui.row().classes("w-full items-center gap-2"):
                                        ui.icon("content_copy", color="primary")
                                        ui.label("复制岗位默认权限").classes("text-lg font-bold")
                                    ui.label(
                                        "复制会用来源岗位的当前权限完整覆盖目标岗位；"
                                        "复制完成后两者互不关联，可以继续单独调整。"
                                    ).classes("text-sm text-gray-600")
                                    with ui.row().classes(
                                        "w-full items-center no-wrap gap-3 rounded-lg bg-blue-50 p-3"
                                    ):
                                        with ui.column().classes("gap-0 flex-grow min-w-0"):
                                            ui.label("目标岗位").classes("text-xs text-gray-500")
                                            ui.label(selected["name"]).classes(
                                                "font-semibold text-blue-900 truncate"
                                            )
                                            ui.label(
                                                f"当前 {len(selected.get('permission_codes', []))} 项权限"
                                            ).classes("text-xs text-blue-700")
                                        ui.icon("arrow_back", color="primary")
                                        with ui.column().classes("gap-0 flex-grow min-w-0"):
                                            ui.label("来源岗位").classes("text-xs text-gray-500")
                                            source_summary = ui.label("请选择岗位").classes(
                                                "font-semibold text-blue-900 truncate"
                                            )
                                            source_permission_summary = ui.label(
                                                "选择后将显示权限数量"
                                            ).classes("text-xs text-blue-700")
                                    source_select = ui.select(
                                        source_options,
                                        label="选择要复制的岗位",
                                        with_input=True,
                                    ).props("outlined options-dense").classes("w-full")

                                    def refresh_source_summary():
                                        source = source_by_id.get(str(source_select.value or ""))
                                        if not source:
                                            source_summary.set_text("请选择岗位")
                                            source_permission_summary.set_text(
                                                "选择后将显示权限数量"
                                            )
                                            return
                                        source_summary.set_text(str(source.get("name", "")))
                                        source_permission_summary.set_text(
                                            f"将复制 {len(source.get('permission_codes', []))} 项权限"
                                        )

                                    def copy_position_permissions():
                                        source = source_by_id.get(str(source_select.value or ""))
                                        if not source:
                                            ui.notify("请先选择来源岗位。", type="warning")
                                            return
                                        permission_codes = list(source.get("permission_codes", []))
                                        try:
                                            user_svc.set_position_permissions(
                                                selected["position_id"],
                                                permission_codes,
                                                actor_username=current_user,
                                            )
                                        except Exception as exc:
                                            ui.notify(
                                                f"岗位权限复制失败：{exc}",
                                                type="negative",
                                                multi_line=True,
                                            )
                                            return
                                        # 先反馈结果，再刷新会删除的权限编辑区域，避免通知丢失 UI 槽位。
                                        ui.notify(
                                            f"已从“{source.get('name', '')}”复制 "
                                            f"{len(permission_codes)} 项权限。",
                                            type="positive",
                                        )
                                        copy_dialog.close()
                                        render_permission_positions()
                                        render_position_permissions()

                                    source_select.on_value_change(
                                        lambda _event: refresh_source_summary()
                                    )
                                    with ui.row().classes("w-full justify-end gap-3"):
                                        ui.button("取消", on_click=copy_dialog.close).props("flat")
                                        ui.button(
                                            "确认覆盖并复制",
                                            icon="content_copy",
                                            on_click=copy_position_permissions,
                                        ).props("color=primary")
                                copy_dialog.open()

                            with ui.row().classes("w-full items-start justify-between gap-3"):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(f"{selected['name']} · 默认权限").classes(
                                        "text-lg font-bold"
                                    )
                                    ui.label(
                                        f"稳定编码：{selected['code']} ｜ 当前任职："
                                        f"{selected.get('member_count', 0)} 人"
                                    ).classes("text-xs text-gray-500")
                                ui.button(
                                    "复制其他岗位权限",
                                    icon="content_copy",
                                    on_click=open_copy_position_permissions_dialog,
                                ).props("outline dense color=primary").classes("shrink-0")
                            ui.label(
                                "权限会自动授予所有以该岗位作为主岗位的在职用户；企业微信职务文本本身不会自动授权。"
                            ).classes("text-xs text-blue-800 bg-blue-50 rounded p-2")
                            auto_save_status = ui.label("勾选状态变化后自动保存").classes(
                                "text-xs text-green-700"
                            )
                            selected_codes = set(selected.get("permission_codes", []))

                            def auto_save_position_permissions(_event=None):
                                """任一权限勾选变化后立即保存整个岗位权限集合。"""
                                auto_save_status.set_text("正在自动保存…")
                                try:
                                    user_svc.set_position_permissions(
                                        selected["position_id"],
                                        [
                                            code
                                            for code, checkbox in checkbox_by_code.items()
                                            if checkbox.value
                                        ],
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    # 失败时先提示，再重建右侧内容以恢复数据库中的真实勾选状态。
                                    ui.notify(
                                        f"岗位权限自动保存失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    render_position_permissions()
                                    return
                                auto_save_status.set_text("已自动保存")
                                # 只刷新左侧权限数量，保留右侧滚动位置和当前勾选上下文。
                                render_permission_positions()

                            checkbox_by_code = render_permission_tables(permissions, selected_codes)
                            for checkbox in checkbox_by_code.values():
                                checkbox.on_value_change(auto_save_position_permissions)

                    render_permission_positions()
                    render_position_permissions()

                with ui.tab_panel(role_tab).classes("w-full h-full p-0 pt-3"):
                    with ui.grid(columns=2).classes("w-full h-full min-h-0 gap-4"):
                        with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                            with ui.row().classes("w-full items-center justify-between shrink-0"):
                                with ui.column().classes("gap-0"):
                                    ui.label("附加权限组").classes("text-lg font-bold")
                                    ui.label("只处理岗位之外的兼任或专项权限，修改后自动保存") \
                                        .classes("text-xs text-gray-500")
                                add_role_button = ui.button("新增权限组", icon="add").props("color=primary")
                            role_list_container = ui.column().classes(
                                "w-full flex-grow min-h-0 overflow-y-auto gap-1"
                            )

                        with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                            role_editor_container = ui.column().classes(
                                "w-full flex-grow min-h-0 overflow-y-auto gap-3"
                            )

                    role_state = {"selected_role_id": None}

                    def load_role_data():
                        roles = [
                            role
                            for role in user_svc.list_security_roles()
                            if not role.get("is_compatibility")
                        ]
                        return roles, user_svc.list_permissions()

                    def select_role(role_id):
                        role_state["selected_role_id"] = role_id
                        render_role_list()
                        render_role_editor()

                    def render_role_list():
                        roles, _ = load_role_data()
                        if not role_state["selected_role_id"] and roles:
                            role_state["selected_role_id"] = roles[0]["role_id"]
                        role_list_container.clear()
                        with role_list_container:
                            if not roles:
                                ui.label("尚无附加权限组，普通用户只需配置岗位。").classes(
                                    "text-gray-500 p-4"
                                )
                            for role in roles:
                                selected = role["role_id"] == role_state["selected_role_id"]
                                background = "bg-blue-50 border-blue-400" if selected else "bg-white border-gray-200"
                                with ui.row().classes(
                                    f"w-full items-center border rounded p-3 cursor-pointer {background}"
                                ).on(
                                    "click",
                                    lambda _event, role_id=role["role_id"]: select_role(role_id),
                                ):
                                    with ui.column().classes("gap-0 flex-grow"):
                                        with ui.row().classes("items-center gap-2"):
                                            ui.label(role["name"]).classes("font-semibold")
                                            if role.get("status") != "active":
                                                ui.chip("已停用", color="negative").props("dense").classes("text-xs")
                                        ui.label(role["code"]).classes("text-xs text-gray-500")
                                    ui.label(
                                        f"{len(role.get('permission_codes', []))} 项权限 / "
                                        f"{role.get('user_count', 0)} 人"
                                    ).classes("text-xs text-gray-500")

                    def render_role_editor():
                        roles, permissions = load_role_data()
                        selected = next(
                            (
                                role
                                for role in roles
                                if role["role_id"] == role_state["selected_role_id"]
                            ),
                            None,
                        )
                        role_editor_container.clear()
                        with role_editor_container:
                            if not selected:
                                ui.label("请选择一个附加权限组。").classes("text-gray-500 p-4")
                                return
                            ui.label("权限组配置").classes("text-lg font-bold")
                            code_input = ui.input("稳定编码", value=selected["code"]).classes("w-full")
                            code_input.disable()
                            name_input = ui.input(
                                "显示名称",
                                value=selected["name"],
                            ).props("debounce=500").classes("w-full")
                            active_switch = ui.switch(
                                "权限组启用",
                                value=selected.get("status") == "active",
                            )
                            auto_save_status = ui.label("内容变化后自动保存").classes(
                                "text-xs text-green-700"
                            )
                            ui.separator()
                            ui.label("权限清单").classes("font-bold")
                            selected_codes = set(selected.get("permission_codes", []))
                            checkbox_by_code = render_permission_tables(permissions, selected_codes)

                            def auto_save_role(_event=None):
                                """名称、启停或权限变化后立即保存整个附加权限组。"""
                                normalized_name = str(name_input.value or "").strip()
                                if not normalized_name:
                                    auto_save_status.set_text("显示名称不能为空，当前修改尚未保存")
                                    auto_save_status.classes(
                                        remove="text-green-700 text-gray-500",
                                        add="text-red-600",
                                    )
                                    return
                                auto_save_status.set_text("正在自动保存…")
                                auto_save_status.classes(
                                    remove="text-red-600 text-green-700",
                                    add="text-gray-500",
                                )
                                try:
                                    user_svc.update_security_role(
                                        selected["role_id"],
                                        name=normalized_name,
                                        status="active" if active_switch.value else "disabled",
                                        permission_codes=[
                                            code for code, checkbox in checkbox_by_code.items() if checkbox.value
                                        ],
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    # 失败时先提示，再恢复数据库里的真实状态。
                                    ui.notify(
                                        f"权限组自动保存失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    render_role_editor()
                                    return
                                auto_save_status.set_text("已自动保存")
                                auto_save_status.classes(
                                    remove="text-red-600 text-gray-500",
                                    add="text-green-700",
                                )
                                # 只刷新左侧摘要，避免右侧编辑区和滚动位置复位。
                                render_role_list()

                            name_input.on_value_change(auto_save_role)
                            active_switch.on_value_change(auto_save_role)
                            for checkbox in checkbox_by_code.values():
                                checkbox.on_value_change(auto_save_role)

                    def open_add_role_form():
                        with ui.dialog() as add_dialog, ui.card().classes("w-[30rem] max-w-[95vw] p-6"):
                            ui.label("新增附加权限组").classes("text-lg font-bold")
                            ui.label(
                                "编码保存后不可修改，例如 ecn.reviewer 或 system.operator。"
                            ).classes(
                                "text-xs text-gray-500"
                            )
                            code_input = (
                                ui.input("稳定编码（保存后不可修改）")
                                .props("debounce=250")
                                .classes("w-full")
                            )
                            code_check = attach_stable_code_check(
                                code_input,
                                lambda: [
                                    role.get("code", "")
                                    for role in user_svc.list_security_roles()
                                ],
                                "附加权限组",
                            )
                            name_input = ui.input("显示名称").classes("w-full")

                            def create_role():
                                error = code_check()
                                if error:
                                    ui.notify(error, type="warning")
                                    return
                                try:
                                    role_id = user_svc.create_security_role(
                                        code=normalize_stable_code(code_input.value),
                                        name=name_input.value,
                                        actor_username=current_user,
                                    )
                                    role_state["selected_role_id"] = role_id
                                    render_role_list()
                                    render_role_editor()
                                    add_dialog.close()
                                    ui.notify("附加权限组已创建，请继续勾选权限。", type="positive")
                                except Exception as exc:
                                    ui.notify(f"权限组创建失败：{exc}", type="negative", multi_line=True)

                            with ui.row().classes("w-full justify-end gap-3"):
                                ui.button("取消", on_click=add_dialog.close).props("flat")
                                ui.button("创建", on_click=create_role).props("color=primary")
                        add_dialog.open()

                    add_role_button.on_click(open_add_role_form)
                    render_role_list()
                    render_role_editor()

                with ui.tab_panel(user_tab).classes("w-full h-full p-0 pt-3"):
                    with ui.card().classes("w-full h-full min-h-0 flex flex-col no-wrap"):
                        ui.label("为用户分配附加权限组").classes("text-lg font-bold")
                        ui.label(
                            "日常权限来自主岗位；只有兼任、专项职责或特殊管理需要在这里额外分配。"
                        ).classes("text-xs text-gray-500")
                        user_options = {
                            username: f"{info.get('display_name') or username} ｜ {username}"
                            for username, info in sorted(app.state.users_data.items())
                        }
                        user_select = ui.select(
                            user_options,
                            label="选择用户",
                            with_input=True,
                        ).props("outlined options-dense").classes("w-full max-w-2xl")
                        user_assignment_container = ui.column().classes(
                            "w-full flex-grow min-h-0 overflow-y-auto gap-3 pt-2"
                        )

                        def render_user_assignment(username=None):
                            user_assignment_container.clear()
                            with user_assignment_container:
                                if not username:
                                    ui.label("选择用户后可查看角色和最终权限。").classes("text-gray-500 p-4")
                                    return
                                all_roles = user_svc.list_security_roles(include_disabled=False)
                                compatibility_roles = user_svc.get_user_security_roles(
                                    username,
                                    include_compatibility=True,
                                )
                                additional_roles = [
                                    role for role in compatibility_roles if not role["code"].startswith("legacy.")
                                ]
                                compatibility_roles = [
                                    role for role in compatibility_roles if role["code"].startswith("legacy.")
                                ]
                                assignable_roles = [
                                    role for role in all_roles if not role.get("is_compatibility")
                                ]
                                with ui.row().classes("items-center gap-2"):
                                    ui.label("旧角色过渡信息：").classes("text-sm font-semibold")
                                    if compatibility_roles:
                                        for role in compatibility_roles:
                                            ui.chip(role["name"], color="grey").props("dense")
                                    else:
                                        ui.label("无").classes("text-sm text-gray-500")
                                role_select = ui.select(
                                    {role["role_id"]: role["name"] for role in assignable_roles},
                                    value=[role["role_id"] for role in additional_roles],
                                    label="附加权限组",
                                    multiple=True,
                                    with_input=True,
                                ).props("outlined use-chips options-dense").classes("w-full max-w-3xl")

                                effective_codes = user_svc.get_user_permission_codes(username)
                                permissions = {
                                    item["code"]: item for item in user_svc.list_permissions()
                                }
                                ui.label(f"当前最终权限：{len(effective_codes)} 项").classes("font-semibold pt-2")
                                if effective_codes:
                                    with ui.row().classes("w-full gap-2"):
                                        for code in sorted(
                                            effective_codes,
                                            key=lambda value: (
                                                permissions.get(value, {}).get("module", ""),
                                                permissions.get(value, {}).get("name", value),
                                            ),
                                        ):
                                            permission = permissions.get(code, {})
                                            ui.chip(permission.get("name", code), color="blue").props("dense").tooltip(
                                                code
                                            )
                                else:
                                    ui.label("当前没有稳定权限；尚未迁移的模块仍按兼容规则运行。").classes(
                                        "text-sm text-orange-700"
                                    )

                                def save_user_roles():
                                    try:
                                        user_svc.set_user_security_roles(
                                            username,
                                            role_select.value or [],
                                            actor_username=current_user,
                                        )
                                    except Exception as exc:
                                        ui.notify(f"用户授权失败：{exc}", type="negative", multi_line=True)
                                        return
                                    # 用户授权区域刷新会删除保存按钮所属槽位，因此先反馈结果。
                                    ui.notify("用户附加权限已保存。", type="positive")
                                    render_user_assignment(username)
                                    render_role_list()

                                with ui.row().classes("w-full justify-end"):
                                    ui.button("保存用户授权", on_click=save_user_roles, icon="save").props(
                                        "color=primary"
                                    )

                        user_select.on_value_change(lambda event: render_user_assignment(event.value))
                        render_user_assignment()

                with ui.tab_panel(workflow_tab).classes("w-full h-full p-0 pt-3"):
                    with ui.element("div").classes("w-full h-full min-h-0 grid gap-4").style(
                        "grid-template-columns: minmax(280px, 3fr) minmax(0, 7fr);"
                    ):
                        with ui.card().classes(
                            "w-full h-full min-h-0 flex flex-col no-wrap p-3 shadow-sm"
                        ):
                            with ui.row().classes("w-full items-center justify-between shrink-0"):
                                with ui.column().classes("gap-0"):
                                    ui.label("流程策略").classes("text-lg font-bold")
                                    ui.label("草稿不会影响业务，发布后才参与匹配。") \
                                        .classes("text-xs text-gray-500")
                                add_workflow_button = ui.button("新增", icon="add").props(
                                    "outline dense"
                                )
                            with ui.button(
                                "从旧配置生成草稿",
                                icon="move_to_inbox",
                            ).props("outline dense no-caps").classes("self-start shrink-0"):
                                with ui.menu().props("auto-close"):
                                    ui.menu_item(
                                        "样品问题旧配置",
                                        on_click=lambda: import_legacy_workflows(
                                            import_sample_issue_legacy_workflows,
                                            "样品问题",
                                        ),
                                    )
                                    ui.menu_item(
                                        "设计知识旧配置",
                                        on_click=lambda: import_legacy_workflows(
                                            import_design_knowledge_legacy_workflows,
                                            "设计知识",
                                        ),
                                    )
                                    ui.menu_item(
                                        "项目概述旧配置",
                                        on_click=lambda: import_legacy_workflows(
                                            import_project_overview_legacy_workflows,
                                            "项目概述",
                                        ),
                                    )
                                    ui.menu_item(
                                        "ECN旧配置",
                                        on_click=lambda: import_legacy_workflows(
                                            import_ecn_legacy_workflows,
                                            "ECN",
                                        ),
                                    )
                            workflow_list_container = ui.column().classes(
                                "w-full flex-grow min-h-0 overflow-y-auto gap-2 pt-2"
                            )
                        with ui.card().classes(
                            "w-full h-full min-h-0 flex flex-col no-wrap p-3 shadow-sm"
                        ):
                            workflow_editor_container = ui.column().classes(
                                "w-full flex-grow min-h-0 overflow-y-auto gap-3"
                            )

                    workflow_state = {"selected_workflow_id": None}

                    def workflow_event_options():
                        return {
                            definition.key: definition.name
                            for definition in APPROVAL_WORKFLOW_EVENTS
                        }

                    def workflow_reference_options():
                        units = [
                            item for item in user_svc.list_org_units()
                            if item.get("status") == "active"
                        ]
                        positions = [
                            item for item in user_svc.list_positions()
                            if item.get("status") == "active"
                        ]
                        users = user_svc.load_users()
                        unit_options = {
                            item["org_unit_id"]: f"{item['name']} ｜ {item['code']}"
                            for item in units
                        }
                        position_options = {
                            item["position_id"]: (
                                f"{item['name']} ｜ "
                                f"{'、'.join(item.get('org_names', [])) or '未划分部门'}"
                            )
                            for item in positions
                        }
                        user_options = {
                            info["user_id"]: f"{info.get('display_name') or username} ｜ {username}"
                            for username, info in sorted(
                                users.items(),
                                key=lambda item: (
                                    str(item[1].get("display_name") or item[0]).casefold(),
                                    item[0].casefold(),
                                ),
                            )
                            if info.get("status", "active") == "active"
                        }
                        simulation_options = {
                            username: f"{info.get('display_name') or username} ｜ {username}"
                            for username, info in sorted(
                                users.items(),
                                key=lambda item: (
                                    str(item[1].get("display_name") or item[0]).casefold(),
                                    item[0].casefold(),
                                ),
                            )
                            if info.get("status", "active") == "active"
                        }
                        return unit_options, position_options, user_options, simulation_options

                    def select_workflow(workflow_id):
                        workflow_state["selected_workflow_id"] = workflow_id
                        render_workflow_list()
                        render_workflow_editor()

                    def render_workflow_list():
                        workflows = user_svc.list_approval_workflows()
                        selected_id = workflow_state["selected_workflow_id"]
                        if selected_id not in {item["workflow_id"] for item in workflows}:
                            workflow_state["selected_workflow_id"] = (
                                workflows[0]["workflow_id"] if workflows else None
                            )
                        workflow_list_container.clear()
                        with workflow_list_container:
                            if not workflows:
                                ui.label("尚无流程，可新增或从旧配置生成草稿。") \
                                    .classes("text-gray-500 p-4")
                                return
                            for workflow in workflows:
                                selected = (
                                    workflow["workflow_id"]
                                    == workflow_state["selected_workflow_id"]
                                )
                                background = (
                                    "bg-blue-50 border-blue-400"
                                    if selected else "bg-white border-gray-200"
                                )
                                with ui.row().classes(
                                    f"w-full items-center border rounded-lg p-2 cursor-pointer {background}"
                                ).on(
                                    "click",
                                    lambda _event, workflow_id=workflow["workflow_id"]: (
                                        select_workflow(workflow_id)
                                    ),
                                ):
                                    with ui.column().classes("gap-0 flex-grow min-w-0"):
                                        ui.label(workflow["name"]).classes("font-semibold")
                                        ui.label(workflow["code"]).classes(
                                            "text-xs text-gray-500 truncate"
                                        )
                                    if workflow.get("draft_version"):
                                        ui.chip("有草稿", color="orange").props("dense")
                                    if workflow.get("status") == "active":
                                        ui.chip("运行中", color="positive").props("dense")
                                    elif workflow.get("status") == "disabled":
                                        ui.chip("已停用", color="grey").props("dense")

                    def render_workflow_editor():
                        workflows = user_svc.list_approval_workflows()
                        selected = next(
                            (
                                item for item in workflows
                                if item["workflow_id"]
                                == workflow_state["selected_workflow_id"]
                            ),
                            None,
                        )
                        workflow_editor_container.clear()
                        with workflow_editor_container:
                            if not selected:
                                ui.label("请选择或新增一个审批流程。") \
                                    .classes("text-gray-500 p-4")
                                return

                            version = selected.get("draft_version") or selected.get("active_version")
                            version = version or {
                                "priority": 100,
                                "condition": {},
                                "approver": {"strategy": "permission"},
                                "notification": {},
                            }
                            condition = version.get("condition", {})
                            event_key = f"{selected['module']}:{selected['event']}"
                            event_definition = next(
                                (
                                    item for item in APPROVAL_WORKFLOW_EVENTS
                                    if item.key == event_key
                                ),
                                None,
                            )
                            permission_catalog = {
                                item["code"]: item for item in user_svc.list_permissions()
                            }
                            permission_options = {
                                code: permission_catalog.get(code, {}).get("name", code)
                                for code in (
                                    event_definition.permission_codes
                                    if event_definition else ()
                                )
                            }
                            unit_options, position_options, user_options, simulation_options = (
                                workflow_reference_options()
                            )

                            initial_mode = (
                                "sequential"
                                if str(version.get("approval_mode") or "any").lower() == "sequential"
                                else "single"
                            )
                            try:
                                initial_nodes = get_approval_workflow_editor_nodes(version)
                            except ValueError as exc:
                                ui.label(f"当前流程节点配置无效：{exc}").classes(
                                    "text-sm text-red-700 bg-red-50 rounded p-2"
                                )
                                return
                            editor_state = {
                                "mode": initial_mode,
                                "nodes": initial_nodes,
                            }

                            active_version = selected.get("active_version")
                            with ui.card().classes(
                                "w-full shadow-none border border-gray-200 rounded-lg p-3 gap-3"
                            ):
                                with ui.row().classes("w-full items-start justify-between gap-3"):
                                    with ui.column().classes("gap-0 min-w-0"):
                                        ui.label(selected["name"]).classes(
                                            "text-lg font-bold truncate"
                                        )
                                        ui.label(
                                            f"稳定编码：{selected['code']} ｜ "
                                            f"当前版本：{active_version.get('version_number') if active_version else '未发布'}"
                                        ).classes("text-xs text-gray-500")
                                    if selected.get("draft_version"):
                                        ui.chip(
                                            f"草稿 v{selected['draft_version']['version_number']}",
                                            color="orange",
                                        ).props("dense").classes("shrink-0")

                                with ui.element("div").classes("w-full grid gap-3").style(
                                    "grid-template-columns: minmax(240px, 2fr) minmax(220px, 1.25fr) 150px;"
                                ):
                                    name_input = ui.input(
                                        "流程名称",
                                        value=selected["name"],
                                    ).props("outlined dense")
                                    ui.select(
                                        workflow_event_options(),
                                        value=event_key,
                                        label="业务事件",
                                    ).props("outlined dense disable options-dense")
                                    priority_input = ui.number(
                                        "优先级",
                                        value=version.get("priority", 100),
                                        min=0,
                                        step=1,
                                    ).props("outlined dense").tooltip("数字越小，匹配优先级越高")

                            # 用一条简洁路线表达实际运行顺序，降低配置表单的阅读负担。
                            with ui.element("div").classes(
                                "w-full grid items-stretch gap-2"
                            ).style(
                                "grid-template-columns: minmax(120px, 1fr) 22px minmax(120px, 1fr) "
                                "22px minmax(120px, 1fr) 22px minmax(120px, 1fr);"
                            ):
                                with ui.row().classes(
                                    "items-center no-wrap gap-2 rounded-lg bg-slate-50 border px-3 py-2"
                                ):
                                    ui.icon("description", color="blue-grey", size="sm")
                                    with ui.column().classes("gap-0"):
                                        ui.label("提交申请").classes("text-sm font-semibold")
                                        ui.label("业务事件进入流程").classes("text-xs text-gray-500")
                                ui.icon("arrow_forward", color="blue-grey").classes("self-center")
                                with ui.row().classes(
                                    "items-center no-wrap gap-2 rounded-lg bg-blue-50 border border-blue-100 px-3 py-2"
                                ):
                                    ui.icon("filter_alt", color="primary", size="sm")
                                    with ui.column().classes("gap-0"):
                                        ui.label("条件匹配").classes("text-sm font-semibold text-blue-900")
                                        ui.label("部门、岗位、优先级").classes("text-xs text-blue-700")
                                ui.icon("arrow_forward", color="primary").classes("self-center")
                                with ui.row().classes(
                                    "items-center no-wrap gap-2 rounded-lg bg-indigo-50 border border-indigo-100 px-3 py-2"
                                ):
                                    ui.icon("account_tree", color="indigo", size="sm")
                                    with ui.column().classes("gap-0"):
                                        ui.label("审批节点").classes("text-sm font-semibold text-indigo-900")
                                        flow_node_summary = ui.label(
                                            f"{len(initial_nodes)} 个节点 · "
                                            f"{'依次审批' if initial_mode == 'sequential' else '单节点'}"
                                        ).classes("text-xs text-indigo-700")
                                ui.icon("arrow_forward", color="positive").classes("self-center")
                                with ui.row().classes(
                                    "items-center no-wrap gap-2 rounded-lg bg-green-50 border border-green-100 px-3 py-2"
                                ):
                                    ui.icon("task_alt", color="positive", size="sm")
                                    with ui.column().classes("gap-0"):
                                        ui.label("流程完成").classes("text-sm font-semibold text-green-900")
                                        ui.label("通过或驳回").classes("text-xs text-green-700")

                            with ui.card().classes(
                                "w-full shadow-none border border-gray-200 rounded-lg p-3 gap-3"
                            ):
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.icon("filter_alt", color="primary", size="sm")
                                    ui.label("触发条件").classes("font-bold")
                                    ui.badge("留空表示全部", color="grey").props("outline")
                                    ui.space()
                                    include_children_switch = ui.switch(
                                        "包含下级部门",
                                        value=condition.get("include_child_org_units", True),
                                    ).props("dense")
                                with ui.element("div").classes("w-full grid gap-3").style(
                                    "grid-template-columns: repeat(2, minmax(260px, 1fr));"
                                ):
                                    requester_org_select = ui.select(
                                        unit_options,
                                        value=condition.get("requester_org_unit_ids", []),
                                        label="申请人所属部门",
                                        multiple=True,
                                        with_input=True,
                                    ).props("outlined dense use-chips options-dense")
                                    requester_position_select = ui.select(
                                        position_options,
                                        value=condition.get("requester_position_ids", []),
                                        label="申请人岗位",
                                        multiple=True,
                                        with_input=True,
                                    ).props("outlined dense use-chips options-dense")

                            with ui.row().classes("w-full items-center gap-3 pt-1"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("account_tree", color="indigo", size="sm")
                                    ui.label("审批路线").classes("font-bold")
                                    ui.icon("help_outline", color="grey", size="xs").tooltip(
                                        f"节点编码格式：{STABLE_CODE_HINT}；同一流程内不能重复。"
                                    )
                                ui.space()
                                workflow_mode_select = ui.select(
                                    {
                                        "single": "单节点审批",
                                        "sequential": "多节点串行审批",
                                    },
                                    value=initial_mode,
                                    label="执行方式",
                                ).props("outlined dense options-dense").classes("w-52")
                            node_editor_container = ui.column().classes("w-full gap-0")

                            def update_node_value(node, key, value):
                                node[key] = value

                            def update_node_approver(node, key, value):
                                node.setdefault("approver", {})[key] = value

                            def update_flow_node_summary():
                                """同步顶部流程概览，避免编辑中的节点数量和模式显示滞后。"""
                                node_count = (
                                    len(editor_state["nodes"])
                                    if editor_state["mode"] == "sequential"
                                    else min(len(editor_state["nodes"]), 1)
                                )
                                mode_name = (
                                    "依次审批"
                                    if editor_state["mode"] == "sequential"
                                    else "单节点"
                                )
                                flow_node_summary.text = f"{node_count} 个节点 · {mode_name}"

                            def move_approval_node(index, offset):
                                nodes = editor_state["nodes"]
                                target_index = index + offset
                                if target_index < 0 or target_index >= len(nodes):
                                    return
                                nodes[index], nodes[target_index] = nodes[target_index], nodes[index]
                                render_approval_nodes()

                            def remove_approval_node(index):
                                nodes = editor_state["nodes"]
                                minimum_nodes = 2 if editor_state["mode"] == "sequential" else 1
                                if len(nodes) <= minimum_nodes:
                                    message = (
                                        "多节点串行审批至少需要保留两个节点。"
                                        if minimum_nodes == 2
                                        else "审批流程至少需要保留一个节点。"
                                    )
                                    ui.notify(message, type="warning")
                                    return
                                nodes.pop(index)
                                # 先更新界面，再由新槽位承接后续操作，避免引用已删除的节点容器。
                                update_flow_node_summary()
                                render_approval_nodes()

                            def next_node_key():
                                existing_keys = {
                                    normalize_stable_code(node.get("node_key", ""))
                                    for node in editor_state["nodes"]
                                }
                                number = len(editor_state["nodes"]) + 1
                                while f"approval_{number}" in existing_keys:
                                    number += 1
                                return f"approval_{number}"

                            def add_approval_node():
                                node_number = len(editor_state["nodes"]) + 1
                                default_permission = next(iter(permission_options), "")
                                editor_state["nodes"].append(
                                    {
                                        "node_key": next_node_key(),
                                        "name": f"审批节点 {node_number}",
                                        "approval_mode": "any",
                                        "required_permission_code": default_permission,
                                        "approver": {
                                            "strategy": "permission",
                                            "permission_code": default_permission,
                                        },
                                    }
                                )
                                update_flow_node_summary()
                                render_approval_nodes()

                            def render_approval_nodes():
                                node_editor_container.clear()
                                nodes = editor_state["nodes"]
                                visible_nodes = nodes if editor_state["mode"] == "sequential" else nodes[:1]
                                with node_editor_container:
                                    for index, node in enumerate(visible_nodes):
                                        node_approver = node.setdefault("approver", {})
                                        with ui.card().classes(
                                            "relative border border-indigo-100 shadow-none bg-white "
                                            "rounded-lg p-3 gap-3 mb-3"
                                        ).style("margin-left: 2rem; width: calc(100% - 2rem); overflow: visible;"):
                                            ui.label(str(index + 1)).classes(
                                                "absolute w-8 h-8 rounded-full bg-indigo-600 text-white "
                                                "font-bold flex items-center justify-center shadow-sm"
                                            ).style("left: -2.55rem; top: 0.65rem;")
                                            if index < len(visible_nodes) - 1:
                                                ui.element("div").classes(
                                                    "absolute w-0.5 bg-indigo-200"
                                                ).style(
                                                    "left: -1.6rem; top: 2.65rem; bottom: -1.1rem;"
                                                )
                                            with ui.row().classes("w-full items-center justify-between"):
                                                with ui.row().classes("items-center gap-2"):
                                                    ui.label(
                                                        node.get("name") or "未命名节点"
                                                    ).classes("font-semibold text-indigo-900")
                                                    ui.badge("审批节点", color="indigo").props("outline")
                                                if editor_state["mode"] == "sequential":
                                                    with ui.row().classes("gap-1"):
                                                        ui.button(
                                                            icon="arrow_upward",
                                                            on_click=lambda idx=index: move_approval_node(idx, -1),
                                                        ).props(
                                                            "flat round dense"
                                                            + (" disable" if index == 0 else "")
                                                        ).tooltip("上移")
                                                        ui.button(
                                                            icon="arrow_downward",
                                                            on_click=lambda idx=index: move_approval_node(idx, 1),
                                                        ).props(
                                                            "flat round dense"
                                                            + (" disable" if index == len(visible_nodes) - 1 else "")
                                                        ).tooltip("下移")
                                                        ui.button(
                                                            icon="delete",
                                                            on_click=lambda idx=index: remove_approval_node(idx),
                                                        ).props("flat round dense color=negative").tooltip("删除节点")

                                            with ui.element("div").classes("w-full grid gap-3").style(
                                                "grid-template-columns: 190px minmax(230px, 1fr) 180px;"
                                            ):
                                                ui.input(
                                                    "节点编码",
                                                    value=node.get("node_key", ""),
                                                    on_change=lambda event, target=node: update_node_value(
                                                        target, "node_key", event.value
                                                    ),
                                                ).props("outlined dense").tooltip(STABLE_CODE_HINT)
                                                ui.input(
                                                    "节点名称",
                                                    value=node.get("name", ""),
                                                    on_change=lambda event, target=node: update_node_value(
                                                        target, "name", event.value
                                                    ),
                                                ).props("outlined dense")
                                                ui.select(
                                                    {
                                                        "any": "任意一人处理",
                                                        "all": "全部人员会签",
                                                    },
                                                    value=node.get("approval_mode", "any"),
                                                    label="节点审批方式",
                                                    on_change=lambda event, target=node: update_node_value(
                                                        target, "approval_mode", event.value
                                                    ),
                                                ).props("outlined dense options-dense")

                                            with ui.element("div").classes("w-full grid gap-3").style(
                                                "grid-template-columns: minmax(260px, 1.25fr) minmax(230px, 1fr);"
                                            ):
                                                node_permission_select = ui.select(
                                                    permission_options,
                                                    value=node.get("required_permission_code"),
                                                    label="审批所需权限",
                                                ).props("outlined dense options-dense")
                                                node_strategy_select = ui.select(
                                                    APPROVER_STRATEGY_NAMES,
                                                    value=node_approver.get("strategy", "permission"),
                                                    label="审批人来源",
                                                ).props("outlined dense options-dense")

                                            with ui.element("div").classes(
                                                "w-full grid gap-3 items-start"
                                            ).style(
                                                "grid-template-columns: minmax(260px, 1.35fr) "
                                                "minmax(190px, 0.8fr) minmax(260px, 1.2fr);"
                                            ) as node_position_settings:
                                                node_position_select = ui.select(
                                                    position_options,
                                                    value=node_approver.get("position_ids", []),
                                                    label="审批岗位",
                                                    multiple=True,
                                                    with_input=True,
                                                ).props("outlined dense use-chips options-dense")
                                                node_org_scope_select = ui.select(
                                                    {
                                                        "any": "不限部门",
                                                        "requester": "与申请人同部门",
                                                        "fixed": "限定到指定部门",
                                                    },
                                                    value=node_approver.get("org_scope", "any"),
                                                    label="部门范围",
                                                ).props("outlined dense options-dense")
                                                node_org_select = ui.select(
                                                    unit_options,
                                                    value=node_approver.get("org_unit_ids", []),
                                                    label="指定审批部门",
                                                    multiple=True,
                                                    with_input=True,
                                                ).props("outlined dense use-chips options-dense")
                                            with ui.element("div").classes(
                                                "grid gap-3"
                                            ).style(
                                                "grid-template-columns: minmax(320px, 640px);"
                                            ) as node_user_settings:
                                                node_user_select = ui.select(
                                                    user_options,
                                                    value=node_approver.get("user_ids", []),
                                                    label="指定审批人员",
                                                    multiple=True,
                                                    with_input=True,
                                                ).props("outlined dense use-chips options-dense")
                                            with ui.row().classes(
                                                "items-center gap-2 self-start bg-blue-50 rounded px-3 py-2"
                                            ) as node_manager_hint:
                                                ui.icon("info", color="primary", size="xs")
                                                ui.label(
                                                    "读取申请人的直属上级，并复核该节点审批权限。"
                                                ).classes("text-xs text-blue-800")
                                            with ui.row().classes(
                                                "items-center gap-2 self-start bg-blue-50 rounded px-3 py-2"
                                            ) as node_permission_hint:
                                                ui.icon("info", color="primary", size="xs")
                                                ui.label(
                                                    "匹配拥有该权限的在职用户；admin 不因管理员身份自动入选。"
                                                ).classes("text-xs text-blue-800")

                                            def update_permission(
                                                _event=None,
                                                *,
                                                target=node,
                                                permission_control=node_permission_select,
                                            ):
                                                permission_code = permission_control.value
                                                target["required_permission_code"] = permission_code
                                                if target.setdefault("approver", {}).get("strategy") == "permission":
                                                    target["approver"]["permission_code"] = permission_code

                                            def update_strategy(
                                                _event=None,
                                                *,
                                                target=node,
                                                strategy_control=node_strategy_select,
                                                position_container=node_position_settings,
                                                user_container=node_user_settings,
                                                manager_container=node_manager_hint,
                                                permission_container=node_permission_hint,
                                            ):
                                                strategy = strategy_control.value or "permission"
                                                target.setdefault("approver", {})["strategy"] = strategy
                                                position_container.visible = strategy == "position"
                                                user_container.visible = strategy == "users"
                                                manager_container.visible = strategy == "direct_manager"
                                                permission_container.visible = strategy == "permission"

                                            def update_node_org_scope(
                                                _event=None,
                                                *,
                                                target=node,
                                                scope_control=node_org_scope_select,
                                                org_container=node_org_select,
                                            ):
                                                scope = scope_control.value or "any"
                                                target.setdefault("approver", {})["org_scope"] = scope
                                                org_container.visible = scope == "fixed"

                                            node_permission_select.on_value_change(update_permission)
                                            node_strategy_select.on_value_change(update_strategy)
                                            node_position_select.on_value_change(
                                                lambda event, target=node: update_node_approver(
                                                    target, "position_ids", event.value or []
                                                )
                                            )
                                            node_org_scope_select.on_value_change(update_node_org_scope)
                                            node_org_select.on_value_change(
                                                lambda event, target=node: update_node_approver(
                                                    target, "org_unit_ids", event.value or []
                                                )
                                            )
                                            node_user_select.on_value_change(
                                                lambda event, target=node: update_node_approver(
                                                    target, "user_ids", event.value or []
                                                )
                                            )
                                            update_permission()
                                            update_strategy()
                                            update_node_org_scope()

                            def update_workflow_mode():
                                editor_state["mode"] = workflow_mode_select.value or "single"
                                add_node_button.visible = editor_state["mode"] == "sequential"
                                multi_node_warning.visible = (
                                    editor_state["mode"] == "sequential"
                                    and not bool(event_definition and event_definition.supports_sequential)
                                )
                                update_flow_node_summary()
                                render_approval_nodes()

                            workflow_mode_select.on_value_change(lambda _event: update_workflow_mode())
                            with ui.row().classes("w-full items-center justify-between"):
                                multi_node_warning = ui.label(
                                    "当前业务事件尚未接入多节点执行接口：可以保存草稿，但暂不能发布。"
                                ).classes("text-xs text-orange-700 bg-orange-50 rounded p-2")
                                add_node_button = ui.button(
                                    "添加审批节点",
                                    icon="add",
                                    on_click=add_approval_node,
                                ).props("outline dense color=primary")
                            add_node_button.visible = initial_mode == "sequential"
                            multi_node_warning.visible = (
                                initial_mode == "sequential"
                                and not bool(event_definition and event_definition.supports_sequential)
                            )
                            render_approval_nodes()

                            def build_node_payload(node, index):
                                node_key = normalize_stable_code(node.get("node_key", ""))
                                code_error = validate_stable_code(node_key)
                                if code_error:
                                    raise ValueError(f"第 {index + 1} 个审批节点编码无效：{code_error}")
                                node_name = str(node.get("name") or "").strip()
                                if not node_name:
                                    raise ValueError(f"第 {index + 1} 个审批节点名称不能为空")
                                permission_code = str(node.get("required_permission_code") or "").strip()
                                if permission_code not in permission_options:
                                    raise ValueError(f"{node_name}没有选择当前业务事件允许的审批权限")
                                node_mode = str(node.get("approval_mode") or "any")
                                if node_mode not in {"any", "all"}:
                                    raise ValueError(f"{node_name}的审批方式无效")
                                source_approver = node.get("approver", {})
                                strategy = str(source_approver.get("strategy") or "permission")
                                if strategy not in APPROVER_STRATEGY_NAMES:
                                    raise ValueError(f"{node_name}的审批人来源无效")
                                approver_rule: dict[str, object] = {"strategy": strategy}
                                if strategy == "position":
                                    position_ids = source_approver.get("position_ids") or []
                                    if not position_ids:
                                        raise ValueError(f"{node_name}至少需要选择一个审批岗位")
                                    approver_rule.update(
                                        {
                                            "position_ids": position_ids,
                                            "org_scope": source_approver.get("org_scope") or "any",
                                            "org_unit_ids": source_approver.get("org_unit_ids") or [],
                                        }
                                    )
                                elif strategy == "users":
                                    user_ids = source_approver.get("user_ids") or []
                                    if not user_ids:
                                        raise ValueError(f"{node_name}至少需要选择一名审批人员")
                                    approver_rule["user_ids"] = user_ids
                                elif strategy == "permission":
                                    approver_rule["permission_code"] = permission_code
                                return {
                                    "node_key": node_key,
                                    "name": node_name,
                                    "approval_mode": node_mode,
                                    "required_permission_code": permission_code,
                                    "approver": approver_rule,
                                }

                            def build_workflow_approval_config():
                                source_nodes = (
                                    editor_state["nodes"]
                                    if editor_state["mode"] == "sequential"
                                    else editor_state["nodes"][:1]
                                )
                                if not source_nodes:
                                    raise ValueError("审批流程至少需要一个审批节点")
                                if editor_state["mode"] == "sequential" and len(source_nodes) < 2:
                                    raise ValueError("多节点串行审批至少需要配置两个节点")
                                nodes = [
                                    build_node_payload(node, index)
                                    for index, node in enumerate(source_nodes)
                                ]
                                node_keys = [node["node_key"] for node in nodes]
                                if len(node_keys) != len(set(node_keys)):
                                    raise ValueError("同一流程内的审批节点编码不能重复")
                                if editor_state["mode"] == "sequential":
                                    return {
                                        "approver": {"nodes": nodes},
                                        "required_permission_code": nodes[0]["required_permission_code"],
                                        "approval_mode": "sequential",
                                    }
                                first_node = nodes[0]
                                return {
                                    "approver": first_node["approver"],
                                    "required_permission_code": first_node["required_permission_code"],
                                    "approval_mode": first_node["approval_mode"],
                                }

                            def save_workflow_draft():
                                try:
                                    approval_config = build_workflow_approval_config()
                                    user_svc.save_approval_workflow_draft(
                                        workflow_id=selected["workflow_id"],
                                        code=selected["code"],
                                        module=selected["module"],
                                        event=selected["event"],
                                        name=name_input.value,
                                        priority=int(priority_input.value or 0),
                                        condition={
                                            "requester_org_unit_ids": requester_org_select.value or [],
                                            "requester_position_ids": requester_position_select.value or [],
                                            "include_child_org_units": bool(
                                                include_children_switch.value
                                            ),
                                        },
                                        approver=approval_config["approver"],
                                        required_permission_code=approval_config[
                                            "required_permission_code"
                                        ],
                                        approval_mode=approval_config["approval_mode"],
                                        notification={
                                            "notify_assignees": True,
                                            "notify_requester_on_result": True,
                                        },
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    ui.notify(
                                        f"流程草稿保存失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    return
                                ui.notify("流程草稿已保存，尚未影响实际业务。", type="positive")
                                render_workflow_list()
                                render_workflow_editor()

                            def publish_workflow():
                                draft_version = selected.get("draft_version") or {}
                                if (
                                    str(draft_version.get("approval_mode") or "any") == "sequential"
                                    and not bool(event_definition and event_definition.supports_sequential)
                                ):
                                    ui.notify(
                                        "该业务事件尚未接入多节点执行接口，多节点草稿暂不能发布。",
                                        type="warning",
                                    )
                                    return
                                try:
                                    user_svc.publish_approval_workflow(
                                        selected["workflow_id"],
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    ui.notify(
                                        f"流程发布失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    return
                                ui.notify("流程已发布，后续新申请将使用该版本。", type="positive")
                                render_workflow_list()
                                render_workflow_editor()

                            def toggle_workflow_status():
                                target_status = (
                                    "disabled" if selected.get("status") == "active" else "active"
                                )
                                try:
                                    user_svc.set_approval_workflow_status(
                                        selected["workflow_id"],
                                        target_status,
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    ui.notify(
                                        f"流程状态修改失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    return
                                ui.notify(
                                    "流程已停用。" if target_status == "disabled" else "流程已启用。",
                                    type="positive",
                                )
                                render_workflow_list()
                                render_workflow_editor()

                            with ui.row().classes("w-full justify-end gap-2"):
                                if selected.get("active_version"):
                                    ui.button(
                                        "停用" if selected.get("status") == "active" else "启用",
                                        on_click=toggle_workflow_status,
                                        icon="pause_circle" if selected.get("status") == "active" else "play_circle",
                                    ).props("outline")
                                ui.button(
                                    "保存草稿",
                                    on_click=save_workflow_draft,
                                    icon="save",
                                ).props("outline color=primary")
                                if selected.get("draft_version"):
                                    ui.button(
                                        "发布草稿",
                                        on_click=publish_workflow,
                                        icon="publish",
                                    ).props("color=primary")

                            with ui.card().classes(
                                "w-full shadow-none border border-gray-200 rounded-lg p-3 gap-3"
                            ):
                                with ui.row().classes("w-full items-center gap-3"):
                                    with ui.column().classes("gap-0 mr-2"):
                                        ui.label("发布结果模拟").classes("font-bold")
                                        ui.label("仅读取已发布版本，不生成待办。") \
                                            .classes("text-xs text-gray-500")
                                    simulation_user_select = ui.select(
                                        simulation_options,
                                        label="选择申请人",
                                        with_input=True,
                                    ).props("outlined dense options-dense").classes("w-80")
                                    simulate_button = ui.button(
                                        "模拟匹配",
                                        icon="science",
                                    ).props("outline dense")
                                simulation_result = ui.column().classes(
                                    "w-full gap-1 bg-gray-50 border rounded p-3"
                                )

                            def simulate_workflow():
                                simulation_result.clear()
                                with simulation_result:
                                    if not simulation_user_select.value:
                                        ui.label("请先选择申请人。") \
                                            .classes("text-orange-700")
                                        return
                                    result = resolve_approval_workflow(
                                        user_svc,
                                        module=selected["module"],
                                        event=selected["event"],
                                        requester_username=simulation_user_select.value,
                                    )
                                    result_type = (
                                        "text-green-700"
                                        if result.get("status") == "matched"
                                        else "text-red-700"
                                    )
                                    ui.label(result.get("message", "未知结果")).classes(
                                        f"font-semibold {result_type}"
                                    )
                                    if result.get("workflow"):
                                        ui.label(
                                            f"命中流程：{result['workflow']['name']}"
                                        ).classes("text-sm")
                                    approvers = result.get("approvers", [])
                                    if approvers:
                                        ui.label(
                                            "实际审批人："
                                            + "、".join(
                                                item.get("display_name") or item["username"]
                                                for item in approvers
                                            )
                                        ).classes("text-sm")
                                    approval_nodes = result.get("approval_nodes", [])
                                    if approval_nodes:
                                        ui.label("审批节点顺序：").classes("text-sm font-semibold mt-1")
                                        for node in approval_nodes:
                                            mode_name = (
                                                "全部会签"
                                                if node.get("approval_mode") == "all"
                                                else "任意一人"
                                            )
                                            names = "、".join(
                                                item.get("display_name") or item.get("username", "")
                                                for item in node.get("approvers", [])
                                            )
                                            ui.label(
                                                f"第 {int(node.get('node_index', 0)) + 1} 步 "
                                                f"{node.get('name', '审批')}（{mode_name}）：{names or '无'}"
                                            ).classes("text-xs")
                                    for warning in result.get("warnings", []):
                                        ui.label(f"提醒：{warning}").classes(
                                            "text-xs text-orange-700"
                                        )

                            simulate_button.on_click(simulate_workflow)

                    def open_add_workflow_form():
                        event_options = workflow_event_options()
                        with ui.dialog() as add_dialog, ui.card().classes(
                            "w-[34rem] max-w-[95vw] p-6"
                        ):
                            ui.label("新增审批流程草稿").classes("text-lg font-bold")
                            ui.label(
                                "编码保存后不可修改，例如 sample_issue.close.hardware。"
                            ).classes("text-xs text-gray-500")
                            code_input = ui.input("稳定编码（保存后不可修改）").props(
                                "debounce=250"
                            ).classes("w-full")
                            code_check = attach_stable_code_check(
                                code_input,
                                lambda: [
                                    workflow.get("code", "")
                                    for workflow in user_svc.list_approval_workflows()
                                ],
                                "审批流程",
                            )
                            name_input = ui.input("流程名称").classes("w-full")
                            event_select = ui.select(
                                event_options,
                                value=next(iter(event_options), None),
                                label="业务事件",
                            ).props("outlined options-dense").classes("w-full")

                            def create_workflow():
                                error = code_check()
                                if error:
                                    ui.notify(error, type="warning")
                                    return
                                definition = next(
                                    (
                                        item for item in APPROVAL_WORKFLOW_EVENTS
                                        if item.key == event_select.value
                                    ),
                                    None,
                                )
                                if not definition:
                                    ui.notify("请选择有效的业务事件。", type="warning")
                                    return
                                try:
                                    workflow_id, _version_id = user_svc.save_approval_workflow_draft(
                                        code=normalize_stable_code(code_input.value),
                                        module=definition.module,
                                        event=definition.event,
                                        name=name_input.value,
                                        priority=100,
                                        condition={
                                            "requester_org_unit_ids": [],
                                            "requester_position_ids": [],
                                            "include_child_org_units": True,
                                        },
                                        approver={
                                            "strategy": "permission",
                                            "permission_code": definition.permission_codes[0],
                                        },
                                        required_permission_code=definition.permission_codes[0],
                                        approval_mode="any",
                                        notification={
                                            "notify_assignees": True,
                                            "notify_requester_on_result": True,
                                        },
                                        actor_username=current_user,
                                    )
                                except Exception as exc:
                                    ui.notify(
                                        f"流程创建失败：{exc}",
                                        type="negative",
                                        multi_line=True,
                                    )
                                    return
                                workflow_state["selected_workflow_id"] = workflow_id
                                add_dialog.close()
                                ui.notify("流程草稿已创建，请继续配置后再发布。", type="positive")
                                render_workflow_list()
                                render_workflow_editor()

                            with ui.row().classes("w-full justify-end gap-2"):
                                ui.button("取消", on_click=add_dialog.close).props("flat")
                                ui.button("创建草稿", on_click=create_workflow).props(
                                    "color=primary"
                                )
                        add_dialog.open()

                    def import_legacy_workflows(importer, source_name):
                        try:
                            created, warnings = importer(
                                user_svc,
                                actor_username=current_user,
                            )
                        except Exception as exc:
                            ui.notify(
                                f"{source_name}旧配置导入失败：{exc}",
                                type="negative",
                                multi_line=True,
                            )
                            return
                        message = f"已生成 {created} 条流程草稿。"
                        if warnings:
                            message += "\n" + "\n".join(warnings)
                        ui.notify(
                            message,
                            type="warning" if warnings else "positive",
                            multi_line=True,
                        )
                        render_workflow_list()
                        render_workflow_editor()

                    add_workflow_button.on_click(open_add_workflow_form)
                    render_workflow_list()
                    render_workflow_editor()

        role_dialog.open()

    # --- 用户管理界面的定义 (抛弃 Table，使用原生卡片列表) ---
    def open_user_management_dialog():
        # 1. 弹窗容器：响应式尺寸，严格控制内外边距和溢出
        with ui.dialog() as dialog, ui.card().classes("w-[90vw] max-w-5xl h-[85vh] p-4 flex flex-col no-wrap"):
            # 2. 顶部标题栏
            with ui.row().classes("w-full items-center justify-between shrink-0 mb-2"):
                with ui.column().classes("gap-0"):
                    ui.label("用户、账号及外部身份管理").classes("text-xl font-bold")
                    ui.label(
                        f"当前用户数据源：{'身份数据库' if app.state.user_service.storage_mode == 'database' else '旧版 Excel'}"
                    ).classes("text-xs text-gray-500")
                    ui.label("绿色：资料完整 ｜ 黄色：仍有待补项 ｜ 灰色：已停用或离职").classes(
                        "text-xs text-gray-500"
                    )
                ui.icon("close", size="sm").classes("cursor-pointer").on("click", dialog.close)

            ui.separator().classes("shrink-0 mb-2")

            # 3. 核心交互函数定义
            async def save_user(action, target_username, form_pwd, form_role, form_dialog):
                try:
                    user_svc = app.state.user_service
                    user_svc.modify_user(action, target_username, form_pwd, form_role)
                    # 更新内存数据
                    app.state.users_data = user_svc.load_users()
                    # 重新渲染列表
                    await refresh_user_list_preserving_scroll()
                    ui.notify("用户数据保存成功！", type="positive")
                    form_dialog.close()
                except Exception as e:
                    ui.notify(f"保存失败: {str(e)}", type="negative")

            def open_form(action="add", target_username=None):
                user_info = app.state.users_data.get(target_username, {}) if target_username else {}

                with ui.dialog() as form_dialog, ui.card().classes("w-96 p-6"):
                    title = "新增系统用户" if action == "add" else f"编辑用户: {target_username}"
                    ui.label(title).classes("text-lg font-bold mb-4")

                    username_input = ui.input("用户名", value=target_username or "").classes("w-full mb-2")
                    password_label = "初始密码" if action == "add" else "重置密码（留空表示保持不变）"
                    password_input = ui.input(password_label, password=True, password_toggle_button=True).classes(
                        "w-full mb-2"
                    )

                    # 【核心修改区】：去掉下拉菜单逻辑，直接使用普通的文本输入框
                    current_role = user_info.get("role", "普通用户")
                    role_input = ui.input("角色", value=current_role).classes("w-full mb-6")

                    if action == "edit":
                        username_input.disable()

                    with ui.row().classes("w-full justify-end gap-4"):
                        ui.button("取消", on_click=form_dialog.close).props("flat color=grey")
                        ui.button(
                            "保存",
                            on_click=lambda: save_user(
                                action, username_input.value, password_input.value, role_input.value, form_dialog
                            ),
                        ).props("color=primary")
                form_dialog.open()

            def open_wecom_binding_form(target_user):
                if app.state.user_service.storage_mode != "database":
                    ui.notify("请先执行一键迁移，再绑定企业微信账号。", type="warning")
                    return
                if str(target_user).strip().casefold() == "admin":
                    ui.notify(
                        "admin 是系统管理账号，无需绑定企业微信；同一企业微信成员仍只允许绑定一个员工账号。",
                        type="info",
                        multi_line=True,
                    )
                    return

                cache_data = load_wecom_contacts_cache()

                def contact_name_sort_key(item):
                    name = str(item.get("name", "")).strip().casefold()
                    # 使用 GB18030 让常见中文姓氏接近拼音顺序，避免增加拼音运行依赖。
                    return (
                        name.encode("gb18030", errors="replace"),
                        str(item.get("userid", "")).casefold(),
                    )

                contacts = sorted(
                    [
                        item
                        for item in cache_data.get("contacts", [])
                        if item.get("userid") and item.get("is_active", True)
                    ],
                    key=contact_name_sort_key,
                )
                contact_map = {str(item["userid"]): item for item in contacts}
                options = {
                    str(item["userid"]): (
                        f"{item.get('name', '')} ｜ {item.get('userid', '')} ｜ "
                        f"{'、'.join(item.get('departments', [])) or '未标部门'} ｜ "
                        f"{item.get('position', '') or '未填职务'}"
                    )
                    for item in contacts
                }
                current_binding = app.state.user_service.get_wecom_binding(target_user)
                auto_suggestion = (
                    {} if current_binding else app.state.user_service.suggest_wecom_contact(target_user, contacts)
                )
                suggested_contact = auto_suggestion.get("contact")
                initial_userid = current_binding.get("external_userid")
                if not initial_userid and isinstance(suggested_contact, dict):
                    initial_userid = suggested_contact.get("userid")

                with ui.dialog() as binding_dialog, ui.card().classes("w-[44rem] max-w-[95vw] p-6"):
                    ui.label(f"绑定企业微信：{target_user}").classes("text-lg font-bold")
                    ui.label("同一企业微信账号只能绑定一个系统用户；绑定不会自动导入部门或岗位字典。").classes(
                        "text-xs text-gray-500"
                    )
                    binding_select = (
                        ui.select(
                            options=options,
                            value=initial_userid,
                            label="企业微信成员",
                            with_input=True,
                            clearable=True,
                        )
                        .props("outlined options-dense")
                        .classes("w-full")
                    )
                    if auto_suggestion:
                        suggestion_type = "positive" if auto_suggestion.get("status") == "matched" else "warning"
                        ui.label(f"自动匹配：{auto_suggestion.get('reason', '未找到候选')}").classes(
                            "text-xs text-green-700" if suggestion_type == "positive" else "text-xs text-orange-700"
                        )

                    async def save_binding():
                        try:
                            if binding_select.value:
                                selected_contact = contact_map[str(binding_select.value)]
                                app.state.user_service.bind_wecom_user(
                                    target_user,
                                    selected_contact,
                                )
                                org_assigned = app.state.user_service.apply_suggested_org_membership(
                                    target_user,
                                    selected_contact,
                                )
                                message = "企业微信账号绑定成功。"
                                if org_assigned:
                                    message += " 已自动补齐部门和可匹配岗位。"
                                else:
                                    message += " 如需自动补齐任职，请先在组织架构中勾选导入对应部门和岗位。"
                                ui.notify(message, type="positive")
                            else:
                                app.state.user_service.unbind_wecom_user(target_user)
                                ui.notify("企业微信账号绑定已解除。", type="positive")
                            await refresh_user_list_preserving_scroll()
                            binding_dialog.close()
                        except Exception as exc:
                            ui.notify(f"绑定失败：{exc}", type="negative", multi_line=True)

                    with ui.row().classes("w-full justify-end gap-3"):
                        ui.button("取消", on_click=binding_dialog.close).props("flat")
                        ui.button("保存绑定", on_click=save_binding).props("color=primary icon=link")
                binding_dialog.open()

            def open_membership_form(target_user):
                if app.state.user_service.storage_mode != "database":
                    ui.notify("请先执行一键迁移，再分配部门和岗位。", type="warning")
                    return
                units = app.state.user_service.list_org_units()
                if not units:
                    ui.notify("请先在组织架构管理中建立部门。", type="warning")
                    return
                positions = app.state.user_service.list_positions()
                current = app.state.user_service.get_primary_membership(target_user)
                binding = app.state.user_service.get_wecom_binding(target_user)
                contact = next(
                    (
                        item
                        for item in load_wecom_contacts_cache().get("contacts", [])
                        if str(item.get("userid", "")) == str(binding.get("external_userid", ""))
                    ),
                    None,
                )
                suggested = (
                    app.state.user_service.suggest_org_membership(contact)
                    if not current and isinstance(contact, dict)
                    else {}
                )
                initial_org_unit_id = current.get("org_unit_id") or suggested.get("org_unit_id")
                initial_position_id = current.get("position_id") or suggested.get("position_id")

                def position_options_for_org(org_unit_id):
                    return {
                        item["position_id"]: item["name"]
                        for item in positions
                        if org_unit_id in item.get("org_unit_ids", [])
                    }

                active_users = {
                    username: info.get("display_name") or username
                    for username, info in app.state.users_data.items()
                    if username != target_user and info.get("status", "active") == "active"
                }
                with ui.dialog() as membership_dialog, ui.card().classes("w-[34rem] max-w-[95vw] p-6"):
                    ui.label(f"组织任职：{target_user}").classes("text-lg font-bold")
                    org_select = ui.select(
                        {item["org_unit_id"]: item["name"] for item in units},
                        value=initial_org_unit_id,
                        label="主部门",
                        with_input=True,
                    ).classes("w-full")
                    position_select = ui.select(
                        position_options_for_org(initial_org_unit_id),
                        value=initial_position_id,
                        label="岗位",
                        with_input=True,
                        clearable=True,
                    ).classes("w-full")

                    def refresh_position_options(event):
                        options = position_options_for_org(event.value)
                        current_position_id = position_select.value
                        position_select.set_options(
                            options,
                            value=current_position_id if current_position_id in options else None,
                        )

                    org_select.on_value_change(refresh_position_options)
                    manager_select = ui.select(
                        active_users,
                        value=current.get("manager_username"),
                        label="直属上级",
                        with_input=True,
                        clearable=True,
                    ).classes("w-full")
                    ui.label("直属上级将作为离职上交和后续审批策略的首选解析对象。").classes("text-xs text-gray-500")
                    ui.label("岗位列表会随主部门自动过滤。").classes("text-xs text-blue-700")
                    if suggested:
                        ui.label(
                            f"企业微信建议：部门 {suggested.get('org_name') or '未匹配'}；"
                            f"岗位 {suggested.get('position_name') or '未匹配'}。"
                        ).classes("text-xs text-blue-700")

                    async def save_membership():
                        if not org_select.value:
                            ui.notify("请选择主部门。", type="warning")
                            return
                        try:
                            app.state.user_service.set_primary_membership(
                                target_user,
                                org_unit_id=org_select.value,
                                position_id=position_select.value,
                                manager_username=manager_select.value,
                            )
                            await refresh_user_list_preserving_scroll()
                            membership_dialog.close()
                            ui.notify("组织任职已保存。", type="positive")
                        except Exception as exc:
                            ui.notify(f"任职保存失败：{exc}", type="negative")

                    with ui.row().classes("w-full justify-end gap-3"):
                        ui.button("取消", on_click=membership_dialog.close).props("flat")
                        ui.button("保存", on_click=save_membership).props("color=primary")
                membership_dialog.open()

            def open_auto_match_dialog():
                if app.state.user_service.storage_mode != "database":
                    ui.notify("请先执行一键迁移，再自动匹配企业微信账号。", type="warning")
                    return
                cache_data = load_wecom_contacts_cache()
                contacts = cache_data.get("contacts", [])
                if not contacts:
                    ui.notify("企业微信通讯录缓存为空，请先同步通讯录。", type="warning")
                    return
                plan = app.state.user_service.build_wecom_match_plan(contacts)
                rows = []
                for index, item in enumerate(plan):
                    contact = item.get("contact") if isinstance(item.get("contact"), dict) else {}
                    status = item.get("status")
                    rows.append(
                        {
                            "id": index,
                            "username": item.get("username", ""),
                            "contact": (f"{contact.get('name', '')} ({contact.get('userid', '')})" if contact else "—"),
                            "organization": (
                                f"{'、'.join(contact.get('departments', [])) or '-'} / "
                                f"{contact.get('position', '') or '-'}"
                                if contact
                                else "—"
                            ),
                            "status": {
                                "matched": "可自动绑定",
                                "ambiguous": "需要人工确认",
                                "unmatched": "未匹配",
                            }.get(status, str(status)),
                            "reason": item.get("reason", ""),
                        }
                    )
                columns = [
                    {"name": "username", "label": "系统用户", "field": "username", "align": "left"},
                    {"name": "contact", "label": "企业微信建议", "field": "contact", "align": "left"},
                    {"name": "organization", "label": "部门 / 职务", "field": "organization", "align": "left"},
                    {"name": "status", "label": "结果", "field": "status", "align": "left"},
                    {"name": "reason", "label": "匹配依据", "field": "reason", "align": "left"},
                ]
                matched_count = sum(1 for item in plan if item.get("status") == "matched")
                with (
                    ui.dialog().props("maximized") as match_dialog,
                    ui.card().classes("w-full h-full p-5 flex flex-col no-wrap"),
                ):
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.column().classes("gap-0"):
                            ui.label("企业微信安全自动匹配预览").classes("text-xl font-bold")
                            ui.label(f"可自动绑定 {matched_count} 人；重名、冲突和未匹配人员不会自动处理。").classes(
                                "text-sm text-gray-600"
                            )
                            ui.label("应用匹配不会自动导入部门或岗位，只使用系统中已有字典补齐任职。") \
                                .classes("text-xs text-blue-700")
                        ui.button(icon="close", on_click=match_dialog.close).props("flat round")
                    ui.table(
                        columns=columns,
                        rows=rows,
                        row_key="id",
                        pagination={"rowsPerPage": 25},
                    ).props("dense flat bordered").classes("w-full flex-grow min-h-0")

                    async def apply_matches():
                        try:
                            bound_count, org_count = app.state.user_service.apply_wecom_match_plan(plan)
                            await refresh_user_list_preserving_scroll()
                            match_dialog.close()
                            ui.notify(
                                f"自动绑定 {bound_count} 人，其中自动补齐组织任职 {org_count} 人。",
                                type="positive",
                            )
                        except Exception as exc:
                            ui.notify(f"自动匹配应用失败：{exc}", type="negative", multi_line=True)

                    with ui.row().classes("w-full justify-end gap-3"):
                        ui.button("取消", on_click=match_dialog.close).props("flat")
                        apply_button = ui.button("应用安全匹配", on_click=apply_matches).props(
                            "color=primary icon=auto_fix_high"
                        )
                        if matched_count == 0:
                            apply_button.disable()
                match_dialog.open()

            def confirm_delete(target_user):
                if target_user == "admin":
                    ui.notify("系统安全限制：禁止删除超级管理员账号", type="warning")
                    return

                if app.state.user_service.storage_mode != "database":
                    ui.notify("请先迁移到身份数据库；旧 Excel 模式不再执行人员删除。", type="warning")
                    return

                with ui.dialog() as confirm_dialog, ui.card().classes("p-6"):
                    ui.label(f"确认停用用户 【{target_user}】 吗？").classes("text-lg font-bold text-red-600")
                    ui.label("账号资料和历史记录会保留，但该用户将无法登录。").classes("text-sm text-gray-500 mb-6")

                    with ui.row().classes("w-full justify-end gap-4"):
                        ui.button("取消", on_click=confirm_dialog.close).props("flat")
                        ui.button(
                            "确认停用",
                            on_click=lambda: save_user("deactivate", target_user, None, None, confirm_dialog),
                        ).props("color=negative")
                confirm_dialog.open()

            async def activate_user(target_user):
                try:
                    app.state.user_service.modify_user("activate", target_user, None, None)
                    app.state.users_data = app.state.user_service.load_users()
                    await refresh_user_list_preserving_scroll()
                    ui.notify(f"用户 {target_user} 已恢复登录。", type="positive")
                except Exception as exc:
                    ui.notify(f"启用失败：{exc}", type="negative")

            # 4. 手工构建列表头部
            with ui.row().classes(
                "w-full bg-blue-50 p-3 font-bold text-blue-900 rounded flex-nowrap shrink-0 items-center border"
            ):
                ui.label("用户名").classes("w-[16%] min-w-[90px]")
                ui.label("密码/状态").classes("w-[15%] min-w-[100px]")
                ui.label("兼容角色").classes("w-[19%] min-w-[100px]")
                ui.label("企业微信").classes("w-[22%] min-w-[120px]")
                ui.label("操作").classes("w-[28%] min-w-[240px] text-center")

            # 5. 数据列表挂载点
            list_container = (
                ui.column()
                .classes("w-full flex-grow min-h-0 overflow-y-auto gap-0 mt-2 border rounded")
                .props("id=manage-user-list-scroll")
            )

            async def refresh_user_list_preserving_scroll():
                """重建用户列表，并保持管理员当前的滚动位置。"""
                try:
                    raw_scroll_top = await ui.run_javascript(
                        """
                        const list = document.getElementById('manage-user-list-scroll');
                        return list ? list.scrollTop : 0;
                        """
                    )
                    scroll_top = float(raw_scroll_top or 0)
                except Exception:
                    scroll_top = 0.0

                render_user_list()
                try:
                    await ui.run_javascript(
                        f"""
                        const restoreManageUserScroll = () => {{
                            const list = document.getElementById('manage-user-list-scroll');
                            if (list) list.scrollTop = {scroll_top};
                        }};
                        requestAnimationFrame(() => requestAnimationFrame(restoreManageUserScroll));
                        setTimeout(restoreManageUserScroll, 80);
                        """
                    )
                except Exception:
                    logger.debug("恢复用户管理列表滚动位置失败", exc_info=True)

            # 6. 列表渲染引擎：每次增删改后，清空容器并重新生成行
            def render_user_list():
                list_container.clear()
                wecom_bindings = app.state.user_service.list_wecom_bindings()
                with list_container:
                    # 【核心修改】：提取字典的键值对，并按照 role 字段进行升序排序
                    # item[0] 是用户名，item[1] 是包含密码和角色的字典
                    sorted_users = sorted(app.state.users_data.items(), key=lambda item: item[1].get("role", ""))

                    for username, info in sorted_users:
                        membership = app.state.user_service.get_primary_membership(username)
                        binding = wecom_bindings.get(username, {})
                        status = info.get("status", "active")
                        is_top_level_account = username == "admin" or str(info.get("role", "")).lower() in {
                            "admin",
                            "boss",
                        }
                        missing_items = []
                        if not info.get("password_set"):
                            missing_items.append("登录密码")
                        if username.casefold() != "admin" and not binding:
                            missing_items.append("企业微信账号")
                        if not membership.get("org_unit_id"):
                            missing_items.append("主部门")
                        if not membership.get("position_id"):
                            missing_items.append("岗位")
                        if not is_top_level_account and not membership.get("direct_manager_user_id"):
                            missing_items.append("直属上级")

                        if status != "active":
                            row_classes = "bg-gray-100 opacity-75 border-l-4 border-gray-400"
                            config_label = "非在职账号"
                            config_color = "grey"
                        elif not missing_items:
                            row_classes = "bg-green-50 border-l-4 border-green-500 hover:bg-green-100"
                            config_label = "资料完整"
                            config_color = "positive"
                        else:
                            row_classes = "bg-amber-50 border-l-4 border-amber-500 hover:bg-amber-100"
                            config_label = f"待补 {len(missing_items)} 项"
                            config_color = "warning"
                        # 使用 hover 效果增强交互感
                        with ui.row().classes(
                            f"w-full items-center p-3 border-b flex-nowrap {row_classes}"
                        ):
                            with ui.column().classes("w-[16%] min-w-[90px] gap-0"):
                                ui.label(username).classes("break-all")
                                if membership:
                                    ui.label(
                                        f"{membership.get('org_name', '')} / "
                                        f"{membership.get('position_name') or '未设岗位'}"
                                    ).classes("text-xs text-gray-500")
                            with ui.column().classes("w-[15%] min-w-[100px] gap-0"):
                                config_chip = (
                                    ui.chip(config_label, color=config_color)
                                    .props("dense")
                                    .classes("text-xs")
                                )
                                if missing_items:
                                    config_chip.tooltip(f"缺少：{'、'.join(missing_items)}")
                                ui.label("密码已设置" if info.get("password_set") else "密码未设置").classes(
                                    "text-xs text-green-700" if info.get("password_set") else "text-xs text-orange-700"
                                )
                                status_text = {
                                    "active": "在职",
                                    "disabled": "已停用",
                                    "departed": "已离职",
                                }.get(status, status)
                                ui.label(status_text).classes("text-xs text-gray-500")

                            with ui.row().classes("w-[19%] min-w-[100px]"):
                                ui.chip(
                                    info.get("role", "普通用户"),
                                    color="primary" if info.get("role") == "管理员" else "default",
                                ).classes("text-xs")

                            with ui.column().classes("w-[22%] min-w-[120px] gap-0"):
                                if username.casefold() == "admin":
                                    ui.label("系统账号免绑定").classes("text-sm text-blue-700")
                                else:
                                    ui.label(binding.get("external_display_name") or "未绑定").classes(
                                        "text-sm" if binding else "text-sm text-orange-700"
                                    )
                                if binding and username.casefold() != "admin":
                                    ui.label(binding.get("external_userid", "")).classes("text-xs text-gray-500")

                            # 原生按钮绑定，绝不会出现点击失效的问题
                            with ui.row().classes("w-[28%] min-w-[240px] justify-center gap-2"):
                                # 注意：这里必须使用 u=username 捕获循环变量，防止闭包晚绑定陷阱
                                ui.button("编辑", on_click=lambda u=username: open_form("edit", u)).props(
                                    "outline size=sm color=primary"
                                )
                                if username.casefold() != "admin":
                                    ui.button("微信", on_click=lambda u=username: open_wecom_binding_form(u)).props(
                                        "outline size=sm color=teal"
                                    )
                                ui.button("组织", on_click=lambda u=username: open_membership_form(u)).props(
                                    "outline size=sm color=indigo"
                                )
                                if info.get("status", "active") == "active":
                                    ui.button("停用", on_click=lambda u=username: confirm_delete(u)).props(
                                        "outline size=sm color=negative"
                                    )
                                else:
                                    ui.button("启用", on_click=lambda u=username: activate_user(u)).props(
                                        "outline size=sm color=positive"
                                    )

            # 初始加载渲染列表
            render_user_list()

            # 7. 底部控制区
            with ui.row().classes("w-full justify-between items-center shrink-0 mt-4 pt-2 border-t"):
                ui.label("系统管理员专属管理通道").classes("text-gray-500 text-sm font-bold")
                with ui.row().classes("gap-3"):
                    ui.button("自动匹配企业微信", on_click=open_auto_match_dialog, icon="auto_fix_high").props(
                        "outline color=teal"
                    )
                    ui.button("新增用户", on_click=lambda: open_form("add"), icon="person_add").classes(
                        "bg-green-600 text-white px-6"
                    )

        dialog.open()

    def open_wecom_contacts_dialog():
        cache_state = {"data": load_wecom_contacts_cache()}
        filter_state = {"keyword": "", "department": "全部", "status": "全部"}

        columns = [
            {"name": "name", "label": "姓名", "field": "name", "align": "left", "sortable": True},
            {"name": "userid", "label": "企业微信账号", "field": "userid", "align": "left", "sortable": True},
            {"name": "departments", "label": "部门", "field": "departments", "align": "left", "sortable": True},
            {"name": "position", "label": "职务", "field": "position", "align": "left", "sortable": True},
            {"name": "status_text", "label": "状态", "field": "status_text", "align": "center", "sortable": True},
            {"name": "department_ids", "label": "部门ID", "field": "department_ids", "align": "left"},
        ]

        with (
            ui.dialog().props("maximized") as dialog,
            ui.card().classes("w-full h-full p-4 flex flex-col no-wrap bg-gray-50"),
        ):
            with ui.row().classes("w-full items-center justify-between shrink-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("contacts", size="sm").classes("text-blue-700")
                    ui.label("企业微信通讯录").classes("text-xl font-bold text-gray-800")
                with ui.row().classes("items-center gap-2"):
                    sync_button = ui.button("同步通讯录", icon="refresh").props("outline color=primary")
                    ui.button(icon="close", on_click=dialog.close).props("flat round dense")

            ui.separator().classes("shrink-0")

            summary_label = ui.label().classes("text-sm text-gray-700 shrink-0")
            scope_label = ui.label().classes("text-xs text-gray-500 shrink-0")

            with ui.row().classes("w-full gap-3 items-center shrink-0"):
                keyword_input = ui.input("搜索姓名、账号、部门或职务").props("outlined dense clearable").classes("w-80")
                department_filter = (
                    ui.select(["全部"], label="部门", value="全部")
                    .props("outlined dense options-dense")
                    .classes("w-52")
                )
                status_filter = (
                    ui.select(["全部", "在职", "停用"], label="状态", value="全部")
                    .props("outlined dense options-dense")
                    .classes("w-36")
                )
                result_label = ui.label().classes("text-sm text-gray-500")

            contacts_table = (
                ui.table(
                    columns=columns,
                    rows=[],
                    row_key="userid",
                    pagination={"rowsPerPage": 20, "sortBy": "departments"},
                )
                .props("dense flat bordered separator=cell")
                .classes("w-full flex-grow min-h-0 bg-white")
            )

            def render_contacts():
                cache_data = cache_state["data"]
                contacts = cache_data.get("contacts", [])
                department_map = {
                    str(item.get("id", "")): item.get("name", "") for item in cache_data.get("departments", [])
                }
                department_options = sorted(
                    {department for contact in contacts for department in contact.get("departments", []) if department}
                )
                department_filter.options = ["全部", *department_options]
                if filter_state["department"] not in department_filter.options:
                    filter_state["department"] = "全部"
                    department_filter.value = "全部"
                department_filter.update()

                keyword = filter_state["keyword"].strip().lower()
                selected_department = filter_state["department"]
                selected_status = filter_state["status"]
                rows = []
                for contact in contacts:
                    departments = contact.get("departments", [])
                    status_text = "在职" if contact.get("is_active", True) else "停用"
                    searchable = " ".join(
                        [
                            contact.get("name", ""),
                            contact.get("userid", ""),
                            contact.get("position", ""),
                            *departments,
                        ]
                    ).lower()
                    if keyword and keyword not in searchable:
                        continue
                    if selected_department != "全部" and selected_department not in departments:
                        continue
                    if selected_status != "全部" and selected_status != status_text:
                        continue
                    rows.append(
                        {
                            "name": contact.get("name", ""),
                            "userid": contact.get("userid", ""),
                            "departments": "、".join(departments) or "-",
                            "position": contact.get("position", "") or "-",
                            "status_text": status_text,
                            "department_ids": "、".join(contact.get("department_ids", [])) or "-",
                        }
                    )

                visible_department_names = [
                    department_map.get(str(department_id), str(department_id))
                    for department_id in cache_data.get("visible_department_ids", [])
                ]
                scope_name = (
                    "自建应用可见范围" if cache_data.get("sync_scope") == "agent_visible_scope" else "配置的根部门范围"
                )
                summary_label.set_text(
                    f"缓存员工 {cache_data.get('contact_count', len(contacts))} 人 ｜ "
                    f"可见部门 {len(visible_department_names)} 个 ｜ "
                    f"最近同步：{cache_data.get('updated_at', '尚未同步')}"
                )
                scope_label.set_text(
                    f"同步范围：{scope_name} ｜ "
                    f"部门：{'、'.join(visible_department_names) or '暂无'} ｜ "
                    f"直接授权成员：{len(cache_data.get('visible_userids', []))} 人"
                )
                result_label.set_text(f"当前显示 {len(rows)} 人")
                contacts_table.rows = rows
                contacts_table.update()

            async def handle_sync_contacts(event):
                event.sender.disable()
                notification = ui.notification("正在同步企业微信通讯录...", timeout=None, spinner=True)
                try:
                    success, message = await sync_wecom_contacts()
                    cache_state["data"] = load_wecom_contacts_cache()
                    render_contacts()
                    notification.dismiss()
                    ui.notify(message, type="positive" if success else "negative", multi_line=True)
                except Exception as exc:
                    notification.dismiss()
                    logger.exception("管理员手动同步企业微信通讯录失败")
                    ui.notify(f"通讯录同步失败：{exc}", type="negative", multi_line=True)
                finally:
                    event.sender.enable()

            def handle_keyword_change(event):
                filter_state["keyword"] = event.value or ""
                render_contacts()

            def handle_department_change(event):
                filter_state["department"] = event.value or "全部"
                render_contacts()

            def handle_status_change(event):
                filter_state["status"] = event.value or "全部"
                render_contacts()

            sync_button.on_click(handle_sync_contacts)
            keyword_input.on_value_change(handle_keyword_change)
            department_filter.on_value_change(handle_department_change)
            status_filter.on_value_change(handle_status_change)
            render_contacts()

        dialog.open()

    def open_identity_config_transfer_dialog():
        """导出本地身份配置，或在服务器预检并合并配置包。"""
        user_svc = app.state.user_service
        if user_svc.storage_mode != "database":
            ui.notify("请先完成当前环境的身份数据库迁移。", type="warning")
            return

        state = {"package": None, "preview": None, "file_name": ""}
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-5xl max-h-[92vh] p-5"):
            with ui.row().classes("w-full items-start justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label("配置迁移").classes("text-xl font-bold text-slate-800")
                    ui.label(
                        "在本地导出，在服务器预检后导入；不会包含或覆盖密码、业务记录、待办和审计日志。"
                    ).classes("text-sm text-slate-500")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")
            ui.separator().classes("my-3")

            with ui.grid().classes("w-full grid-cols-1 md:grid-cols-2 gap-4"):
                with ui.card().classes("w-full p-4 border-l-4 border-blue-500"):
                    ui.label("1. 从已配置环境导出").classes("font-bold text-blue-900")
                    ui.label(
                        "部门、岗位、岗位权限、附加权限组、用户关联、企业微信绑定和审批流程，"
                        "均按稳定编码或用户名写入 JSON。"
                    ).classes("text-sm text-slate-600 leading-6")

                    def export_configuration():
                        try:
                            package = user_svc.export_identity_configuration()
                            content = json.dumps(package, ensure_ascii=False, indent=2).encode("utf-8")
                            filename = (
                                "identity_configuration_"
                                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                            )
                            ui.download(content, filename=filename)
                            ui.notify("配置包已生成，请妥善保存并上传到服务器。", type="positive")
                        except Exception as exc:
                            logger.exception("导出身份配置失败")
                            ui.notify(f"配置导出失败：{exc}", type="negative", multi_line=True)

                    ui.button("导出配置包", icon="download", on_click=export_configuration).props(
                        "color=primary"
                    )

                with ui.card().classes("w-full p-4 border-l-4 border-indigo-500"):
                    ui.label("2. 在目标服务器上传预检").classes("font-bold text-indigo-900")
                    include_user_links = ui.switch(
                        "同步匹配用户的任职和附加权限", value=True
                    ).classes("text-sm")
                    include_wecom_bindings = ui.switch(
                        "同步匹配用户的企业微信绑定", value=True
                    ).classes("text-sm")
                    ui.label(
                        "用户仅按用户名匹配；服务器不存在的用户会跳过，服务器密码始终保留。"
                    ).classes("text-xs text-orange-700")

                    def pick_file():
                        uploader.reset()
                        uploader.run_method("pickFiles")

                    ui.button("选择配置包", icon="upload_file", on_click=pick_file).props(
                        "outline color=indigo"
                    )
                    uploader = ui.upload(
                        auto_upload=True,
                        max_file_size=10_485_760,
                    ).props('accept=".json"').classes("hidden")

            preview_container = ui.column().classes("w-full gap-2 overflow-y-auto")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button("关闭", on_click=dialog.close).props("flat")
                import_button = ui.button("确认导入", icon="publish").props("color=positive")
                import_button.disable()

            def render_preview():
                preview_container.clear()
                preview = state.get("preview")
                with preview_container:
                    if preview is None:
                        ui.label("上传配置包后，这里会显示新增项、用户匹配和冲突检查结果。") \
                            .classes("text-sm text-slate-500 border border-dashed rounded p-4 w-full")
                        return
                    summary = preview.summary
                    with ui.row().classes("w-full gap-2 flex-wrap"):
                        ui.chip(
                            f"部门 {summary.get('departments_total', 0)}，新增 {summary.get('departments_new', 0)}",
                            icon="account_tree",
                        ).props("outline")
                        ui.chip(
                            f"岗位 {summary.get('positions_total', 0)}，新增 {summary.get('positions_new', 0)}",
                            icon="badge",
                        ).props("outline")
                        ui.chip(
                            f"权限组 {summary.get('permission_groups_total', 0)}",
                            icon="shield",
                        ).props("outline")
                        ui.chip(
                            f"流程 {summary.get('workflows_total', 0)}",
                            icon="account_tree",
                        ).props("outline")
                        ui.chip(
                            f"用户匹配 {summary.get('users_matched', 0)}/{summary.get('users_total', 0)}",
                            icon="person_search",
                        ).props("outline")
                    if preview.errors:
                        with ui.expansion(
                            f"必须处理的错误（{len(preview.errors)}）",
                            icon="error",
                            value=True,
                        ).classes("w-full border border-red-200 rounded bg-red-50"):
                            for error in preview.errors:
                                ui.label(error).classes("text-sm text-red-700")
                    if preview.warnings:
                        with ui.expansion(
                            f"将跳过或留空的项目（{len(preview.warnings)}）",
                            icon="warning_amber",
                            value=True,
                        ).classes("w-full border border-orange-200 rounded bg-orange-50"):
                            for warning in preview.warnings:
                                ui.label(warning).classes("text-sm text-orange-700")
                    if preview.can_import:
                        ui.label(
                            "预检通过。导入前会自动备份服务器数据库；同编码配置合并，服务器独有配置保留。"
                        ).classes("text-sm text-green-700 bg-green-50 rounded p-3 w-full")

            def refresh_preview():
                package = state.get("package")
                if not isinstance(package, dict):
                    state["preview"] = None
                    import_button.disable()
                    render_preview()
                    return
                try:
                    preview = user_svc.preview_identity_configuration(
                        package,
                        include_user_links=bool(include_user_links.value),
                        include_wecom_bindings=bool(include_wecom_bindings.value),
                    )
                    state["preview"] = preview
                    if preview.can_import:
                        import_button.enable()
                    else:
                        import_button.disable()
                    render_preview()
                except Exception as exc:
                    logger.exception("预检身份配置包失败")
                    state["preview"] = None
                    import_button.disable()
                    preview_container.clear()
                    with preview_container:
                        ui.label(f"预检失败：{exc}").classes("text-sm text-red-700")

            async def handle_package_upload(event: UploadEventArguments):
                try:
                    raw_content = await event.file.read()
                    package = json.loads(raw_content.decode("utf-8-sig"))
                    if not isinstance(package, dict):
                        raise ValueError("配置包根节点必须是对象")
                    state["package"] = package
                    state["file_name"] = str(event.file.name)
                    refresh_preview()
                    ui.notify("配置包解析完成，请检查预检结果。", type="positive")
                except Exception as exc:
                    logger.exception("读取身份配置包失败")
                    state["package"] = None
                    state["preview"] = None
                    import_button.disable()
                    render_preview()
                    ui.notify(f"配置包读取失败：{exc}", type="negative", multi_line=True)

            uploader.on_upload(handle_package_upload)
            include_user_links.on_value_change(lambda _event: refresh_preview())
            include_wecom_bindings.on_value_change(lambda _event: refresh_preview())

            async def execute_import(confirm_dialog):
                package = state.get("package")
                preview = state.get("preview")
                if not isinstance(package, dict) or preview is None or not preview.can_import:
                    ui.notify("请先上传并通过配置预检。", type="warning")
                    return
                confirm_dialog.close()
                import_button.disable()
                notice = ui.notification("正在备份并导入配置……", timeout=None, spinner=True)
                try:
                    result = await run.io_bound(
                        user_svc.import_identity_configuration,
                        package,
                        actor_username=current_user,
                        include_user_links=bool(include_user_links.value),
                        include_wecom_bindings=bool(include_wecom_bindings.value),
                    )
                    update_users_data()
                    notice.dismiss()
                    ui.notify(
                        "配置导入完成。已自动备份到：" + result.backup_path,
                        type="positive",
                        multi_line=True,
                        timeout=12_000,
                    )
                    dialog.close()
                except Exception as exc:
                    notice.dismiss()
                    logger.exception("导入身份配置失败")
                    ui.notify(f"配置导入失败，数据库事务已回滚：{exc}", type="negative", multi_line=True)
                    refresh_preview()

            def confirm_import():
                with ui.dialog() as confirm_dialog, ui.card().classes("w-full max-w-xl p-5"):
                    ui.label("确认合并配置？").classes("text-lg font-bold")
                    ui.label(
                        "同编码部门、岗位、权限组和流程将按配置包更新；匹配用户的任职与授权按当前开关同步。"
                    ).classes("text-sm text-slate-600")
                    ui.label("系统会先生成完整 SQLite 备份，任何写入失败都会整体回滚。") \
                        .classes("text-sm text-green-700")
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("取消", on_click=confirm_dialog.close).props("flat")
                        ui.button(
                            "开始导入",
                            icon="publish",
                            on_click=lambda: execute_import(confirm_dialog),
                        ).props("color=positive")
                confirm_dialog.open()

            import_button.on_click(confirm_import)
            render_preview()

        dialog.open()

    def open_identity_management_center():
        """以单一入口汇总用户、组织、权限、企业微信与迁移维护。"""
        user_svc = app.state.user_service
        database_mode = user_svc.storage_mode == "database"
        user_count = len(app.state.users_data)
        binding_count = len(user_svc.list_wecom_bindings()) if database_mode else 0
        org_count = len(user_svc.list_org_units()) if database_mode else 0
        position_count = len(user_svc.list_positions()) if database_mode else 0

        with (
            ui.dialog().props("maximized") as center_dialog,
            ui.card().classes("w-full h-full p-6 flex flex-col no-wrap bg-slate-50"),
        ):
            with ui.row().classes("w-full items-center justify-between shrink-0"):
                with ui.column().classes("gap-0"):
                    ui.label("用户、组织与权限中心").classes("text-2xl font-bold text-slate-800")
                    ui.label(
                        "一个入口维护系统账号、组织任职、岗位默认权限、附加权限及企业微信关联。"
                    ).classes("text-sm text-slate-500")
                ui.button(icon="close", on_click=center_dialog.close).props("flat round dense")
            ui.separator().classes("my-3")

            with ui.row().classes("w-full gap-3 shrink-0"):
                ui.chip(
                    "身份数据库" if database_mode else "旧版 Excel",
                    color="positive" if database_mode else "warning",
                    icon="database",
                )
                ui.chip(f"系统用户 {user_count} 人", icon="group").props("outline")
                if database_mode:
                    ui.chip(f"微信已绑定 {binding_count} 人", icon="link").props("outline")
                    ui.chip(f"部门 {org_count} 个", icon="account_tree").props("outline")
                    ui.chip(f"岗位 {position_count} 个", icon="badge").props("outline")

            ui.label("管理功能").classes("text-lg font-bold text-slate-700 mt-4")
            with ui.grid().classes(
                "w-full grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4 overflow-y-auto p-1"
            ):

                def management_card(title, description, icon, color, handler, status_text=""):
                    with ui.card().classes(
                        "w-full min-h-[10rem] p-5 cursor-pointer border-l-4 "
                        f"border-{color}-500 hover:shadow-lg transition-shadow"
                    ).on("click", handler):
                        with ui.row().classes("w-full items-start justify-between"):
                            ui.icon(icon, size="42px").classes(f"text-{color}-600")
                            if status_text:
                                ui.chip(status_text, color=color).props("dense outline")
                        ui.label(title).classes("text-lg font-bold text-slate-800")
                        ui.label(description).classes("text-sm text-slate-500 leading-6")

                management_card(
                    "用户与微信账号",
                    "维护登录账号、账号状态、企业微信绑定及每位员工的组织任职。",
                    "manage_accounts",
                    "blue",
                    lambda: open_user_management_dialog(),
                    f"{user_count} 人",
                )
                management_card(
                    "组织架构与岗位字典",
                    "维护部门上下级、系统岗位、职级和直属上级关系。",
                    "account_tree",
                    "cyan",
                    lambda: open_organization_management_dialog(),
                    f"{org_count} 部门 / {position_count} 岗位" if database_mode else "迁移后可用",
                )
                management_card(
                    "岗位、权限与审批流程",
                    "统一维护岗位默认权限、附加授权，以及按组织和具体人员运行的审批流程。",
                    "admin_panel_settings",
                    "deep-purple",
                    lambda: open_security_role_management_dialog(),
                    "迁移后可用" if not database_mode else "权限配置",
                )
                management_card(
                    "企业微信通讯录",
                    "同步并查询企业微信成员、部门和职务，供账号及组织自动匹配。",
                    "contacts",
                    "teal",
                    lambda: open_wecom_contacts_dialog(),
                    f"已绑定 {binding_count} 人" if database_mode else "通讯录",
                )
                management_card(
                    "身份数据库迁移",
                    "将当前部署机器的用户安全迁移到身份数据库，不覆盖已有数据库密码。",
                    "database",
                    "indigo",
                    lambda: open_user_migration_dialog(),
                    "已迁移" if database_mode else "待迁移",
                )
                management_card(
                    "配置迁移",
                    "导出或预检导入部门、岗位、权限、用户关联和审批流程，不迁移密码及业务数据。",
                    "move_to_inbox",
                    "orange",
                    open_identity_config_transfer_dialog,
                    "部署工具" if database_mode else "迁移后可用",
                )

                def refresh_users():
                    try:
                        update_users_data()
                        ui.notify("用户数据已刷新到内存；重新打开中心可查看最新统计。", type="positive")
                    except Exception as exc:
                        ui.notify(f"用户数据刷新失败：{exc}", type="negative")

                management_card(
                    "刷新用户运行数据",
                    "重新读取当前用户数据源，更新运行中的用户缓存。",
                    "refresh",
                    "green",
                    refresh_users,
                    "维护操作",
                )

            ui.label(
                "建议日常只维护用户的部门和岗位；确有兼任或专项职责时，再分配附加权限组。"
            ).classes("text-sm text-blue-800 bg-blue-50 rounded p-3 mt-auto shrink-0")

        center_dialog.open()

    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("系统管理员界面").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    with ui.column().classes("w-full h-calc(100vh-9rem) -space-y-2"):
        # --- 【新增】在线用户监控区域 ---
        # with ui.card().classes("w-full -space-y-2 border-l-4 border-green-500"):
        #     with ui.row().classes("items-center justify-between w-full"):
        #         ui.label("当前在线用户监控").classes("text-lg font-bold mb-2")
        #         # 显示实时在线人数
        #         online_count_label = ui.label("检测中...").classes("text-sm font-mono bg-gray-200 px-2 rounded")

        #     # 定义表格列
        #     columns = [
        #         {"name": "username", "label": "用户名称", "field": "username", "align": "left"},
        #         {"name": "login_time", "label": "登录/连接时间", "field": "login_time", "align": "center"},
        #         {"name": "ip", "label": "IP来源", "field": "ip", "align": "left"},
        #         {"name": "status", "label": "状态", "field": "status", "align": "center"},
        #     ]

        #     # 在线用户表格
        #     online_table = ui.table(columns=columns, rows=[], pagination=5).classes("w-full h-40")
        #     online_table.props("dense flat bordered")  # 紧凑样式

        #     def refresh_online_data():
        #         """刷新表格数据的函数"""
        #         # 将全局字典转换为表格需要的列表格式
        #         # 注意：这里需要根据您的 online_users 实际结构调整
        #         rows = []
        #         # 过滤掉 admin 自己，或者保留，看您需求。这里全部显示。
        #         # current_connected_ids = online_users.keys()

        #         for client_id, info in online_users.items():
        #             rows.append(
        #                 {
        #                     "username": info.get("username", "未知"),
        #                     "login_time": info.get("login_time", "-"),
        #                     "ip": info.get("ip", "127.0.0.1"),
        #                     "status": "🟢 在线",
        #                 }
        #             )

        #         # 更新表格和计数器
        #         online_table.rows = rows
        #         online_table.update()  # 显式触发更新
        #         online_count_label.set_text(f"当前在线: {len(rows)} 人")

        #         # 如果只有 admin 一人在线，且 admin 正在看这个页面，可以将背景变绿提示安全
        #         if len(rows) <= 1:
        #             online_count_label.classes(remove="bg-red-200 text-red-800", add="bg-green-200 text-green-800")
        #         else:
        #             online_count_label.classes(remove="bg-green-200 text-green-800", add="bg-red-200 text-red-800")

        with ui.card().classes("w-full -space-y-2"):
            ui.label("系统配置更新").classes("text-lg font-bold mb-2")
            with ui.row().classes("gap-4"):
                ui.button("①生成临时需求配置并校验(excel->json)", on_click=lambda: get_temp_config_service()).props(
                    ""
                ).classes("bg-amber-7")
                ui.button("②更新加载需求配置(excel->json)", on_click=lambda: update_config_service()).props("").classes(
                    "bg-amber-7"
                )
                ui.separator().props("size=1px")
                ui.button("编辑配置项关联影响(Impact List)", on_click=open_impact_editor).props(
                    "icon=edit_note"
                ).classes("bg-purple-600 text-white")
                ui.button(
                    "配置需求对概述影响",
                    on_click=open_requirement_overview_impact_editor,
                ).props("icon=account_tree").classes("bg-indigo-700 text-white")

                ui.separator().props("size=1px")
                ui.button("更新概述配置(JSON->General)", on_click=lambda: updata_overview_config()).props("").classes(
                    ""
                )
                ui.button("更新项目列表(JSON->General)", on_click=lambda: project_summary_update()).props("").classes(
                    ""
                )
                ui.button(
                    "更新项目总表动态信息更新配置(JSON->General)", on_click=lambda: project_table_update_config_update()
                ).props("").classes("")
            with ui.row().classes("gap-4"):
                ui.separator().props("size=1px")
                ui.button(
                    "用户、组织与权限中心",
                    on_click=open_identity_management_center,
                ).props("icon=manage_accounts size=lg").classes("bg-blue-700 text-white px-6")
        # 日志监控区域
        with ui.card().classes("w-full -space-y-2 overflow-hidden"):
            # 日志标题栏
            with ui.row().classes("w-full bg-gray-100 p-2 items-center justify-between border-b"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("terminal", size="sm")
                    ui.label("系统实时日志 (logs/app.log)").classes("font-bold text-gray-700")

                # 添加控制按钮
                with ui.row():
                    # 强制刷新按钮：不仅仅是清屏，而是重新读取最后一段日志
                    ui.button("重载日志", on_click=lambda: reload_logs(), icon="refresh").props("flat").classes(
                        "text-sm"
                    )
                    ui.button("清屏", on_click=lambda: log_view.clear(), icon="block").props("flat color=red").classes(
                        "text-sm"
                    )

            # 日志显示组件 (ui.log)
            # max_lines 防止浏览器内存溢出
            log_view = ui.log(max_lines=2000).classes(
                "w-full h-90 bg-[#1e1e1e] text-green-400 font-mono text-sm p-2 overflow-y-auto"
            )
        with ui.card().classes("w-full q-pa-md"):
            ui.label("数据安全与维护").classes("text-h6 q-mb-md")

            # --- 添加备份按钮 ---
            ui.button("立即备份所有数据", on_click=handle_manual_backup).props("icon=save color=primary").tooltip(
                "同时备份 storage-general.json 和 SQLite 数据库"
            )
    # --- 日志读取逻辑 ---
    log_file_path = os.path.join(BASE_DIR, "logs", "app.log")
    file_cursor = 0  # 文件指针，记录读取到了哪里

    def init_log_cursor():
        """初始化文件指针，只读取最后 10KB，避免卡顿"""
        nonlocal file_cursor
        if os.path.exists(log_file_path):
            size = os.path.getsize(log_file_path)
            # 如果文件大于 10KB，则从最后 10KB 开始读
            file_cursor = max(0, size - 10240)
        else:
            file_cursor = 0

    async def read_logs():
        nonlocal file_cursor
        if not os.path.exists(log_file_path):
            return

        try:
            # 获取当前文件大小
            current_size = os.path.getsize(log_file_path)

            # 如果当前文件比上次记录的指针还要小，说明发生了日志轮转(Rotating)，或者是新文件
            # 此时重置指针从头读取
            if current_size < file_cursor:
                file_cursor = 0

            # 如果没有新内容，直接返回
            if current_size == file_cursor:
                return

            with open(log_file_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(file_cursor)
                # 读取所有新行
                new_lines = f.readlines()
                # 更新指针位置
                file_cursor = f.tell()

                if new_lines:
                    # 将新行推送到 UI
                    for line in new_lines:
                        # 去掉末尾换行符，因为 log.push 会自动换行
                        log_view.push(line.rstrip())

        except Exception as e:
            # 避免日志读取逻辑本身报错导致崩坏，简单打印即可
            logger.error(f"Error reading logs: {e}")

    async def reload_logs():
        """手动重载：清屏并重新初始化读取"""
        log_view.clear()
        init_log_cursor()
        # 立即触发一次读取
        await read_logs()

    # 初始化并启动定时器
    init_log_cursor()
    # 稍微延迟启动第一次读取，确保UI已渲染
    ui.timer(0.1, read_logs, once=True)
    ui.timer(1.5, read_logs)  # 之后每1.5秒轮询
    # --- 【新增】定时刷新在线用户列表 ---
    # 每 3 秒检查一次全局字典，更新UI
    # 这样如果有用户下线或上线，管理员在3秒内就能看到变化
    # ui.timer(3.0, refresh_online_data)
