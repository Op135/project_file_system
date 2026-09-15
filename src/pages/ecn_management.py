# -*- encoding: utf-8 -*-
from typing import (
    Any,
)

from nicegui import (
    app,
    ui,
)

from .. import (
    db_storage,
)
from ..config import (
    IMG_DIR,
    PRESET_AVATARS,
    ECNState,
)
from ..ecn_access import (
    build_ecn_access_snapshot,
    can_create_ecn_request,
    can_delete_ecn,
    can_view_ecn,
)
from ..ecn_management_config import (
    get_ecn_scheme_target_projects,
)
from ..modules.ecn.detail import (
    open_ecn_detail_dialog as open_detail,
)
from ..modules.ecn.list_view import (
    build_ecn_management_grid_row as build_ecn_management_grid_row,
)
from ..modules.ecn.list_view import (
    format_ecn_list_date as format_ecn_list_date,
)
from ..modules.ecn.list_view import (
    get_ecn_list_progress_summary as get_ecn_list_progress_summary,
)
from ..modules.ecn.list_view import (
    get_ecn_management_grid_columns as get_ecn_management_grid_columns,
)
from ..modules.ecn.models import (
    append_ecn_approval_log_once as append_ecn_approval_log_once,
)
from ..modules.ecn.models import (
    generate_ecn_id as generate_ecn_id,
)
from ..modules.ecn.models import (
    generate_initial_ecn_data as generate_initial_ecn_data,
)
from ..modules.ecn.models import (
    get_dept_from_role as get_dept_from_role,
)
from ..modules.ecn.models import (
    get_ecn_template as get_ecn_template,
)
from ..modules.ecn.overview_execution import (
    build_overview_activation_state as build_overview_activation_state,
)
from ..modules.ecn.overview_execution import (
    deactivate_overview_chip_for_ecn as deactivate_overview_chip_for_ecn,
)
from ..modules.ecn.overview_execution import (
    execute_ecn_overview_schemes as execute_ecn_overview_schemes,
)
from ..modules.ecn.repository import (
    atomic_ecn_deep_update as atomic_ecn_deep_update,
)
from ..modules.ecn.repository import (
    del_ecn_deep_item as del_ecn_deep_item,
)
from ..modules.ecn.repository import (
    save_ecn_deep_item as save_ecn_deep_item,
)
from ..modules.ecn.repository import (
    save_ecn_root_item as save_ecn_root_item,
)
from ..utils import (
    get_cache_busted_path,
    logout,
    setup_global_activity_tracking,
    sync_current_user_role,
)
from ..modules.ecn.special_tasks_ui import open_special_tasks_dialog


@ui.page("/ecn_management")
async def ecn_management_page():
    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    ui.add_head_html("""
        <style>
            .q-dialog__inner--minimized>div { max-width: 4000px; }
            .pdf-border { border: 1px solid #cbd5e1; }
            .pdf-border-b { border-bottom: 1px solid #cbd5e1; }
            .pdf-border-r { border-right: 1px solid #cbd5e1; }
            .ecn-management-grid .ecn-grid-header-center .ag-header-cell-label { justify-content: center; }
            .ecn-management-grid .ag-row.row-pending { background-color: #fff1f2 !important; }
            .ecn-management-grid .ag-row.row-rejected { background-color: #fff7ed !important; }
            .ecn-management-grid .ag-row.row-executing { background-color: #f5f3ff !important; }
            .ecn-management-grid .ag-row.row-completed { background-color: #f0fdf4 !important; }
            .ecn-management-grid .ag-row:hover { filter: brightness(0.98); }
            .ecn-management-grid .ecn-trace-closed { color: #15803d; font-weight: 600; }
            .ecn-management-grid .ecn-trace-progress { color: #7c3aed; font-weight: 600; }
            .ecn-management-grid .ecn-trace-pending { color: #c2410c; font-weight: 600; }
            .ecn-management-grid .ecn-trace-not-started { color: #64748b; }
            .ecn-management-grid .ecn-trace-na { color: #cbd5e1; }
            /*::-webkit-scrollbar {
                width: 3px; /* 极细滚动条 */
                background-color: transparent; /* 轨道透明，不占视觉空间 */
            }
            ::-webkit-scrollbar-thumb {
                background-color: #cbd5e1; /* 滚动条颜色 */
                border-radius: 1px;*/
        </style>
    """)
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login?redirect_to=%2Fecn_management")
        return

    current_user = app.storage.user.get("current_user", "未知用户")
    # 会话可能跨服务重启保留，进入页面时同步岗位显示文本；数据库权限不依赖该文本。
    current_role = sync_current_user_role()
    if not can_view_ecn(current_role, current_user):
        ui.notify("当前用户没有查看ECN工程变更的权限", type="warning")
        ui.navigate.to("/main")
        return
    can_create_request = can_create_ecn_request(current_role, current_user)
    can_delete_record = can_delete_ecn(current_role, current_user)
    current_display_path = get_cache_busted_path(
        app.storage.general.get("user_preferences", {}).get(current_user, {}).get("avatar", PRESET_AVATARS[0])
    )

    page_state = {"search_keyword": "", "filter_state": "全部"}

    # ui.dialog: NiceGUI框架提供的模态对话框组件
    dialog = ui.dialog().props("persistent")

    async def open_ecn_detail_dialog(ecn_id=None):
        await open_detail(ecn_id, current_user=current_user, current_role=current_role, refresh_list=refresh_list)

    # ==========================================
    # 管理员功能：删除确认与执行
    # ==========================================
    async def confirm_delete(ecn_id):
        if not can_delete_ecn(current_role, current_user):
            return ui.notify("当前用户没有删除ECN单据的权限", type="warning")
        dialog.clear()
        with dialog, ui.card().classes("p-6"):
            ui.label("删除确认 (仅管理员)").classes("text-xl font-bold text-red-600 border-b pb-2 mb-4 w-full")
            ui.label(f"您确定要永久删除 ECN 单号【{ecn_id}】吗？")
            ui.label("该操作将清除所有的表单与审批流转记录，且不可恢复！").classes("text-sm text-gray-500 mt-2")
            with ui.row().classes("w-full justify-end mt-6 gap-3"):
                ui.button("取消", on_click=dialog.close).props("outline color=grey")

                async def do_delete():
                    if not can_delete_ecn(current_role, current_user):
                        ui.notify("当前用户没有删除ECN单据的权限", type="warning")
                        dialog.close()
                        return
                    # 采用代理的原子化深层删除，避免并发读写并触发全局刷新
                    success = await del_ecn_deep_item(["ecn_management_data", ecn_id])

                    if success:
                        ui.notify(f"单号 {ecn_id} 已被彻底删除", type="positive")
                        refresh_list()
                    else:
                        ui.notify(f"删除失败，单据 {ecn_id} 可能已不存在或发生异常", type="negative")
                    dialog.close()

                ui.button("确认删除", color="red", on_click=do_delete)
        dialog.open()

    # ==========================================
    # 主页面 UI (头部与列表总览)
    # ==========================================
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
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

    # ==========================================
    # 优化点 1：主页面列表的静默轮询与刷新机制 (终极 O(1) 性能版)
    # ==========================================
    # 这里的 hash 变量名我们改叫 version_stamp，更符合语意
    last_ecn_state_tracker = {"version_stamp": 0.0}

    def check_and_refresh_list():
        # 极限性能：不遍历、不拼接、不哈希。直接拿全局时间戳对比！
        current_stamp = db_storage.get_item("ecn_global_version_stamp", 0.0)
        # 判断时间戳是否发生改变
        if last_ecn_state_tracker["version_stamp"] != 0.0 and current_stamp != last_ecn_state_tracker["version_stamp"]:
            last_ecn_state_tracker["version_stamp"] = current_stamp
            refresh_list()
        elif last_ecn_state_tracker["version_stamp"] == 0.0:
            # 首次加载时记录初始时间戳
            last_ecn_state_tracker["version_stamp"] = current_stamp

    # ui.timer: NiceGUI第三方Web框架中用于周期性执行异步或同步函数的类
    ui.timer(5.0, check_and_refresh_list)

    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-slate-50 flex flex-col"):
        with ui.row().classes("w-full justify-between items-center bg-white p-4 shadow-sm rounded-md shrink-0"):
            with ui.row().classes("gap-4 items-center"):
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
                        ECNState.CLOSED,
                        ECNState.CANCEL,
                        ECNState.REJECTED,
                    ],
                    label="状态筛选",
                ).props("dense outlined").bind_value(page_state, "filter_state").classes("w-40")
                ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("color=primary outline")
                ui.button("刷新", icon="refresh", on_click=lambda: refresh_list()).props("flat color=primary")
                execution_focus_switch = (
                    ui.switch("关注执行进度", value=False)
                    .props("dense color=purple")
                    .tooltip("开启后隐藏中间信息列，优先完整展示各追溯范围的执行状态")
                )
            with ui.row().classes("gap-2 items-center"):
                ui.label("点击“详情”打开ECN").classes("text-xs text-gray-500")
                if can_create_request:
                    ui.button("新建 ECR 申请", icon="add_box", on_click=lambda: open_ecn_detail_dialog()).props(
                        "color=red-7"
                    )
        with ui.element("div").classes("w-full flex-1 min-h-0 p-4 md:p-6"):
            ecn_grid = ui.aggrid(
                {
                    "columnDefs": get_ecn_management_grid_columns(can_delete_record),
                    "rowData": [],
                    "defaultColDef": {
                        "sortable": True,
                        "resizable": True,
                        "cellStyle": {"textAlign": "center"},
                        "headerClass": "ecn-grid-header-center",
                        "filterParams": {"buttons": ["reset"], "debounceMs": 250},
                    },
                    "headerHeight": 42,
                    "rowHeight": 42,
                    "enableCellTextSelection": True,
                    "columnMenu": "new",
                    "suppressMenuHide": True,
                    "pagination": True,
                    "paginationPageSize": 30,
                    "paginationPageSizeSelector": [20, 30, 50, 100],
                    "animateRows": False,
                    "rowClassRules": {
                        "row-pending": "data.row_tone == 'pending'",
                        "row-rejected": "data.row_tone == 'rejected'",
                        "row-executing": "data.row_tone == 'executing'",
                        "row-completed": "data.row_tone == 'completed'",
                    },
                    "overlayNoRowsTemplate": "<span class='text-gray-500'>没有符合当前条件的工程变更记录</span>",
                },
                auto_size_columns=False,
            ).classes("ecn-management-grid ag-theme-alpine w-full h-full min-h-0")

            execution_focus_hidden_fields = [
                "projects",
                "applicant",
                "apply_date",
            ]

            def apply_execution_focus(enabled: bool) -> None:
                ecn_grid.run_grid_method(
                    "setColumnsVisible",
                    execution_focus_hidden_fields,
                    not enabled,
                )

            execution_focus_switch.on_value_change(lambda event: apply_execution_focus(bool(event.value)))

            async def handle_ecn_grid_cell(event: Any) -> None:
                event_args = event.args if isinstance(event.args, dict) else {}
                row_data = event_args.get("data")
                if not isinstance(row_data, dict):
                    return
                ecn_id = str(row_data.get("record_id") or "").strip()
                if not ecn_id:
                    return
                column_id = str(event_args.get("colId") or "")
                if column_id == "detail_action":
                    await open_ecn_detail_dialog(ecn_id)
                elif column_id == "special_tasks":
                    await open_special_tasks_dialog(ecn_id, current_user, current_role, refresh_list)
                elif column_id == "delete_action" and can_delete_record:
                    await confirm_delete(ecn_id)

            async def open_ecn_grid_record(event: Any) -> None:
                event_args = event.args if isinstance(event.args, dict) else {}
                if str(event_args.get("colId") or "") == "delete_action":
                    return
                row_data = event_args.get("data")
                ecn_id = str(row_data.get("record_id") or "").strip() if isinstance(row_data, dict) else ""
                if ecn_id:
                    await open_ecn_detail_dialog(ecn_id)

            ecn_grid.on("cellClicked", handle_ecn_grid_cell)
            ecn_grid.on("rowDoubleClicked", open_ecn_grid_record)

            def refresh_list():
                all_ecns = db_storage.get_item("ecn_management_data", {})
                access_snapshot = build_ecn_access_snapshot(app.state.user_service)
                keyword = str(page_state.get("search_keyword") or "").lower().strip()
                filter_state = str(page_state.get("filter_state") or "全部")
                raw_ecns = all_ecns.values() if isinstance(all_ecns, dict) else []
                valid_ecns = [
                    ecn for ecn in raw_ecns if isinstance(ecn, dict) and isinstance(ecn.get("basic_info"), dict)
                ]

                def get_apply_date(ecn: dict) -> str:
                    basic_info = ecn.get("basic_info", {})
                    return str(basic_info.get("apply_date") or "") if isinstance(basic_info, dict) else ""

                valid_ecns.sort(
                    key=get_apply_date,
                    reverse=True,
                )
                rows = []
                for ecn in valid_ecns:
                    basic_info = ecn.get("basic_info", {})
                    if not isinstance(basic_info, dict):
                        continue
                    workflow = ecn.get("workflow", {})
                    workflow = workflow if isinstance(workflow, dict) else {}
                    current_state = str(workflow.get("current_state") or "")
                    searchable = " ".join(
                        [
                            str(ecn.get("ecn_id") or ""),
                            " ".join(get_ecn_scheme_target_projects(ecn)),
                            str(basic_info.get("applicant") or ""),
                            str(basic_info.get("title") or ""),
                        ]
                    ).lower()
                    if keyword and keyword not in searchable:
                        continue
                    if filter_state != "全部" and current_state != filter_state:
                        continue
                    rows.append(
                        build_ecn_management_grid_row(
                            ecn,
                            current_user,
                            current_role,
                            include_delete=can_delete_record,
                            user_service=app.state.user_service,
                            access_snapshot=access_snapshot,
                        )
                    )
                ecn_grid.options["rowData"] = rows
                ecn_grid.update()
                if execution_focus_switch.value:
                    apply_execution_focus(True)

            refresh_list()
