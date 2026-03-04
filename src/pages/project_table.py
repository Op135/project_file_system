# -*- encoding: utf-8 -*-
import asyncio
import copy
import datetime
import json
import logging
import os

from nicegui import app, ui

from .. import db_storage  # 导入我们创建的模块
from ..config import BASE_DIR, IGNORE_STR, IMG_DIR, PRESET_AVATARS, PROJECT_STATE_LIST, REQ_DIR
from ..utils import (
    find_files_with_prefix_and_version,
    get_cache_busted_path,
    get_overviow_page,
    logout,
    overview_role_update,
    project_summary_update,
    project_table_update_config_update,
    set_project_custom_labels,
    update_overview_charge_pending_dic,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/project_table")
def project_table_page():
    # 向页面的 <head> 部分添加自定义的 HTML 代码。这通常用于添加自定义的 CSS 样式、JavaScript 代码或元数据（如 <meta> 标签）
    ui.add_head_html("""
        <style>
            /* === 自定义 Loading 动画样式 === */
            .custom-loading-overlay {
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 20px;
                background: rgba(255, 255, 255, 0.9); /* 半透明白色背景 */
                border-radius: 12px;
                box-shadow: 0 4px 15px rgba(0, 0, 0, 0.1);
                backdrop-filter: blur(5px); /* 毛玻璃效果 */
            }

            .loading-spinner {
                width: 40px;
                height: 40px;
                border: 4px solid #e9f7f8; /* 浅色底环 */
                border-top: 4px solid #2196F3; /* 蓝色主色，可改成你的主题色 */
                border-radius: 50%;
                animation: spin 1s linear infinite;
                margin-bottom: 10px;
            }

            .loading-text {
                color: #555;
                font-size: 14px;
                font-weight: 600;
                font-family: 'Arial', sans-serif;
            }

            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            .ag-theme-alpine {
                --ag-font-family: 'Arial', sans-serif !important;
                --ag-foreground-color: #111 !important;       /* 单元格文本颜色 */
                --ag-header-foreground-color: #000 !important;       /*  表头文本颜色 */
                --ag-header-background-color: #f1f0ed !important; /* 表头背景色 */
                --ag-odd-row-background-color: #f9cb8b33 !important; /* 奇数行背景色 */
                --ag-background-color: #93d5dc33 !important; /* 背景色 */
                --ag-row-hover-color: #41b34933 !important;     /* 行悬停颜色 */
                --ag-border-color: #ddd !important;           /* 边框颜色 */
                --ag-cell-horizontal-border: solid 1px var(--ag-border-color) !important; /* 单元格右侧边框 */
                --ag-row-border: solid 1px var(--ag-border-color) !important; /* 单元格底部边框 */
                --ag-header-column-resize-handle-display: none !important; /*隐藏表头单元格间多出来的竖线*/
                --ag-font-size: 12px !important;

            }
            .q-field--auto-height .q-field__control, .q-field--auto-height .q-field__native{
                min-height: 30px !important;
            }
            .q-field__marginal {
                height: 30px !important;
                color: rgba(0, 0, 0, .54);
                font-size: 24px;
            }
            /* === 新增：状态行背景颜色控制 === */
            /* 待定状态：淡黄色背景，深黄色文字 */
            .row-wait {
                background-color: #88888833 !important;
                color: #000000 !important;
            }
            /* 作废状态：浅灰色背景，灰色文字，且加删除线 */
            .row-invalid {
                background-color: #88888866 !important;
                color: #000000 !important;
                text-decoration: line-through !important;
            }
            /* 核心修复：必须直接针对行内的 .ag-cell 设置删除线，否则会被 flex 布局阻断 */
            .row-invalid .ag-cell {
                text-decoration: line-through !important;
            }

            /* 修正行内单元格背景，设为透明，让行的背景色透出来 */
            .row-wait .ag-cell, .row-invalid .ag-cell {
                background-color: transparent !important;
            }
                     
            /*控制表格筛选下拉菜单背景颜色*/
            .ag-menu {
                background-color: white;
            }
            .ag-picker-field-wrapper {
                background-color: #e9f7f8;
            }
            .ag-select-list{
                background-color: white;
            }
            /*控制展开式按钮样式*/
            .q-btn--fab {
                padding-left: 8px;
                padding-right: 8px;
                padding-bottom: 4px;
                padding-top: 4px;
                min-height: 30px;
                min-width: 30px;
            }
            .q-btn--fab-mini {
                padding: 6px;
                min-height: 30px;
                min-width: 30px;
            }
            .q-fab__actions .q-btn {
                margin: 3px;
            }
            /*控制选项框内选项样式*/
            .q-item {
                min-height: 30px;
                padding: 8px 16px;
                color: inherit;
                transition: color 0.3s,background-color 0.3s
            }
            .ag-header-group-cell-label, .ag-header-cell-label {
                display: flex;
                flex: 1 1 auto;
                align-self: center;
                align-items: center;
                justify-content: center;
            }
            .ag-cell {
                display: flex !important;
                position: absolute !important;
                white-space: nowrap !important;
                height: 100% !important;
                align-items: center;
                justify-content: center;
                line-height: 25px;
            }

            /* 基础单元格样式 */
            /* 文字自动换行样式 */
            .left-auto-break {
                white-space: pre-wrap !important;
                word-break: break-all; /*overflow-wrap: break-word;*/
                overflow: hidden;
                text-align: left;
                justify-content: left !important;
            }
            /* 文字居中自动换行样式 */
            .center-auto-break {
                white-space: pre-wrap !important;
                word-break: break-all; /*overflow-wrap: break-word;*/
                overflow: hidden;
                text-align: center;
            }
        
            /* 字体加粗样式 */
            .bold-text {
                font-weight: 700 !important;
            }
            .red-text {
                color: red !important;
            }
            .amber-text {
                color: #df8b00 !important;
            }
        </style>
    """)
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    current_user = app.storage.user.get("current_user")
    current_role = app.storage.user.get("current_role")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

    table_dialog = ui.dialog()

    # === 新增功能：处理新增项目的逻辑 ===
    def save_new_project_to_file(new_project_data):
        """
        读取JSON，插入新数据，按Key排序，保存文件，并更新内存缓存
        """
        try:
            # 1. 读取现有文件
            target_file = os.path.join(BASE_DIR, "data/project_summary.json")

            # 如果找不到文件，尝试使用 storage 里的数据反向生成（兜底策略）
            data = {}
            if os.path.exists(target_file):
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                # 假如文件路径配置比较复杂，这里仅作演示，实际请确保路径正确
                data = copy.deepcopy(app.storage.general.get("project_summary", {}))

            # 2. 检查是否已存在
            project_name = new_project_data["project_name"]  # 这里仅作为临时变量名，实际JSON key是 sub_project

            key = project_name

            if key in data:
                ui.notify(
                    f"项目 {key} 已存在！",
                    type="info",
                    position="bottom",
                    timeout=2000,
                    progress=True,
                    close_button="✖",
                )
                return False

            # 3. 构造要保存的结构 (对应截图中的 Value 部分)
            # 移除临时的 project_name 字段，保留数据字段
            save_value = {
                "state": new_project_data["state"],
                "model_notes": new_project_data["model_notes"],
                "creation_date": new_project_data["creation_date"],
                "introduction": new_project_data["introduction"],
                "customer": new_project_data["customer"],
            }

            # 4. 插入数据
            data[key] = save_value

            # 5. 排序逻辑：按 Key 字母顺序排序
            # 这能保证同一系列在一起，新系列排在后面（如果首字母更靠后）
            sorted_keys = sorted(data.keys())
            sorted_data = {k: data[k] for k in sorted_keys}

            # 6. 写入文件
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(sorted_data, f, ensure_ascii=False, indent=4)

            # 7. 更新内存中的全局缓存
            project_summary_update()

            logger.error(f"项目 {key} 创建成功！总项目数增加到{str(len(sorted_data.keys()))}个。")
            ui.notify(
                f"项目 {key} 创建成功！总项目数增加到{str(len(sorted_data.keys()))}个。",
                type="positive",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return True

        except Exception as e:
            logger.error(f"保存项目失败: {e}")
            ui.notify(
                f"新增项目失败: {e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False

    def save_revise_project_to_file(new_project_data):
        """
        读取JSON，插入新数据，按Key排序，保存文件，并更新内存缓存
        """
        try:
            # 1. 读取现有文件
            target_file = os.path.join(BASE_DIR, "data/project_summary.json")

            # 如果找不到文件，尝试使用 storage 里的数据反向生成（兜底策略）
            data = {}
            if os.path.exists(target_file):
                with open(target_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                # 假如文件路径配置比较复杂，这里仅作演示，实际请确保路径正确
                data = copy.deepcopy(app.storage.general.get("project_summary", {}))

            # 2. 检查是否不存在
            project_name = new_project_data["project_name"]  # 这里仅作为临时变量名，实际JSON key是 sub_project

            key = project_name

            if key not in data:
                ui.notify(
                    f"项目 {key} 不存在，无法修改！",
                    type="info",
                    position="bottom",
                    timeout=2000,
                    progress=True,
                    close_button="✖",
                )
                return False

            # 3. 构造要保存的结构 (对应截图中的 Value 部分)
            # 移除临时的 project_name 字段，保留数据字段
            save_value = {
                "state": new_project_data["state"],
                "model_notes": new_project_data["model_notes"],
                "creation_date": new_project_data["creation_date"],
                "introduction": new_project_data["introduction"],
                "customer": new_project_data["customer"],
            }

            # 4. 插入数据
            data[key] = save_value

            # 5. 排序逻辑：按 Key 字母顺序排序
            # 这能保证同一系列在一起，新系列排在后面（如果首字母更靠后）
            sorted_keys = sorted(data.keys())
            sorted_data = {k: data[k] for k in sorted_keys}

            # 6. 写入文件
            with open(target_file, "w", encoding="utf-8") as f:
                json.dump(sorted_data, f, ensure_ascii=False, indent=4)

            # 7. 更新内存中的全局缓存
            project_summary_update()

            logger.info(f"项目 {key} 修改成功！")
            ui.notify(
                f"项目 {key} 修改成功！",
                type="positive",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return True

        except Exception as e:
            logger.error(f"修改项目失败: {e}")
            ui.notify(
                f"修改项目失败: {e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False

    # === 新增功能：自动刷新并定位到对应系列 ===
    async def auto_refresh_view(target_project_name) -> None:
        """
        保存后调用：重新加载数据，重新计算分类，并将视图切换到目标项目所在的系列
        """
        # 声明引用外部变量，以便修改它们
        nonlocal rows, select_dic

        # 1. 重新获取最新数据 (因为 save_... 已经更新了 app.storage 和文件)
        copy_project_dic = copy.deepcopy(app.storage.general["project_summary"])
        rows = list(copy_project_dic.values())

        # 2. 重新计算分类字典 (防止新增了从未有过的大类或小类)
        #    重新生成所有项目名列表
        select_li_new = list(set([pro_sum["project"] for pro_sum in rows]))
        #    更新全局的 select_dic
        select_dic = get_select_dic(select_li_new)

        # 3. 解析目标项目的分类 (反推它属于哪个大类和小类)
        target_major = "RFFM"  # 默认兜底
        target_sub = "所有"

        # 复用 get_select_dic 里的切分逻辑
        if "-" in target_project_name:
            parts = target_project_name.split("-")
            target_major = parts[0]
            if len(parts) > 1:
                # 取第二段的前两位作为子类 (如 15)
                target_sub = parts[1][:2]
        else:
            # 如果没有横杠，属于“其它”
            target_major = "其它"
            target_sub = target_project_name

        # 4. 更新 UI 控件
        # 4.1 更新大类选项 (以防新增了大类)
        select_major.set_options(list(select_dic.keys()))

        # 4.2 设定大类值 (这通常会触发 on_value_change，但为了保险我们手动处理后续)
        select_major.value = target_major

        # 4.3 手动强制更新一下小类选项
        #    (update_sub_select 依赖外部变量 select_major_value，此时可能还未同步，所以手动取字典)
        if target_major in select_dic:
            select_sub.set_options(select_dic[target_major])

        # 4.4 设定小类值
        select_sub.value = target_sub

        # 5. 刷新表格
        #    此时 select_major.value 和 select_sub.value 已经更新
        #    update_aggrid 会读取这两个值来筛选 rows
        await update_aggrid(aggrid)

    # === 新增功能：构建弹窗 UI ===
    def open_add_project_dialog():
        table_dialog.clear()
        with table_dialog, ui.card().classes("w-[500px]"):
            ui.label("新增/修改研发项目").classes("text-xl font-bold mb-4")

            # 表单数据绑定
            form_data = {
                "project_name": "RFXX-XXXX-X/RM3000",  # 预填前缀
                "state": "研发",
                "creation_date": datetime.date.today().strftime("%Y-%m-%d"),
                "model_notes": "",
                "introduction": "",
                "customer": "",
            }

            # 查找传入项目是否存在总表里，存在则把该项目信息更新到输入框绑定变量上，以供编辑
            def find_project_data(project_name):
                project_data = app.storage.general["project_summary"].get(project_name, {})
                if project_data:
                    form_data["state"] = project_data["state"]
                    form_data["creation_date"] = project_data["creation_date"]
                    form_data["model_notes"] = project_data["model_notes"]
                    form_data["introduction"] = project_data["introduction"]
                    form_data["customer"] = project_data["customer"]

            with ui.column().classes("w-full gap-2"):
                # 项目名称 (Key)
                ui.input("内部产品型号", value=form_data["project_name"]).props("autofocus outlined").bind_value(
                    form_data, "project_name"
                ).on_value_change(lambda: find_project_data(form_data["project_name"]))

                with ui.row().classes("w-full gap-2"):
                    # 状态
                    ui.select(PROJECT_STATE_LIST, value=form_data["state"], label="状态").bind_value(
                        form_data, "state"
                    ).classes("w-1/3")
                    # 日期
                    ui.input("立项日期", value=form_data["creation_date"]).bind_value(
                        form_data, "creation_date"
                    ).classes("w-1/2").props("type=date")

                # 简介 (多行)
                ui.textarea("产品简介", value=form_data["introduction"]).bind_value(form_data, "introduction").props(
                    "outlined rows=3"
                ).classes("w-full")

                # 备注
                ui.input("型号备注", value=form_data["model_notes"]).bind_value(form_data, "model_notes").props(
                    "outlined"
                ).classes("w-full")

                # 客户
                ui.input("客户简称", value=form_data["customer"]).bind_value(form_data, "customer").props(
                    "outlined"
                ).classes("w-full")

            with ui.row().classes("w-full justify-end mt-4"):

                async def on_add_confirm():
                    # 简单校验
                    if form_data["project_name"].split("-")[0] == "RFTS":
                        ui.notify(
                            "临时项目不在此创建，仅处理正式项目!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                        return

                    # 执行保存
                    success = save_new_project_to_file(form_data)
                    if success:
                        # table_dialog.close()
                        # 刷新页面或表格 (最简单是直接刷新页面，或者手动更新 rows)
                        # ui.navigate.to("/project_table")  # 重新加载当前页以刷新数据
                        await auto_refresh_view(form_data["project_name"])  # 自动刷新并定位

                async def on_revise_confirm():
                    # 简单校验
                    if form_data["project_name"] not in app.storage.general["project_summary"]:
                        ui.notify(
                            "项目不在清单里，无法修改!",
                            type="warning",
                            position="bottom",
                            timeout=3000,
                            progress=True,
                            close_button="✖",
                        )
                        return

                    # 执行保存
                    success = save_revise_project_to_file(form_data)
                    if success:
                        # table_dialog.close()
                        # 刷新页面或表格 (最简单是直接刷新页面，或者手动更新 rows)
                        # ui.navigate.to("/project_table")  # 重新加载当前页以刷新数据
                        await auto_refresh_view(form_data["project_name"])  # 自动刷新并定位

                ui.button("确认创建", on_click=on_add_confirm).props("color=green")
                ui.button("确认修改", on_click=on_revise_confirm).props("color=blue")
                ui.button("取消", on_click=table_dialog.close).props("color=grey-8")

            table_dialog.open()

    # === 新增功能：配置项目概述负责人弹窗 ===
    def open_set_table_dialog():
        table_dialog.clear()
        # 增加 max-w-[90vw] 以适配不同浏览器页面大中小情况
        with table_dialog, ui.card().classes("w-[500px] max-w-[90vw]"):
            ui.label("配置概述负责人").classes("text-xl font-bold mb-4")

            # 获取所有项目名列表并排序，供自动补全提示使用
            project_list = list(app.storage.general.get("project_summary", {}).keys())
            project_list.sort()

            form_data = {"project_name": ""}
            role_form_data = {}

            # 动态表单容器，用于在切换项目时重绘输入框
            form_container = ui.column().classes("w-full gap-2")

            def on_project_change(e):
                pn = e.value

                # 每次输入变化时先清空下方容器
                form_container.clear()
                role_form_data.clear()

                # 【核心拦截】：只有当输入的项目名确切存在于总表里时，才进行配置项的渲染
                if not pn or pn not in app.storage.general.get("project_summary", {}):
                    return

                # 调用 utils 里的 overview_role_update 进行初始化绑定准备
                overview_role_update(pn, "initialize")

                # 获取该项目最新的角色字典
                current_roles = app.storage.general["overview_role"].get(pn, {})

                with form_container:
                    # 遍历系统中所有的角色配置（如 光学、结构、硬件、软件、UI 等）
                    for role in app.storage.general.get("over_config_data", {}).keys():
                        # 获取现有人员，去掉自动记录时的“最近：”或“最多：”前缀，以展示纯净人名
                        latest_user_raw = current_roles.get(role, {}).get("latest_user", "")
                        clean_user = latest_user_raw.split("：")[1] if "：" in latest_user_raw else latest_user_raw
                        role_form_data[role] = clean_user

                        ui.input(f"{role}负责人", value=clean_user).bind_value(role_form_data, role).props(
                            "outlined"
                        ).classes("w-full")

            # 将 select 替换为带有 autocomplete 的 input
            # 支持直接输入，也支持下拉自动补全选择，增加 clearable 方便一键清空重新输入
            ui.input("输入或选择项目名", autocomplete=project_list).bind_value(
                form_data, "project_name"
            ).on_value_change(on_project_change).props("outlined clearable").classes("w-full mb-4")

            with ui.row().classes("w-full justify-end mt-4"):

                async def on_confirm():
                    pn = form_data["project_name"]
                    # 提交前再次校验项目名是否合法
                    if not pn or pn not in app.storage.general.get("project_summary", {}):
                        ui.notify("请先输入有效的项目名！", type="warning", position="bottom")
                        return

                    # 运行时初始化，安全获取全局待定状态字典
                    pending_storage = app.storage.general.setdefault("overview_charge_pending", {})

                    # 将填写的负责人更新到全局存储中
                    for role, user in role_form_data.items():
                        new_user = user.strip()

                        # 获取原负责人
                        old_user_raw = app.storage.general["overview_role"][pn].get(role, {}).get("latest_user", "")
                        old_user = old_user_raw.split("：")[1] if "：" in old_user_raw else old_user_raw

                        # 【核心逻辑】：只有当负责人发生实质性变化时，才进行后台数据清洗与更新
                        if old_user != new_user:
                            # 1. 更新存储标志
                            if new_user:
                                app.storage.general["overview_role"][pn][role]["latest_user"] = f"最近：{new_user}"
                            else:
                                app.storage.general["overview_role"][pn][role]["latest_user"] = ""

                            # 2. 获取该角色下的所有概述项配置，用于精准剥离旧数据和生成新数据
                            role_config = app.storage.general.get("over_config_data", {}).get(role, {})

                            for group_dic in role_config.values():
                                for chip_dic in group_dic.values():
                                    title = chip_dic.get("title")
                                    label = chip_dic.get("label")

                                    # 步骤 A: 抹除原负责人的待办数据（字典树精准修剪）
                                    if old_user and old_user in pending_storage:
                                        if pn in pending_storage[old_user]:
                                            pending_storage[old_user][pn].pop(label, None)
                                            # 如果该项目下没有其它待办了，清理空项目节点，防止内存泄漏
                                            if not pending_storage[old_user][pn]:
                                                pending_storage[old_user].pop(pn, None)

                                    # 步骤 B: 利用 local 模式，极速刷新新负责人的待办状态
                                    if new_user and title:
                                        update_overview_charge_pending_dic("local", new_user, pn, title)

                    ui.notify(f"项目 {pn} 各角色概述负责人配置成功！", type="positive", position="bottom")
                    table_dialog.close()
                    # 刷新表格展示，确保修改后的负责人立刻呈现在表格中
                    await update_aggrid(aggrid)

                ui.button("确认配置", on_click=on_confirm).props("color=blue")
                ui.button("取消", on_click=table_dialog.close).props("color=grey-8")

        table_dialog.open()

    # 按照项目名里“-”符号切分为大类和小类，并输出二层结构的类别字典
    def get_select_dic(select_li):
        # select_li为所有项目名列表：RFFM-1519-A
        select_li.sort()
        select_dic = {}
        # 单独加入一个所有大类，以供显示所有型号
        select_dic["所有"] = ["所有"]
        # 现将临时项目加在靠前位置
        select_dic["RFTS"] = ["所有"]
        for s in select_li:
            # 判断指定字符出现次数
            if s.count("-") >= 1:
                parts = s.split("-")
                if parts[0] not in select_dic.keys():
                    # 每个小类都先给个 所有 的选项
                    select_dic[parts[0]] = ["所有"]
                if parts[1][:2] not in select_dic[parts[0]]:
                    # 将代表小类的 两位数字 加入到该大类的小类选项里
                    select_dic[parts[0]].append(parts[1][:2])
                    # 排序
                    select_dic[parts[0]].sort(reverse=True)
            # 对于没有-字符的类别做特殊处理
            else:
                # 单独设置一个其它的大类，且该大类下的小类选项里先加入 所有
                if "其它" not in select_dic.keys():
                    select_dic["其它"] = ["所有"]
                # 将完整项目号直接加入小类选项里
                if s not in select_dic["其它"]:
                    select_dic["其它"].append(s)
                    # 排序
                    select_dic["其它"].sort(reverse=True)
        return select_dic

    # 按照第一选项的值，生成更新第二选框的选项列表
    def update_sub_select(select_sub):
        select_sub.set_options(
            select_dic[select_major_value["value"]], value=select_dic[select_major_value["value"]][0]
        )

    # 按照两个选项的值，更新表格行数据，将概述填写内容同步到简介表，刷新表格显示
    async def update_aggrid(aggrid):
        # step 1: 立即显示 AG Grid 的加载遮罩
        # 这会显示 "Loading..." 或者转圈动画，覆盖在表格上
        aggrid.run_grid_method("showLoadingOverlay")

        # step 2: 关键步骤！让出 0.01 秒的时间给 UI 线程
        # 如果没有这行，Python 会一口气算完数据才去更新 UI，用户依然看不到加载动画
        await asyncio.sleep(0.01)

        nonlocal rows_select
        # 清空
        rows_select = []
        s = ""
        # 如果第一选框选择的是“所有”
        if select_major_value["value"] == "所有":
            s = "all"
        else:
            # 设置筛选字符串
            # 如果第二选项选的是“所有”
            if select_sub_value["value"] == "所有":
                # 且第一选项选的不是“其它”
                # 匹配正常项目的所有，即匹配大类，如所有 RFFM
                if select_major_value["value"] != "其它":
                    # s = RFFM
                    s = select_major_value["value"]
                # 且第一选项选的是“其它”，则拿“-”字符来排除
                # 匹配其它所有，如RM3000、RM5000等等
                else:
                    # s = -
                    s = "-"
            # 如果第一选项选的不是“其它”，且不是“所有”
            elif select_major_value["value"] != "其它":
                # 则拿正常项目-字符前后较完整字符串来匹配，如RFFM-17
                # s = RFFM-17
                s = f"{select_major_value['value']}-{select_sub_value['value']}"
            # 第一选项选的是“其它”且第二选项不是“所有”，拿具体特殊项目名来匹配，如RM3000
            else:
                # s = RM3000
                s = select_sub_value["value"]

        # 遍历无分类行数据列表，将符合筛选条件的行数据找出来
        for row_data in rows:
            # 如果匹配字符不为“-”且匹配字符串在项目名里（筛选正常项目，如具体的RM3000或含RFMM或含RFMM-17的项目）
            # 或 匹配字符为“-”且匹配字符不在项目名里（筛选特殊项目，如RM3000,RM5000,所有不含-字符的项目）
            if s == "all" or (s != "-" and s in row_data["project"]) or (s == "-" and s not in row_data["project"]):
                # 获取当前行数据所属项目名
                project_name = row_data["sub_project"]
                overview_data = db_storage.get_item(f"{project_name}_over_data", {})

                # 遍历服务器的项目与概述数据对照字典
                # 不在这个字典里的数据列，不会被修改，即显示固定内容
                for pro_key, over_key_li in app.storage.general["project_table_update_config"].items():
                    # 专门处理概述负责人配置部分显示内容
                    if (
                        "charge" in pro_key
                        and over_key_li != ""
                        and project_name in app.storage.general["overview_role"]
                        and over_key_li in app.storage.general["overview_role"][project_name]
                    ):
                        show_str = app.storage.general["overview_role"][project_name][over_key_li].get(
                            "latest_user", ""
                        )

                        # 获取负责人名
                        charge_person = show_str.split("：")[1] if show_str else ""
                        # 当前项目的当前over_key_li角色比如“光学”，存在最近编辑者
                        # if charge_person:
                        #     selected_bool = False
                        #     break_bool = False
                        #     for class_dic in overview_data.values():
                        #         if break_bool:
                        #             break
                        #         for ver_dic in class_dic.values():
                        #             if break_bool:
                        #                 break
                        #             select_activ_dic = ver_dic.get("select_activ_dic", {})
                        #             if select_activ_dic:
                        #                 max_ver = max([int(float(ver)) for ver in select_activ_dic.keys()])
                        #                 # chip处于待选择激活状态下 且 over_key_li角色比如“光学”和当前chip的编辑角色一致
                        #                 if select_activ_dic[f"{max_ver}.0"] is None and over_key_li == ver_dic.get(
                        #                     "role", ""
                        #                 ):
                        #                     selected_bool = True
                        #                     # 查到一个需要改变角色显示状态的就不要再继续遍历了
                        #                     break_bool = True
                        #     if selected_bool:
                        #         # 处理表格显示信息
                        #         charge_person = f"待{charge_person}\n选概述"

                        row_data[pro_key] = charge_person

                    # 其它需要动态更新且配置非空 或 如：定制要点、需求输入等不用配置也固定动态更新的列
                    elif pro_key in ["custom_labels", "requirement"] or over_key_li != []:
                        show_str = ""
                        # 遍历对照配置列表（可能一个项目简介配置了多个对应的概述数据项）
                        for over_key in over_key_li:
                            # 当前概述数据项label存在服务器概述数据对应项目里，说明可能存在概述内容
                            if over_key in overview_data:
                                chip_data_li = overview_data.get(over_key, {}).values()
                                # 遍历概述内容每个chip数据
                                for chip_data in chip_data_li:
                                    # 该chip内容是激活 或者 待定状态 才显示
                                    if chip_data["enabled"] or chip_data["enabled"] is None:
                                        text = ""
                                        # 文本型内容，直接显示
                                        if chip_data.get("type") in ["text", "test"]:
                                            text = chip_data.get("content")
                                            if text in IGNORE_STR:
                                                continue
                                        # 文件名类型内容，去除后缀
                                        elif chip_data.get("type") in ["search", "svn", "file", "image"]:
                                            text = ".".join(chip_data["content"].split(".")[:-1])

                                        # 待定状态的概述内容串 加上特殊标记符号
                                        if chip_data["enabled"] is None:
                                            text = f"「{text}」?"

                                        # 将文本拼接到待显示字符串上
                                        # 这几类换行拼接
                                        if pro_key in [
                                            "light_source",
                                            "target_distance",
                                            "pcb",
                                            "electronic_bom",
                                            "software_executable_file",
                                        ]:
                                            show_str = f"{show_str}\n{text}"
                                        else:
                                            show_str = f"{show_str}，{text}"

                        # 定制内容列，则在概述内容基础上，拼接添加需求项输出标签内容
                        if pro_key == "custom_labels":
                            label_list = app.storage.general["custom_labels"].get(project_name, [])
                            if label_list:
                                set_project_custom_labels(project_name)
                                show_str = f"{show_str}，{'，'.join(label_list)}"
                        # 需求录入列，动态内容设置
                        elif pro_key == "requirement":
                            project_state_dic = app.storage.general["wait_review"].get(project_name, {})
                            if project_state_dic:
                                max_num = max([int(float(v)) for v in project_state_dic.keys()])
                                if max_num:
                                    max_ver = f"{str(max_num)}.0"
                                    show_str = f"V{max_ver}{project_state_dic[max_ver].get('state', '未知')}\n点击升级"
                            else:
                                show_str = "点击录入"
                        # 待确定的内容，统一更换仅显示一个?号
                        # if "?" in show_str:
                        #     show_str = "?"
                        # 将处理完成的字符串作为该行数据对应项目简介项的显示内容
                        row_data[pro_key] = show_str.strip("，").removeprefix(
                            "\n"
                        )  # removeprefix移除字符串前缀，strip移除首尾指定字符

                # 单独处理项目简介表里每行 负责销售 单元格的显示
                row_data["sale_charge"] = app.storage.general["project_sale"].get(project_name, "")
                # 单独处理项目简介表里每行 项目工程师 单元格的显示
                row_data["project_charge"] = app.storage.general["project_engineer"].get(project_name, "")
                # 将行数据加入待显示的符合选框的数据列表里
                rows_select.append(row_data)

        # aggrid.run_grid_method("setRowData", rows_select)
        # step 3: 更新数据
        # AG Grid 在接收到新数据时，会自动移除 Loading Overlay
        aggrid.options["rowData"] = rows_select
        aggrid.update()

    # 设定aggrid元素某列的可见性为传入的visible，如果这个参数不传，则是切换可见性
    async def toggle_visibility(grid, field_li: list, visible=None):
        """
        设定 aggrid 元素指定列的可见性。

        :param grid: AgGrid 组件实例。
        :param field_li: 需要操作的列ID列表 (e.g., ['state', 'creation_date'])。
        :param visible: 如果提供布尔值，则直接设定可见性 (True=可见, False=隐藏)。
                        如果为 None，则切换这些列的当前可见性。
        """
        # 如果明确指定了 visible 状态，直接调用 API 并返回
        if visible is not None:
            grid.run_grid_method("setColumnsVisible", field_li, bool(visible))
            return

        # --- 切换可见性的逻辑 ---

        # 1. 一次性获取所有列的状态
        try:
            all_columns_state = await grid.run_grid_method("getColumnState")
        except Exception as e:
            ui.notify(
                f"获取列状态失败: {e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return

        # 2. 在内存中计算哪些列需要显示，哪些需要隐藏
        cols_to_show = []
        cols_to_hide = []

        # 创建一个从 colId 到 hide 状态的映射，方便快速查找
        state_map = {col["colId"]: col.get("hide", False) for col in all_columns_state}

        for field in field_li:
            # 如果列当前是隐藏的 (hide=True)，那么我们就要显示它
            if state_map.get(field):  # .get(field) 默认为 None (False)，如果 hide=True 则为 True
                cols_to_show.append(field)
            # 如果列当前是可见的 (hide=False)，那么我们就要隐藏它
            else:
                cols_to_hide.append(field)

        # 3. 分批次更新，减少与前端的通信次数
        if cols_to_show:
            grid.run_grid_method("setColumnsVisible", cols_to_show, True)

        if cols_to_hide:
            grid.run_grid_method("setColumnsVisible", cols_to_hide, False)
        # 刷新 Ag-Grid
        # grid.update()

    # 创建一个按钮，其点击事件调用 run_grid_method
    # 调用 AG Grid API 清除所有筛选模型
    def clear_all_filters(grid):
        grid.run_grid_method("setFilterModel", None)

    # 按钮点击事件（操作存储/行数据）
    async def handle_cell_click(event, aggrid):
        row_data = event.args["data"]  # 整行的数据
        col_id = event.args["colId"]  # 点击列的字段名
        # row_index = event.args["rowIndex"]  # 行索引
        # row_id = event.args["rowId"]  # 点击行的ID
        project_name = row_data["sub_project"]
        if col_id == "requirement":
            # 查找指定路径下，含有提供项目名的文件，得到一个字典，完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
            project_exists_file = find_files_with_prefix_and_version(REQ_DIR, project_name)
            if project_exists_file:
                v_max = max([float(s) for s in project_exists_file.keys()])
                # 定义文件路径
                file_path = os.path.join(REQ_DIR, project_exists_file[str(v_max)]["name"])
                # 不止
                ui.navigate.to(
                    f"/main/requirement?type=requirement&project_name={row_data['sub_project']}&json_path={file_path}"
                )
            else:
                ui.navigate.to(f"/main/requirement?type=requirement&project_name={row_data['sub_project']}")

        elif col_id == "overview":
            await get_overviow_page(project_name, False)
        elif col_id == "test_summary":
            ui.run_javascript(f'window.open("/report/test_summary/{project_name}", "_blank")')

    async def switch_toggle_vis(visible=None):
        # 切换传入列的可见性
        await toggle_visibility(
            aggrid,
            [
                "state",  # 状态列
                "introduction",  # 简介列
                "model_notes",  # 型号备注列
                "creation_date",  # 创建日期列
                "light_source",  # 光源选型列
                "project",  # 对外项目号列
                "target_distance",  # 目标距离列
                "output_mode",  # 输出型号模式列
                "guide_beam",  # 导光束要求列
                "adapter_options",  # 转接座选型列
                "customer",  # 客户简称列
                "pcb",  # 驱动PCB规格
                "electronic_bom",  # 驱动电子BOM
                "software_executable_file",  # 研发版软件
            ],
            visible,
        )

    # 定义项目主界面列配置
    # 文本筛选器 ("filter": "agTextColumnFilter"): 用于文本列，支持包含、开始于、结束于等多种筛选模式。
    # 数值筛选器 ("filter": "agNumberColumnFilter"): 用于数值列，支持等于、大于、小于、范围等。
    # 集合筛选器 ("filter":  "agDateColumnFilter"): 用于日期列，支持等于、不等于、之前、之后、之间、空白、非空等。
    project_summary_columns = [
        {
            "field": "sub_project",
            "headerName": "内部产品型号",
            "width": 140,
            "filter": "agTextColumnFilter",
            "pinned": "left",  # 固定到左侧
        },
        {"field": "project", "headerName": "对外产品型号", "width": 120},
        {
            "field": "model_notes",
            "headerName": "型号备注",
            "width": 150,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "state", "headerName": "产品状态", "width": 80, "filter": "agTextColumnFilter"},
        {"field": "creation_date", "headerName": "立项日期", "width": 100, "filter": "agDateColumnFilter"},
        {
            "field": "introduction",
            "headerName": "产品简介",
            "width": 400,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "custom_labels",
            "headerName": "定制要点",
            "width": 400,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "light_source",
            "headerName": "光源选型",
            "width": 400,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "photometric",
            "headerName": "光度学要求",
            "width": 250,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "target_distance",
            "headerName": "目标面距离",
            "width": 100,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "adapter_options",
            "headerName": "转接座可选类别",
            "width": 140,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "color", "headerName": "外观颜色", "width": 80, "filter": "agTextColumnFilter"},
        {"field": "input_voltage", "headerName": "产品输入电压", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "input_mode", "headerName": "输入控制模式", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "output_mode", "headerName": "输出模式", "width": 100, "filter": "agTextColumnFilter"},
        {
            "field": "guide_beam",
            "headerName": "导光束要求",
            "width": 115,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "pcb",
            "headerName": "PCB规格",
            "width": 180,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "electronic_bom",
            "headerName": "电子BOM",
            "width": 250,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "software_executable_file",
            "headerName": "固件执行文件",
            "width": 250,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "requirement",
            "headerName": "需求录入",
            "width": 100,
            "cellClass": "center-auto-break",
            "autoHeight": True,
        },
        {"field": "overview", "headerName": "概述整理", "width": 80},
        {"field": "test_summary", "headerName": "测试项", "width": 80},
        {"field": "customer", "headerName": "客户缩写", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "sale_charge", "headerName": "销售", "width": 80, "filter": "agTextColumnFilter"},
        {
            "field": "project_charge",
            "headerName": "项目",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "optics_charge",
            "headerName": "光学",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "structure_charge",
            "headerName": "结构",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "hardware_charge",
            "headerName": "硬件",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "software_charge",
            "headerName": "软件",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "ui_charge",
            "headerName": "UI",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "craft_charge",
            "headerName": "工艺",
            "width": 80,
            "cellClass": "left-auto-break",
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
    ]
    for col in project_summary_columns:
        if "width" in col:
            col["minWidth"] = col["width"]
        col["headerClass"] = "center-auto-break"
        # 设置单元格样式规则
        col["cellClassRules"] = {
            # 当表达式为 true 时，应用 'red-text' 类
            # typeof x === 'string': 这是一个安全检查，确保我们只在字符串类型的数据上执行后续操作
            # x.includes('?') 这是 JavaScript 的内建函数，如果字符串 x 中包含子字符串 '?'，它就返回 true
            "amber-text": "typeof x === 'string' && (x.includes('?') || x.includes('概述'))",
        }
        # col["cellClass"] = "ag-cell"
    # 防止没有初始化导致报错
    if not app.storage.general["project_summary"]:
        project_summary_update()
    if not app.storage.general["project_table_update_config"]:
        project_table_update_config_update()
    # 从服务器获取完整项目摘要
    copy_project_dic = copy.deepcopy(app.storage.general["project_summary"])
    # 抽取出无分类项目摘要列表
    rows = list(copy_project_dic.values())
    # 初始化表格行数据选项列表
    rows_select = []
    # 单独抽取出所有项目名，除重后生成列表
    select_li = list(set([pro_sum["project"] for pro_sum in rows]))
    # 用于同步两个选项框的选项值
    select_major_value = {"value": "RFFM"}
    select_sub_value = {"value": "10"}
    # 获取按照大类和小类整理后的项目类别字典，用于选项框的选项动态生成
    select_dic = get_select_dic(select_li)
    select_major_li = list(select_dic.keys())

    # 项目信息表
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("项目信息表").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                if current_role in ["研发经理"]:
                    ui.menu_item("新增/修改项目", on_click=open_add_project_dialog)
                    ui.menu_item("配置概述负责人", on_click=open_set_table_dialog)
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    with ui.column().classes("w-full h-[88vh] -space-y-2"):
        with ui.row().classes("items-center -space-x-2") as tool_row:
            ui.label("项目筛选：").classes("text-[16px]/[28px]")
            select_major = (
                ui.select(select_major_li).bind_value(select_major_value, "value").props("outlined").classes("")
            )
            select_sub = (
                ui.select(select_dic["RFFM"]).bind_value(select_sub_value, "value").props("outlined").classes("")
            )

        # 初始化 AG-Grid
        aggrid = ui.aggrid(
            {
                "columnDefs": project_summary_columns,
                "rowData": rows_select,
                "headerHeight": 50,
                # 强制渲染所有行，禁用虚拟滚动
                "suppressRowVirtualisation": True,
                # 允许单元格文本选择
                "enableCellTextSelection": True,
                # 分页相关设置
                "pagination": True,  # 开启分页
                "paginationPageSize": 30,  # 每页显示 10 行
                "paginationPageSizeSelector": [10, 30, 50, 70, 90],  # (可选) 允许用户在底部选择每页行数
                # === 新增：根据状态字段应用 CSS 类 ===
                "rowClassRules": {
                    "row-wait": "data.state == '待定'",
                    "row-invalid": "data.state == '作废'",
                },
                # === 自定义加载模板 ===
                # 注意：这里使用 HTML 字符串引用我们在 CSS 中定义的类
                "overlayLoadingTemplate": """
                    <div class="custom-loading-overlay">
                        <div class="loading-spinner"></div>
                        <div class="loading-text">正在加载数据...</div>
                    </div>
                """,
            }
        ).classes("ag-theme-alpine ag-header-cell-resize::after h-full")

        # 按照两个选项的值，更新表格行数据，将概述填写内容同步到简介表，刷新表格显示
        select_major.on_value_change(lambda select_sub=select_sub: update_sub_select(select_sub))
        select_major.on_value_change(lambda aggrid=aggrid: update_aggrid(aggrid))
        select_sub.on_value_change(lambda aggrid=aggrid: update_aggrid(aggrid))
        aggrid.on("cellClicked", lambda e, aggrid=aggrid: handle_cell_click(e, aggrid))
        ui.timer(0.1, lambda: update_aggrid(aggrid), once=True)

        with tool_row:
            with ui.row().classes("items-center -space-x-4"):
                ui.label("功能按键：").classes("text-[16px]/[28px]")
                # with ui.fab("construction", label="", color="blue", direction="right"):
                ui.button(icon="zoom_in_map", color="amber-9", on_click=lambda: switch_toggle_vis(False)).props(
                    "flat round"
                )
                ui.button(icon="zoom_out_map", color="green-9", on_click=lambda: switch_toggle_vis(True)).props(
                    "flat round"
                )
                ui.button(icon="filter_alt_off", color="purple-9", on_click=lambda: clear_all_filters(aggrid)).props(
                    "flat round"
                )
