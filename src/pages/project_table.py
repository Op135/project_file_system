# -*- encoding: utf-8 -*-
import os

from nicegui import app, ui

from .. import db_storage  # 导入我们创建的模块
from ..config import IMG_DIR, REQ_DIR
from ..utils import find_files_with_prefix_and_version, get_overviow_page, logout, project_summary_update


@ui.page("/project_table")
def project_table_page():
    # 向页面的 <head> 部分添加自定义的 HTML 代码。这通常用于添加自定义的 CSS 样式、JavaScript 代码或元数据（如 <meta> 标签）
    ui.add_head_html("""
        <style>
            .ag-theme-alpine {
                --ag-font-family: 'Arial', sans-serif !important;
                --ag-foreground-color: #111 !important;       /* 单元格文本颜色 */
                --ag-header-foreground-color: #000 !important;       /*  表头文本颜色 */
                --ag-header-background-color: #f1f0ed !important; /* 表头背景色 */
                --ag-odd-row-background-color: #f6dead33 !important; /* 奇数行背景色 */
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
                word-wrap: break-all;
                overflow: hidden;
                text-align: left;
                justify-content: left !important;
            }
            /* 文字居中自动换行样式 */
            .center-auto-break {
                white-space: pre-wrap !important;
                word-wrap: break-all;
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

    # 按照项目名里“-”符号切分为大类和小类，并输出二层结构的类别字典
    def get_select_dic(select_li):
        select_li.sort()
        select_dic = {}
        # 单独加入一个所有大类，以供显示所有型号
        select_dic["所有"] = ["所有"]
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
    def update_aggrid(aggrid):
        nonlocal rows_select
        # 清空
        rows_select = []
        s = ""
        # 如果第一选矿选择的是“所有”
        if select_major_value["value"] == "所有":
            rows_select = rows
        else:
            # 设置筛选字符串
            # 如果第二选项选的是“所有”
            if select_sub_value["value"] == "所有":
                # 且第一选项选的不是“其它”，择拿正常项目名前面的字符串来匹配RFFM
                if select_major_value["value"] != "其它":
                    s = select_major_value["value"]
                # 且第一选项选的是“其它”，则拿“-”字符来排除
                else:
                    s = "-"
            # 如果第一选项选的不是“其它”，且不是“所有”
            elif select_major_value["value"] != "其它":
                # 则拿正常项目-字符前后较完整字符串来匹配，如RFFM-17
                s = f"{select_major_value['value']}-{select_sub_value['value']}"
            # 第一选项选的是“其它”且第二选项不是“所有”，拿具体特殊项目名来匹配，如RM3000
            else:
                s = select_sub_value["value"]

            # 遍历无分类行数据列表，将符合筛选条件的行数据找出来
            for r in rows:
                # 如果匹配字符不为“-”且匹配字符串在项目名里（正常项目） 或 匹配字符为“-”且匹配字符不在项目名里（特殊项目）
                if s != "-" and s in r["project"] or s == "-" and s not in r["project"]:
                    # 获取当前行数据所属项目名
                    project_name = r["sub_project"]
                    overview_data = db_storage.get_item(f"{project_name}_over_data", {})
                    # 如果服务器储存的概述数据里存在该当前项目对应概述资料
                    if overview_data:
                        # 遍历服务器 项目简介与概述数据对照字典
                        for pro_key, over_key_li in app.storage.general["project_overview_config"].items():
                            # 如果当前处理的不是负责人配置，且项目简介对照配置非空
                            if "charge" not in pro_key and over_key_li != []:
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
                                                # 如果有content键，则应该是文字型chip
                                                if "content" in chip_data:
                                                    text = chip_data["content"]
                                                # 如果有filename键，则应该是文件或图片型chip
                                                elif "filename" in chip_data:
                                                    text = ".".join(chip_data["filename"].split(".")[:-1])

                                                # 待定状态的概述内容串 加上特殊标记符号
                                                if chip_data["enabled"] is None:
                                                    text = f"「{text}」?"
                                                # 将文本拼接到待显示字符串上
                                                # 这几类换行拼接
                                                if pro_key in [
                                                    "light_source",
                                                    "target_distance",
                                                    "drive_pcb",
                                                    "electronic_bom",
                                                    "software_research",
                                                    "software_mass",
                                                ]:
                                                    show_str = f"{show_str}\n{text}"
                                                else:
                                                    show_str = f"{show_str}，{text}"
                                # 将处理完成的字符串作为该行数据对应项目简介项的显示内容
                                r[pro_key] = show_str.strip("，").removeprefix("\n")  # removeprefix移除字符串前缀
                            # 处理负责人配置部分显示内容
                            elif (
                                "charge" in pro_key
                                and over_key_li != ""
                                and project_name in app.storage.general["overview_role"]
                                and over_key_li in app.storage.general["overview_role"][project_name]
                            ):
                                show_str = app.storage.general["overview_role"][project_name][over_key_li][
                                    "latest_user"
                                ]
                                show_str = show_str.split("：")[1] if show_str else ""
                                if show_str:
                                    selected_bool = False
                                    for class_dic in overview_data.values():
                                        for ver_dic in class_dic.values():
                                            select_activ_dic = ver_dic.get("select_activ_dic", {})
                                            if select_activ_dic:
                                                max_ver = max([int(float(ver)) for ver in select_activ_dic.keys()])
                                                if select_activ_dic[f"{max_ver}.0"] is None:
                                                    if over_key_li == ver_dic.get("role", ""):
                                                        selected_bool = True
                                    if selected_bool:
                                        show_str = f"待{show_str}\n选概述"

                                r[pro_key] = show_str

                    # 单独处理项目简介表里每行 负责销售 单元格的显示
                    r["sale_charge"] = app.storage.general["project_sale"].get(project_name, "")
                    # 将行数据加入待显示的符合选框的数据列表里
                    rows_select.append(r)

        # aggrid.run_grid_method("setRowData", rows_select)
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
            ui.notify(f"获取状态失败: {e}", type="negative")
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
            # ui.notify(f"已显示列: {', '.join(cols_to_show)}")

        if cols_to_hide:
            grid.run_grid_method("setColumnsVisible", cols_to_hide, False)
            # ui.notify(f"已隐藏列: {', '.join(cols_to_hide)}")
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
                ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")
            else:
                ui.navigate.to(f"/main/requirement?type=requirement&project_name={row_data['sub_project']}")
        elif col_id == "overview":
            await get_overviow_page(project_name, False)

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
                "drive_pcb",  # PCB规格
                "electronic_bom",  # 电子BOM
                "software_research",  # 研发版软件
                "software_mass",  # 量产版软件
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
            "pinned": "left",  # 固定到左侧
        },
        {"field": "project", "headerName": "对外产品型号", "width": 120},
        {"field": "model_notes", "headerName": "型号备注", "width": 150, "autoHeight": True},
        {"field": "state", "headerName": "产品状态", "width": 80, "filter": "agTextColumnFilter"},
        {"field": "creation_date", "headerName": "立项日期", "width": 100, "filter": "agDateColumnFilter"},
        {"field": "introduction", "headerName": "产品简介", "width": 300, "autoHeight": True},
        {"field": "custom_labels", "headerName": "定制要点", "width": 400, "autoHeight": True},
        {
            "field": "light_source",
            "headerName": "光源选型",
            "width": 400,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
            # "cellStyle": {"white-space": "pre-line"},
        },
        {"field": "photometric", "headerName": "光度学要求", "width": 120, "autoHeight": True},
        {"field": "target_distance", "headerName": "目标面距离", "width": 100, "autoHeight": True},
        {
            "field": "adapter_options",
            "headerName": "转接座可选类别",
            "width": 140,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "color", "headerName": "外观颜色", "width": 80, "filter": "agTextColumnFilter"},
        {"field": "input_voltage", "headerName": "产品输入电压", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "input_mode", "headerName": "输入控制模式", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "output_mode", "headerName": "输出模式", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "guide_beam", "headerName": "导光束要求", "width": 100},
        {
            "field": "drive_pcb",
            "headerName": "PCB规格",
            "width": 180,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "electronic_bom",
            "headerName": "电子BOM",
            "width": 180,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "software_research",
            "headerName": "研发版软件",
            "width": 200,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "software_mass",
            "headerName": "量产版软件",
            "width": 200,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "requirement", "headerName": "需求录入", "width": 80},
        {"field": "overview", "headerName": "概述整理", "width": 80},
        {"field": "customer", "headerName": "客户缩写", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "sale_charge", "headerName": "销售", "width": 80, "filter": "agTextColumnFilter"},
        {
            "field": "project_charge",
            "headerName": "项目",
            "width": 80,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "optics_charge", "headerName": "光学", "width": 80, "autoHeight": True},
        {"field": "structure_charge", "headerName": "结构", "width": 80, "autoHeight": True},
        {"field": "hardware_charge", "headerName": "硬件", "width": 80, "autoHeight": True},
        {"field": "software_charge", "headerName": "软件", "width": 80, "autoHeight": True},
        {"field": "ui_charge", "headerName": "UI", "width": 80, "autoHeight": True},
        {"field": "craft_charge", "headerName": "工艺", "width": 80, "autoHeight": True},
    ]
    for col in project_summary_columns:
        if "width" in col:
            col["minWidth"] = col["width"]
        if "autoHeight" in col:
            # 该类使得\n符号会起作用，达到手动换行作用
            col["cellClass"] = "left-auto-break"
        col["headerClass"] = "center-auto-break"
        # 设置单元格样式规则
        col["cellClassRules"] = {
            # 当表达式为 true 时，应用 'red-text' 类
            # typeof x === 'string': 这是一个安全检查，确保我们只在字符串类型的数据上执行后续操作
            # x.includes('?') 这是 JavaScript 的内建函数，如果字符串 x 中包含子字符串 '?'，它就返回 true
            "amber-text": "typeof x === 'string' && (x.includes('?') || x.includes('概述'))",
        }
        # col["cellClass"] = "ag-cell"

    # 将手动数据添加覆盖到服务器保存数据里
    project_summary_update()
    # 从服务器获取完整项目摘要
    project_dic = app.storage.general["project_summary"]
    # 抽取出无分类项目摘要列表
    rows = list(project_dic.values())
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
    with ui.header().classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("项目信息表").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white"):  # 右侧对齐
            with ui.menu() as menu:
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)
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
            }
        ).classes("ag-theme-alpine ag-header-cell-resize::after h-full")
        # min-width: 1000px;       /* 防止宽度过小 */
        # overflow-x: auto;        /* 启用水平滚动 */
        # aggrid.run_grid_method("domLayout", "print")
        # aggrid.style("text-align:center;width: 150%;")

        # 按照两个选项的值，更新表格行数据，将概述填写内容同步到简介表，刷新表格显示
        select_major.on_value_change(lambda select_sub=select_sub: update_sub_select(select_sub))
        select_major.on_value_change(lambda aggrid=aggrid: update_aggrid(aggrid))
        select_sub.on_value_change(lambda aggrid=aggrid: update_aggrid(aggrid))
        aggrid.on("cellClicked", lambda e, aggrid=aggrid: handle_cell_click(e, aggrid))
        update_aggrid(aggrid)

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
