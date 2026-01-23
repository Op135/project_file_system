# -*- encoding: utf-8 -*-
import json
import logging
import os

from nicegui import app, ui

from ..config import BASE_DIR, IMG_DIR, PRESET_AVATARS
from ..utils import (
    get_cache_busted_path,
    get_temp_config_service,
    logout,
    online_users,
    project_summary_update,
    project_table_update_config_update,
    updata_overview_config,
    update_config_service,
    update_users_data,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/manage")
def manage_page():
    # 管理员管理界面
    if app.storage.user.get("current_user") != "admin":
        ui.navigate.to("/main")  # 如果不是管理员，跳转到主界面
        return
    current_user = app.storage.user.get("current_user")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

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

            for cat, groups in config_data.items():
                for group, items in groups.items():
                    for k, v in items.items():
                        if isinstance(v, dict):
                            item_map[k] = v
                            if "label" in v:
                                label_to_key_map[v["label"]] = k
                            if "impact_list" not in v:
                                v["impact_list"] = []

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
                    items = list(config_data[cat][grp].keys())
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
                    items = list(config_data[cat][grp].keys())
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
        with ui.card().classes("w-full -space-y-2 border-l-4 border-green-500"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("当前在线用户监控").classes("text-lg font-bold mb-2")
                # 显示实时在线人数
                online_count_label = ui.label("检测中...").classes("text-sm font-mono bg-gray-200 px-2 rounded")

            # 定义表格列
            columns = [
                {"name": "username", "label": "用户名称", "field": "username", "align": "left"},
                {"name": "login_time", "label": "登录/连接时间", "field": "login_time", "align": "center"},
                {"name": "ip", "label": "IP来源", "field": "ip", "align": "left"},
                {"name": "status", "label": "状态", "field": "status", "align": "center"},
            ]

            # 在线用户表格
            online_table = ui.table(columns=columns, rows=[], pagination=5).classes("w-full h-40")
            online_table.props("dense flat bordered")  # 紧凑样式

            def refresh_online_data():
                """刷新表格数据的函数"""
                # 将全局字典转换为表格需要的列表格式
                # 注意：这里需要根据您的 online_users 实际结构调整
                rows = []
                # 过滤掉 admin 自己，或者保留，看您需求。这里全部显示。
                # current_connected_ids = online_users.keys()

                for client_id, info in online_users.items():
                    rows.append(
                        {
                            "username": info.get("username", "未知"),
                            "login_time": info.get("login_time", "-"),
                            "ip": info.get("ip", "127.0.0.1"),
                            "status": "🟢 在线",
                        }
                    )

                # 更新表格和计数器
                online_table.rows = rows
                online_table.update()  # 显式触发更新
                online_count_label.set_text(f"当前在线: {len(rows)} 人")

                # 如果只有 admin 一人在线，且 admin 正在看这个页面，可以将背景变绿提示安全
                if len(rows) <= 1:
                    online_count_label.classes(remove="bg-red-200 text-red-800", add="bg-green-200 text-green-800")
                else:
                    online_count_label.classes(remove="bg-green-200 text-green-800", add="bg-red-200 text-red-800")

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

                ui.separator().props("size=1px")
                ui.button("更新概述配置(JSON->General)", on_click=lambda: updata_overview_config()).props("").classes(
                    ""
                )
                ui.button("更新用户数据(JSON->内存)", on_click=lambda: update_users_data()).props("").classes("")
                ui.button("更新项目列表(JSON->General)", on_click=lambda: project_summary_update()).props("").classes(
                    ""
                )
                ui.button(
                    "更新项目总表动态信息更新配置(JSON->General)", on_click=lambda: project_table_update_config_update()
                ).props("").classes("")

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
    ui.timer(3.0, refresh_online_data)
