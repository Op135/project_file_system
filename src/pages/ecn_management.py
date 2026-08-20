# -*- encoding: utf-8 -*-
import copy  # copy: Python标准库，用于创建对象的副本
import logging
import mimetypes
import os
import ssl
import time
import uuid  # uuid: Python标准库，用于生成全局唯一的标识符
from datetime import datetime

import httpx
from httpx import BasicAuth
from nicegui import app, ui  # nicegui: 第三方轻量级Python Web框架，用于纯Python编写前端UI

from .. import db_storage
from ..components import FileThumbnail
from ..config import (
    ECN_ALLOWED_PROJECT_STATES,
    ECN_SCHEMA_CONFIG,
    ECN_SCHEME_INITIATOR_ROLES,
    ECN_SCHEME_WRITER_ROLES,
    ECN_WORKFLOW_ROUTES,
    FILES_URL_DIR,
    IMG_DIR,
    PDF_PREVIEW_CACHE,
    PRESET_AVATARS,
    SVN_PASSWORD,
    SVN_USERNAME,
    UPLOADS_DIR,
    ECNState,
)
from ..custom_ui import custom_upload
from ..ecn_management_config import (
    ECN_DISPOSITION_MEASURES,
    ECN_DOCUMENT_CHANGE_TYPES,
    ECN_ITEM_STATUS_CONFIG,
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_ITEM_STATUS_NORMAL,
    ECN_ITEM_STATUS_REVISED_CONFIRMED,
    ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION,
    ECN_MATERIAL_CHANGE_TYPE_ADD,
    ECN_MATERIAL_CHANGE_TYPE_ADJUST_QUANTITY,
    ECN_MATERIAL_CHANGE_TYPE_DISCONTINUE,
    ECN_MATERIAL_CHANGE_TYPE_REPLACE,
    ECN_MATERIAL_CHANGE_TYPES,
    ECN_MATERIAL_DEFAULT_UNIT,
    ECN_ORDINARY_DOCUMENT_FILE_VIEW_ROLES_BY_TYPE,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_LABELS,
    ECN_OVERVIEW_ACTION_UPDATE,
    ECN_OVERVIEW_CONFLICT_AUTO_CLOSE_SECONDS,
    ECN_PARTICIPANT_STATUS_CONFIG,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECN_REQUIRE_REJECTED_ITEM_SELECTION,
    ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_SCHEME_GROUP_UNKNOWN,
    ECN_TRACEABILITY_LEVELS,
    build_overview_validation_signature,
    can_view_ecn_scheme_non_image_file,
    classify_ecn_change_item,
    collect_ecn_pending_overview_overrides,
    confirm_revised_scheme_items,
    ecn_overview_requires_new_content,
    expand_new_material_traceability_selection,
    get_active_overview_row_contents,
    get_ecn_material_change_display,
    get_ecn_material_change_missing_fields,
    get_ecn_overview_project_new_data,
    get_ecn_scheme_coverage,
    get_ecn_scheme_target_projects,
    has_unrevised_rejected_scheme_items,
    is_ecn_disposition_condition_required,
    is_ecn_material_disposition_required,
    is_ecn_pending_for_user,
    is_ecn_review_info_blank,
    mark_rejected_scheme_item_revised,
    merge_ecn_impact_audit_log,
    register_ecn_impact_handler,
    reject_ecn_scheme_items,
    resolve_ecn_overview_parameter_config,
)
from ..utils import get_cache_busted_path, logout, setup_global_activity_tracking

logger = logging.getLogger(__name__)


# ==========================================
# 数据模板与合并机制 (防御性架构核心)
# ==========================================
def get_ecn_template() -> dict:
    """
    生成当前系统最新版本的 ECN 标准数据结构模板。
    """
    return {
        "ecn_id": "",
        "form_no": "RF-FM-280-A4",
        # 基本信息
        "basic_info": {
            "title": "",
            "applicant_dept": "",  # 申请部门
            "applicant": "",  # 申请人
            "apply_date": "",  # 申请日期
            "requirement_date": "",  # 需求日期
            "file_no": "",  # 文件编号
            "nature": ECN_SCHEMA_CONFIG["change_natures"][0],  # 变更性质
            "erp_no": "",  # ERP编号
            "reasons": {r: False for r in ECN_SCHEMA_CONFIG["reasons"]},  # 变更原因，按照常量生成未勾选选项
            "other_reason_desc": "",  # 其它原因，用户填写的信息
            "requirements": [],  # 变更要求
            "reason_desc": "",  # 变更原因说明
        },
        # 变更涉及的项目型号
        "target_projects": [],
        # 评审信息
        "review_info": {
            "expanded_projects_mass": [],  # 扩展的转产后项目型号
            "expanded_projects_non_mass": [],  # 扩展的转产前项目型号
            "impact_change_log": [],  # ECN影响字段级审计：项目增删、影响项勾选/取消
            "impacts": {
                dim: False for dim in ECN_SCHEMA_CONFIG["impact_dimensions"]
            },  # 变更影响维度，按照常量生成未勾选选项
            "involved_docs": {
                doc: False for doc in ECN_SCHEMA_CONFIG["document_types"]
            },  # 涉及的文档资料，按照常量生成未勾选选项
            "other_docs_desc": "",  # 其它文档资料说明，用户填写的信息
            # 涉及的物料类别及对应的变更行动，按照常量生成未勾选选项的嵌套字典结构
            "involved_materials": {
                mat: {act: False for act in ECN_SCHEMA_CONFIG["material_actions"]}
                for mat in ECN_SCHEMA_CONFIG["material_categories"]
            },
        },
        # 执行信息
        "execution_info": {
            "traceability_level": "无影响",
            "handling_measures": {measure: False for measure in ECN_SCHEMA_CONFIG["execution_handling_measures"]},
            "trial_conclusion": "",
        },
        "change_items": [],
        # 评审工作流程
        "workflow": {
            "current_state": ECNState.DRAFT,  # ECN当前流程状态
            "current_phase": "ECR_PHASE",  # 当前流程阶段
            "current_step_index": 0,  # 当前步骤索引
            "route_type": "",  # 路由类型
            "pending_roles": [],  # 待处理角色
            "step_approvals": {},  # 步骤审批信息
            "scheme_participants": {},  # 方案参与者
            "impact_handlers": [],  # 实际维护过ECN影响区的具体人员，用于精准待办提醒
        },
        "approval_log": [],  # 审批日志，记录每一步的审批人、时间、意见等信息
        "timestamp": {},  # 时间戳记录，记录每次重要操作的时间和描述，用于前端 O(1) 轮询刷新机制
    }


# ==========================================
# 辅助与业务逻辑函数 (独立于 UI 树)
# ==========================================
def get_dept_from_role(role: str) -> str:
    """
    如果传入的角色名称里，含有指定字符串，返回该字符串对应的该角色的部门名称
    """
    role_to_dept_map = {
        "研发": "研发部",
        "销售": "销售部",
        "工程": "工程部",
        "生产": "生产部",
        "质量": "质量部",
        "采购": "采购部",
        "PMC": "物资部",
    }
    for key, dept in role_to_dept_map.items():
        if key in role:
            return dept
    return "其它部门"


def generate_ecn_id(all_ecns: dict) -> str:
    """
    找到all_ecns里最大的当前日期最大序号，加1生成新的ECN编号
    """
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
    return f"{prefix}{str(max_count + 1).zfill(2)}"


def generate_initial_ecn_data(applicant: str, role: str, all_ecns: dict) -> dict:
    """
    在模板基础上，初始化运行时强相关的动态ECN数据（如单号、时间、申请人）
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ecn_id = generate_ecn_id(all_ecns)
    applicant_dept = get_dept_from_role(role)

    new_data = get_ecn_template()
    new_data["ecn_id"] = ecn_id  # 初始化ECN编号
    new_data["basic_info"]["applicant_dept"] = applicant_dept  # 初始化申请部门
    new_data["basic_info"]["applicant"] = applicant  # 初始化申请人
    new_data["basic_info"]["apply_date"] = now_str  # 初始化申请日期
    new_data["basic_info"]["file_no"] = ecn_id  # 初始化文件编号，与ECN编号一致
    new_data["timestamp"][now_str] = f"由 {applicant} 创建草稿"  # 记录一条日志

    return new_data


# ==========================================
# ECN 专属数据写入代理 (O(1) 轮询架构核心 & 原子化)
# ==========================================
async def atomic_ecn_deep_update(path: list, update_function, *args, **kwargs):
    """
    原子化深层更新代理：
    拦截底层 db_storage.atomic_deep_update，并在更新成功后刷新全局时间戳，
    驱动所有在线用户的前端进行 O(1) 轮询刷新。
    """
    success = await db_storage.atomic_deep_update(path, update_function, *args, **kwargs)
    if success:
        await db_storage.set_item("ecn_global_version_stamp", time.time())
    return success


async def del_ecn_deep_item(path: list):
    """
    原子化深层删除代理：
    拦截底层 db_storage.del_deep_item，并在删除成功后刷新全局时间戳。
    """
    success = await db_storage.del_deep_item(path)
    if success:
        await db_storage.set_item("ecn_global_version_stamp", time.time())
    return success


async def save_ecn_deep_item(path: list, data):
    """保存ECN执行阶段关联的数据节点，并更新全局版本戳。"""
    await db_storage.set_deep_item(path, data)
    await db_storage.set_item("ecn_global_version_stamp", time.time())


async def save_ecn_root_item(key: str, data):
    """拦截根节点数据保存 (例如整个 all_ecns)，并更新全局版本戳"""
    await db_storage.set_item(key, data)
    await db_storage.set_item("ecn_global_version_stamp", time.time())


def append_ecn_approval_log_once(approval_log: list, entry: dict) -> bool:
    """幂等追加相邻的同一条流程日志，避免首次发起时重复落盘。"""
    if approval_log and approval_log[-1] == entry:
        return False
    approval_log.append(copy.deepcopy(entry))
    return True


def deactivate_overview_chip_for_ecn(
    chip: dict,
    req_ver: str,
    ecn_id: str,
    operation_time: str,
) -> dict:
    """生成 ECN 自动失活后的旧 Chip，并同步最近操作人与状态历史。"""
    result = copy.deepcopy(chip)
    operator = f"ECN自动执行 ({ecn_id})"
    result.setdefault("select_activ_dic", {})[req_ver] = False
    result["enabled"] = False
    result["bg_color"] = "bg-grey-5"
    result["icon"] = "block"
    result["creator"] = operator
    result.setdefault("timestamp", {})[operation_time] = {
        "creator": operator,
        "select_activ_dic": copy.deepcopy(result["select_activ_dic"]),
    }
    return result


# ==========================================
# 主路由页面定义
# ==========================================
# @ui.page: NiceGUI框架的路由装饰器，用于定义页面路径
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
        ui.navigate.to("/login")
        return

    current_user = app.storage.user.get("current_user", "未知用户")
    current_role = app.storage.user.get("current_role", "未知角色")
    current_display_path = get_cache_busted_path(
        app.storage.general.get("user_preferences", {}).get(current_user, {}).get("avatar", PRESET_AVATARS[0])
    )

    page_state = {"search_keyword": "", "filter_state": "全部"}

    # ui.dialog: NiceGUI框架提供的模态对话框组件
    dialog = ui.dialog().props("persistent")
    root_dialog = ui.dialog().props("maximized persistent")

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

    # ==========================================
    # 独立解耦弹窗 1：系统内资料变更方案设计
    # ==========================================
    def open_overview_change_dialog(ecn_data, current_user, on_save_callback, edit_item=None):
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
                        sel_role = ui.select(options=roles, label="1. 技术维度", value=sel_state["role"]).classes(
                            "w-full"
                        )
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
                                    ui.label(
                                        "统一方案 / 新内容 (必填)" if requires_new_content else "失效说明"
                                    ).classes("text-xs text-blue-700 font-bold mb-2")

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
                                                from ..utils import validate_search_path, validate_svn_url

                                                project_results = {}
                                                local_file_path = ""
                                                if ptype == "search":
                                                    primary_proj = (
                                                        sel_state["projects"][0] if sel_state["projects"] else ""
                                                    )
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
                                            from ..config import FILES_URL_DIR, UPLOADS_DIR

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
                    if (
                        p_state["action"] == ECN_OVERVIEW_ACTION_ADD
                        and not is_first_col
                        and not p_state["anchor_row_id"]
                    ):
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
                    "item_id": edit_data.get("item_id", str(uuid.uuid4())),
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
                await on_save_callback(payload, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4 shrink-0"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")

        dialog.open()

    # ==========================================
    # 独立解耦弹窗 2：文本描述方案设计 (复用组件)
    # ==========================================
    def open_text_change_dialog(
        ecn_data,
        current_user,
        on_save_callback,
        edit_item=None,
        scheme_category=ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ):
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
            ui.label(f"修改{dialog_title}" if is_edit else f"添加{dialog_title}").classes(
                "text-lg font-bold text-blue-900"
            )

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
                                ui.number("用量（必填）").classes("w-full").bind_value(
                                    material_state, "quantity"
                                ).props("outlined dense bg-white step=any")
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
                                ui.input("改前单位（必填）").classes("w-full").bind_value(
                                    material_state, "old_unit"
                                ).props("outlined dense bg-white")
                            ui.label("改后物料").classes("text-[11px] font-bold text-slate-500 mt-1")
                            with ui.grid(columns=3).classes("w-full gap-3"):
                                ui.input("改后物料名称（必填）").classes("w-full").bind_value(
                                    material_state, "new_material_name"
                                ).props("outlined dense bg-white")
                                ui.number("改后用量（必填）").classes("w-full").bind_value(
                                    material_state, "new_quantity"
                                ).props("outlined dense bg-white step=any")
                                ui.input("改后单位（必填）").classes("w-full").bind_value(
                                    material_state, "new_unit"
                                ).props("outlined dense bg-white")

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
                    "item_id": edit_data.get("item_id", str(uuid.uuid4())),
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
                await on_save_callback(payload, is_edit)
                dialog.close()

            with ui.row().classes("w-full justify-end mt-4"):
                ui.button("取消", on_click=dialog.close).props("flat color=grey")
                ui.button("确认修改" if is_edit else "确认添加", on_click=save_item).props("color=primary")
        dialog.open()

    # ------------------------------------------
    # 核心总控台：详情与流转操作
    # ------------------------------------------
    async def open_ecn_detail_dialog(ecn_id=None):
        is_new = ecn_id is None
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
            ecn_data = generate_initial_ecn_data(current_user, current_role, all_ecns)
        else:
            ecn_data = all_ecns[ecn_id]

        local_data = copy.deepcopy(ecn_data)

        wf = local_data["workflow"]
        basic = local_data["basic_info"]
        review = local_data["review_info"]
        participants = wf.setdefault("scheme_participants", {})

        is_draft_or_reject = is_new or wf["current_state"] in [ECNState.DRAFT, ECNState.REJECTED]
        # 是否处于编写方案阶段
        is_scheming_phase = wf["current_state"] == ECNState.ECN_SCHEMING
        # 综合权限判断：既要是编写阶段，当前角色又必须在允许的常量白名单中
        is_scheme_writer = is_scheming_phase and any(role in current_role for role in ECN_SCHEME_WRITER_ROLES)

        # === 建立一个跨 Tab 刷新的引用桥梁 ===
        dashboard_updater = {"refresh": lambda: None}  # 初始值为一个空函数，后续会被覆盖为真正的刷新函数

        def record_impact_change(field, target, action, before, after):
            if not is_scheme_writer:
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
            if ecn_id and is_scheme_writer:

                def merge_review_data(current_ecn, local_review, handler):
                    if not current_ecn:
                        return current_ecn
                    current_review = current_ecn.setdefault("review_info", {})
                    # 仅更新 review_info 中的部分字段，避免覆盖掉 workflow 或 basic 中的其他数据
                    current_review["expanded_projects_mass"] = copy.deepcopy(
                        local_review.get("expanded_projects_mass", [])
                    )
                    current_review["expanded_projects_non_mass"] = copy.deepcopy(
                        local_review.get("expanded_projects_non_mass", [])
                    )
                    current_review.setdefault("impacts", {}).update(local_review.get("impacts", {}))
                    current_review.setdefault("involved_docs", {}).update(local_review.get("involved_docs", {}))

                    for mat, acts in local_review.get("involved_materials", {}).items():
                        if isinstance(acts, dict):
                            current_review.setdefault("involved_materials", {}).setdefault(mat, {}).update(acts)

                    current_review["other_docs_desc"] = local_review.get("other_docs_desc", "")
                    merge_ecn_impact_audit_log(
                        current_review,
                        local_review.get("impact_change_log", []),
                    )
                    # 只有影响区已有有效内容时才认领处理人；已认领者不会因后续取消勾选而丢失。
                    register_ecn_impact_handler(current_ecn, handler, local_review)
                    return current_ecn

                success = await atomic_ecn_deep_update(
                    ["ecn_management_data", ecn_id], merge_review_data, review, current_user
                )
                if success and not is_ecn_review_info_blank(review):
                    register_ecn_impact_handler(local_data, current_user, review)

                if dashboard_updater["refresh"]:
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
                    ui.label(f"{local_data['ecn_id']}").classes("text-lg font-mono font-bold text-gray-700")
                ui.button(icon="close", on_click=root_dialog.close).props("flat round dense").classes("ml-15")

            # ui.tabs: NiceGUI框架用于创建选项卡导航容器的类
            with ui.tabs().classes("w-full shrink-0 bg-white") as tabs:
                tab_ecr = ui.tab("1. ECR-申请", icon="assignment")
                tab_impact = ui.tab("2. ECN-影响", icon="fact_check")
                tab_scheme = ui.tab("3. ECN-方案", icon="design_services")
                tab_exec = ui.tab("4. ECN-执行", icon="assignment_turned_in")
                tab_workflow = ui.tab("审批记录", icon="timeline")

            # 当前用户是否为ECN申请人，且处于草稿或驳回待编辑状态
            is_ecr_editable = is_new or (
                basic.get("applicant") == current_user
                and wf.get("current_state") in [ECNState.DRAFT, ECNState.REJECTED]
            )

            with ui.tab_panels(tabs, value=tab_ecr).classes("w-full flex-1 min-h-0 p-2 md:p-4"):
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
                            ui.input("文件编号", value=basic["file_no"]).props(
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
                                            ui.label("尚未添加变更对象 (项目)").classes(
                                                "text-xs text-red-400 italic mt-1"
                                            )
                                        # 如果有目标项目，生成它们的chip，并在可编辑状态下添加删除功能，删除后会重新调用自己，进行刷新
                                        for p in local_data["target_projects"]:
                                            with ui.chip(color="primary", text_color="white").classes(
                                                "gap-1 items-center"
                                            ):
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
                                            .props(
                                                f"dense outlined bg-white {'readonly' if not is_ecr_editable else ''}"
                                            )
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
                                                ui.label(req["content"]).classes(
                                                    "text-sm text-gray-800 flex-1 break-all"
                                                )
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
                                        ui.label("ECR申请涵盖项目:").classes(
                                            "text-xs font-bold text-gray-500 w-36 pt-1"
                                        )
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
                                                            ui.select(
                                                                options={}, on_change=lambda e: ps.update(l3=e.value)
                                                            )
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
                                                            f"outline dense {'disable' if not is_scheme_writer else ''}"
                                                        ).classes("mt-0")

                                                chip_container = ui.row().classes("gap-1")

                                                def render_chips():
                                                    chip_container.clear()
                                                    with chip_container:
                                                        if not target_list:
                                                            ui.label("未扩大").classes("text-xs text-gray-400 mt-1")
                                                        for p in target_list:
                                                            with ui.chip(p, color=color, text_color="white").props(
                                                                "dense"
                                                            ):
                                                                if is_scheme_writer:

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
                                            f"{'disable' if not is_scheme_writer else ''} dense"
                                        ).on_value_change(on_impact_change)

                            with ui.column().classes("w-full p-2 pdf-border-b gap-2 hover:bg-gray-50"):
                                ui.label("变更涉及资料 (必出方案):").classes("font-bold text-gray-700")
                                with ui.grid().classes(
                                    "w-full grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-2 gap-y-1 ml-4 items-center"
                                ):
                                    # 动态读取配置遍历
                                    for doc_key in ECN_SCHEMA_CONFIG["document_types"]:
                                        ui.checkbox(doc_key).bind_value(review["involved_docs"], doc_key).props(
                                            f"{'disable' if not is_scheme_writer else ''} dense"
                                        ).on_value_change(auto_save_review)

                                # bind_visibility_from: 实现“其它”项仅在勾选后显示
                                ui.input("其它:").bind_value(review, "other_docs_desc").bind_visibility_from(
                                    review["involved_docs"], "其它"
                                ).props(
                                    f"outlined dense {'readonly bg-gray-100' if not is_scheme_writer else 'bg-white'}"
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
                                                        f"{'disable' if not is_scheme_writer else ''} dense"
                                                    ).on_value_change(auto_save_review)

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
                                                with ui.row().classes(
                                                    "items-center gap-2 border-b border-orange-200 pb-2 w-full"
                                                ):
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
                                                                        int(value)
                                                                        if str(value).isdigit()
                                                                        else float("inf"),
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
                                                        ui.label("✔ 资料方案已全覆盖").classes(
                                                            "text-xs text-green-600 font-bold"
                                                        )

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
                                            ).props(
                                                f"color=indigo outline dense {'disable' if not is_scheming_phase else ''}"
                                            )

                                            ui.button(
                                                "添加其它特定事项/资料变更方案",
                                                icon="article",
                                                on_click=lambda: open_text_change_dialog(
                                                    local_data,
                                                    current_user,
                                                    handle_save_item,
                                                    scheme_category=ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
                                                ),
                                            ).props(
                                                f"color=primary outline dense {'disable' if not is_scheming_phase else ''}"
                                            )

                                            ui.button(
                                                "添加物料变更方案",
                                                icon="inventory",
                                                on_click=lambda: open_text_change_dialog(
                                                    local_data,
                                                    current_user,
                                                    handle_save_item,
                                                    scheme_category=ECN_SCHEME_GROUP_MATERIAL,
                                                ),
                                            ).props(
                                                f"color=secondary outline dense {'disable' if not is_scheming_phase else ''}"
                                            )

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
                                                        on_click=lambda: toggle_part_status(
                                                            ECN_PARTICIPANT_STATUS_CONFIRMED
                                                        ),
                                                    ).props("color=green outline dense")
                                                elif cur_status == ECN_PARTICIPANT_STATUS_CONFIRMED:
                                                    ui.button(
                                                        "重新开启编辑",
                                                        icon="lock_open",
                                                        on_click=lambda: toggle_part_status(
                                                            ECN_PARTICIPANT_STATUS_EDITING
                                                        ),
                                                    ).props("color=orange outline dense")

                                    render_my_actions()

                                    # 切换参与者状态的显示与数据库对应状态数据
                                    async def toggle_part_status(new_status):
                                        if (
                                            new_status == ECN_PARTICIPANT_STATUS_CONFIRMED
                                            and ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION
                                            and has_unrevised_rejected_scheme_items(local_data, current_user)
                                        ):
                                            return ui.notify(
                                                "仍有被驳回方案尚未修改，请先完成整改后再重新确认。",
                                                type="warning",
                                            )
                                        # 1. 定义原子更新回调。底层把 ATOMIC_NO_UPDATE 视作正常事务，
                                        # 因此另用业务标记区分“写入成功”和“并发校验拦截”。
                                        blocked_by_unrevised = {"value": False}

                                        def update_my_status(current_ecn, user, status):
                                            if not current_ecn:
                                                return current_ecn
                                            current_parts = current_ecn.setdefault("workflow", {}).setdefault(
                                                "scheme_participants", {}
                                            )
                                            if (
                                                status == ECN_PARTICIPANT_STATUS_CONFIRMED
                                                and ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION
                                                and has_unrevised_rejected_scheme_items(current_ecn, user)
                                            ):
                                                blocked_by_unrevised["value"] = True
                                                return db_storage.ATOMIC_NO_UPDATE
                                            current_parts[user] = status
                                            if status == ECN_PARTICIPANT_STATUS_CONFIRMED:
                                                confirm_revised_scheme_items(current_ecn, user)
                                            return current_ecn

                                        # 2. 执行包裹了时间戳更新的原子操作
                                        success = await atomic_ecn_deep_update(
                                            [
                                                "ecn_management_data",
                                                local_data["ecn_id"],
                                            ],
                                            update_my_status,
                                            current_user,
                                            new_status,
                                        )
                                        if not success:
                                            return ui.notify("确认状态更新失败，请刷新后重试。", type="negative")
                                        if blocked_by_unrevised["value"]:
                                            return ui.notify(
                                                "方案已被他人更新，仍有被驳回方案尚未修改，请刷新后重试。",
                                                type="warning",
                                            )
                                        participants[current_user] = new_status
                                        if new_status == ECN_PARTICIPANT_STATUS_CONFIRMED:
                                            confirm_revised_scheme_items(local_data, current_user)

                                        # 3. 触发重新渲染
                                        render_parts()  # 更新参与者状态显示标签
                                        render_my_actions()  # 更新状态对应的可行动按钮
                                        render_items()  # 状态切换后，必须通知下方的方案列表重新渲染，以更新编辑/删除按钮的显示状态

                                # 方案内容显示列
                                item_container = ui.column().classes("w-full gap-3")
                                scheme_group_expansion_state = {
                                    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT: True,
                                    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT: True,
                                    ECN_SCHEME_GROUP_MATERIAL: True,
                                    ECN_SCHEME_GROUP_UNKNOWN: True,
                                }

                                async def handle_save_item(item_data, is_edit=False):
                                    """保存方案 (原子化重构)"""

                                    # ==== 添加方案时的核心逻辑 ====
                                    def update_ecn_scheme(current_ecn, new_item, edit_mode, user):
                                        """
                                            将新添加或更新的方案条目数据合并到当前 ECN 数据中，并同步更新当前用户的参与状态为 editing
                                        key:
                                            current_ecn: 当前数据库中的 ECN 数据
                                            new_item: 本次需要添加或更新的方案条目数据
                                            edit_mode: 是否为编辑模式(更新)
                                            user: 当前用户
                                        """
                                        if not current_ecn:
                                            return current_ecn

                                        # a. 更新 change_items 里记录的方案
                                        items = current_ecn.setdefault("change_items", [])
                                        # 是否属于更新
                                        if edit_mode:
                                            for idx, e_item in enumerate(items):
                                                if e_item["item_id"] == new_item["item_id"]:
                                                    if e_item.get("rejection_history"):
                                                        new_item["review_status"] = ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
                                                        new_item["rejection_history"] = copy.deepcopy(
                                                            e_item.get("rejection_history", [])
                                                        )
                                                        mark_rejected_scheme_item_revised(new_item)
                                                    items[idx] = new_item
                                                    break
                                        # 添加方案
                                        else:
                                            items.append(new_item)

                                        # b. 修改被驳回方案后保留定向整改语义，其余编辑重置为普通编写中。
                                        parts_dict = current_ecn.setdefault("workflow", {}).setdefault(
                                            "scheme_participants", {}
                                        )
                                        mark_rejected_scheme_item_revised(new_item)
                                        if (
                                            new_item.get("review_status")
                                            == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
                                        ):
                                            parts_dict[user] = ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
                                        else:
                                            parts_dict[user] = ECN_PARTICIPANT_STATUS_EDITING

                                        return current_ecn

                                    success = await atomic_ecn_deep_update(
                                        ["ecn_management_data", local_data["ecn_id"]],
                                        update_ecn_scheme,
                                        item_data,
                                        is_edit,
                                        current_user,
                                    )
                                    if success:
                                        # 同步本地数据以更新 UI
                                        if is_edit:
                                            for idx, e_item in enumerate(local_data["change_items"]):
                                                if e_item["item_id"] == item_data["item_id"]:
                                                    if e_item.get("rejection_history"):
                                                        item_data["review_status"] = ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
                                                        item_data["rejection_history"] = copy.deepcopy(
                                                            e_item.get("rejection_history", [])
                                                        )
                                                        mark_rejected_scheme_item_revised(item_data)
                                                    local_data["change_items"][idx] = item_data
                                                    break
                                        else:
                                            local_data["change_items"].append(item_data)

                                        mark_rejected_scheme_item_revised(item_data)
                                        participants[current_user] = (
                                            ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
                                            if item_data.get("review_status")
                                            == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
                                            else ECN_PARTICIPANT_STATUS_EDITING
                                        )

                                        render_parts()  # 更新参与者状态显示标签
                                        render_my_actions()  # 更新状态对应的可行动按钮
                                        render_items()  # 更新方案列表显示
                                        render_coverage_dashboard()  # 更新覆盖率看板
                                    else:
                                        ui.notify("方案保存失败，请重试。", type="negative")

                                def get_item_projects(item):
                                    return [project for project in item.get("projects", []) if project]

                                # --- 替换列表渲染分组部分 ---
                                def render_items():
                                    """按资料分类渲染舒适型对比表格。"""
                                    item_container.clear()
                                    with item_container:
                                        change_items = local_data.get("change_items", [])
                                        if not change_items:
                                            ui.label("暂未添加具体的方案条目").classes(
                                                "text-sm text-slate-400 m-auto mt-4"
                                            )
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
                                                disposition_condition = str(
                                                    item.get("disposition_condition") or ""
                                                ).strip()
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
                                                    ui.label(f"要求 {requirement_idx}").classes(
                                                        "text-sm text-slate-700 break-all"
                                                    )
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
                                                        ui.label(table_item_title(item)).classes(
                                                            "text-xs text-slate-500 break-all"
                                                        )
                                                    ui.button(
                                                        icon="close",
                                                        on_click=overview_existing_data_dialog.close,
                                                    ).props("flat round dense color=blue-grey-7")
                                                with ui.column().classes("w-full gap-2 p-4 overflow-y-auto"):
                                                    for index, entry in enumerate(entries, start=1):
                                                        with ui.card().classes(
                                                            "w-full p-3 gap-1 shadow-none border border-slate-200"
                                                        ):
                                                            ui.label(
                                                                f"{index}. {entry.get('source', '当前已有')}"
                                                            ).classes("text-xs font-bold text-slate-500")
                                                            ui.label(str(entry.get("content") or "（空内容）")).classes(
                                                                "text-sm text-slate-800 break-all whitespace-pre-line"
                                                            )
                                                with ui.row().classes(
                                                    "w-full px-4 py-3 border-t border-slate-200 justify-end"
                                                ):
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
                                                                f"text-xs font-bold rounded px-2 py-0.5 "
                                                                f"self-center {action_class}"
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
                                            auth = (
                                                BasicAuth(SVN_USERNAME, SVN_PASSWORD)
                                                if SVN_USERNAME and SVN_PASSWORD
                                                else None
                                            )
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
                                            is_pdf = normalized_type == "application/pdf" or file_name.lower().endswith(
                                                ".pdf"
                                            )
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
                                            processing_type = str(
                                                file_data.get("type") or item.get("config_processing_type") or ""
                                            )
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
                                                or (
                                                    "image/*"
                                                    if processing_type == "image"
                                                    else "application/octet-stream"
                                                )
                                            )
                                            is_remote_svn = processing_type == "svn" and file_url.startswith(
                                                ("http://", "https://")
                                            )

                                            def file_tooltip(*parts):
                                                return "\n".join(str(part) for part in parts if str(part or "").strip())

                                            file_text_color = (
                                                "text-slate-700" if display_label == "旧" else "text-slate-900"
                                            )
                                            is_uploaded_image = processing_type in {"file", "image"} and (
                                                processing_type == "image" or file_type.startswith("image/")
                                            )
                                            if not is_uploaded_image:
                                                can_view_file = can_view_ecn_scheme_non_image_file(
                                                    item,
                                                    current_role,
                                                    app.storage.general.get("over_config_data_flat", {}),
                                                    ECN_ORDINARY_DOCUMENT_FILE_VIEW_ROLES_BY_TYPE,
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
                                                        ui.label(file_name).classes(
                                                            "text-sm font-semibold break-all min-w-0"
                                                        )
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
                                                    from ..utils import validate_search_path

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
                                                                ui.label(f"{file_name}（文件不存在）").classes(
                                                                    "text-xs min-w-0"
                                                                )

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
                                                        "image_not_supported"
                                                        if processing_type == "image"
                                                        else "link_off",
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
                                                        file_lab=(
                                                            f"ecn-{item.get('item_id', '')}-{display_label}-{file_name}"
                                                        ),
                                                        display_lab=display_label,
                                                        parents_h=8,
                                                        delet_lab=False,
                                                        local_file_path=local_file_path,
                                                    )
                                                    ui.label(file_name).classes(
                                                        f"text-sm font-semibold {file_text_color} "
                                                        "break-all min-w-0 flex-1"
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
                                                    "text-sm font-semibold break-all underline-offset-2 "
                                                    "hover:underline min-w-0"
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
                                                ui.label(old_text).classes(
                                                    "w-full text-sm font-semibold text-slate-700 "
                                                ).tooltip(old_text)
                                                return

                                            entries = [
                                                entry
                                                for entry in project_state.get("existing_contents", [])
                                                if isinstance(entry, dict)
                                            ]
                                            current_entries = [
                                                entry for entry in entries if entry.get("source") == "当前已有"
                                            ]
                                            pending_entries = [
                                                entry for entry in entries if entry.get("source") != "当前已有"
                                            ]
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
                                                ).props("flat dense no-caps color=primary").classes(
                                                    "text-[11px] self-start -ml-2"
                                                )
                                                return
                                            current_contents = [
                                                str(entry.get("content") or "（空内容）") for entry in current_entries
                                            ]
                                            tooltip_text = "\n".join(
                                                f"{index}. {content}"
                                                for index, content in enumerate(current_contents, start=1)
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
                                                                            for entry in project_state.get(
                                                                                "existing_contents", []
                                                                            )
                                                                        )
                                                                        else "当前无内容，将生成新内容"
                                                                    )
                                                                    if action == ECN_OVERVIEW_ACTION_ADD
                                                                    else "原内容失效"
                                                                )
                                                                is_file_content = str(
                                                                    new_data.get("type")
                                                                    or item.get("config_processing_type")
                                                                    or ""
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
                                                                        "w-full text-sm font-semibold text-slate-900 "
                                                                        "cursor-help"
                                                                    ).tooltip(result_note)
                                                return
                                            ui.label(new_content).classes(
                                                "text-sm font-semibold text-slate-900 break-all"
                                            )

                                        rejection_history_dialog = ui.dialog()

                                        def get_rejection_history(item):
                                            history = item.get("rejection_history", [])
                                            return (
                                                [
                                                    copy.deepcopy(record)
                                                    for record in history
                                                    if isinstance(record, dict)
                                                ]
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
                                                            f"当前已有 {current_count} 条"
                                                            if current_count
                                                            else "当前无内容"
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
                                                    new_content = str(
                                                        snapshot.get("new_data", {}).get("content") or "（未填写）"
                                                    )
                                                    results = []
                                                    for project, state in project_states.items():
                                                        if not isinstance(state, dict):
                                                            continue
                                                        action = state.get("action")
                                                        if action == ECN_OVERVIEW_ACTION_ADD:
                                                            has_current_content = any(
                                                                isinstance(entry, dict)
                                                                and entry.get("source") == "当前已有"
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
                                                            results.append(
                                                                f"{project}：更换为 {new_content}；原内容失效"
                                                            )
                                                    if results:
                                                        return "\n".join(results)
                                                return "未记录执行结果"
                                            return str(snapshot.get("new_content") or "无")

                                        def snapshot_requirements(snapshot):
                                            requirement_indexes = snapshot.get("req_idxs", [])
                                            return (
                                                ", ".join(
                                                    f"要求 {requirement_idx}" for requirement_idx in requirement_indexes
                                                )
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
                                            with ui.card().classes(
                                                f"w-full p-3 gap-2 shadow-none border {accent_classes}"
                                            ):
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
                                                    snapshot_traceability_levels = snapshot.get(
                                                        "traceability_levels", []
                                                    )
                                                    snapshot_disposition_measure = snapshot.get("disposition_measure")
                                                    snapshot_fields.append(
                                                        (
                                                            "追溯处置范围（多选）",
                                                            ", ".join(map(str, snapshot_traceability_levels))
                                                            or "未配置",
                                                        )
                                                    )
                                                    if snapshot_is_material and is_ecn_material_disposition_required(
                                                        snapshot.get("change_type")
                                                    ):
                                                        snapshot_disposition_text = (
                                                            snapshot_disposition_measure or "未配置"
                                                        )
                                                        snapshot_condition = str(
                                                            snapshot.get("disposition_condition") or ""
                                                        ).strip()
                                                        if snapshot_condition:
                                                            snapshot_disposition_text += f"\n条件：{snapshot_condition}"
                                                        elif is_ecn_disposition_condition_required(
                                                            snapshot_disposition_measure
                                                        ):
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
                                                            ui.label(field_label).classes(
                                                                "text-[10px] font-bold text-slate-400"
                                                            )
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
                                                ui.card().classes(
                                                    "w-[1100px] max-w-full max-h-[90vh] p-0 gap-0 overflow-hidden"
                                                ),
                                            ):
                                                with ui.row().classes(
                                                    "w-full px-5 py-3 bg-slate-100 border-b border-slate-200 "
                                                    "items-center justify-between shrink-0"
                                                ):
                                                    with ui.column().classes("gap-0 min-w-0"):
                                                        ui.label(f"方案 #{global_idx + 1:02d} · 驳回记录").classes(
                                                            "text-lg font-bold text-blue-950"
                                                        )
                                                        ui.label(table_item_title(item)).classes(
                                                            "text-xs text-slate-500 break-all"
                                                        )
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
                                                            with ui.row().classes(
                                                                "w-full items-center justify-between gap-2"
                                                            ):
                                                                ui.label(
                                                                    "最近一次驳回"
                                                                    if is_latest
                                                                    else f"历史驳回 {len(records) - reverse_idx + 1}"
                                                                ).classes(
                                                                    "text-xs font-bold "
                                                                    + (
                                                                        "text-red-700"
                                                                        if is_latest
                                                                        else "text-slate-600"
                                                                    )
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
                                                            with ui.grid(columns=2).classes(
                                                                "w-full gap-3 mt-1 items-stretch"
                                                            ):
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
                                            row_background = (
                                                "bg-amber-50/50" if display_row_idx % 2 == 0 else "bg-blue-50/50"
                                            )
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
                                                        "px-2 py-1 border-l border-slate-200 flex "
                                                        "items-center justify-center"
                                                    ):
                                                        author = str(item.get("author") or "未知")
                                                        ui.label(author).classes(
                                                            "text-sm text-slate-700 text-center break-all"
                                                        )
                                                    with ui.element("div").classes(
                                                        "px-2 py-1 border-l border-slate-200 flex items-center justify-center"
                                                    ):
                                                        with ui.row().classes(
                                                            f"items-center justify-center gap-1 {status_class}"
                                                        ):
                                                            ui.icon(status_icon).classes("text-lg")
                                                            ui.label(status_label).classes(
                                                                "text-xs font-bold text-center"
                                                            )
                                                    with ui.element("div").classes(
                                                        "px-2 py-1 border-l border-r border-slate-200 flex items-center justify-center"
                                                    ):
                                                        can_edit_item = (
                                                            is_scheming_phase
                                                            and item.get("author") == current_user
                                                            and participants.get(current_user)
                                                            != ECN_PARTICIPANT_STATUS_CONFIRMED
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
                                                                    ).props(
                                                                        "flat round dense text-color=red-5 size=sm"
                                                                    ).tooltip("删除方案")
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
                                            completed = sum(
                                                1 for _, item in items_in_group if table_status_view(item)[3]
                                            )
                                            pending = len(items_in_group) - completed
                                            group_expansion = (
                                                ui.expansion(
                                                    f"{group_title}  {len(items_in_group)} 项",
                                                    caption=f"{completed} 已完成 · {pending} 待处理",
                                                    icon=group_icon,
                                                    value=scheme_group_expansion_state[group_type],
                                                )
                                                .classes(
                                                    "w-full bg-white border border-slate-200 "
                                                    "rounded-lg mb-2 overflow-hidden"
                                                )
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
                                                                    ui.label(header).classes(
                                                                        "text-sm font-bold text-slate-600"
                                                                    )
                                                        for display_row_idx, (global_idx, item) in enumerate(
                                                            items_in_group
                                                        ):
                                                            render_table_item(
                                                                global_idx,
                                                                item,
                                                                display_row_idx,
                                                            )

                                async def remove_item(item_to_remove):
                                    """删除方案 (原子化重构)"""
                                    target_item_id = item_to_remove["item_id"]

                                    def delete_ecn_scheme(current_ecn, item_id):
                                        if not current_ecn:
                                            return current_ecn

                                        items = current_ecn.setdefault("change_items", [])
                                        # 获取目标删除条目的作者信息
                                        target_author = None

                                        for item in items:
                                            if item["item_id"] == item_id:
                                                target_author = item.get("author")
                                                break
                                        # 删除目标条目并更新 change_items 列表
                                        current_ecn["change_items"] = [
                                            item for item in items if item["item_id"] != item_id
                                        ]

                                        if target_author:
                                            has_other = any(
                                                item.get("author") == target_author
                                                for item in current_ecn["change_items"]
                                            )
                                            # 如果没有其他条目是同一作者，则从 scheme_participants 中移除该作者的参与状态
                                            if not has_other:
                                                current_ecn.setdefault("workflow", {}).setdefault(
                                                    "scheme_participants", {}
                                                ).pop(target_author, None)

                                        return current_ecn

                                    success = await atomic_ecn_deep_update(
                                        ["ecn_management_data", local_data["ecn_id"]], delete_ecn_scheme, target_item_id
                                    )

                                    if success:
                                        # 同步本地数据以更新 UI
                                        local_data["change_items"].remove(item_to_remove)
                                        author = item_to_remove.get("author")
                                        # 删除方案后检查该作者是否还有其他方案条目，如果没有则从参与者列表中移除该作者的状态
                                        if author and not any(
                                            existing_item.get("author") == author
                                            for existing_item in local_data["change_items"]
                                        ):
                                            participants.pop(author, None)

                                        render_parts()  # 更新参与者状态显示标签
                                        render_my_actions()  # 更新状态对应的可行动按钮
                                        render_items()  # 更新方案列表显示
                                        render_coverage_dashboard()  # 更新覆盖率看板
                                    else:
                                        ui.notify("删除方案失败，请重试。", type="negative")

                                render_items()

                # --- [TAB 4] ECN 执行与试产 ---
                with ui.tab_panel(tab_exec).classes(
                    "gap-4 p-0 max-w-[1500px] mx-auto  overflow-y-scroll overflow-x-hidden"
                ):
                    if wf["current_state"] in [ECNState.DRAFT, ECNState.ECR_REVIEWING, ECNState.REJECTED]:
                        ui.label("当前尚未进入执行环节。").classes(
                            "text-gray-500 m-8 text-center bg-white p-2 border rounded"
                        )
                    else:
                        is_exec_phase = wf["current_state"] in [ECNState.ECN_EXECUTING, ECNState.PENDING_FINAL_EXECUTE]
                        exec_info = local_data.setdefault("execution_info", get_ecn_template()["execution_info"])
                        with ui.card().classes("w-full p-0 pdf-border bg-white shadow-sm"):
                            ui.label("ECN-执行 & 试产").classes(
                                "text-lg font-bold bg-green-100 text-green-900 w-full p-1 pdf-border-b text-center tracking-wider"
                            )
                            with ui.row().classes("w-full p-3 pdf-border-b items-start gap-6 hover:bg-gray-50"):
                                ui.label("追溯处置范围:").classes("font-bold text-gray-700 w-20 pt-1")
                                with ui.row().classes("gap-x-6 gap-y-2 flex-1 flex-wrap"):
                                    ui.radio(ECN_TRACEABILITY_LEVELS).bind_value(exec_info, "traceability_level").props(
                                        f"{'disable' if not is_exec_phase else ''} inline"
                                    )
                            with ui.row().classes("w-full p-3 pdf-border-b items-center gap-6 bg-gray-50"):
                                ui.label("处理措施:").classes("font-bold text-gray-700 w-20")
                                with ui.row().classes("gap-6 flex-1"):
                                    for measure in ECN_SCHEMA_CONFIG["execution_handling_measures"]:
                                        exec_info["handling_measures"].setdefault(measure, False)
                                        ui.checkbox(measure).bind_value(exec_info["handling_measures"], measure).props(
                                            f"{'disable' if not is_exec_phase else ''}"
                                        )
                            with ui.row().classes("w-full p-3 items-center gap-6 hover:bg-gray-50"):
                                ui.label("试产结论:").classes("font-bold text-gray-700 w-20")
                                with ui.row().classes("gap-6 flex-1"):
                                    ui.radio(ECN_SCHEMA_CONFIG["trial_conclusions"]).bind_value(
                                        exec_info, "trial_conclusion"
                                    ).props(f"{'disable' if not is_exec_phase else ''} inline")

                # --- [TAB 4] 审批流转记录 ---
                with ui.tab_panel(tab_workflow).classes("gap-4 p-4 max-w-[1000px] mx-auto bg-white rounded border"):
                    workflow_container = ui.column().classes("w-full")

                    def render_workflow_tab():
                        workflow_container.clear()
                        with workflow_container:
                            if is_new:
                                ui.label("暂无审批记录，请先发起申请。").classes(
                                    "text-gray-500 mt-4 text-center w-full"
                                )
                            else:
                                if wf["pending_roles"]:
                                    pending_list = [r for r in wf["pending_roles"] if not wf["step_approvals"].get(r)]
                                    approved_list = [r for r in wf["pending_roles"] if wf["step_approvals"].get(r)]
                                    with ui.card().classes(
                                        "w-full bg-blue-50/50 shadow-sm mb-4 border border-blue-100 p-3"
                                    ):
                                        if pending_list:
                                            ui.label(f"▶ 当前节点等待审批: {', '.join(pending_list)}").classes(
                                                "text-orange-600 font-bold text-sm"
                                            )
                                        if approved_list:
                                            ui.label(f"▶ 当前节点已同意: {', '.join(approved_list)}").classes(
                                                "text-green-600 text-sm mt-1"
                                            )

                                # ui.timeline: NiceGUI框架用于展示时间线或流转步骤的类
                                with ui.timeline(color="secondary"):
                                    for log in local_data.get("approval_log", []):
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
                    ).bind_value(selected_state, "item_ids").props("outlined use-chips").classes("w-full")
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
                if is_draft_or_reject:
                    if basic["applicant"] == current_user or is_new:
                        ui.button("保存为草稿", on_click=lambda: execute_db_action("save_draft")).props("color=grey-7")
                        ui.button("发起/重新发起 ECR", on_click=lambda: execute_db_action("submit_ecr")).props(
                            "color=primary"
                        )
                else:
                    is_pending_user = current_role in wf["pending_roles"]
                    if wf["current_state"] == ECNState.ECR_REVIEWING and basic["applicant"] == current_user:
                        ui.button("撤回修改", icon="undo", on_click=lambda: execute_db_action("withdraw")).props(
                            "color=orange"
                        )
                        ui.button("作废", icon="delete_forever", on_click=lambda: execute_db_action("cancel")).props(
                            "color=red"
                        )
                    if wf["current_state"] == ECNState.PENDING_FINAL_EXECUTE and "研发经理" in current_role:
                        ui.button("驳回至影响/方案阶段", color="red", on_click=lambda: open_scheme_reject_dialog())
                        ui.button(
                            "确认各部已就绪，立刻执行数据变更并归档",
                            icon="warning",
                            on_click=lambda: execute_db_action("final_execute"),
                        ).props("color=red")
                    elif is_scheming_phase and any(r in current_role for r in ECN_SCHEME_INITIATOR_ROLES):
                        all_confirmed = len(participants) > 0 and all(
                            status == ECN_PARTICIPANT_STATUS_CONFIRMED for status in participants.values()
                        )
                        btn = ui.button(
                            "发起 ECN 方案评审", on_click=lambda: execute_db_action("initiate_scheme_review")
                        ).props(f"color=purple {'disabled' if not all_confirmed else ''}")
                        if not all_confirmed:
                            btn.tooltip("需要所有提供方案的人员点击'确认完成'后方可发起")
                    elif is_pending_user and wf["current_state"] not in [
                        ECNState.CLOSED,
                        ECNState.CANCEL,
                        ECNState.REJECTED,
                        ECNState.ECN_SCHEMING,
                    ]:
                        note_input = ui.input("审批意见 (选填)").props("dense outlined").classes("w-64")
                        if wf.get("current_phase") == "ECR_PHASE":
                            ui.button(
                                "驳回",
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
                            "同意", color="green", on_click=lambda: execute_db_action("approve", note=note_input.value)
                        )

            # ------------------------------------------
            # 提取的数据库与流转控制逻辑中心
            # ------------------------------------------
            async def execute_db_action(action_type, note="", rejected_item_ids=None):
                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rejected_item_ids = list(rejected_item_ids or [])

                if (
                    action_type == "reject"
                    and wf.get("current_phase") != "ECR_PHASE"
                    and ECN_REQUIRE_REJECTED_ITEM_SELECTION
                    and not rejected_item_ids
                ):
                    return ui.notify("请至少选择一个需要改进的方案。", type="warning")

                if action_type == "submit_ecr":
                    if not basic.get("nature"):
                        return ui.notify("请选择变更性质", type="warning")
                    if not any(basic.get("reasons", {}).values()):
                        return ui.notify("请至少勾选一项变更原因", type="warning")
                    if basic.get("reasons", {}).get("其他") and not basic.get("other_reason_desc", "").strip():
                        return ui.notify("请填写其他说明", type="warning")
                    if not local_data.get("target_projects"):
                        return ui.notify("请至少添加一个变更对象", type="warning")
                    if not basic.get("requirements"):
                        return ui.notify("请至少填写一条变更要求", type="warning")
                    if not basic.get("reason_desc", "").strip():
                        return ui.notify("请填写原因说明", type="warning")

                    basic["title"] = (
                        f"{','.join(local_data['target_projects'][:2])}等 - {'/'.join([k for k, v in basic['reasons'].items() if v])}变更"
                    )
                    wf["current_state"] = ECNState.ECR_REVIEWING
                    wf["current_phase"] = "ECR_PHASE"
                    wf["route_type"] = "SALES_INITIATED" if "销售" in current_role else "RD_INITIATED"
                    wf["current_step_index"] = 0
                    wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECR_PHASE"][wf["route_type"]][0]
                    wf["step_approvals"] = {}
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "发起申请", "time": now_str}
                    )

                elif action_type == "withdraw":
                    wf["current_state"], wf["pending_roles"], wf["step_approvals"] = ECNState.DRAFT, [], {}
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "撤回修改", "time": now_str}
                    )

                elif action_type == "cancel":
                    wf["current_state"], wf["pending_roles"], wf["step_approvals"] = ECNState.CANCEL, [], {}
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "作废变更", "time": now_str}
                    )

                elif action_type == "initiate_scheme_review":
                    coverage = get_ecn_scheme_coverage(local_data)
                    missing_requirements = coverage["missing_requirements"]
                    missing_docs = coverage["missing_docs"]
                    missing_mats = coverage["missing_materials"]
                    incomplete_material_schemes = coverage["incomplete_material_schemes"]
                    if missing_requirements or missing_docs or missing_mats or incomplete_material_schemes:
                        msg = ["【系统拦截】以下项目缺少方案关联："]
                        if missing_requirements:
                            msg.append(
                                "▶ 遗漏变更要求: " + ", ".join(f"要求 {idx}" for idx in sorted(missing_requirements))
                            )
                        if missing_docs:
                            msg.append(f"▶ 遗漏文档: {', '.join(sorted(missing_docs))}")
                        if missing_mats:
                            msg.append(f"▶ 遗漏物料: {', '.join(sorted(missing_mats))}")
                        if incomplete_material_schemes:
                            msg.append(
                                "▶ 物料方案未配置追溯处置范围或适用的旧料处置措施: "
                                + ", ".join(sorted(incomplete_material_schemes))
                            )
                        return ui.notify("\n".join(msg), type="negative", multi_line=True)

                    wf["current_state"], wf["current_phase"], wf["current_step_index"] = (
                        ECNState.ECN_REVIEWING,
                        "ECN_SCHEME_REVIEW_PHASE",
                        0,
                    )
                    wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_SCHEME_REVIEW_PHASE"][0]
                    local_data["approval_log"].append(
                        {"user": current_user, "role": current_role, "action": "发起方案评审", "time": now_str}
                    )

                elif action_type in ["approve", "reject"]:
                    act_name = "同意" if action_type == "approve" else "驳回"
                    local_log_entry: dict[str, object] = {
                        "user": current_user,
                        "role": current_role,
                        "action": act_name,
                        "note": note,
                        "time": now_str,
                    }
                    if action_type == "reject" and rejected_item_ids:
                        local_log_entry["rejected_item_ids"] = rejected_item_ids
                    local_data["approval_log"].append(local_log_entry)
                    if action_type == "reject":
                        if wf["current_phase"] == "ECR_PHASE":
                            wf["current_state"], wf["pending_roles"] = ECNState.REJECTED, []
                        else:
                            wf["current_phase"], wf["current_state"], wf["pending_roles"] = (
                                "ECN_SCHEME_PHASE",
                                ECNState.ECN_SCHEMING,
                                [],
                            )
                            wf["step_approvals"] = {}
                            reject_ecn_scheme_items(
                                local_data,
                                rejected_item_ids,
                                current_user,
                                current_role,
                                note,
                                now_str,
                            )
                    else:
                        wf["step_approvals"][current_role] = True
                        if all(wf["step_approvals"].get(r, False) for r in wf["pending_roles"]):
                            wf["current_step_index"] += 1
                            wf["step_approvals"] = {}
                            route = (
                                ECN_WORKFLOW_ROUTES[wf["current_phase"]][wf["route_type"]]
                                if wf["current_phase"] == "ECR_PHASE"
                                else ECN_WORKFLOW_ROUTES[wf["current_phase"]]
                            )
                            if wf["current_step_index"] >= len(route):
                                if wf["current_phase"] == "ECR_PHASE":
                                    wf["current_phase"], wf["current_state"], wf["pending_roles"] = (
                                        "ECN_SCHEME_PHASE",
                                        ECNState.ECN_SCHEMING,
                                        [],
                                    )
                                else:
                                    wf["current_phase"], wf["current_state"], wf["current_step_index"] = (
                                        "ECN_EXECUTION_PHASE",
                                        ECNState.ECN_EXECUTING,
                                        0,
                                    )
                                    wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_EXECUTION_PHASE"][0]
                            else:
                                wf["pending_roles"] = route[wf["current_step_index"]]
                                if "研发经理_EXECUTE" in wf["pending_roles"]:
                                    wf["current_state"] = ECNState.PENDING_FINAL_EXECUTE

                elif action_type == "final_execute":
                    try:
                        # --- 优化点 2 收尾：对执行列表进行拓扑排序，保证基准列先执行 ---
                        def execution_sort_key(item):
                            if item.get("type") == "overview_update":
                                # 强制让 first_col_label 相同的条目排在最前面执行 (权值为 0)
                                return 0 if item.get("label") == item.get("first_col_label") else 1
                            return 2

                        sorted_items = sorted(local_data["change_items"], key=execution_sort_key)
                        pending_id_to_row_id = {}  # 虚拟暂存 ID 到真实 row_id 的映射表

                        # 遍历经过排序的 sorted_items 而不是 local_data["change_items"]
                        for item in sorted_items:
                            if item["type"] == "overview_update":
                                processing_type = item.get("config_processing_type", "text")
                                icon_map = {
                                    "file": "attachment",
                                    "search": "saved_search",
                                    "svn": "saved_search",
                                    "image": "image",
                                    "video": "play_circle",
                                }
                                new_icon = icon_map.get(processing_type, None)

                                project_states = item.get("project_states", {})

                                updated = False

                                # 数据工厂函数：统一生成新的 Chip 模板
                                def create_new_chip_template(proj, author, processing_type, new_icon, new_data):
                                    req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(proj, "1.0")
                                    new_activ_dic = {
                                        f"{i}.0": (f"{i}.0" == req_max_ver)
                                        for i in range(1, int(float(req_max_ver)) + 1)
                                    }

                                    new_chip = {
                                        "id": str(uuid.uuid4()),
                                        "role": item["role"],
                                        "type": processing_type,
                                        "icon": new_icon,
                                        "enabled": True,
                                        "bg_color": "bg-light-blue-1",
                                        "content": new_data.get("content", ""),
                                        "notes": f"依据: {local_data['ecn_id']} 执行",
                                        "creator": author,
                                        "req_ver": req_max_ver,
                                        "select_activ_dic": new_activ_dic,
                                        "timestamp": {
                                            now_str: {
                                                "creator": author,
                                                "select_activ_dic": copy.deepcopy(new_activ_dic),
                                            }
                                        },
                                    }
                                    # 注入特有属性
                                    if "test_select_data" in new_data:
                                        new_chip["test_select_data"] = copy.deepcopy(new_data["test_select_data"])
                                    if "file_type" in new_data:
                                        new_chip["file_type"] = new_data["file_type"]
                                    if "url_path" in new_data:
                                        new_chip["url_path"] = new_data["url_path"]
                                    if "local_file_path" in new_data:
                                        new_chip["local_file_path"] = new_data["local_file_path"]
                                    if "warehouse" in new_data:
                                        new_chip["warehouse"] = new_data["warehouse"]
                                    return new_chip, req_max_ver

                                for project, p_state in project_states.items():
                                    action = p_state.get("action")
                                    chip_id = p_state.get("chip_id")
                                    anchor_row_id = p_state.get("anchor_row_id")
                                    project_new_data = get_ecn_overview_project_new_data(
                                        item.get("new_data", {}),
                                        p_state,
                                    )

                                    if action == ECN_OVERVIEW_ACTION_DEACTIVATE and chip_id:
                                        path = [f"{project}_over_data", item["label"], chip_id]
                                        old_chip = db_storage.get_deep_item(path)
                                        if old_chip:
                                            req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(
                                                project,
                                                "1.0",
                                            )
                                            deactivated_chip = deactivate_overview_chip_for_ecn(
                                                old_chip,
                                                req_max_ver,
                                                local_data["ecn_id"],
                                                now_str,
                                            )
                                            await save_ecn_deep_item(path, deactivated_chip)
                                            updated = True

                                    elif action == ECN_OVERVIEW_ACTION_UPDATE and chip_id:
                                        path = [f"{project}_over_data", item["label"], chip_id]
                                        old_chip = db_storage.get_deep_item(path)

                                        if old_chip:
                                            # 生成新节点（借用工厂）
                                            new_chip, req_max_ver = create_new_chip_template(
                                                project,
                                                item.get("author", current_user),
                                                processing_type,
                                                new_icon,
                                                project_new_data,
                                            )
                                            new_chip["row_id"] = old_chip.get("row_id")  # 严格继承旧 row_id

                                            # 继承原节点的版本激活矩阵，并将原节点失活
                                            new_chip["select_activ_dic"] = copy.deepcopy(
                                                old_chip.get("select_activ_dic", {})
                                            )
                                            new_chip["select_activ_dic"][req_max_ver] = True
                                            new_chip["timestamp"][now_str]["select_activ_dic"] = copy.deepcopy(
                                                new_chip["select_activ_dic"]
                                            )

                                            deactivated_chip = deactivate_overview_chip_for_ecn(
                                                old_chip,
                                                req_max_ver,
                                                local_data["ecn_id"],
                                                now_str,
                                            )

                                            # 写入失活旧节点与新生节点
                                            await save_ecn_deep_item(path, deactivated_chip)
                                            await save_ecn_deep_item(
                                                [f"{project}_over_data", item["label"], new_chip["id"]], new_chip
                                            )
                                            updated = True

                                    elif action == ECN_OVERVIEW_ACTION_ADD:
                                        new_chip, _ = create_new_chip_template(
                                            project,
                                            item.get("author", current_user),
                                            processing_type,
                                            new_icon,
                                            project_new_data,
                                        )

                                        is_first_col = item["label"] == item.get("first_col_label", "")
                                        if is_first_col:
                                            # 如果是基准列，生成真实 UUID，并存入映射表供后续列使用
                                            new_row_id = str(uuid.uuid4())
                                            new_chip["row_id"] = new_row_id
                                            pending_id_to_row_id[item["item_id"]] = new_row_id
                                        else:
                                            # 如果是后续列，解析虚拟锚点
                                            if anchor_row_id and str(anchor_row_id).startswith("PENDING_NEW_"):
                                                temp_item_id = anchor_row_id.replace("PENDING_NEW_", "")
                                                # 从映射表中获取刚刚生成的真实 row_id
                                                new_chip["row_id"] = pending_id_to_row_id.get(
                                                    temp_item_id, str(uuid.uuid4())
                                                )
                                            else:
                                                new_chip["row_id"] = anchor_row_id

                                        await save_ecn_deep_item(
                                            [f"{project}_over_data", item["label"], new_chip["id"]], new_chip
                                        )
                                        updated = True

                                if updated:
                                    item["execute_status"] = "success"

                        wf["current_state"], wf["pending_roles"] = ECNState.CLOSED, []
                        local_data["approval_log"].append(
                            {"user": current_user, "role": current_role, "action": "执行变更", "time": now_str}
                        )
                    except Exception as e:
                        logger.error(f"执行ECN分裂变更失败: {e}", exc_info=True)
                        return ui.notify(f"执行失败: {e}", type="negative")

                # ==========================================
                # 状态机原子化落盘核心
                # ==========================================
                # ==========================================
                # 状态机原子化落盘核心
                # ==========================================
                def state_machine_transition(
                    current_ecn,
                    act_type,
                    user,
                    role,
                    comment,
                    time_str,
                    rejected_ids,
                    local_full_data,
                ):
                    # 【核心修复】：如果是新建的 ECN，数据库里还没有数据（current_ecn 为 None），
                    # 则直接使用前端传入的完整本地数据作为基底。
                    if not current_ecn:
                        current_ecn = local_full_data
                    else:
                        # 如果是已存在的 ECN，将前端 UI 可能修改的申请表单内容同步进去
                        current_ecn.setdefault("basic_info", {}).update(local_full_data.get("basic_info", {}))
                        current_ecn["target_projects"] = local_full_data.get("target_projects", [])
                        if "execution_info" in local_full_data:
                            current_ecn.setdefault("execution_info", {}).update(local_full_data["execution_info"])

                    c_wf = current_ecn.setdefault("workflow", {})
                    c_log = current_ecn.setdefault("approval_log", [])

                    if act_type == "submit_ecr":
                        c_wf["current_state"] = ECNState.ECR_REVIEWING
                        c_wf["current_phase"] = "ECR_PHASE"
                        c_wf["route_type"] = "SALES_INITIATED" if "销售" in role else "RD_INITIATED"
                        c_wf["current_step_index"] = 0
                        c_wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECR_PHASE"][c_wf["route_type"]][0]
                        c_wf["step_approvals"] = {}
                        append_ecn_approval_log_once(
                            c_log,
                            {"user": user, "role": role, "action": "发起申请", "time": time_str},
                        )

                    elif act_type == "withdraw":
                        c_wf["current_state"], c_wf["pending_roles"], c_wf["step_approvals"] = ECNState.DRAFT, [], {}
                        append_ecn_approval_log_once(
                            c_log,
                            {"user": user, "role": role, "action": "撤回修改", "time": time_str},
                        )

                    elif act_type == "cancel":
                        c_wf["current_state"], c_wf["pending_roles"], c_wf["step_approvals"] = ECNState.CANCEL, [], {}
                        append_ecn_approval_log_once(
                            c_log,
                            {"user": user, "role": role, "action": "作废变更", "time": time_str},
                        )

                    elif act_type == "initiate_scheme_review":
                        c_wf["current_state"] = ECNState.ECN_REVIEWING
                        c_wf["current_phase"] = "ECN_SCHEME_REVIEW_PHASE"
                        c_wf["current_step_index"] = 0
                        c_wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_SCHEME_REVIEW_PHASE"][0]
                        append_ecn_approval_log_once(
                            c_log,
                            {"user": user, "role": role, "action": "发起方案评审", "time": time_str},
                        )

                    elif act_type in ["approve", "reject"]:
                        act_name = "同意" if act_type == "approve" else "驳回"
                        log_entry: dict[str, object] = {
                            "user": user,
                            "role": role,
                            "action": act_name,
                            "note": comment,
                            "time": time_str,
                        }
                        if act_type == "reject" and rejected_ids:
                            log_entry["rejected_item_ids"] = list(rejected_ids)
                        append_ecn_approval_log_once(c_log, log_entry)

                        if act_type == "reject":
                            if c_wf.get("current_phase") == "ECR_PHASE":
                                c_wf["current_state"], c_wf["pending_roles"] = ECNState.REJECTED, []
                            else:
                                if ECN_REQUIRE_REJECTED_ITEM_SELECTION and not rejected_ids:
                                    transition_blocked["value"] = True
                                    return db_storage.ATOMIC_NO_UPDATE
                                c_wf["current_phase"] = "ECN_SCHEME_PHASE"
                                c_wf["current_state"] = ECNState.ECN_SCHEMING
                                c_wf["pending_roles"] = []
                                c_wf["step_approvals"] = {}
                                rejected_authors = reject_ecn_scheme_items(
                                    current_ecn,
                                    rejected_ids,
                                    user,
                                    role,
                                    comment,
                                    time_str,
                                )
                                if ECN_REQUIRE_REJECTED_ITEM_SELECTION and not rejected_authors:
                                    transition_blocked["value"] = True
                                    return db_storage.ATOMIC_NO_UPDATE
                        else:
                            c_wf.setdefault("step_approvals", {})[role] = True
                            if all(c_wf["step_approvals"].get(r, False) for r in c_wf["pending_roles"]):
                                c_wf["current_step_index"] += 1
                                c_wf["step_approvals"] = {}

                                route = (
                                    ECN_WORKFLOW_ROUTES[c_wf["current_phase"]][c_wf["route_type"]]
                                    if c_wf["current_phase"] == "ECR_PHASE"
                                    else ECN_WORKFLOW_ROUTES[c_wf["current_phase"]]
                                )

                                if c_wf["current_step_index"] >= len(route):
                                    if c_wf["current_phase"] == "ECR_PHASE":
                                        c_wf["current_phase"] = "ECN_SCHEME_PHASE"
                                        c_wf["current_state"] = ECNState.ECN_SCHEMING
                                        c_wf["pending_roles"] = []
                                    else:
                                        c_wf["current_phase"] = "ECN_EXECUTION_PHASE"
                                        c_wf["current_state"] = ECNState.ECN_EXECUTING
                                        c_wf["current_step_index"] = 0
                                        c_wf["pending_roles"] = ECN_WORKFLOW_ROUTES["ECN_EXECUTION_PHASE"][0]
                                else:
                                    c_wf["pending_roles"] = route[c_wf["current_step_index"]]
                                    if "研发经理_EXECUTE" in c_wf["pending_roles"]:
                                        c_wf["current_state"] = ECNState.PENDING_FINAL_EXECUTE

                    elif act_type == "final_execute":
                        c_wf["current_state"], c_wf["pending_roles"] = ECNState.CLOSED, []
                        append_ecn_approval_log_once(
                            c_log,
                            {"user": user, "role": role, "action": "执行变更", "time": time_str},
                        )

                    return current_ecn

                # 执行代理包裹了时间戳的原子更新
                transition_blocked = {"value": False}
                success = await atomic_ecn_deep_update(
                    ["ecn_management_data", local_data["ecn_id"]],
                    state_machine_transition,
                    action_type,
                    current_user,
                    current_role,
                    note,
                    now_str,
                    rejected_item_ids,
                    copy.deepcopy(local_data),  # 【核心修复】：传入完整的本地数据副本供初始化兜底
                )

                if success and not transition_blocked["value"]:
                    ui.notify("操作成功！", type="positive")
                    root_dialog.close()
                    refresh_list()
                elif transition_blocked["value"]:
                    ui.notify("所选方案已发生变化，请刷新后重新选择。", type="warning")
                    root_dialog.close()
                    refresh_list()
                else:
                    ui.notify("状态流转异常，请刷新重试。", type="negative")

            # --- 协同同步定时器 ---
            async def sync_schemes():
                """
                协同同步方案编写阶段的核心函数，定期从数据库拉取最新数据并对比当前本地数据，智能更新界面以反映其他用户的修改
                """
                if ecn_id:
                    # copy.deepcopy: Python标准库函数，用于递归复制对象，防止内存引用导致的数据污染
                    fresh = db_storage.get_deep_item(["ecn_management_data", ecn_id])
                    if not fresh:
                        return

                    # 1. 同步工作流状态
                    fresh_wf = fresh.get("workflow", {})
                    if (
                        fresh_wf.get("current_state") != wf["current_state"]
                        or fresh_wf.get("pending_roles") != wf["pending_roles"]
                    ):
                        wf["current_state"] = fresh_wf.get("current_state")
                        wf["pending_roles"] = fresh_wf.get("pending_roles")
                        wf["step_approvals"] = fresh_wf.get("step_approvals", {})
                        local_data["approval_log"] = copy.deepcopy(fresh.get("approval_log", []))
                        render_workflow_tab()  # 触发刷新流转页面
                        ui.notify("后台流转状态已更新，已为您同步。", type="info")

                    # 2. 同步方案内容 (仅在方案编写阶段需要动态重绘卡片)
                    if wf["current_state"] == ECNState.ECN_SCHEMING:
                        if (
                            str(fresh.get("change_items", [])) != str(local_data["change_items"])
                            or fresh["workflow"].get("scheme_participants", {}) != participants
                        ):
                            local_data["change_items"].clear()
                            local_data["change_items"].extend(copy.deepcopy(fresh.get("change_items", [])))
                            participants.clear()
                            participants.update(copy.deepcopy(fresh["workflow"].get("scheme_participants", {})))
                            render_parts()
                            render_my_actions()
                            render_items()
                            render_coverage_dashboard()  # 同时更新覆盖率看板状态

                        fresh_rev = fresh.get("review_info", {})
                        if fresh_rev:
                            review["impacts"].update(fresh_rev.get("impacts", {}))
                            review["involved_docs"].update(fresh_rev.get("involved_docs", {}))
                            for mat, acts in fresh_rev.get("involved_materials", {}).items():
                                if mat in review["involved_materials"] and isinstance(acts, dict):
                                    review["involved_materials"][mat].update(acts)
                            review["sop_impact"] = fresh_rev.get("sop_impact", "无影响")
                            review["fixture_impact"] = fresh_rev.get("fixture_impact", "无影响")
                            review["tool_impact"] = fresh_rev.get("tool_impact", "无影响")

            if wf["current_state"] == ECNState.ECN_SCHEMING and not is_new:
                sync_timer = ui.timer(3.0, sync_schemes)
                root_dialog.on("close", sync_timer.cancel)

        root_dialog.open()

    # ==========================================
    # 管理员功能：删除确认与执行
    # ==========================================
    async def confirm_delete(ecn_id):
        dialog.clear()
        with dialog, ui.card().classes("p-6"):
            ui.label("删除确认 (仅管理员)").classes("text-xl font-bold text-red-600 border-b pb-2 mb-4 w-full")
            ui.label(f"您确定要永久删除 ECN 单号【{ecn_id}】吗？")
            ui.label("该操作将清除所有的表单与审批流转记录，且不可恢复！").classes("text-sm text-gray-500 mt-2")
            with ui.row().classes("w-full justify-end mt-6 gap-3"):
                ui.button("取消", on_click=dialog.close).props("outline color=grey")

                async def do_delete():
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
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
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
                        ECNState.CANCEL,
                        ECNState.REJECTED,
                    ],
                    label="状态筛选",
                ).props("dense outlined").bind_value(page_state, "filter_state").classes("w-40")
                ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("color=primary outline")
            ui.button("新建 ECR 申请", icon="add_box", on_click=lambda: open_ecn_detail_dialog()).props("color=red-7")
        with ui.element("div").classes("w-full h-full overflow-y-auto overflow-x-hidden p-4 md:p-6"):
            # with ui.column().classes("w-full p-4 h-[calc(100vh-4rem)] bg-gray-100"):
            list_container = ui.column().classes("w-full mt-4 gap-3 flex-grow overflow-y-auto")

            def refresh_list():
                list_container.clear()
                ALL_ECNS = db_storage.get_item("ecn_management_data", {})
                kw = page_state["search_keyword"].lower()
                f_state = page_state["filter_state"]

                # 【增加防御性过滤】：剔除掉值为 None，或者缺失 basic_info 的脏数据
                valid_ecns = [
                    ecn
                    for ecn in ALL_ECNS.values()
                    if ecn and isinstance(ecn, dict) and "basic_info" in ecn and "apply_date" in ecn["basic_info"]
                ]

                # 按照申请时间排序，最新的在前面
                sorted_ecns = sorted(valid_ecns, key=lambda x: x["basic_info"]["apply_date"], reverse=True)

                with list_container:
                    if not sorted_ecns:
                        return ui.label("暂无工程变更记录").classes("text-gray-500 m-auto mt-10")

                    for ecn in sorted_ecns:
                        # 增加单号、项目、申请人搜索过滤
                        if (
                            kw
                            and kw not in ecn["ecn_id"].lower()
                            and kw not in str(ecn.get("target_projects", "")).lower()
                            and kw not in ecn["basic_info"]["applicant"].lower()
                        ):
                            continue

                        # 获取ECN状态
                        current_state = ecn["workflow"]["current_state"]
                        # 增加ECN状态筛选过滤
                        if f_state != "全部" and current_state != f_state:
                            continue

                        with ui.card().classes(
                            "w-full flex flex-row justify-between items-center p-4 bg-blue-50 hover:bg-amber-50 transition-colors cursor-pointer border-l-4 border-blue-500 shadow-sm relative"
                        ) as card:
                            card.on("click", lambda _, e_id=ecn["ecn_id"]: open_ecn_detail_dialog(e_id))

                            if current_user.lower() == "admin":
                                ui.button(icon="delete", color="red").props("flat round dense").classes(
                                    "absolute top-1 right-1 z-10"
                                ).on("click.stop", lambda e, e_id=ecn["ecn_id"]: confirm_delete(e_id)).tooltip(
                                    "永久删除此数据 (管理员专用)"
                                )

                            with ui.column().classes("gap-1"):
                                # ECN流程状态与方案编写状态信息展示行
                                with ui.row().classes("items-center gap-3"):
                                    ui.label(ecn["ecn_id"]).classes("font-mono font-bold text-gray-800 text-lg")
                                    # 状态标签
                                    ui.badge(
                                        current_state,
                                        color="red"
                                        if current_state == ECNState.REJECTED
                                        else "orange"
                                        if "中" in current_state
                                        else "green"
                                        if "完成" in current_state
                                        else "grey",
                                    ).props("outline")

                                    pending_roles = ecn["workflow"].get("pending_roles", [])

                                    # 对于审批状态的ECN，增加显示相应信息标签
                                    if pending_roles and current_state not in [
                                        ECNState.DRAFT,
                                        ECNState.CLOSED,
                                        ECNState.CANCEL,
                                        ECNState.REJECTED,
                                    ]:
                                        ui.label(f"等待审批: {', '.join(pending_roles)}").classes(
                                            "text-xs font-bold text-orange-600 bg-orange-100 px-2 py-0.5 rounded"
                                        )
                                    # 对于方案编制状态的ECN，增加显示相应信息标签
                                    elif current_state == ECNState.ECN_SCHEMING:
                                        # 分开显示常规编写与被驳回后的重新确认，避免列表文案掩盖整改状态。
                                        needs_reconfirmation = [
                                            p
                                            for p, status in ecn["workflow"].get("scheme_participants", {}).items()
                                            if status == ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
                                        ]
                                        editing_participants = [
                                            p
                                            for p, status in ecn["workflow"].get("scheme_participants", {}).items()
                                            if status
                                            not in [
                                                ECN_PARTICIPANT_STATUS_CONFIRMED,
                                                ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
                                            ]
                                        ]

                                        coverage = get_ecn_scheme_coverage(ecn)
                                        missing_requirements = coverage["missing_requirements"]
                                        missing_docs = coverage["missing_docs"]
                                        missing_mats = coverage["missing_materials"]
                                        incomplete_material_schemes = coverage["incomplete_material_schemes"]
                                        has_missing_deliverables = bool(
                                            missing_requirements
                                            or missing_docs
                                            or missing_mats
                                            or incomplete_material_schemes
                                        )

                                        # 3. 综合判断并渲染状态标签
                                        if needs_reconfirmation:
                                            ui.label(f"方案待改进/重新确认: {', '.join(needs_reconfirmation)}").classes(
                                                "text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded"
                                            )
                                        elif editing_participants:
                                            ui.label(f"等待完成方案编写: {', '.join(editing_participants)}").classes(
                                                "text-xs font-bold text-purple-600 bg-purple-100 px-2 py-0.5 rounded"
                                            )
                                        elif has_missing_deliverables:
                                            # 核心防呆：人员都点完了确认，但系统查出仍有漏交的强制项
                                            miss_text = []
                                            if missing_requirements:
                                                miss_text.append("变更要求")
                                            if missing_docs:
                                                miss_text.append("资料")
                                            if missing_mats:
                                                miss_text.append("物料")
                                            if incomplete_material_schemes:
                                                miss_text.append("物料追溯/处置配置")
                                            ui.label(f"尚缺{'、'.join(miss_text)}方案，待补充").classes(
                                                "text-xs font-bold text-red-600 bg-red-100 px-2 py-0.5 rounded"
                                            )
                                        elif ecn["workflow"].get("scheme_participants"):
                                            ui.label("方案已齐，待发起评审").classes(
                                                "text-xs font-bold text-green-600 bg-green-100 px-2 py-0.5 rounded"
                                            )
                                # ECN涉及项目信息
                                ui.label(
                                    ecn["basic_info"].get(
                                        "title", f"涉及项目: {', '.join(ecn.get('target_projects', []))}"
                                    )
                                ).classes("text-sm text-gray-800 font-bold")

                            with ui.column().classes("items-end gap-1"):
                                ui.label(f"申请人: {ecn['basic_info']['applicant']}").classes("text-sm text-gray-600")
                                ui.label(ecn["basic_info"]["apply_date"]).classes("text-xs text-gray-400 font-mono")

                                # 与主页角标使用同一套精准待办规则，避免未参与的工程师被提醒。
                                if is_ecn_pending_for_user(ecn, current_user, current_role):
                                    # ui.chip: NiceGUI框架中用于渲染小标签/徽章的类
                                    ui.chip("待处理", icon="notifications_active", color="red").props(
                                        "dense outline size=sm"
                                    )

            refresh_list()
