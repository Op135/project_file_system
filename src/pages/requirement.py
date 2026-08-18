# -*- encoding: utf-8 -*-
import ast
import asyncio
import copy
import hashlib
import html
import io
import itertools
import json
import logging
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from nicegui import app, events, ui
from nicegui.events import (
    GenericEventArguments,
    KeyEventArguments,
    MouseEventArguments,
    UploadEventArguments,
    ValueChangeEventArguments,
)

from .. import db_storage  # 导入我们创建的模块
from ..components import ButtonUploader, FileThumbnail, InteractiveButton, OverviewTableGroup, OverviewVersionManager
from ..config import (
    BASE_DIR,
    FILES_URL_DIR,
    IMG_DIR,
    NONE_REGULAR,
    OVER_UPLOADS_FILE_TYPE,
    OVERVIEW_UI_RENDER_REGISTRY,
    PRESET_AVATARS,
    PROJECT_STATE_LIST,
    REQ_DIR,
    REQ_UPLOADS_FILE_TYPE,
    TEMP_PROJECT_NUM_LENGTH,
    UPLOAD_URL_DIR,
    UPLOADS_DIR,
)
from ..custom_ui import custom_upload
from ..overview_batch_operations import (
    BATCH_OVERVIEW_ALLOWED_PROJECT_STATES,
    BATCH_OVERVIEW_STAGING_DIR,
    BATCH_OVERVIEW_TOOL_ROLES,
    apply_related_overview_impacts,
    archive_related_record,
    build_batch_result_lines,
    build_new_overview_chip,
    build_project_category_map,
    build_project_model_range_options,
    cascade_deactivate_table_row,
    collect_editable_overview_configs,
    create_batch_overview_request,
    filter_batch_projects,
    find_projects_without_row_anchors,
    get_batch_overview_reviewer_roles,
    insert_overview_chip,
    is_table_child_state_allowed,
    update_overview_chip_state,
    validate_overview_content,
)
from ..overview_corrections import get_project_correction_archives
from ..requirement_overview_impact import RequirementOverviewImpactConfigError
from ..utils import (
    compare_configs_by_id,
    copy_overview_data,
    find_files_with_prefix_and_version,
    find_key_position,
    format_overview_timestamp,
    get_cache_busted_path,
    get_max_numeric_key,
    get_requirement_overview_impacts,
    handle_key,
    logout,
    merge_data_with_template,
    overview_role_update,
    refresh_overview_pending_labels,
    set_overview_active_state,
    setup_global_activity_tracking,
    update_overview_charge_pending_dic,
    validate_format_regex,
    validate_search_path,
    validate_svn_url,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/main/requirement")
async def requirement_page(
    type="",
    json_path="",
    project_name="",
    correction_label="",
    correction_chip_id="",
):
    ui.add_head_html("""
        <style>
            .multiline-chip .q-chip__content {
                white-space: normal !important;
                word-break: break-all !important;
                line-height: 1.3 !important;
            }
            /*控制折叠元素的icon*/
            [dir="ltr"] .q-item__section--side {
                padding-right: 8px;
            }
            .q-item__section--avatar {
                color: inherit;
                min-width: 36px;
            }
            
            .q-btn{
                /*min-height: 2.1em;*/   
            }
            .nicegui-editor .q-editor__content p, .nicegui-markdown p {
                margin: 0.2rem 0;
            }
                  
            /*控制选项框内选项样式*/
            .q-item {
                min-height: 30px;
                padding: 10px 16px;
                color: inherit;
                transition: color 0.3s,background-color 0.3s
            }
                     
                     
            /* 定义一个名为 small-select 的类，专门用于减小高度 */
            /* 1. 覆盖 min-height 和 height */
            .small-select .q-field__control, 
            .small-select .q-field__native {
                min-height: 32px !important; /* 调整为你想要的小高度 */
                height: 32px !important;
            }
            /* 2. 调整图标/下拉箭头容器的高度，确保居中 */
            .small-select .q-field__marginal {
                height: 32px !important;
                font-size: 20px !important; /* 图标也可以稍微改小一点 */
            }
            /* 3. (可选) 如果用了 label，可能需要调整 label 的行高或位置 */
            .small-select .q-field__label {
                top: 6px; 
            }
               
            /*已注释掉——控制下拉选框高度*/
            /*.q-field--auto-height .q-field__control, .q-field--auto-height .q-field__native {
                min-height: 40px;
            }
            .q-field--auto-height .q-field__control {
                height: 50px;
            }
            .q-field__marginal {
                height: 30px;
                font-size: 24px;
            }*/
            /*.q-menu {
                background-color:#efffff;
            }*/
            
            .q-dialog__inner--minimized {
                padding: 12px;
            }
            .q-dialog__backdrop {
                background-color: rgba(0, 0, 0, 0.8);
            }
            .q-textarea textarea {
                /* 1. 设置一个最小高度，而不是固定高度 */
                height: 50px;
                min-height: 50px;

                /* 2. 明确允许用户垂直方向拖动调整大小 (也可设为 "both") */
                resize: vertical;

                /* 3. 当内容超出当前高度时，自动显示垂直滚动条 */
                overflow-y: auto !important; /* Quasar 有时会设置 overflow:hidden, !important 确保覆盖 */
            }
            .q-notification__message {
                padding: 8px 0;
                white-space: pre-line;
            }
            .nicegui-expansion .q-expansion-item__content {
                gap: 4px
            }
        </style>
    """)

    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    setup_global_activity_tracking()
    current_user = app.storage.user.get("current_user")
    current_role = app.storage.user.get("current_role")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 用户研发经理切换项目状态为转产时，记录项目哪些类别的概述被改成待定状态
    changed_labels = set()
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)
    # 存储用户层级需求相关数据的变量初始化
    # 用于记录键盘按键状态
    app.storage.client.setdefault("key_state", {})
    if type == "overview" and correction_label and correction_chip_id:
        app.storage.client["overview_correction_auto_open"] = {
            "label": str(correction_label),
            "chip_id": str(correction_chip_id),
        }
    else:
        app.storage.client.pop("overview_correction_auto_open", None)
    # 需求配置数据字典初始化
    app.storage.client.setdefault("config_data", app.state.init_config_data)
    # 一个空列表，用于存储当前管理的文件列表。可以在这个列表中添加文件路径、文件名或其他文件相关信息
    app.storage.client.setdefault("files", [])
    # 一个空集合（set），用于存储已经被删除的文件的标识符（例如文件名或路径）
    app.storage.client.setdefault("deleted_files", [])
    # 一个整数，初始值为 0，用于记录文件的总数或其他与文件计数相关的逻辑
    app.storage.client.setdefault("file_counter", 0)
    # 一个文件缩略图实例化对象字典
    app.storage.client.setdefault("file_thumbnail_dic", {})
    # 保存添加过某个数字引用的各个确认项构成的字典，{数字引用:[(确认项序号,确认项内容),......]}
    app.storage.client.setdefault("ref_question_dic", {})
    # 记录项目名称的变量
    app.storage.client.setdefault("project_name", "")
    # 记录项目改名的目标名称的变量
    app.storage.client.setdefault("target_project_name", "")
    # 初始化需求版本
    app.storage.client.setdefault("version", "0.0")
    # 初始化需求版本
    # app.storage.client.setdefault("target_version", "")
    # 需求确认项按钮字典
    app.storage.client.setdefault("buttons_dic", {})
    # 新增一个地方来存放当前页面的关键UI元素
    app.storage.client.setdefault("page_elements", {})
    # 用于后续保存需求问题项，已选填数目
    app.storage.client.setdefault("req_activ_num", 0)
    # 用于后续保存需求问题项，未选填数目
    app.storage.client.setdefault("req_not_activ_num", 0)
    # 用于后续保存需求问题项，总数目
    app.storage.client.setdefault("req_com_num", 0)
    # 用于保存当前需求问题序号
    app.storage.client.setdefault("current_question_num", 0)
    # 初始化自动保存开关为 False (关)，用于用户思考是否使用自动保存记录期间，避免自动保存机制覆盖掉原有自动保存需求
    app.storage.client["allow_autosave"] = False

    # 在全局作用域创建对话框（确保在菜单系统之外）
    general_dialog = ui.dialog()
    batch_overview_dialog = ui.dialog().props("persistent")
    correction_archive_dialog = ui.dialog().props("persistent")
    # 创建项目名修改对话框
    with ui.dialog().props("persistent").classes("") as project_dialog:
        project_card = ui.card().classes("w-1/4")
    # 创建并显示对比对话框
    with ui.dialog() as contrast_dialog:
        contrast_card = (
            ui.card().classes("gap-2").style("min-width: 800px; max-width: 90vw; min-hight: 800px; max-hight: 90vw;")
        )
    # 创建用于选择需求版本的对话框
    with ui.dialog().props("persistent").classes("") as req_version_dialog:
        version_card = ui.card().classes("w-1/3")

    with ui.dialog().props("persistent").classes("") as over_dialog:
        over_card = ui.card().classes("w-1/3")
    # 存储对话框引用
    app.storage.client["page_elements"]["project_card"] = project_card
    app.storage.client["page_elements"]["project_dialog"] = project_dialog
    app.storage.client["page_elements"]["contrast_card"] = contrast_card
    app.storage.client["page_elements"]["contrast_dialog"] = contrast_dialog
    app.storage.client["page_elements"]["version_card"] = version_card
    app.storage.client["page_elements"]["req_version_dialog"] = req_version_dialog

    # 获取所有JSON配置文件的文件名
    try:
        config_files = [f.name for f in Path(REQ_DIR).glob("*.json") if f.is_file()]
        # 合并列表
        config_files.extend([f.name for f in Path(f"{REQ_DIR}/temp/{current_user}").glob("*.json") if f.is_file()])
        if not config_files:
            ui.notify(
                "系统初始化，目录下未找到任何JSON配置文件。",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            config_files = []
    except Exception as e:
        ui.notify(
            f"读取配置文件目录时出错: {e}",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )
        config_files = []

    def set_project_engineer_dialog(project_name, engineer_button):
        general_dialog.clear()
        with general_dialog, ui.card().classes("w-[500px]"):
            project_engineer = app.storage.general["project_engineer"].get(project_name, "")
            ui.label("设置项目工程师负责人").classes("text-xl font-bold mb-4")

            ui.input("实时修改", value=project_engineer).props("autofocus outlined").bind_value(
                app.storage.general["project_engineer"], project_name
            )

            general_dialog.open()

    # 键盘事件跟踪处理函数
    def requirement_handle_key(e: KeyEventArguments):
        k = app.storage.client["current_question_num"]
        options_type = app.storage.client["config_data"]["data"][k]["answer_type"]

        if e.key.name == "ArrowLeft" and e.action.keydown:
            app.storage.client["key_state"]["arrowleft"] = 1
            get_option(k, options_type, -1)
        elif e.key.name == "ArrowLeft" and e.action.keyup:
            app.storage.client["key_state"]["arrowleft"] = 0
        if e.key.name == "ArrowRight" and e.action.keydown:
            app.storage.client["key_state"]["arrowright"] = 1
            get_option(k, options_type, 1)
        elif e.key.name == "ArrowRight" and e.action.keyup:
            app.storage.client["key_state"]["arrowright"] = 0

            # app.storage.client["key_state"]["enter"] = 0

    # 遍历传入的整个概述资料，找到svn类型chip，如果其最高版本激活状态不是False，则将其设置成False
    def set_overview_data_not_true(over_data, project_name):
        changed_labels.clear()
        for label, label_dic in over_data.items():
            label_updtae_bool = False
            for id, chip_dic in label_dic.items():
                req_max_ver = app.storage.general["project_req_max_ver"][project_name]
                select_activ_state = chip_dic.get("select_activ_dic", {}).get(req_max_ver)
                old_notes = chip_dic.get("notes", "")
                creator = chip_dic.get("creator", "系统修改")
                # 只处理svn类型 且 非放在产品仓库的 chip
                if chip_dic.get("type") == "svn" and chip_dic.get("warehouse") != "Product":
                    # 最高激活状态不是False
                    if select_activ_state or select_activ_state is None:
                        label_updtae_bool = True
                        # 项目状态切换为转产时，svn类失活掉
                        # over_data[label][id]["select_activ_dic"][req_max_ver] = False
                        # over_data[label][id]["icon"] = "block"
                        # over_data[label][id]["enabled"] = False
                        # over_data[label][id]["bg_color"] = "bg-grey-5"

                        # 项目状态切换为转产时，svn类不再失活掉，而是改成待定状态，同步修改仓库位置，增加时间戳记录
                        over_data[label][id]["select_activ_dic"][req_max_ver] = None
                        over_data[label][id]["icon"] = "question_mark"
                        over_data[label][id]["enabled"] = None
                        over_data[label][id]["bg_color"] = "bg-amber-5"
                        over_data[label][id]["warehouse"] = "Product"
                        over_data[label][id]["notes"] = old_notes + "转产迁移（自动修改）"
                        over_data[label][id]["timestamp"] = {
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                                "creator": creator,
                                "select_activ_dic": over_data[label][id]["select_activ_dic"],
                            }
                        }
                # 其它类型的chip
                elif chip_dic.get("type") != "svn":
                    # 最高激活状态是True
                    if select_activ_state:
                        label_updtae_bool = True
                        over_data[label][id]["select_activ_dic"][req_max_ver] = None
                        over_data[label][id]["icon"] = "question_mark"
                        over_data[label][id]["enabled"] = None
                        over_data[label][id]["bg_color"] = "bg-amber-5"
            if label_updtae_bool:
                changed_labels.add(label)

        return over_data

    # 编辑project_summary json文件
    def edit_project_summary(project_name, state, recovery_bool):
        # 如果是恢复项目的操作附带导致该函数被调取，不用操作，跳过
        if recovery_bool:
            # 复位标记
            app.storage.client["recovery_bool"] = False
            return
        project_data = {}
        with open(f"{BASE_DIR}/data/project_summary.json", "r", encoding="utf-8") as f:
            project_data = json.load(f)
            project_data[project_name]["state"] = state
        # 将字典转换为 JSON 字符串
        json_str = json.dumps(project_data, indent=4, ensure_ascii=False)
        # 写入文件
        try:
            with open(f"{BASE_DIR}/data/project_summary.json", "w", encoding="utf-8") as f:
                f.write(json_str)
            app.storage.general["project_summary"][project_name]["state"] = state
            ui.notify(
                "修改项目状态成功。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        except Exception as e:
            ui.notify(
                f"修改项目状态错误错误：{e}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    async def conversion_chip_state(project_name, state):
        # 先编辑json文件
        edit_project_summary(project_name, state, app.storage.client.get("recovery_bool", False))
        # 将该项目所有svn类的chip失活
        success = await db_storage.atomic_deep_update(
            [f"{project_name}_over_data"], set_overview_data_not_true, project_name
        )
        if success:
            for label in changed_labels:
                OverviewVersionManager.bump(project_name, label)
            overview_role_update(project_name, "all_update")
        # 必须在数据修改完成后，再激活概述特殊刷新标记
        # app.storage.general["conversion_refresh"][project_name] = True
        # 0.8秒比概述定时0.5秒刷新稍长情况下，关闭特殊刷新开关
        # ui.timer(0.8, lambda: close_conversion_refresh(project_name), once=True)
        over_dialog.close()

    def set_project_conversion_dialog(project_name, state, on_cancel_action):
        over_card.clear()
        with over_card:
            ui.label("确认项目开始转产？").classes("text-base text-red")
            ui.label("所有svn概述将设置为失活状态，其它激活概述将设置为待定状态！").classes("text-base text-red")
            with ui.row().classes("w-full justify-end"):
                ui.button("确认", on_click=lambda: conversion_chip_state(project_name, state))
                ui.button("取消", on_click=on_cancel_action)
        over_dialog.open()

    def set_project_trial_dialog(project_name, state, on_cancel_action):
        pending_dic = app.storage.general.get("overview_charge_pending", {})
        # 用于保存导致该项目不能切换状态到试产的相关人员与其未确定概述字典
        pending_out_dic = {}
        for charge_user, user_dic in pending_dic.items():
            for pn, pending_dic in user_dic.items():
                if (
                    pn == project_name
                    and pending_dic
                    and any([v in ["缺必填", "有待定"] for v in pending_dic.values()])
                ):
                    pending_out_dic[charge_user] = [k for k, v in pending_dic.items() if v in ["缺必填", "有待定"]]
        over_flat = app.storage.general.get("over_config_data_flat", {})
        over_card.clear()
        with over_card:
            if pending_out_dic:
                html_str = ""
                ui.label("下列人员负责的概述未填或状态待定，无法将项目切换为试产状态！").classes("text-base text-red")
                for user, user_dic in pending_out_dic.items():
                    html_str += (
                        f"<b>{user}：</b><br>"
                        + "<br>".join(
                            [f"• {over_flat.get(over_label, {}).get('title', '未知概述项')}" for over_label in user_dic]
                        )
                        + "<br>"
                    )
                ui.html(html_str, sanitize=False).classes("text-sm text-gray-800")
                with ui.row().classes("w-full justify-end"):
                    ui.button("确认", on_click=on_cancel_action)
            else:
                ui.label("确认项目进入试产阶段，概述内容将只能通过ECN修改，且切换不可逆！").classes(
                    "text-base text-red"
                )
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "确认",
                        on_click=lambda: edit_project_summary(
                            project_name, state, app.storage.client.get("recovery_bool", False)
                        ),
                    ).on("click", lambda: over_dialog.close())
                    ui.button("取消", on_click=on_cancel_action)
        over_dialog.open()

    # 关闭项目概述特殊刷新标记
    # def close_conversion_refresh(project_name):
    #     app.storage.general["conversion_refresh"][project_name] = False

    # 修改项目状态
    async def set_project_state(project_name, e):
        state = e.value
        # 获取下拉框组件对象，用于后续如果取消了，把值改回去
        select_element = e.sender
        previous_state = app.storage.general["project_summary"][project_name].get("state")

        # 定义一个取消时的回调函数：把下拉框的值改回旧状态，并关闭弹窗
        def on_cancel_action():
            # 恢复标记打开，防止状态改回时按照正常修改状态操作文件和弹出提示信息
            app.storage.client["recovery_bool"] = True
            select_element.value = previous_state  # 视觉上改回旧值
            over_dialog.close()

        if (
            previous_state in ["作废", "待定"]
            and state in ["转产", "试产", "量产"]
            or previous_state in ["研发"]
            and state in ["试产", "量产"]
            or previous_state in ["转产"]
            and state in ["量产"]
            or previous_state in ["试产", "量产"]
            and state in ["转产"]
            or previous_state in ["转产", "试产", "量产"]
            and state in ["研发", "待定", "作废"]
        ):
            # 恢复标记打开，防止状态改回时按照正常修改状态操作文件和弹出提示信息
            app.storage.client["recovery_bool"] = True
            select_element.value = previous_state
            ui.notify(
                "禁止违规状态切换！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        # 如果是从研发状态改为转产或量产，将所有svn概述全部失活掉，然后进行特殊刷新
        elif previous_state == "研发" and state == "转产":
            if current_role != "研发经理":
                # 无权限时，也要把界面改回去
                # 恢复标记打开，防止状态改回时按照正常修改状态操作文件和弹出提示信息
                app.storage.client["recovery_bool"] = True
                select_element.value = previous_state
                ui.notify(
                    "当前用户无权限修改项目状态！",
                    type="info",
                    position="bottom",
                    timeout=2000,
                    progress=True,
                    close_button="✖",
                )
            else:
                set_project_conversion_dialog(project_name, state, on_cancel_action)
        elif previous_state == "转产" and state == "试产":
            set_project_trial_dialog(project_name, state, on_cancel_action)
        else:
            edit_project_summary(project_name, state, app.storage.client.get("recovery_bool", False))

    # 显示传入数据的用户填写内容
    def show_user_output(data):
        ui.label(f"确认项: {data['guide_content']}")
        if "单选" in data["answer_type"]:
            if not data["user_must_out"]:
                ui.label("（无此项配置）").classes("text-light-blue-9")
                return

            value = list(data["user_must_out"].values())[0]
            if value == "True":
                ui.label("（是）").classes("text-light-blue-9")
            elif value == "False":
                ui.label("（否）").classes("text-light-blue-9")
            else:
                ui.label(f"（{value}）").classes("text-light-blue-9")

            if data["ref_out"]:
                ui.label(f"（引用文件：{'，'.join(data['ref_out'])}）").classes("text-amber-9")

        elif "多选" in data["answer_type"]:
            if not data["user_must_out"]:
                ui.label("（无此项配置）").classes("text-light-blue-9")
                return

            for k, v in data["user_must_out"].items():
                if v:
                    ui.label(f"（{k}）").classes("text-light-blue-9")

            if data["ref_out"]:
                ui.label(f"（引用文件：{'，'.join(data['ref_out'])}）").classes("text-amber-9")

        elif data["answer_type"] in ["正整数", "单行文本", "多行文本"]:
            if not data["user_must_out"]:
                ui.label("（无此项配置）").classes("text-light-blue-9")
                return

            if data["input_tolerance"] == "正负":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）典型值（{v}），公差（{data['option_tolerance_out'][k]}）").classes(
                        "text-light-blue-9"
                    )
            elif data["input_tolerance"] == "范围":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）下限（{v}），上限（{data['option_tolerance_out'][k]}）").classes(
                        "text-light-blue-9"
                    )
            elif data["input_tolerance"] == "上限":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）上限（{v}）").classes("text-light-blue-9")
            elif data["input_tolerance"] == "下限":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）下限（{v}）").classes("text-light-blue-9")
            else:
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）填写（{v}）").classes("text-light-blue-9")

            if data["ref_out"]:
                ui.label(f"（引用文件：{'，'.join(data['ref_out'])}）").classes("text-amber-9")

    def show_comparison_dialog():
        contrast_card = app.storage.client["page_elements"].get("contrast_card")
        contrast_card.clear()
        app.storage.client["page_elements"].get("contrast_dialog").props("persistent")
        app.storage.client["page_elements"].get("contrast_dialog").open()

        with contrast_card:
            with ui.row().classes("w-full justify-between"):
                ui.label("产品配置对比工具").classes("text-h6")

                ui.button(
                    "",
                    icon="close",
                    on_click=lambda: app.storage.client["page_elements"].get("contrast_dialog").close(),
                ).props("flat round").classes("text-black bg-transparent")
            with ui.row().classes("w-full items-center justify-between"):
                # 下拉选择框
                select1 = ui.select(config_files, label="选择旧版本配置 (产品A)").props("outlined").classes("w-2/5")
                # 对比按钮
                ui.button("开始对比", on_click=lambda: perform_comparison()).classes("bg-amber-8")
                select2 = ui.select(config_files, label="选择新版本配置 (产品B)").props("outlined").classes("w-2/5")

            ui.separator().props("size=1px")
            # 结果展示区域
            results_area = ui.scroll_area().classes("gap-2 w-full h-96 p-2 bg-grey-2 rounded-lg")
            ui.separator().props("size=1px")

        async def perform_comparison():
            """执行对比并更新UI"""
            old_file = select1.value
            new_file = select2.value

            if not old_file or not new_file:
                ui.notify(
                    "请选择两个需要对比的配置文件。",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return

            if old_file == new_file:
                ui.notify(
                    "请选择两个不同的配置文件进行对比。",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return

            # 读取和解析JSON文件
            try:
                old_data = {}
                new_data = {}
                if Path(f"{REQ_DIR}/{old_file}").is_file():
                    with open(f"{REQ_DIR}/{old_file}", "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                elif Path(f"{REQ_DIR}/temp/{current_user}/{old_file}").is_file():
                    with open(f"{REQ_DIR}/temp/{current_user}/{old_file}", "r", encoding="utf-8") as f:
                        old_data = json.load(f)
                else:
                    ui.notify(
                        f"文件不存在: {old_file}",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                if Path(f"{REQ_DIR}/{new_file}").is_file():
                    with open(f"{REQ_DIR}/{new_file}", "r", encoding="utf-8") as f:
                        new_data = json.load(f)
                elif Path(f"{REQ_DIR}/temp/{current_user}/{new_file}").is_file():
                    with open(f"{REQ_DIR}/temp/{current_user}/{new_file}", "r", encoding="utf-8") as f:
                        new_data = json.load(f)
                else:
                    ui.notify(
                        f"文件不存在: {new_file}",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )

            except Exception as e:
                ui.notify(
                    f"读取或解析文件时出错: {e}",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return

            # 调用对比函数
            diff = compare_configs_by_id(old_data["data"], new_data["data"], ["guide_content"])

            # 清空并填充结果区域
            results_area.clear()
            with results_area:
                if not any(diff.values()):
                    ui.label("两个配置完全相同，没有差异。").classes("text-lg text-green-8")
                    return

                # 1. 展示新增项
                if diff["added"]:
                    with ui.expansion("新增项", icon="add_circle", value=True).classes(
                        "gap-2 w-full bg-green-100 rounded"
                    ):
                        for item_id, item_data in diff["added"].items():
                            with ui.card().classes("gap-1 w-full my-2"):
                                ui.label(f"ID: {item_id}").classes("text-bold")
                                ui.label(f"确认项内容: {item_data.get('guide_content', 'N/A')}")

                # 2. 展示删除项
                if diff["deleted"]:
                    with ui.expansion("删除项", icon="remove_circle", value=True).classes(
                        "gap-2 w-full bg-red-100 rounded"
                    ):
                        for item_id, item_data in diff["deleted"].items():
                            with ui.card().classes("gap-1 w-full my-2"):
                                ui.label(f"ID: {item_id}").classes("text-bold")
                                ui.label(f"确认项内容: {item_data.get('guide_content', 'N/A')}")

                # 3. 展示修改项
                if diff["modified"]:
                    with ui.expansion("修改项", icon="sync_alt", value=True).classes(
                        "gap-2 w-full bg-orange-100 rounded"
                    ):
                        for item_id, changes in diff["modified"].items():
                            with ui.card().classes("gap-1 w-full my-2"):
                                ui.label(f"ID: {item_id}").classes("text-bold mb-2")
                                ui.separator().props("size=1px")
                                with ui.grid(columns=2).classes("w-full mt-2"):
                                    # 旧值
                                    with ui.card_section():
                                        ui.label("旧版本").classes("text-grey-7")
                                        show_user_output(changes["old_data"])

                                    # 新值
                                    with ui.card_section():
                                        ui.label("新版本").classes("text-bold")
                                        show_user_output(changes["new_data"])

    # 弹出项目名设置弹窗
    def get_project_dialog(key_str="revise"):
        project_card = app.storage.client["page_elements"].get("project_card")
        project_old_name = app.storage.client["project_name"]
        project_card.clear()
        app.storage.client["page_elements"].get("project_dialog").props("persistent")
        app.storage.client["page_elements"].get("project_dialog").open()
        with project_card:
            with ui.column().classes("-space-y-4"):
                ui.label("请输入项目号：").classes("text-xl font-bold")
                ui.label("1. 提交需求或选择查阅版本时该设置才生效，暂存需求不起效。").classes("text-base text-red")
                ui.label("2. 新建临时项目输入RFTS即可，系统自动顺延产生项目号。").classes("text-base text-red")
                ui.label("3. 输入完整临时项目号，存在则属于升级版本，不存在则无效。").classes("text-base text-red")
            input_field = ui.input().classes("text-[20px]/[22px] w-full")
            # 写入的值绑定到目标项目名变量
            input_field.bind_value(app.storage.client, "target_project_name")
            with ui.row().classes("flex-nowrap w-full"):
                ui.button("确认", icon="check", on_click=lambda: confirm_peoject_name(key_str)).classes("w-full")
                ui.button("取消", icon="cancel", on_click=lambda: cancel_peoject_name(project_old_name)).classes(
                    "w-full"
                )

    # 确认项目命名处理函数
    def confirm_peoject_name(key_str):
        target_project_name = app.storage.client["target_project_name"]
        # 参照项目的审核状态
        # version = app.storage.client["version"]
        # original_review_state = ""
        # if app.storage.general["wait_review"].get(project_name, {}):
        #     original_review_state = app.storage.general["wait_review"][project_name].get(
        #         f"{version.split('.')[0]}.0", {"state": ""}
        #     )["state"]
        # project_name = app.storage.client["project_name"]
        if target_project_name == "":
            ui.notify(
                "请输入非空名称！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            app.storage.client["target_project_name"] = app.storage.client["project_name"]
        elif (
            target_project_name.split("-")[0] != "RFTS"
            and target_project_name not in app.storage.general["project_summary"]
        ):
            ui.notify(
                "非临时项目，又未正式立项，命名不可用，请重新命名！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            app.storage.client["target_project_name"] = app.storage.client["project_name"]
        elif (
            target_project_name != "RFTS"
            and target_project_name.split("-")[0] == "RFTS"
            and not validate_format_regex(target_project_name, r"^RFTS-\d{4}$")
        ):
            ui.notify(
                "不符合临时项目号命名规则：RFTS-4位数字！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            app.storage.client["target_project_name"] = app.storage.client["project_name"]
        elif (
            target_project_name != "RFTS"
            and target_project_name.split("-")[0] == "RFTS"
            and target_project_name not in app.storage.general["temp_project_name"]
        ):
            ui.notify(
                "不能指定临时项目号进行新建项目，请输入“RFTS”进行新建！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            app.storage.client["target_project_name"] = app.storage.client["project_name"]
        # elif original_review_state == "待修改":
        #     ui.notify(
        #         "当前需求处于待修改状态，禁止衍生！",
        #         type="warning",
        #         position="bottom",
        #         timeout=3000,
        #         progress=True,
        #         close_button="✖",
        #     )
        #     app.storage.client["target_project_name"] = app.storage.client["project_name"]
        # # 禁止待审、待修改需求导出
        # elif original_review_state == "待审":
        #     ui.notify(
        #         "当前需求处于待审状态，禁止衍生！",
        #         type="warning",
        #         position="bottom",
        #         timeout=3000,
        #         progress=True,
        #         close_button="✖",
        #     )
        #     app.storage.client["target_project_name"] = app.storage.client["project_name"]
        else:
            app.storage.client["page_elements"].get("target_project_button").props(remove="icon")
            # 为了新建项目需求而弹窗，则调用新需求处理函数
            if key_str == "new":
                ui.navigate.to(f"/main/requirement?type=requirement&project_name={target_project_name}")

        project_dialog.close()

    # 取消项目命名处理函数
    def cancel_peoject_name(project_old_name):
        app.storage.client["target_project_name"] = project_old_name
        project_dialog.close()

    # 新建需求初始化所有配置
    def new_requirement():
        app.storage.client["config_data"] = app.state.init_config_data
        app.storage.client["files"] = []
        app.storage.client["deleted_files"] = []
        app.storage.client["file_counter"] = 0
        app.storage.client["file_thumbnail_dic"] = {}
        app.storage.client["ref_question_dic"] = {}
        app.storage.client["buttons_dic"] = {}
        app.storage.client["version"] = "0.0"
        # app.storage.client["target_version"] = ""
        app.storage.client["original_version"] = "0.0"
        app.storage.client["original_project"] = ""

        requirement_input_frame()
        # 刷新界面
        set_question_list(0)  # 初始化一次确认项列表
        app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
        question_display(None, "1")  # 触发点击事件
        app.storage.client["page_elements"].get("target_project_button").props(remove="icon")
        req_thumbnail_display()
        # 显示成功通知
        ui.notify(
            "成功创建新需求",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )

    # 解析json配置文件，并生成需求界面
    def loads_requirements(json_data, loads_bool: bool):
        # --- 修改开始：使用内存中的全局配置 ---
        try:
            # 直接从全局状态获取最新的配置模版 (假设你在 main.py 启动时已经加载了它)
            template_data = app.state.init_config_data

            # 这里的 template_data 传进去后，merge 函数内部第一行必须是 deepcopy
            final_config_data = merge_data_with_template(json_data, template_data)

        except Exception as e:
            logger.error(f"合并最新模版失败，回退到使用文件原数据: {e}", exc_info=True)
            final_config_data = json_data
        # --- 修改结束 ---
        # -----------------------------------------------
        try:
            # 获取文件缩略图字典内容，直接覆盖现有内容
            # file_information = json_data["file_dic"]
            file_information = final_config_data.get("file_dic", {})
            app.storage.client["file_thumbnail_dic"] = {}
            for k, v in file_information.items():
                app.add_static_file(local_file=f"{UPLOADS_DIR}/{v['file_name_hash']}", url_path=v["file_url"])
                file_thumbnail = FileThumbnail(
                    file_url=v["file_url"],
                    file_type=v["file_type"],
                    file_name_suffix=v["file_name_suffix"],
                    file_lab=v["file_lab"],
                    parents_h=v["parents_h"],
                    auto_create=False,
                    delet_lab=True,
                    on_add_ref_click=add_ref_button,
                    on_question_display_click=question_display,
                )
                app.storage.client["file_thumbnail_dic"][k] = {
                    "file_obj": file_thumbnail,
                    "file_information": v,
                }
        except Exception as e:
            logger.error(f"读取需求配置出错，引用文件丢失: {e}", exc_info=True)
            ui.notify(
                f"读取需求配置出错，引用文件丢失: {e}，请联系系统管理员",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return
        # # 恢复文件状态记录
        # app.storage.client["files"] = json_data["files"]
        # app.storage.client["deleted_files"] = json_data["deleted_files"]
        # app.storage.client["file_counter"] = json_data["file_counter"]
        # # 恢复项目名称与版本
        # app.storage.client["project_name"] = json_data["project_name"]
        # app.storage.client["version"] = json_data["version"]
        # # 设置提交目标名称与版本
        # if not loads_bool:
        #     app.storage.client["target_project_name"] = json_data[
        #         "project_name"
        #     ]  # 导入需求则不设置，因为导入前都是先进入某个项目，即默认导入数据就是为了提交成这个项目
        # # 将衍生自哪个项目的信息获取过来
        # app.storage.client["original_project"] = json_data["original_project"]
        # app.storage.client["original_version"] = json_data["original_version"]

        # # 将剩余配置与用户填写记录信息覆盖现有配置
        # app.storage.client["config_data"] = json_data
        # # 遍历配置信息，抽取引用信息，重新恢复引用_确认项记录
        # app.storage.client["ref_question_dic"] = {}  # 先清空
        # for k, v in json_data["data"].items():
        #     question_k = k
        #     question = v["guide_content"]
        #     if v["ref_out"]:
        #         for ref in v["ref_out"]:
        #             if ref in app.storage.client["ref_question_dic"].keys():
        #                 app.storage.client["ref_question_dic"][ref].append([question_k, question])
        #             else:
        #                 app.storage.client["ref_question_dic"][ref] = [
        #                     [question_k, question],
        #                 ]

        # 恢复状态记录 (从 final_config_data 读取)
        app.storage.client["files"] = final_config_data.get("files", [])
        app.storage.client["deleted_files"] = final_config_data.get("deleted_files", [])
        app.storage.client["file_counter"] = final_config_data.get("file_counter", 0)

        # 恢复项目名称与版本
        app.storage.client["project_name"] = final_config_data.get("project_name", "")
        app.storage.client["version"] = final_config_data.get("version", "0.0")

        # 设置提交目标名称与版本
        if not loads_bool:
            app.storage.client["target_project_name"] = final_config_data.get("project_name", "")

        # 衍生信息
        app.storage.client["original_project"] = final_config_data.get("original_project", "")
        app.storage.client["original_version"] = final_config_data.get("original_version", "0.0")

        # 将合并后的配置赋值给 storage
        app.storage.client["config_data"] = final_config_data

        # 恢复引用 (逻辑不变，但在 final_config_data 上操作)
        app.storage.client["ref_question_dic"] = {}
        for k, v in final_config_data["data"].items():
            question_k = k
            question = v["guide_content"]
            if v.get("ref_out"):  # 使用 get 防止 key 缺失
                for ref in v["ref_out"]:
                    if ref in app.storage.client["ref_question_dic"].keys():
                        app.storage.client["ref_question_dic"][ref].append([question_k, question])
                    else:
                        app.storage.client["ref_question_dic"][ref] = [
                            [question_k, question],
                        ]
        requirement_input_frame()
        # set_question_list(0)  # 初始化一次确认项列表
        # app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
        # question_display(None, "1")  # 触发点击事件
        # req_thumbnail_display()
        # 显示成功通知
        ui.notify(
            "成功导入项目数据",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )

    # json数据导入处理函数——触发上传窗口
    def import_config_data(upload):
        # 在上传新文件前，先清空upload列表，否则后续删除文件后，不能在重新插入
        upload.reset()
        # 触发隐藏的上传组件
        upload.run_method("pickFiles")  # 触发浏览器的文件选择对话框

    # json数据导入处理函数——处理数据
    async def json_handle_upload(e: events.UploadEventArguments):
        """处理上传的JSON文件"""
        try:
            # 获取上传的文件内容
            content_obj = await e.file.read()
            content = content_obj.decode("utf-8")
            # 解析JSON数据
            json_data = json.loads(content)
            loads_requirements(json_data, True)

        except json.JSONDecodeError:
            ui.notify(
                "文件上传失败",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
        except Exception as ex:
            logger.error("上传处理失败", exc_info=True)  # 在服务器端打印错误详情
            ui.notify(
                f"上传文件 '{e.file.name}' 失败: {str(ex)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 创建一个图片上传组件，包括一个上传按钮和上传好的图片缩略图
    def get_img_group(button_name="上传", input_any_suffix="/*", parents_h=9):
        with ui.row().classes(f"h-{str(parents_h)}").classes("p-0 -space-x-4"):
            ButtonUploader(
                on_upload=handle_upload,
                label=button_name,
                input_any_suffix=input_any_suffix,
                classes_str="h-full",
                parents_h=parents_h,
            )

    # 文件上传后的处理函数
    async def handle_upload(e: UploadEventArguments, parents_h):
        try:
            hash_obj = hashlib.md5()
            # new_file_hash = ""
            # 使用 os.path.splitext 来更稳健地分离文件名和后缀
            file_name, file_suffix = os.path.splitext(e.file.name)
            # 获取文件类型
            file_type = e.file.content_type  # 图片类返回image/xxx，文件类返回application/xxx，文本类型text/xxx

            # 如果是文件或文本类型，要检查后缀，图片类型不用检查
            if ("application" in file_type or "text" in file_type) and file_suffix not in REQ_UPLOADS_FILE_TYPE:
                ui.notify(
                    f'文件 "{file_name}" 不是规定的：{", ".join(REQ_UPLOADS_FILE_TYPE)} 文件类型，无法上传!',
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return

            # 移除前导的点
            file_suffix = file_suffix.lstrip(".")

            # 1. 一次性将文件内容完整读入内存中的 bytes 对象
            #    无论 e.file 是 SmallFileUpload 还是 FileUpload，.read() 都是支持的。
            file_content = await e.file.read()
            # 2. 使用 io.BytesIO 将内存中的 bytes 数据包装成一个标准的文件对象
            #    这个 file_like_object 的行为与真实文件完全一致，始终支持 seek()。
            file_content_object = io.BytesIO(file_content)

            # 计算文件哈希值
            file_content_object.seek(0)  # <--- 重要：将文件指针重置到开头
            while chunk := file_content_object.read(4096):  # 分块读取，每块 4096 字节
                hash_obj.update(chunk)
            # 返回哈希值的十六进制字符串
            new_file_hash = hash_obj.hexdigest()
            # 拼接带哈希值的文件名
            file_name_hash = f"{file_name}.{new_file_hash}.{file_suffix}"
            # 拼接带哈希值文件名的文件服务器存放路径
            new_file_path = os.path.join(UPLOADS_DIR, file_name_hash)
            if not os.path.isfile(new_file_path):
                # 保存上传的文件
                file_content_object.seek(0)  # <--- 重要：再次将文件指针重置到开头以进行写入
                with open(new_file_path, "wb") as f:
                    while chunk := file_content_object.read(4096):  # <--- 重要：循环读取和写入
                        f.write(chunk)

            # 将文件路径映射为可访问的 URL
            url_path = f"{UPLOAD_URL_DIR}/{file_name_hash}"

            app.add_static_file(local_file=new_file_path, url_path=url_path)
            if (
                file_name_hash in app.storage.client["files"]
                and file_name_hash not in app.storage.client["deleted_files"]
            ):
                logger.info("文件已存在")
                ui.notify(
                    f"文件已存在: {str(e.file.name)}",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
            else:
                app.storage.client["files"].append(file_name_hash)
                app.storage.client["file_counter"] += 1
                file_lab = str(app.storage.client["file_counter"])
                if file_name_hash in app.storage.client["deleted_files"]:
                    app.storage.client["deleted_files"].remove(file_name_hash)

                # 实例化缩略图对象
                # 从 user storage 中获取当前活跃的 question_column
                # 而不是使用闭包捕获的旧变量
                current_img_row = app.storage.client["page_elements"].get("img_row")
                with current_img_row:
                    file_thumbnail = FileThumbnail(
                        file_url=url_path,
                        file_type=e.file.content_type,
                        file_name_suffix=e.file.name,
                        file_lab=file_lab,
                        parents_h=parents_h,
                        on_add_ref_click=add_ref_button,
                        on_question_display_click=question_display,
                    )
                    # 将文件缩略图实例存入字典
                    app.storage.client["file_thumbnail_dic"][file_thumbnail.file_index] = {
                        "file_obj": file_thumbnail,
                        "file_information": {
                            "file_del_bool": False,
                            "file_name": file_name,
                            "file_url": url_path,
                            "file_name_hash": file_name_hash,
                            "file_name_suffix": e.file.name,
                            "file_type": e.file.content_type,
                            "file_lab": file_lab,
                            "parents_h": parents_h,
                        },
                    }

                    # 显示缩略图
                    file_thumbnail.thumbnail
        except Exception as ex:
            logger.error("上传处理失败", exc_info=True)
            ui.notify(
                f"上传文件 '{e.file.name}' 失败: {str(ex)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 引用按钮上删除按钮点击响应函数
    def del_ref_button(ref, ref_row, question_k, question):
        # 删除数字引用按钮自己
        ref.delete()
        # 在数字引用于问题字典里，找到对应的引用数字键，删除一个里面记存的对应问题
        if ref.text in app.storage.client["ref_question_dic"].keys():
            app.storage.client["ref_question_dic"][ref.text].remove([question_k, question])
        # 在当前确认项引用行字典里，减掉一个对应的数字引用记录
        if app.storage.client["config_data"]["data"][question_k]["ref_out"]:
            app.storage.client["config_data"]["data"][question_k]["ref_out"].remove(ref.text)

        # 删除该数字按钮同级元素上面的“X”按钮
        for ref_lab in ref_row.default_slot.children:
            for lab in ref_lab.default_slot.children:
                lab.delete()

    # 为引用数字图标加“X”号删除按钮
    def add_del_lab(ref_row, question_k, question):
        for ref_button in ref_row.default_slot.children:
            with ref_button:
                (
                    ui.button(on_click=lambda e, ref=ref_button: del_ref_button(ref, ref_row, question_k, question))
                    .classes("absolute -bottom-2 -right-1 m-0 p-0 q-py-1 bg-red-8 text-white ")
                    .props('round padding="0px 0px" icon="close"')
                    .style("font-size: 8px;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

    # 缩略图加号激活添加函数
    def add_activ_ref(ref_row, question_k, question):
        for k, v in app.storage.client["file_thumbnail_dic"].items():
            file_information = v.get("file_information", {})
            file_obj = v.get("file_obj")
            if file_information.get("file_del_bool") or file_obj is None or not hasattr(file_obj, "thumbnail"):
                continue
            # 防止重复添加加号激活按键
            if not file_obj.add_lab_bool and file_obj.add_add_lab(ref_row, k, question_k, question):
                file_obj.add_lab_bool = True

    # 缩略图加号删除函数
    def delete_activ_ref():
        for v in app.storage.client["file_thumbnail_dic"].values():
            file_obj = v.get("file_obj")
            if file_obj is None:
                continue
            # 防止重复添加加号激活按键
            if file_obj.add_lab_bool and hasattr(file_obj, "ref_lab"):
                file_obj.ref_lab.delete()
                file_obj.add_lab_bool = False

    # 添加数字引用按钮函数
    def add_ref_button(thumbnail_obj, ref_row, question_k: str, question: str, add_bool: bool):
        """
        Args:
            thumbnail_obj：每个数字小按钮在被点击时相当于点击的缩略图图像。
            ref_row：在该行元素里添加数字小按钮。
            question_k：用于记录数字按钮添加在那个问题项里。
            question：用于记录数字按钮添加所在问题项的问题内容标题。
            add_bool：用于识别按照data数据生成已有数字按钮（False），还是新增添加数字按钮（True）
        Returns:
        """
        k = thumbnail_obj.file_index
        # 在引用行里添加于缩略图编号一致的数字引用按钮
        with ref_row:
            ui.button(k, on_click=lambda: thumbnail_obj.handle_index_click()).classes(
                "m-0 text-white bg-brown-6"
            ).props('round padding="0px 6px"').style("font-size: 11px;")
        if add_bool:
            # 如果该数字已经在数字引用于问题字典里存在
            if k in app.storage.client["ref_question_dic"].keys():
                # 在相应数字键的值列表里添加添加该数字引用的元素的问题内容
                app.storage.client["ref_question_dic"][k].append([question_k, question])
            else:
                # 在数字引用于问题字典里新建数字键并录入第一个引用该数字的问题内容
                app.storage.client["ref_question_dic"][k] = [
                    [question_k, question],
                ]
            # 在当前确认项引用行字典里，增加一个对应的数字引用记录
            if app.storage.client["config_data"]["data"][question_k]["ref_out"]:
                app.storage.client["config_data"]["data"][question_k]["ref_out"].append(k)
            else:
                app.storage.client["config_data"]["data"][question_k]["ref_out"] = [
                    k,
                ]
            # 删除同级元素的激活按钮
            delete_activ_ref()

    # 激活条件逻辑文本处理
    def logic_out(k, cond_lgoic_str):
        # 初始化节点激活判断，默认节点不激活
        logic_out_bool = False
        # 设定多条件逻辑分隔字符串列表，如："4any['硬件'] and 17==True"
        logic_delimiters = ["and", "or"]
        # 设定条件逻辑分隔字符串列表
        cond_delimiters = ["any", "all", "==", "!="]

        # 构造正则表达式，escape对字符串中的特殊字符进行转义成普通字符处理
        # 多条件逻辑分隔字符串正则表达式，分隔符有括号包裹起来
        logic_pattern = "|".join(f"({re.escape(delimiter)})" for delimiter in logic_delimiters)
        # 条件逻辑分隔字符串正则表达式
        cond_pattern = "|".join(map(re.escape, cond_delimiters))

        # 使用正则表达式分割字符串
        logic_result = re.split(logic_pattern, cond_lgoic_str)
        # 过滤掉空字符串
        logic_result = [s for s in logic_result if s]
        # 分离分割后的子字符串和分隔符
        # 分割开的各个条件，如：4any['硬件'] 和 17==True
        elements = [s for s in logic_result if s not in logic_delimiters]
        # 用于分隔的逻辑分割字符串，如：and
        separators = [s for s in logic_result if s in logic_delimiters]

        bool_list = []
        cond_id_list = []
        # 遍历分割出来的单个逻辑语句块，4any['硬件'] 和 17==True
        for p in elements:
            # 用条件分隔符分割条件逻辑字符串,如：4 和 ['硬件']
            cond_result = re.split(cond_pattern, p)
            # 将整条逻辑语句里的涉及的前置条件节点序号提取出来
            cond_id = cond_result[0].replace("not", "").strip()
            cond_id_list.append(cond_id)
        # 去重，否则后面如果遇到条件里，and/or处理的是同个序号的条件，将引发bool_list倍数增加，result_str拼接过长，如：True and True  True  True
        cond_id_list = list(set(cond_id_list))
        # 先排查用户是否存在未选择的节点，如有则不满足处理条件，退出
        # 遍历该节点条件里涉及的条件序号
        for c_id in cond_id_list:
            op_user_out = dict(app.storage.client["config_data"]["data"][c_id]["user_must_out"])
            # 如果依赖的节点还没有用户做选填操作
            if op_user_out == {}:
                # 先结束判断，返回该节点激活条件不够
                return logic_out_bool
            # 特殊检查：如果是单选且value为None，视为未填
            if app.storage.client["config_data"]["data"][c_id]["answer_type"] in ["单选", "下拉单选"]:
                if op_user_out.get("value") is None:
                    return logic_out_bool

        # 3. 逻辑计算
        # 遍历分割开出来的各个条件，如：4any['硬件'] 或 17==True 等
        for p in elements:
            # 将逻辑语句按 any|all|==|!= 切分开来
            cond_result = re.split(cond_pattern, p)
            # 获取本次判断涉及的 ID
            current_c_id = cond_result[0].replace("not", "").strip()

            # 获取该 ID 的数据和类型
            node_data = app.storage.client["config_data"]["data"][current_c_id]
            user_out = node_data.get("user_must_out", {})
            answer_type = node_data.get("answer_type")

            # === 【核心修改点】简化数据提取 ===
            # 直接提取存储的值，不再需要去 options 里反查
            op_user_out_list = []

            if answer_type == "多选":
                # 多选存储结构：{"Red": True, "Blue": False} -> 提取 ["Red"]
                op_user_out_list = [k for k, v in user_out.items() if v]

            elif answer_type in ["单选", "下拉单选"]:
                # 单选存储结构：{"value": "Red"} -> 提取 ["Red"]
                val = user_out.get("value")
                if val is not None:
                    op_user_out_list = [str(val)]  # 确保转为字符串比较

            elif answer_type in ["正整数", "单行文本", "多行文本"]:
                # 输入类存储结构：{"1": "val1", "2": "val2"} -> 提取 ["val1", "val2"]
                op_user_out_list = [str(v) for v in user_out.values()]

            # === 逻辑比对 (保持原有逻辑，稍作优化) ===
            try:
                target_val_str = cond_result[1].strip()

                # 处理 any / all (列表包含关系)
                if "any" in p or "all" in p:
                    # 解析条件列表，例如 "['Hardware', 'Software']" -> list
                    condition = ast.literal_eval(target_val_str)

                    if "any" in p:
                        res = any(str(item) in condition for item in op_user_out_list)
                    else:
                        op_user_set = set(op_user_out_list)
                        cond_set = set(str(i) for i in condition)  # 确保类型一致
                        # op_user_set 集合的所有元素是否都包含在 cond_set 集合中，如果是则返回 True，否则返回 False
                        res = op_user_set.issubset(cond_set)

                    if "not" in p:
                        bool_list.append(not res)
                    else:
                        bool_list.append(res)

                # 处理 == (单值相等)
                elif "==" in p:
                    # 如果用户选了多个（理论上单值比较只用于单选/输入），取第一个比较
                    user_val = op_user_out_list[0] if op_user_out_list else None
                    # 注意：target_val_str 可能是 'True' 字符串，需要注意类型
                    # 你的配置里 True/False 通常存的是字符串 "True"/"False" 还是布尔值?
                    # 假设是字符串比较，直接比
                    bool_list.append(str(user_val) == str(target_val_str))

                # 处理 != (单值不等)
                elif "!=" in p:
                    user_val = op_user_out_list[0] if op_user_out_list else None
                    bool_list.append(str(user_val) != str(target_val_str))

            except Exception as e:
                ui.notify(
                    f"需求项激活逻辑计算出错: ID={current_c_id}, 表达式={p}, 错误={e}, 请暂存需求，联系管理员处理。",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                logger.error(f"逻辑计算错误: ID={current_c_id}, 表达式={p}, 错误={e}")
                bool_list.append(False)  # 出错默认 False

        # 拼接并执行最终逻辑
        result_str = "".join(f"{x} {y} " for x, y in itertools.zip_longest(bool_list, separators, fillvalue=""))
        try:
            logic_out_bool = eval(result_str)
        except Exception:
            logic_out_bool = False

        return logic_out_bool

    def get_input_num_from_dependency(data_section, item_data):
        """
        获取输入类题目实际需要生成的输入框数量。
        若没有配置数量依据，默认只生成 1 个；若依据值异常，也回退到 1 个，避免页面渲染中断。
        """
        input_num_accor = item_data.get("input_num_accor", "")
        if input_num_accor == "":
            return 1

        try:
            # 数量依据题约定把数量填在 user_must_out["1"] 中。
            dep_value = data_section[input_num_accor]["user_must_out"].get("1", "1")
            return max(int(float(dep_value)), 0)
        except (KeyError, TypeError, ValueError):
            return 1

    def get_input_target_keys(data_section, k):
        """
        计算输入类题目当前应使用的绑定 key。
        key 优先来自“输入项名称依据”的填写值；没有名称依据或名称为空时，退回到 "1"、"2" 这类序号 key。
        """
        item_data = data_section[k]
        input_num = get_input_num_from_dependency(data_section, item_data)
        input_name_accor = item_data.get("input_name_accor", "")
        name_values = []

        if input_name_accor:
            # 依赖题的填写值会成为当前题输入框的显示标签和存储 key。
            dep_node = data_section.get(input_name_accor, {})
            name_values = list(dict(dep_node.get("user_must_out", {})).values())

        target_keys = []
        for i in range(input_num):
            if i < len(name_values) and str(name_values[i]).strip() != "":
                target_keys.append(str(name_values[i]))
            else:
                target_keys.append(str(i + 1))

        # 渲染输入框时仍按序号遍历，但真正绑定到 user_must_out 的 key 使用 target_keys。
        input_name_dic = {str(i + 1): target_keys[i] for i in range(input_num)}
        return input_num, input_name_dic, target_keys

    def sync_input_output_keys(data_section, k):
        """
        输入类题目的后置绑定 key 来自“数量依据”和“名称依据”。
        名称依据变化时，旧答案不能按旧 key 清掉，要先按位置迁移到新 key。
        """
        input_num, input_name_dic, target_keys = get_input_target_keys(data_section, k)
        item_data = data_section[k]
        stored_data_ref = item_data.setdefault("user_must_out", {})
        stored_tolerance_ref = item_data.setdefault("option_tolerance_out", {})
        if not isinstance(stored_data_ref, dict):
            stored_data_ref = {}
        if not isinstance(stored_tolerance_ref, dict):
            stored_tolerance_ref = {}

        # key 已经与当前依赖结果一致时，不做任何迁移，保留原字典对象给绑定继续使用。
        if list(stored_data_ref.keys()) == target_keys and list(stored_tolerance_ref.keys()) in [[], target_keys]:
            return input_num, input_name_dic, target_keys

        # 只按位置继承旧值：第 1 个旧答案给第 1 个新 key，第 2 个旧答案给第 2 个新 key。
        # 这样用户只改前置名称时，后置答案不会因为旧 key 不存在而丢失。
        old_values = list(stored_data_ref.values())
        old_tolerance_values = list(stored_tolerance_ref.values())
        new_data_map = {}
        new_tolerance_map = {}

        for i, new_key in enumerate(target_keys):
            if i < len(old_values):
                new_data_map[new_key] = old_values[i]
            if i < len(old_tolerance_values):
                new_tolerance_map[new_key] = old_tolerance_values[i]

        # 原子替换为当前有效 key；超出当前数量的旧值会自然被丢弃。
        item_data["user_must_out"] = new_data_map
        item_data["option_tolerance_out"] = new_tolerance_map
        return input_num, input_name_dic, target_keys

    # 问题列表展示函数
    def set_question_list(index):
        # 清空已填需求项数目记录
        app.storage.client["req_activ_num"] = 0
        # 清空未填需求项数目记录
        app.storage.client["req_not_activ_num"] = 0
        # 获取问题表元素
        current_question_table = app.storage.client["page_elements"].get("question_table")
        # 清空之前的 UI 元素
        current_question_table.clear()

        app.storage.client["buttons_dic"].clear()
        data = app.storage.client["config_data"]["data"]
        with current_question_table:
            button_num = 0
            for k, v in data.items():
                # 如果是 无条件 需要创立的就直接创建
                if v["condition"] == "无条件":
                    button_num += 1
                    with (
                        ui.button(
                            # 将按钮序号和问题内容作为按键文字显示
                            f"{button_num}. {v['guide_content']}",
                            on_click=lambda e, k=k: question_display(e, k),
                        )
                        .classes("text-sm w-full")
                        .props('align="left" disabled flat color="grey-10"') as button
                    ):
                        ui.badge(
                            f"{v['option_group_id']}组/ID{v['node_id']}", color="#22222222", text_color="white"
                        ).props("floating transparent").classes("my-1 mr-1 px-[2px] py-[1px] text-[10px]/[10px]")
                    # 如果该按钮对应的确认项有用户输出内容，则启用按钮
                    if v["user_must_out"]:
                        if "单选" in v["answer_type"] and v["user_must_out"]["value"]:
                            button.classes("bg-green-1").props(remove="disabled")
                        elif "多选" in v["answer_type"] and any(v["user_must_out"].values()):
                            button.classes("bg-green-1").props(remove="disabled")
                        elif v["answer_type"] in ["正整数", "单行文本", "多行文本"] and all(
                            v["user_must_out"].values()
                        ):
                            button.classes("bg-green-1").props(remove="disabled")
                    # 将新按钮加入到按钮字典里
                    app.storage.client["buttons_dic"][k] = button
                # 处理遇到节点序号条件为空的异常
                elif v["condition"] == "":
                    ui.notify(
                        f"需求节点序号为{k}的激活条件为空，无法处理，请暂存需求，联系管理员处理后再继续。",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    logger.info(f"配置表节点序号为{k}的配置项激活条件为空，无法处理！")
                # 逻辑处理
                else:
                    # cond_id_list = v["condition_id"].split("&")
                    # 获取节点激活条件内容字符串
                    cond_lgoic_str = v["condition"].strip()
                    # 调用节点激活条件逻辑处理函数处理逻辑字符串，结果为真则按钮激活创建
                    if logic_out(k, cond_lgoic_str):
                        button_num += 1
                        with (
                            ui.button(
                                # 将按钮序号和问题内容作为按键文字显示
                                f"{button_num}. {v['guide_content']}",
                                on_click=lambda e, k=k: question_display(e, k),
                            )
                            .classes("text-sm w-full")
                            .props('align="left" disabled flat color="grey-10"') as button
                        ):
                            ui.badge(
                                f"{v['option_group_id']}组/ID{v['node_id']}", color="#22222222", text_color="white"
                            ).props("floating transparent").classes("my-1 mr-1  px-[2px] py-[1px] text-[10px]/[10px]")
                        # 如果该按钮对应的确认项有用户输出内容，则启用按钮
                        if v["user_must_out"]:
                            if "单选" in v["answer_type"] and v["user_must_out"]["value"]:
                                button.classes("bg-green-1").props(remove="disabled")
                            elif "多选" in v["answer_type"] and any(v["user_must_out"].values()):
                                button.classes("bg-green-1").props(remove="disabled")
                            elif v["answer_type"] in ["正整数", "单行文本", "多行文本"] and all(
                                v["user_must_out"].values()
                            ):
                                button.classes("bg-green-1").props(remove="disabled")

                        app.storage.client["buttons_dic"][k] = button
                    else:
                        # 不能激活的节点，即使前面曾经激活过并选填过内容，也要清理掉
                        app.storage.client["config_data"]["data"][k]["user_must_out"] = {}
                        app.storage.client["config_data"]["data"][k]["option_tolerance_out"] = {}
                        # 清空缩略图引用记录字典里，与当前失效问题有关的记录
                        if app.storage.client["config_data"]["data"][k]["ref_out"]:
                            for ref_num, que_li in app.storage.client["ref_question_dic"].items():
                                for q in que_li:
                                    if q[0] == k:
                                        app.storage.client["ref_question_dic"][ref_num].remove([q[0], q[1]])
                        app.storage.client["config_data"]["data"][k]["ref_out"] = []
                # 将当前按钮聚焦到视图中显示
                if len(app.storage.client["buttons_dic"].values()) > index:
                    ui.run_javascript(
                        f'document.getElementById("{list(app.storage.client["buttons_dic"].values())[index].html_id}").scrollIntoView({{ behavior: "smooth" }})'
                    )
        # 更新需求问题项总数目
        app.storage.client["req_com_num"] = len(app.storage.client["buttons_dic"])
        app.storage.client["page_elements"]["circular_activ"].props["max"] = app.storage.client["req_com_num"]
        app.storage.client["page_elements"]["circular_not_activ"].props["max"] = app.storage.client["req_com_num"]
        app.storage.client["page_elements"]["circular_activ"].update()
        app.storage.client["page_elements"]["circular_not_activ"].update()
        # 只有当所有激活确认项的必填项都非空，才意味着全部填完
        button_activ_li = []
        for b_k in app.storage.client["buttons_dic"].keys():
            # 必填项存在键值对
            if data[b_k]["user_must_out"]:
                # 单选类型，必填项的值为空，意味着该项确实有有效选填内容
                if "单选" in data[b_k]["answer_type"] and data[b_k]["user_must_out"]["value"]:
                    button_activ_li.append(True)
                    app.storage.client["req_activ_num"] += 1
                # 多选类型，存在至少一个True，意味着该项确实有有效选填内容
                elif "多选" in data[b_k]["answer_type"] and any(data[b_k]["user_must_out"].values()):
                    button_activ_li.append(True)
                    app.storage.client["req_activ_num"] += 1
                # 文本输入类型，所有必填输入框均非空，意味着该项确实有完整的有效内容
                elif data[b_k]["answer_type"] in ["正整数", "单行文本", "多行文本"] and all(
                    data[b_k]["user_must_out"].values()
                ):
                    button_activ_li.append(True)
                    app.storage.client["req_activ_num"] += 1
                # 其它情况判断该项没有完成选填
                else:
                    app.storage.client["req_not_activ_num"] += 1
                    button_activ_li.append(False)
            # 连键值对都没有，意味着该项都没有展示过，判定为没有完成选填
            else:
                app.storage.client["req_not_activ_num"] += 1
                button_activ_li.append(False)

        # 全部需求项均有有效选填
        if all(button_activ_li):
            # 更改录入状态
            app.storage.client["config_data"]["entry_status"] = True
        # 否则更新录入状态为False
        else:
            app.storage.client["config_data"]["entry_status"] = False

    # 问题展示页面按钮处理函数
    def get_option(k, options_type, next):
        # 单选，包括单选项与下拉单选
        radio_bool = False
        # 多选，包括多选项与下拉多选
        checkboxe_bool = False
        # dropdown_bool = False
        input_bool = False
        # 获取当前问题的配置表键
        index = find_key_position(app.storage.client["buttons_dic"], k)

        # 获取可能的输出值
        # out_keys = list(app.storage.client["config_data"]["data"][k]["user_must_out"].keys())
        out_value = list(app.storage.client["config_data"]["data"][k]["user_must_out"].values())
        # 输入框没有出来，则字典为{}，构成的列表则为[]
        out_tolerance_value = list(app.storage.client["config_data"]["data"][k]["option_tolerance_out"].values())

        # 单选或下拉单选，用户没选择键值对为："value": None; 用户选择了则为："value": "设定值"
        # 本次处理的是单选，且内容非空，说明是单选项且做了选择
        if (
            options_type in ["单选", "下拉单选"]
            and app.storage.client["config_data"]["data"][k]["user_must_out"]["value"] is not None
        ):
            radio_bool = True

        # 多选，且用户做出勾选了其中某个选项
        elif options_type == "多选" and True in out_value:
            checkboxe_bool = True

        # 本次处理的是输入框
        elif (
            options_type
            in [
                "正整数",
                "单行文本",
                "多行文本",
            ]  # 不能省略，省略在上面条件不成立情况下，会导致直接执行v.strip()报某种类型无.strip方法的错误
            and all(v.strip() != "" for v in out_value)
            and all(w.strip() != "" for w in out_tolerance_value)
        ) and (
            (options_type == "正整数" and all(v.isdigit() for v in out_value) and all(int(v) != 0 for v in out_value))
            or (options_type in ["单行文本", "多行文本"])
        ):
            input_bool = True

        # 以上必填项没有任意一项有填写则弹出提醒，禁止进入下一道确认项，但允许返回
        if not (radio_bool or checkboxe_bool or input_bool) and next == 1:
            ui.notify(
                "请选填",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return
        # 禁止从第一道倒退回最后一道确认项
        if index == 0 and next == -1:
            ui.notify(
                "这已经是第一个问题了",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return

        index += next
        # 更新问题列表
        set_question_list(index)

        # 判断是否为最后一道确认项
        if index == len(app.storage.client["buttons_dic"].keys()):
            ui.notify(
                "这是最后一个问题，检查所有问题都选填后即可提交需求。",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        # 不是最后一道确认项
        else:
            new_k = list(app.storage.client["buttons_dic"].keys())[index]

            question_display(None, new_k)  # 触发点击事件

    # 问题内容展示函数
    def question_display(event, k):
        app.storage.client["current_question_num"] = k
        # 获取当前问题的配置表键
        index = find_key_position(app.storage.client["buttons_dic"], k)
        # 更新问题列表,重复更新是为了让所有按钮恢复应该的禁用状态
        set_question_list(index)
        # 目标确认项对应的列表按钮更新为可点击状态
        app.storage.client["buttons_dic"][k].classes("bg-amber-1").props(remove="disabled")  # 启用按钮
        # --- 修改开始 ---
        # 从 user storage 中获取当前活跃的 question_column
        # 而不是使用闭包捕获的旧变量
        current_question_column = app.storage.client["page_elements"].get("question_column")
        if not current_question_column:
            ui.notify(
                "无法找到问题显示区域，请刷新页面重试。",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return
        # --- 修改结束 ---
        question = app.storage.client["config_data"]["data"][k]["guide_content"]
        option_hint = app.storage.client["config_data"]["data"][k]["option_hint"]
        options_type = app.storage.client["config_data"]["data"][k]["answer_type"]
        options_list = app.storage.client["config_data"]["data"][k]["options"]
        ref_config_bool = True if app.storage.client["config_data"]["data"][k]["ref_config"] == "True" else False
        # user_out_list = []
        # 清空元素的子元素
        current_question_column.clear()
        with current_question_column:
            ui.label(question).classes("text-3xl text-black")
            # 需求填写提示
            # ui.label(option_hint).classes("text-sm/6 text-brown-6 max-w-2/3 whitespace-pre-wrap")
            if option_hint.strip():
                with ui.row().classes(
                    "w-full bg-blue-50 border-l-4 border-blue-400 p-3 my-2 rounded-r shadow-sm items-start gap-3"
                ):
                    # 左侧图标
                    ui.icon("lightbulb", color="blue").classes("text-xl mt-0.5")
                    # 右侧文字容器
                    with ui.column().classes("gap-1 flex-1"):
                        ui.label("填写说明:").classes("text-sm font-bold text-blue-800")
                        ui.label(option_hint).classes("text-sm text-gray-700 leading-relaxed whitespace-pre-wrap")
            # === 新增代码开始：显示失效的旧数据快照 ===
            old_data_ref = app.storage.client["config_data"]["data"][k].get("ref_old_data")
            if old_data_ref:
                with ui.card().classes("bg-amber-50 border-l-4 border-amber-500 p-3 mb-2 shadow-sm"):
                    with ui.row().classes("items-center mb-1"):
                        ui.icon("warning", color="amber-9").classes("text-xl mr-2")
                        ui.label("注意：配置项结构已升级，旧数据已失效，请参考原内容重新选填：").classes(
                            "text-amber-9 font-bold text-sm"
                        )

                    # 格式化显示旧数据内容
                    main_val = old_data_ref.get("main", {})
                    tol_val = old_data_ref.get("tolerance", {})
                    ref_val = old_data_ref.get("ref", [])

                    with ui.column().classes("ml-7 gap-1"):
                        if isinstance(main_val, dict):
                            # 单选 {"value": "xxx"}
                            if "value" in main_val:
                                if main_val["value"]:
                                    ui.label(f"原选择: {main_val['value']}").classes("text-gray-700 text-xs font-mono")
                            # 多选/输入类
                            else:
                                vals = []
                                for vk, v in main_val.items():
                                    # 多选类
                                    if isinstance(v, bool) and v:
                                        vals.append(f"{vk}: √")
                                    # 输入类
                                    else:
                                        vals.append(f"{vk}: {v}")
                                if vals:
                                    ui.label(f"原内容: {'; '.join(vals)}").classes("text-gray-700 text-xs font-mono")

                        if isinstance(tol_val, dict) and tol_val:
                            vals = [f"{k}: {v}" for k, v in tol_val.items() if v]
                            if vals:
                                ui.label(f"原公差: {'; '.join(vals)}").classes("text-gray-500 text-xs font-mono")

                        if isinstance(ref_val, list) and ref_val:
                            ui.label(f"原引用文件编号: {'; '.join(ref_val)}").classes("text-gray-500 text-xs font-mono")
            # === 新增代码结束 ===

            with ui.column().classes("m-0 gap-8 w-full items-center justify-start overflow-auto"):
                if options_type == "单选":
                    radio_dic = {}
                    for op_dic in options_list:
                        radio_dic[op_dic["option_out"]] = op_dic["option_content"]
                    # 创建单选按钮 options:	a list ['value1', ...] or dictionary {'value1':'label1', ...} specifying the options
                    radio = ui.radio(radio_dic).classes("").props("inline")
                    radio.bind_value(app.storage.client["config_data"]["data"][k]["user_must_out"], "value")

                elif options_type == "多选":
                    with ui.row().classes("items-start justify-center w-full"):
                        for op_dic in options_list:
                            # 创建复选框
                            checkbox = ui.checkbox(op_dic["option_content"]).classes("")
                            # 绑定复选框的值到列表
                            checkbox.bind_value(
                                app.storage.client["config_data"]["data"][k]["user_must_out"], op_dic["option_out"]
                            )

                elif options_type == "下拉单选":
                    dropdown_dic = {}
                    for op_dic in options_list:
                        dropdown_dic[op_dic["option_out"]] = op_dic["option_content"]
                    # 创建下拉选择框
                    dropdown = ui.select(dropdown_dic).classes("w-1/6 text-base")
                    # dropdown.bind_value(selected_dropdown_dic)
                    dropdown.bind_value(app.storage.client["config_data"]["data"][k]["user_must_out"], "value")

                elif options_type in ["正整数", "单行文本", "多行文本"]:
                    placeholder = app.storage.client["config_data"]["data"][k]["placeholder"]
                    tolerance_placeholder = app.storage.client["config_data"]["data"][k]["tolerance_placeholder"]
                    # 获取公差要求
                    input_tolerance_bool = app.storage.client["config_data"]["data"][k]["input_tolerance"]

                    input_num, input_name_dic, current_target_keys = sync_input_output_keys(
                        app.storage.client["config_data"]["data"], k
                    )

                    # ===【新增代码开始】：检测并显示“隐形/孤儿”数据 ===
                    # 1. 获取当前页面要求的有效键列表
                    # ... (下面是原有的检测孤儿数据的逻辑，可以保留，但因为上面已经做了迁移，
                    #      这里主要会检测到那种被彻底删减掉的数据，体验会更好) ...
                    active_keys = (
                        current_target_keys  # 直接复用上面的变量  客户来回改数量，导致前面填的多出来了，要截取
                    )

                    # 2. 获取存储里的所有键
                    stored_data = app.storage.client["config_data"]["data"][k]["user_must_out"]
                    stored_keys = list(stored_data.keys())

                    # 3. 找出“存储里有，但当前界面没用到”的键 (即失效的旧键名)
                    orphaned_items = {}
                    for sk in stored_keys:
                        # 注意：stored_data 中的 value 可能为空字符串，这种不算有效丢失
                        if sk not in active_keys and str(stored_data[sk]).strip() != "":
                            orphaned_items[sk] = stored_data[sk]

                    # 4. 如果发现孤儿数据，且没有结构性快照(ref_old_data)，则显示黄色警告框
                    # (如果有 ref_old_data，说明已经强制重置了，那边会显示，这里不用重复)
                    if orphaned_items and not app.storage.client["config_data"]["data"][k].get("ref_old_data"):
                        with ui.card().classes("w-full bg-amber-50 border-l-4 border-amber-500 p-3 mb-2 shadow-sm"):
                            with ui.row().classes("items-center mb-1"):
                                ui.icon("link_off", color="amber-9").classes("text-xl mr-2")
                                ui.label("检测到关联项变更，以下旧数据已失效，请确认是否需要迁移：").classes(
                                    "text-amber-9 font-bold text-sm"
                                )

                            with ui.column().classes("ml-7 gap-1"):
                                vals = [f"{k}: {v}" for k, v in orphaned_items.items()]
                                ui.label(f"失效内容: {', '.join(vals)}").classes(
                                    "text-gray-700 text-xs font-mono bg-white px-1 rounded border border-gray-200"
                                )
                    # ===【新增代码结束】===

                    # 获取可能的已有用户输入内容
                    with ui.column().classes("min-w-1/3 -space-y-2"):
                        for n in range(input_num):
                            with ui.row().classes("justify-center flex-nowrap items-stretch w-full"):
                                # 可能是数字123也可能是前置依赖的客户输出识别字符串
                                input_label_key = list(input_name_dic.values())[n]

                                label_1 = "内容"
                                label_2 = ""
                                if input_tolerance_bool == "正负":
                                    label_1 = "典型值"
                                    label_2 = "正负公差范围"
                                elif input_tolerance_bool == "范围":
                                    label_1 = "下限值"
                                    label_2 = "上限值"
                                elif input_tolerance_bool == "下限":
                                    label_1 = "下限值"
                                elif input_tolerance_bool == "上限":
                                    label_1 = "上限值"

                                # 编辑配置好输入框标签内容
                                if input_label_key.isdigit():
                                    input_label = f"项{input_label_key}的{label_1}:"
                                    input_tolerance_label = f"项{input_label_key}的{label_2}:"
                                else:
                                    input_label = f"{input_label_key}的{label_1}:"
                                    input_tolerance_label = f"{input_label_key}的{label_2}:"

                                # 处理正整数输入框
                                if options_type == "正整数":
                                    input_field = (
                                        ui.input(
                                            label=input_label,
                                            placeholder=placeholder,
                                            validation={"必须是整数": lambda value: value.isdigit()},
                                        )
                                        .props("outlined stack-label")
                                        .classes("text-[14px]/[16px] w-full")
                                    )
                                    input_field.bind_value(
                                        app.storage.client["config_data"]["data"][k]["user_must_out"], input_label_key
                                    )
                                    if input_tolerance_bool in ["正负", "范围"]:
                                        input_tolerance = (
                                            ui.input(
                                                label=input_tolerance_label,
                                                placeholder=tolerance_placeholder,
                                                validation={"不能空白": lambda value: value.strip() != ""},
                                            )
                                            .props("outlined stack-label")
                                            .classes("text-[14px]/[16px] w-full")
                                        )
                                        input_tolerance.bind_value(
                                            app.storage.client["config_data"]["data"][k]["option_tolerance_out"],
                                            input_label_key,
                                        )
                                # 处理单行文本输入框
                                elif options_type == "单行文本":
                                    input_field = (
                                        ui.input(
                                            label=input_label,
                                            placeholder=placeholder,
                                            validation={"不能空白": lambda value: value.strip() != ""},
                                        )
                                        .props("outlined stack-label")
                                        .classes("text-[14px]/[16px] w-full")
                                    )
                                    input_field.bind_value(
                                        app.storage.client["config_data"]["data"][k]["user_must_out"], input_label_key
                                    )
                                    if input_tolerance_bool in ["正负", "范围"]:
                                        input_tolerance = (
                                            ui.input(
                                                label=input_tolerance_label,
                                                placeholder=tolerance_placeholder,
                                                validation={"不能空白": lambda value: value.strip() != ""},
                                            )
                                            .props("outlined stack-label")
                                            .classes("w-full text-[14px]/[16px]")
                                        )
                                        input_tolerance.bind_value(
                                            app.storage.client["config_data"]["data"][k]["option_tolerance_out"],
                                            input_label_key,
                                        )
                                # 处理多行文本输入框，多行文本不处理公差范围.
                                elif options_type == "多行文本":
                                    input_field = (
                                        ui.textarea(
                                            label=input_label,
                                            placeholder=placeholder,
                                            validation={"不能空白": lambda value: value.strip() != ""},
                                        )
                                        .props("outlined stack-label autogrow")
                                        .classes("w-full text-[14px]/[16px]")
                                    )
                                    input_field.bind_value(
                                        app.storage.client["config_data"]["data"][k]["user_must_out"], input_label_key
                                    )
                                    if input_tolerance_bool in ["正负", "范围"]:
                                        input_tolerance = (
                                            ui.textarea(
                                                label=input_tolerance_label,
                                                placeholder=tolerance_placeholder,
                                                validation={"不能空白": lambda value: value.strip() != ""},
                                            )
                                            .props("outlined stack-label autogrow")
                                            .classes("w-full text-[14px]/[16px]")
                                        )
                                        input_tolerance.bind_value(
                                            app.storage.client["config_data"]["data"][k]["option_tolerance_out"],
                                            input_label_key,
                                        )
                # 处理需要插入引用的确认项
                if ref_config_bool:
                    with ui.row().classes("gap-1 w-full justify-center"):
                        with ui.column().classes(
                            "w-1/4 h-fit -space-y-5 border-2 border-solid border-Gray-500 rounded-md"
                        ):
                            ui.label("引用：").classes("p-1 text-sm text-gray-500")
                            ref_row = ui.row().classes("space-x-0 p-2")

                            if app.storage.client["config_data"]["data"][k]["ref_out"]:
                                # 将需求data里的已有引用数字生成数字按钮
                                for t_lab in app.storage.client["config_data"]["data"][k]["ref_out"]:
                                    add_ref_button(
                                        app.storage.client["file_thumbnail_dic"][t_lab]["file_obj"],
                                        ref_row,
                                        k,
                                        question,
                                        False,
                                    )
                        ui.button(
                            on_click=lambda ref_row=ref_row, question_k=k, question=question: add_activ_ref(
                                ref_row, question_k, question
                            )
                        ).props('icon-right="add_link"').classes("h-full p-2")
                        ui.button(
                            on_click=lambda ref_row=ref_row, question_k=k, question=question: add_del_lab(
                                ref_row, question_k, question
                            )
                        ).props('icon-right="link_off"').props().classes("h-full p-2 bg-blue-grey-8")

            # 确认项“确认”与“返回”按钮
            # with ui.button_group().props("push").classes("absolute bottom-0 right-2"):
            ui.button(
                # "上一个",
                icon="arrow_back_ios",
                color="amber-8",
                on_click=lambda kk=k: get_option(kk, options_type, -1),
            ).props("flat").classes("absolute top-1/2 left-2 -translate-y-1/2 h-1/2 text-xl")
            ui.button(
                # "下一个",
                icon="arrow_forward_ios",
                color="green-8",
                on_click=lambda kk=k: get_option(kk, options_type, 1),
            ).props("flat").classes("absolute top-1/2 right-2 -translate-y-1/2 h-1/2 text-xl")

    # 刷新需求录入界面文件缩略图显示区域函数
    def req_thumbnail_display():
        # 从 user storage 中获取当前活跃的 img_row
        # 而不是使用闭包捕获的旧变量
        current_img_row = app.storage.client["page_elements"].get("img_row")
        if not current_img_row:
            ui.notify(
                "无法找到文件缩略图显示区域，请刷新页面重试。",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return
        current_img_row.clear()
        with current_img_row:
            if app.storage.client["file_thumbnail_dic"]:
                for file_data in app.storage.client["file_thumbnail_dic"].values():
                    if not file_data["file_information"]["file_del_bool"]:
                        file_data["file_obj"].get_thumbnail()

    def update_new_data_in_place(old_data: dict, new_data: dict) -> tuple:
        """
        根据 old_data 的内容，就地更新 new_data 字典。

        函数逻辑:
        1. 遍历 new_data['file_dic'] 中的每个文件条目。
        2. 如果文件在 old_data 中存在冲突（key 相同但内容不同），则为其分配一个新 ID。
        3. 新 ID 基于 old_data['file_counter'] 的递增值。
        4. **直接修改 new_data['file_dic']**：
        - 删除旧的数字键条目。
        - 以新 ID 为键，添加更新后的文件条目。
        - **更新文件条目字典内部的 'file_lab' 键值为新 ID**。
        5. 记录所有 ID 变更的映射关系 (old_key -> new_key)。
        6. **直接修改 new_data['data']**：使用映射关系更新 'ref_out' 列表中的文件引用。
        7. 函数返回修改后的 new_data 字典。

        Args:
            old_data (dict): 用于比对和提供文件计数器起点的旧字典。
            new_data (dict): 将被就地修改的新字典。

        Returns:
            tuple: 经过就地修改后的 new_data 字典和迭代后的file_counter。
        """
        # 从旧字典获取文件计数器起点和文件列表
        file_counter = old_data.get("file_counter", 0)
        old_file_dic = old_data.get("file_dic", {})

        # 直接操作新字典的文件列表
        new_file_dic = new_data.get("file_dic", {})
        if not new_file_dic:
            return (new_data, file_counter)  # 如果没有文件，则无需处理

        key_map = {}  # 用于存储旧键到新键的映射
        temp_file_dic = copy.deepcopy(new_file_dic)

        # 遍历键的列表副本，因为我们将在循环中修改字典本身
        for original_key in list(temp_file_dic.keys()):
            file_info = temp_file_dic[original_key]

            is_identical = False
            # 检查 key 是否存在于旧字典中且内容一致
            if original_key in old_file_dic:
                _temp_new = copy.deepcopy(file_info)
                del _temp_new["file_del_bool"]
                _temp_old = copy.deepcopy(old_file_dic[original_key])
                del _temp_old["file_del_bool"]
                if _temp_new == _temp_old:
                    is_identical = True

            if is_identical:
                # 内容一致，无需修改，键映射保持不变
                key_map[original_key] = original_key
                continue

            # 如果 key 在旧字典中不存在，或者存在但内容不一致（冲突），则分配新 key
            file_counter += 1
            new_key = str(file_counter)

            # 记录键的映射关系
            key_map[original_key] = new_key

            # 核心步骤：就地修改 new_data['file_dic']
            # 1. 首先更新文件条目字典内部的 "file_lab"
            file_info["file_lab"] = new_key

            # 2. 然后从字典中移除旧键的条目
            entry_to_move = temp_file_dic.pop(original_key)
            if int(original_key) <= old_data.get("file_counter", 0):
                del new_file_dic[original_key]
            # 3. 使用新键重新插入该条目
            new_file_dic[new_key] = entry_to_move

        # 就地更新 new_data['data'] 部分中的文件引用
        data_section = new_data.get("data", {})
        for node_content in data_section.values():
            if "ref_out" in node_content and isinstance(node_content.get("ref_out"), list):
                # 使用列表推导式和 key_map 来创建更新后的引用列表
                updated_ref_out = [key_map.get(ref, ref) for ref in node_content["ref_out"]]
                node_content["ref_out"] = updated_ref_out

        return (new_data, file_counter)

    # def loads_change_name_out_config(data, type):
    #     project_name = app.storage.client["project_name"].strip()
    #     target_project_name = app.storage.client["target_project_name"].strip()
    #     version = app.storage.client["version"]
    #     version_str_li = version.split(".")
    #     #  改了项目名 且
    #     if project_name != target_project_name and int(version_str_li[1]) != 0:

    # [新增一个删除辅助函数，放在 output_config_data 内部或者外部均可]
    def clean_autosave_file(project_name):
        try:
            # 构造自动保存的文件路径
            autosave_path = os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_AUTOSAVE.json")
            if os.path.exists(autosave_path):
                os.remove(autosave_path)
                logger.info(f"清理临时草稿文件: {autosave_path}")
        except Exception as e:
            logger.error(f"清理草稿失败: {e}")

    # 需求数据输出处理函数
    async def output_config_data(data, type):
        # [新增安全锁检查] 如果是自动保存，且开关未开启，直接中止
        if type == "autosave" and not app.storage.client.get("allow_autosave", False):
            # 可选：打印日志方便调试
            # logger.debug("自动保存已跳过：等待用户确认加载状态")
            return
        # 先复制整个数据
        data_json = data

        # === 提交/导出/暂存前同步输入类题目的动态 key ===
        # 名称依据变更时先按索引迁移旧答案，再自然丢弃超出当前数量的旧项
        for k, v in data_json["data"].items():
            if v["answer_type"] in ["正整数", "单行文本", "多行文本"]:
                sync_input_output_keys(data_json["data"], k)

        project_name = app.storage.client["project_name"].strip()
        version = app.storage.client["version"]
        target_project_name = app.storage.client["target_project_name"].strip()
        # target_version = app.storage.client["target_version"].strip()
        original_project = app.storage.client["original_project"]
        original_version = app.storage.client["original_version"]
        version_str_li = version.split(".")

        # 如果目标项目名只有RFTS，则需要推算临时项目的顺延项目号
        new_temp_project_bool = False
        if type == "submit" and target_project_name == "RFTS":
            # 存在临时项目了
            if app.storage.general.get("temp_project_name", []):
                # 找到临时项目号存在的最大值
                project_num_max = max([int(k.split("-")[1]) for k in app.storage.general.get("temp_project_name", [])])
                # 如果加一后的项目号长度短于常量设置值，则在其左边用0字符补充到指定长度
                target_project_name = f"RFTS-{str(project_num_max + 1).rjust(TEMP_PROJECT_NUM_LENGTH, '0')}"
                # 先占个位置
                app.storage.general["temp_project_name"].append(target_project_name)
            # 不存在临时项目
            else:
                target_project_name = "RFTS-0001"
                # 先占个位置
                app.storage.general["temp_project_name"].append(target_project_name)
            # 如果当前项目名与目标项目名一样都是RFTS，则需要同步更新，防止后续按照衍生项目情况处理
            if project_name == "RFTS":
                project_name = target_project_name
            new_temp_project_bool = True

        file_dic = {}
        for k, v in app.storage.client["file_thumbnail_dic"].items():
            file_dic[k] = v["file_information"]
        data_json["file_dic"] = file_dic
        data_json["file_counter"] = app.storage.client["file_counter"]
        data_json["files"] = app.storage.client["files"]
        data_json["deleted_files"] = app.storage.client["deleted_files"]
        data_json["current_user"] = current_user

        # 参照项目的审核状态
        original_review_state = ""
        # 参照项目的提交人
        original_submitter = ""
        target_review_state = ""
        if app.storage.general["wait_review"].get(project_name, {}):
            original_review_state = app.storage.general["wait_review"][project_name].get(
                f"{version.split('.')[0]}.0", {"state": ""}
            )["state"]
            original_submitter = app.storage.general["wait_review"][project_name].get(
                f"{version.split('.')[0]}.0", {"submitter": ""}
            )["submitter"]
        if app.storage.general["wait_review"].get(target_project_name, {}):
            if app.storage.general["wait_review"][target_project_name].keys():
                ver_max = (
                    f"{int(max([float(v) for v in app.storage.general['wait_review'][target_project_name].keys()]))}.0"
                )
                target_review_state = app.storage.general["wait_review"][target_project_name].get(
                    ver_max, {"state": ""}
                )["state"]
        # 当前需求待审状态，导出和提交均会被阻止
        # 当前需求已审、查不到（初次、导出版本上再导出提交）状态的，可导出可提交
        # 当前需求待修改状态，可提交
        # 当前项目已审，可正常更新
        if original_review_state == "已审":
            # 记录项目名
            data_json["project_name"] = target_project_name  # 该项操作提交正常生效、导出时会被覆盖掉不起效
            # 记录参照当前版本
            data_json["original_version"] = version  # 已审项目，提交时无论升版本还是衍生其它项目，当前版本作为参照版本
            # 参照项目名为当前项目名
            data_json["original_project"] = (
                project_name  # 已审项目，提交时无论升版本还是衍生其它项目，当前项目名作为参照项目名
            )
        # 待修改，不动作就保持了原有数据；
        elif original_review_state == "待修改":
            # 记录项目名
            data_json["project_name"] = project_name  # 待修改项目，项目名不改
            # 记录参照当前版本
            data_json["original_version"] = original_version  # 待修改项目，参照版本不迭代，照抄原来
            # 项目名相当于没变，接着记录
            data_json["original_project"] = original_project  # 待修改项目，参照项目名不迭代，照抄原来
        # 查不到待审状态（初次、导出版本上再导出提交）,及其它状态，待审后面拦截不能导出和提交
        # 项目名可迭代，参照信息不迭代
        else:
            # 记录项目名
            data_json["project_name"] = target_project_name  # 该项操作导出时会被覆盖掉，不起效
            # 初版或导出版本上输出，均保持参照版本记录不变
            data_json["original_version"] = original_version
            # 初版或导出版本上输出，均保持参照项目名记录不变
            data_json["original_project"] = original_project

        # 输出类型为导出到本地，导出不修改名称（目标项目名不起效），只迭代小数点后版本，更新时间戳
        if type == "export":
            if original_review_state == "待修改" and original_submitter != current_user:
                ui.notify(
                    "参照需求处于待修改状态，且当前用户不是参照需求提交人，禁止暂存！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return
            # 禁止待审、待修改需求导出
            if original_review_state == "待审":
                ui.notify(
                    "参照需求处于待审状态，禁止暂存！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return
            if project_name.split("-")[0] == "RFTS" and not validate_format_regex(project_name, r"^RFTS-\d{4}$"):
                ui.notify(
                    "不符合临时项目号命名规则：RFTS-4位数字！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return

            # 一旦校验通过，开始暂存流程，立即禁止后续的自动保存，防止竞态条件
            app.storage.client["allow_autosave"] = False

            # 记录衍生版本
            # 如果不等，则以为这改名时将当前版本改成改后名的最高版本了，这里参照版本得用真真参照的版本
            # if original_version != version:
            #     data_json["original_version"] = original_version
            # else:
            #     data_json["original_version"] = version
            # 将文件版本的小数点位加1
            version_a_str = version_str_li[0]
            # 注意出现3.11比3.2版本浮点数小，但是实际版本更高的影响
            version_b_str = str(int(version_str_li[1]) + 1)
            new_version = f"{version_a_str}.{version_b_str}"
            app.storage.client["version"] = new_version
            data_json["version"] = new_version
            # 导出数据不迭代项目名称
            data_json["project_name"] = project_name
            # 导出时加入或更新时间戳
            data_json["req_timestamp"] = datetime.now().isoformat()
            # 1. 将字典转换为 JSON 字符串
            json_str = json.dumps(data_json, indent=4, ensure_ascii=False)

            # ------------------------------------原导出下载到本地的功能代码----------------------------------
            # 2. 生成 JavaScript 下载代码
            # js_code = f"""
            #     const blob = new Blob([{json.dumps(json_str)}], {{ type: 'application/json' }});
            #     const url = URL.createObjectURL(blob);
            #     const a = document.createElement('a');
            #     a.href = url;
            #     a.download = 'data.json';  // 下载文件名
            #     document.body.appendChild(a);
            #     a.click();
            #     document.body.removeChild(a);
            #     URL.revokeObjectURL(url);
            # """
            # 3. 执行 JavaScript
            # ui.run_javascript(js_code)
            # ui.notify(
            #         f"需求已导出，版本已迭代到: V{version}，且导出时不会更改项目名称。",
            #         type="positive",
            #         position="bottom",
            #         timeout=1000,
            #         progress=True,
            #         close_button="✖",
            #     )
            # -----------------------------------------------------------------------------------------------
            # 写入文件
            file_path = os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_需求配置_V{new_version}.json")
            try:
                # 这一行代码即可完成：检查 + 递归创建 + 忽略已存在错误
                Path(os.path.join(REQ_DIR, f"temp/{current_user}")).mkdir(parents=True, exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json_str)

                # 将该需求版本标记到待审字典里
                if not app.storage.general["temp_req"].get(current_user, {}):
                    app.storage.general["temp_req"][current_user] = {}
                if not app.storage.general["temp_req"][current_user].get(project_name, []):
                    app.storage.general["temp_req"][current_user][project_name] = []
                app.storage.general["temp_req"][current_user][project_name].append(new_version)
                app.storage.general["temp_req"][current_user][project_name] = sorted(
                    set(app.storage.general["temp_req"][current_user][project_name])
                )

                # 删除可能存在的自动保存需求文件
                clean_autosave_file(project_name)

                logger.info(f"成功暂存需求配置：{project_name}_需求配置_V{new_version}.json")
                ui.notify(
                    f"需求已暂存，版本迭代到: V{new_version}，待办项已增加相应记录。",
                    type="positive",
                    position="center",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 新增：跳转前取消定时器
                if "autosave_timer" in app.storage.client:
                    app.storage.client["autosave_timer"].cancel()
                # 不传入项目名，就不会识别个人自动保存的需求文件
                ui.timer(
                    1,
                    callback=lambda: ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}"),
                    once=True,
                )

            except Exception as e:
                logger.error("暂存项目需求时发生其他错误", exc_info=True)
                ui.notify(
                    f"暂存项目需求资料出错：{e}",
                    type="negative",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                app.storage.client["allow_autosave"] = True

        # [新增] >>> 在 export 分支之前或之后插入 autosave 分支 <<<
        elif type == "autosave":
            # 如下情况，这里就没必要自动保存了
            if (
                # 用户没权限配置需求
                current_role not in ["销售", "销售总监", "admin"]
                # 参考需求待审，后面不能暂存或提交，这里没必要自动保存
                or original_review_state == "待审"
                # 参考需求待修改且当前用户无权修改，后面不能暂存或提交，这里没必要自动保存
                or original_review_state == "待修改"
                and original_submitter != current_user
                # 临时项目命名有问题，后面不能暂存或提交，这里没必要自动保存
                or project_name.split("-")[0] == "RFTS"
                and not validate_format_regex(project_name, r"^RFTS-\d{4}$")
                # 非临时项且不在正式项目名称列表里，后面不能暂存或提交，这里没必要自动保存
                or target_project_name.split("-")[0] != "RFTS"
                and target_project_name not in app.storage.general["project_summary"]
            ):
                return
            # 1. 保持当前版本号和项目名不变
            data_json["project_name"] = project_name
            data_json["version"] = version
            # 2. 更新时间戳
            data_json["req_timestamp"] = datetime.now().isoformat()

            # 3. 固定文件名后缀，覆盖保存 (例如: RFTS-0001_AUTOSAVE.json)
            # 这样不会产生无数个垃圾文件，永远只有一份最新的草稿
            file_path = os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_AUTOSAVE.json")

            try:
                Path(os.path.join(REQ_DIR, f"temp/{current_user}")).mkdir(parents=True, exist_ok=True)
                json_str = json.dumps(data_json, indent=4, ensure_ascii=False)
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json_str)
                # 静默成功，仅在后台打印日志，不弹窗打扰用户
                # logger.info(f"Auto-save success: {file_path}")
            except Exception as e:
                logger.error(f"自动保存失败: {e}")

        # 输出类型为提交到服务器
        elif type == "submit":
            if target_project_name == "":
                ui.notify(
                    "提交需求必须给项目命名！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 没有填写名字，更不会占用临时项目号，不用处理
                # 如果属于新创建临时项目，失败则删除占位的临时项目号
                # if new_temp_project_bool:
                #     app.storage.general["temp_project_name"].remove(target_project_name)
                return
            if current_role not in ["销售", "销售总监", "admin"]:
                ui.notify(
                    "当前用户无权限提交需求！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 如果属于新创建临时项目，失败则删除占位的临时项目号
                if new_temp_project_bool:
                    app.storage.general["temp_project_name"].remove(target_project_name)
                return
            if (
                target_project_name.split("-")[0] != "RFTS"
                and target_project_name not in app.storage.general["project_summary"]
            ):
                ui.notify(
                    "非临时项目，又未正式立项，不可提交服务器，只可暂存！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 不是临时项目，更不会占用临时项目号，不用处理
                # 如果属于新创建临时项目，失败则删除占位的临时项目号
                # if new_temp_project_bool:
                #     app.storage.general["temp_project_name"].remove(target_project_name)
                return
            # print(target_project_name)
            if target_project_name.split("-")[0] == "RFTS" and not validate_format_regex(
                target_project_name, r"^RFTS-\d{4}$"
            ):
                ui.notify(
                    "不符合临时项目号命名规则：RFTS-4位数字！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 既然已经是想升级临时项目，则不是单单RFTS，不会占位临时项目号，不用这个处理
                # 如果属于新创建临时项目，失败则删除占位的临时项目号
                # if new_temp_project_bool:
                #     app.storage.general["temp_project_name"].remove(target_project_name)
                return
            if original_review_state == "待修改":
                if current_user != original_submitter:
                    ui.notify(
                        "参照项目的需求处于待修改状态，只有原提交人能修改，禁止提交！",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                    # 如果属于新创建临时项目，失败则删除占位的临时项目号
                    if new_temp_project_bool:
                        app.storage.general["temp_project_name"].remove(target_project_name)
                    return
                elif project_name != target_project_name:
                    ui.notify(
                        "参照项目的需求处于待修改状态，禁止衍生成新项目！",
                        type="warning",
                        position="bottom",
                        timeout=3000,
                        progress=True,
                        close_button="✖",
                    )
                    # 如果属于新创建临时项目，失败则删除占位的临时项目号
                    if new_temp_project_bool:
                        app.storage.general["temp_project_name"].remove(target_project_name)
                    return
            # 如果最近一次需求配置文件还处于未审状态，本次需求还不能提交
            if original_review_state == "待审":
                ui.notify(
                    "参照项目的需求处于待审状态，禁止提交！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 如果属于新创建临时项目，失败则删除占位的临时项目号
                if new_temp_project_bool:
                    app.storage.general["temp_project_name"].remove(target_project_name)
                return
            if target_review_state == "待审":
                ui.notify(
                    "目标项目的需求处于待审状态，禁止升级版本！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 如果属于新创建临时项目，失败则删除占位的临时项目号
                if new_temp_project_bool:
                    app.storage.general["temp_project_name"].remove(target_project_name)
                return

            # 一旦通过校验，开始提交流程，立即禁止后续的自动保存，防止竞态条件
            app.storage.client["allow_autosave"] = False

            change_name = False
            #  改了项目名
            if project_name != target_project_name:
                change_name = True

            if data_json["entry_status"]:
                new_version = version
                # 迭代更新版本
                version_a = int(version_str_li[0])

                # 查找指定路径下，含有提供项目名的文件，得到一个字典，完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
                project_exists_file = find_files_with_prefix_and_version(REQ_DIR, target_project_name)
                # 服务器存在该项目配置，则需要升级版本
                if project_exists_file:
                    v_max = max([float(s) for s in project_exists_file.keys()])
                    # 当前版本比服务器最高版本低 或 由其它项目衍生过来的，均按照本项目服务器最高版本来+1保存
                    # if float(version) < v_max or change_name:
                    version_a = int(project_exists_file[str(v_max)]["v_a"])
                    # 已审状态才能升级版本, 待修改不升级
                    if target_review_state in ["已审", ""]:
                        new_version = f"{version_a + 1}.0"
                    # 防止修改需求暂存后提交时，版本迭代小数而提交，
                    else:
                        new_version = f"{version_a}.0"

                    # 获取旧版最高版需求文件数据
                    old_data_path = os.path.join(REQ_DIR, project_exists_file[str(v_max)]["name"])
                    try:
                        with open(old_data_path, "r", encoding="utf-8") as f:
                            # 使用 json.load() 读取文件内容并解析
                            old_data = json.load(f)

                            # 处理新需求插入文件数字可能的与旧版本需求的冲突
                            return_tuple = update_new_data_in_place(old_data, data_json)
                            data_json = return_tuple[0]
                            data_json["file_counter"] = return_tuple[1]
                    except json.JSONDecodeError:
                        ui.notify(
                            f"项目旧版本需求文件查阅失败！错误：文件 '{old_data_path}' 不是有效的 JSON 格式。",
                            type="negative",
                            position="center",
                            timeout=0,
                            progress=False,
                            close_button="✖",
                        )
                        # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                        app.storage.client["allow_autosave"] = True
                        return
                    except Exception as e:
                        ui.notify(
                            f"项目旧版本需求文件查阅失败！错误：读取文件时发生其他错误：{e}",
                            type="negative",
                            position="center",
                            timeout=0,
                            progress=False,
                            close_button="✖",
                        )
                        # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                        app.storage.client["allow_autosave"] = True
                        return

                # 服务器不存在该项目配置文件
                else:
                    # 刚刚改了项目名，且不是导入需求后再次改名（这种情况应该复制参考项目的需求，而不是当前项目需求），临时项目与正式项目均先复制参考的项目需求
                    if change_name:
                        # 查阅服务器需求改名直接提交衍生新项目
                        # if int(version_str_li[1]) == 0:
                        copy_project_name = project_name
                        copy_version = version
                        # 导入外部需求改名提交衍生新项目
                        # else:
                        #     copy_project_name = original_project
                        #     copy_version = original_version
                        if float(copy_version) < 1.0:
                            ui.notify(
                                "复制衍生项目需求文件失败！参照的项目版本低于1.0。",
                                type="warning",
                                position="bottom",
                                timeout=3000,
                                progress=True,
                                close_button="✖",
                            )
                            # 如果属于新创建临时项目，失败则删除占位的临时项目号
                            if new_temp_project_bool:
                                app.storage.general["temp_project_name"].remove(target_project_name)
                            # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                            app.storage.client["allow_autosave"] = True
                            return
                        # 定义文件路径
                        old_file_path = os.path.join(
                            REQ_DIR, f"{copy_project_name}_需求配置_V{copy_version.split('.')[0]}.0.json"
                        )
                        old_data_json = {}
                        try:
                            # 每次都以配置文件为准，不以服务器现有数据为准
                            # 配置更新能直接呈现，但配置减项将导致原有数据不呈现
                            with open(old_file_path, "r", encoding="utf-8") as f:
                                # 使用 json.load() 读取文件内容并解析
                                old_data_json = json.load(f)
                        except json.JSONDecodeError:
                            ui.notify(
                                f"复制衍生项目需求文件失败！错误：文件 '{old_file_path}' 不是有效的 JSON 格式。",
                                type="negative",
                                position="center",
                                timeout=0,
                                progress=False,
                                close_button="✖",
                            )
                            # 如果属于新创建临时项目，失败则删除占位的临时项目号
                            if new_temp_project_bool:
                                app.storage.general["temp_project_name"].remove(target_project_name)
                            # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                            app.storage.client["allow_autosave"] = True
                            return
                        except Exception as e:
                            ui.notify(
                                f"复制衍生项目需求文件失败！错误：读取文件时发生其他错误：{e}",
                                type="negative",
                                position="center",
                                timeout=0,
                                progress=False,
                                close_button="✖",
                            )
                            # 如果属于新创建临时项目，失败则删除占位的临时项目号
                            if new_temp_project_bool:
                                app.storage.general["temp_project_name"].remove(target_project_name)
                            # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                            app.storage.client["allow_autosave"] = True
                            return
                        old_data_json["project_name"] = target_project_name
                        old_data_json["current_user"] = current_user
                        old_data_json["original_project"] = copy_project_name
                        old_data_json["version"] = "1.0"
                        old_data_json["original_version"] = f"{copy_version.split('.')[0]}.0"
                        old_data_json["req_timestamp"] = datetime.now().isoformat()
                        # 衍生复制过来的需求，默认通过审核
                        # old_data_json["original_review_state"] = True
                        # 将该需求版本标记到待审字典里
                        app.storage.general["wait_review"][target_project_name] = {
                            "1.0": {"state": "已审", "submitter": current_user}
                        }

                        # 将字典转换为 JSON 字符串
                        old_json_str = json.dumps(old_data_json, indent=4, ensure_ascii=False)
                        # 写入文件
                        copy_file_path = os.path.join(REQ_DIR, f"{target_project_name}_需求配置_V1.0.json")
                        try:
                            with open(copy_file_path, "w", encoding="utf-8") as f:
                                f.write(old_json_str)
                            # 成功复制参照项目需求文件后，马上复制该项目概述内容
                            await copy_overview_data(
                                copy_project_name, f"{copy_version.split('.')[0]}.0", target_project_name
                            )
                            # 更新目标项目概述角色统计结果，以便第一时间在项目总表能看到统计结果和状态
                            overview_role_update(target_project_name, "all_update")

                            logger.info(f"成功复制{target_project_name}的需求配置。")
                            ui.notify(
                                "复制衍生项目需求文件概述资料成功。",
                                type="positive",
                                position="bottom",
                                timeout=1000,
                                progress=True,
                                close_button="✖",
                            )
                            # 复制旧版本概述成功后，更新客户端数据
                            app.storage.client["version"] = "1.0"
                            app.storage.client["project_name"] = target_project_name
                            app.storage.client["target_project_name"] = target_project_name
                            app.storage.client["original_project"] = target_project_name
                            app.storage.client["original_version"] = "1.0"
                            # 复制保存好旧版本临时需求配置文件后，接着处理一次
                            await output_config_data(data, type)
                            # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                            app.storage.client["allow_autosave"] = True
                            return
                        except Exception as e:
                            logger.error("复制衍生项目需求与概述时发生其他错误", exc_info=True)
                            ui.notify(
                                f"复制衍生项目需求与概述资料出错：{e}",
                                type="negative",
                                position="center",
                                timeout=0,
                                progress=False,
                                close_button="✖",
                            )
                            # 不知失败，要清掉可能生成的需求文件与概述复制内容
                            if os.path.exists(copy_file_path):
                                os.remove(copy_file_path)
                            await db_storage.set_item(f"{target_project_name}_over_data", {})
                            # 如果属于新创建临时项目，失败则删除占位的临时项目号
                            if new_temp_project_bool:
                                app.storage.general["temp_project_name"].remove(target_project_name)
                            # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                            app.storage.client["allow_autosave"] = True
                            return
                    # 排除其它项目衍生过来的情况，那种情况保持衍生的记录版本
                    else:
                        original_version = "0.0"
                        new_version = "1.0"

                # 不管服务器有没有该项目需求配置文件
                # app.storage.client["version"] = new_version
                # app.storage.client["original_version"] = version
                data_json["version"] = new_version

                # 导出时加入或更新时间戳
                data_json["req_timestamp"] = datetime.now().isoformat()
                # 定义文件路径
                file_path = os.path.join(REQ_DIR, f"{target_project_name}_需求配置_V{new_version}.json")
                # 将字典转换为 JSON 字符串
                json_str = json.dumps(data_json, indent=4, ensure_ascii=False)

                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(json_str)
                # 将该需求版本标记到待审字典里
                if not app.storage.general["wait_review"].get(target_project_name, {}):
                    app.storage.general["wait_review"][target_project_name] = {}
                app.storage.general["wait_review"][target_project_name][new_version] = {
                    "state": "待审",
                    "submitter": current_user,
                }

                # 将提交该需求的用户更新为该项目负责的销售员
                app.storage.general["project_sale"][target_project_name] = current_user

                # 删除可能存在的自动保存需求文件
                clean_autosave_file(project_name)

                logger.info(f"成功提交{target_project_name}的需求配置，版本：V{new_version}。")
                ui.notify(
                    f"需求已提交，版本已迭代到: V{new_version}",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
                # 新增：跳转前取消定时器
                if "autosave_timer" in app.storage.client:
                    app.storage.client["autosave_timer"].cancel()
                # 不传入项目名，就不会识别个人自动保存的需求文件
                ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")
            else:
                ui.notify(
                    "需求确认项未全部选填完毕，不能提交！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                # 为了用户体验，建议在 return 前恢复为 True，以便用户能继续自动保存
                app.storage.client["allow_autosave"] = True

    def get_select_req(select_project_name):
        if select_project_name:
            # 定义文件路径
            file_path = os.path.join(REQ_DIR, select_project_name)
            # 新增：跳转前取消定时器
            if "autosave_timer" in app.storage.client:
                app.storage.client["autosave_timer"].cancel()
            # 不传入项目名，就不会识别个人自动保存的需求文件
            ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    # 滚动获取特定版本需求配置文件，并重新跳转页面
    def select_project_req():
        select_value = {"value": ""}
        target_project_name = app.storage.client.get("target_project_name", "")
        if target_project_name == "":
            ui.notify(
                "项目名或需求版本获取失败，无法响应！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return
        req_version_dialog = app.storage.client["page_elements"].get("req_version_dialog").props("persistent")
        version_card = app.storage.client["page_elements"].get("version_card")
        version_card.clear()
        # 查找指定路径下，含有提供项目名的文件，得到一个字典，完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
        project_exists_file = find_files_with_prefix_and_version(REQ_DIR, target_project_name)
        if project_exists_file:
            # current_version = float(current_version)
            version_li = list([float(s) for s in project_exists_file.keys()])
            version_li.sort()
            with version_card:
                with ui.column().classes("w-full"):
                    ui.label("选择切换的需求版本：").classes("text-xl font-bold")
                    ui.label("确定切换将覆盖当前编辑的需求内容").classes("text-base text-red")
                    ui.radio(version_li, value=version_li[-1]).props("inline").bind_value_to(select_value, "value")
                    with ui.row().classes("w-full justify-end"):
                        ui.button(
                            "确定",
                            on_click=lambda: get_select_req(project_exists_file[str(select_value["value"])]["name"]),
                        ).on("click", lambda: req_version_dialog.close())
                        ui.button("取消", on_click=lambda: req_version_dialog.close())
            req_version_dialog.open()
        else:
            ui.notify(
                "该项目当前没有其它需求配置！",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )

    # 需求显示界面框架构造函数
    def requirement_input_frame():
        # 需求界面内容
        header.clear()
        with header:
            ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
            ui.label("需求管理模块").classes(
                "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
            )  # 绝对定位居中
            # 创建文件上传组件
            upload = ui.upload(
                on_upload=json_handle_upload,  # 绑定上传处理函数
                auto_upload=True,
                label="选择JSON文件",
            ).props("accept=.json")
            upload.set_visibility(False)  # 隐藏上传组件
            with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
                ui.image(current_display_path)
                with ui.menu().props("auto-close"):
                    ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                    ui.separator().props("size=1px")
                    ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                    ui.menu_item("返回项目信息表", on_click=lambda: ui.navigate.to("/project_table"))
                    ui.separator().props("size=1px")
                    if current_role in ["销售", "销售总监", "admin"]:
                        ui.menu_item("新建需求", on_click=lambda: get_project_dialog("new"))
                        ui.menu_item(
                            "暂存需求", on_click=lambda: output_config_data(app.storage.client["config_data"], "export")
                        )
                        ui.menu_item(
                            "提交需求", on_click=lambda: output_config_data(app.storage.client["config_data"], "submit")
                        )
                    # ui.menu_item("从本地导入", on_click=lambda: import_config_data(upload))
                    # ui.separator().props("size=1px")
                    ui.menu_item("对比需求", on_click=show_comparison_dialog)
                    ui.separator().props("size=1px")
                    ui.menu_item("注销登录", on_click=lambda: logout())
            # 需求行
            with ui.row().classes("font-sans h-[calc(100vh-9rem)] items-stretch flex-nowrap w-full text-black"):
                with ui.column().classes("w-1/4 min-w-[400px] items-center justify-start overflow-y-auto"):
                    with ui.row().classes("-space-x-2 items-center justify-center w-full"):
                        ui.space()
                        ui.label("确认项清单").classes("text-xl")
                        ui.space()
                        # 统计圆环
                        circular_activ = (
                            ui.circular_progress(size="md", color="green")
                            .bind_value_from(app.storage.client, "req_activ_num")
                            .props("rounded")
                            .classes("")
                        )
                        with circular_activ:
                            ui.tooltip("已选填")
                        circular_not_activ = (
                            ui.circular_progress(size="md", color="orange")
                            .bind_value_from(app.storage.client, "req_not_activ_num")
                            .props("rounded")
                            .classes("")
                        )
                        with circular_not_activ:
                            ui.tooltip("未选填")
                        app.storage.client["page_elements"]["circular_activ"] = circular_activ
                        app.storage.client["page_elements"]["circular_not_activ"] = circular_not_activ

                    question_table = ui.column().classes("w-full items-center overflow-y-auto -space-y-3")
                    with question_table:
                        # 将新创建的 question_table 实例存入 user storage
                        app.storage.client["page_elements"]["question_table"] = question_table
                        # 初始化一次确认项列表
                        set_question_list(0)

                ui.separator().props("vertical size=1px")
                with ui.column().classes("relative w-3/4 min-w-[700px] items-center"):
                    with ui.row().classes("relative w-full items-center justify-center"):
                        with ui.column().classes("absolute left-0 -top-2 -space-y-5 items-center justify-left"):
                            with ui.row().classes("-space-x-3 items-center justify-left w-full"):
                                ui.label("型号设置").classes("text-base ")
                                target_project_button = (
                                    ui.button("", on_click=lambda: get_project_dialog())
                                    .props("flat")
                                    .classes("text-base text-amber-9 px-0")
                                    .bind_text(app.storage.client, "target_project_name")
                                )
                                if app.storage.client["target_project_name"].strip() == "":
                                    target_project_button.set_icon("quiz")
                                app.storage.client["page_elements"]["target_project_button"] = target_project_button
                            with ui.row().classes("-space-x-3 items-center justify-left w-full"):
                                ui.label("版本查阅").classes("text-base ")
                                ui.button(icon="list_alt", on_click=lambda: select_project_req()).props("flat").classes(
                                    "text-base text-amber-9 px-0"
                                )
                                # .bind_text(app.storage.client, "target_version")

                                # if app.storage.client["target_version"].strip() == "":
                                # target_version_button.set_icon("quiz")
                                # app.storage.client["page_elements"]["target_version_button"] = target_version_button
                        with ui.row().classes("-space-x-3 items-center justify-center w-full "):
                            ui.label("当前编辑需求：").classes("text-xl ")
                            ui.label().classes("text-xl text-amber-8").bind_text(app.storage.client, "project_name")
                            ui.label("_V").classes("text-xl text-amber-8")
                            ui.label().classes("text-xl text-amber-8").bind_text(app.storage.client, "version")

                    with ui.column().classes(
                        "mx-0 mt-10 px-22 gap-6 w-full items-center justify-start overflow-y-auto"
                    ) as question_column:
                        # --- 修改开始 ---
                        # 将新创建的 question_column 实例存入 user storage
                        app.storage.client["page_elements"]["question_column"] = question_column
                        # --- 修改结束 ---
                        app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
                        question_display(None, "1")  # 触发点击事件
            # 缩略图行
            with ui.row().classes("fixed bottom-0 left-0 right-0 bg-sky-50 p-3 items-center shadow-inner"):
                # 创建一个按钮组件，组件里有一个空白行，待后续往里面放缩略图
                row_h = 9
                get_img_group("上传", "/*", row_h)
                with ui.row().classes(f"h-{str(row_h + 1)}").classes("p-0 overflow-y-auto") as img_row:
                    # 将新创建的 img_row 实例存入 user storage
                    app.storage.client["page_elements"]["img_row"] = img_row
                    # 检查缩略图对象存放字典，有对象则会创建缩略图
                    req_thumbnail_display()
            # 需求状态提醒
            if app.storage.general.get("wait_review", {}):
                if app.storage.general["wait_review"].get(app.storage.client["project_name"], {}):
                    if app.storage.general["wait_review"][app.storage.client["project_name"]].get(
                        app.storage.client["version"], {}
                    ):
                        if (
                            app.storage.general["wait_review"][app.storage.client["project_name"]][
                                app.storage.client["version"]
                            ]["state"]
                            == "待审"
                        ):
                            ui.notify(
                                "当前需求处于待审状态，禁止暂存和提交，编辑后将无法保存！",
                                type="warning",
                                position="center",
                                timeout=0,
                                progress=False,
                                close_button="✖",
                            )
                        elif (
                            app.storage.general["wait_review"][app.storage.client["project_name"]][
                                app.storage.client["version"]
                            ]["state"]
                            == "待修改"
                        ):
                            if (
                                current_user
                                == app.storage.general["wait_review"][app.storage.client["project_name"]][
                                    app.storage.client["version"]
                                ]["submitter"]
                            ):
                                ui.notify(
                                    "当前需求处于待修改状态，可修改后提交或暂存，但不能衍生成其它项目！",
                                    type="warning",
                                    position="center",
                                    timeout=0,
                                    progress=False,
                                    close_button="✖",
                                )
                            else:
                                ui.notify(
                                    "当前需求处于待修改状态，只有原提交人可修改后提交或暂存，其他人不能！",
                                    type="warning",
                                    position="center",
                                    timeout=0,
                                    progress=False,
                                    close_button="✖",
                                )
            # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
            ui.keyboard(on_key=requirement_handle_key, ignore=["input", "textarea"])

    # 根据需求条目数据，格式化最终显示的字符串
    def format_show_string(item: dict) -> str:
        if not item:
            return "无"

        # show_template = item.get("option_show", "{V}")
        user_out = item.get("user_must_out", {})
        answer_type = item.get("answer_type")

        if not user_out:
            # 如果没有用户输出
            return "无"

        # 1. 处理单选类型
        if answer_type == "单选" or answer_type == "下拉单选":
            val = user_out.get("value")
            # 遍历所有单选项配置
            for option in item.get("options", []):
                # 当选项输出值与用户选择的选项输出值匹配上
                if str(option.get("option_out")) == str(val):
                    # 优先使用选项中的option_bold，如果它不为空
                    if option.get("option_bold"):
                        return option.get("option_show").replace(
                            "{V}", f'<b><span style="color: #2376b7;">{option["option_bold"]}</span></b>'
                        )
            # 如果没找到匹配的option_bold，则直接返回值
            return "无"

        # 2. 处理多选类型
        elif answer_type == "多选":
            show_template = ""
            show_bool = False
            selected_options = [key for key, value in user_out.items() if value]
            selec_show = []
            # 遍历所有多选项配置
            for option in item.get("options", []):
                if option["option_out"] in selected_options:
                    # 优先用 option_bold，没有则用 option_content
                    text = option.get("option_bold") or option.get("option_content")
                    selec_show.append(text)
                # 只认改确认项选项配置里，靠最前的选型展示语句
                if option["option_show"] and not show_bool:
                    show_template = option["option_show"]
                    show_bool = True
            # 如果选项展示语句为空
            if show_template == "":
                show_template = "选项无展示配置"
            val_str = "、".join(selec_show) if selec_show else "无"
            vor_num = str(len(selec_show))
            return show_template.replace("{V}", f'<b><span style="color: #207f4c;">{val_str}</span></b>').replace(
                "{N}", f'<b><span style="color: #207f4c;">{vor_num}</span></b>'
            )

        # 3. 处理文本输入类型 (单行/多行)
        elif answer_type in ["单行文本", "多行文本", "正整数"]:
            key_color = "#126bae"
            text_color = "#603d30"
            # 替换 {V}, {K}, {T}
            content_li = []
            # 键为1/2/3或用户起的多个名字
            key_li = list(user_out.keys())
            tolerance_out = item.get("option_tolerance_out", {})

            option_li = item.get("options", [])
            if not option_li:
                return "选项无展示配置"
            show_template = option_li[0]["option_show"]
            pattern = r"(.*?)(?:\[(.*?)\])(.*)"
            match = re.search(pattern, show_template)
            # 匹配得上，则意味着有多项输入内容
            if match:
                # 提取并打印所有捕获组的内容
                prefix = match.group(1)  # [ 之前的内容
                content = match.group(2)  # [ ] 之间的内容
                suffix = match.group(3)  # ] 之后的内容
                # 键为1/2/3或用户起的多个名字
                for k in key_li:
                    # ---【修改点 1】：先转义下划线，再处理换行 ---
                    # 必须填写的输入内容为多行文本，则默认在最前面加上换行标签，且内部\n统一替换成换行标签
                    raw_str = str(user_out[k]).replace("_", "\\_").replace("*", "\\*")  # 转义 Markdown 下划线

                    if answer_type == "多行文本":
                        user_out_str = f"{raw_str.replace('\n', '<br>')}"
                    else:
                        user_out_str = raw_str

                    # ---【修改点 2】：公差里的下划线也要转义 ---
                    tol_str = str(tolerance_out[k]).replace("_", "\\_").replace("*", "\\*") if tolerance_out else "无"

                    content_li.append(
                        content.replace("{K}", f'<b><span style="color: {key_color};">{k}</span></b>')
                        .replace(
                            "{V}",
                            f'<b><span style="color: {text_color};">{user_out_str}</span></b>',
                        )
                        .replace(
                            "{T}",
                            f'<b><span style="color: {text_color};">{tol_str}</span></b>',
                        )
                    )
                result = f"{prefix}<br>{'<br>'.join(content_li)}<br>{suffix}"
            # 只有一项输入内容
            else:
                # ---【修改点 3】：先转义下划线，再处理换行 ---
                raw_str = str(user_out[key_li[0]]).replace("_", "\\_").replace("*", "\\*")  # 转义 Markdown 下划线

                # 必须填写的输入内容为多行文本，则默认在最前面加上换行标签，且内部\n统一替换成换行标签
                if answer_type == "多行文本":
                    user_out_str = f"<br>{raw_str.replace('\n', '<br>')}"
                else:
                    user_out_str = raw_str

                # ---【修改点 4】：公差里的下划线也要转义 ---
                tol_str = (
                    str(tolerance_out[key_li[0]]).replace("_", "\\_").replace("*", "\\*") if tolerance_out else "无"
                )

                result = (
                    show_template.replace("{K}", f'<b><span style="color: {key_color};">{key_li[0]}</span></b>')
                    .replace(
                        "{V}",
                        f'<b><span style="color: {text_color};">{user_out_str}</span></b>',
                    )
                    .replace(
                        "{T}",
                        f'<b><span style="color: {text_color};">{tol_str}</span></b>',
                    )
                )
            return result

        # 默认回退
        return "、".join(map(str, user_out.values()))

    # 概述界面，需求项后面数字引用按钮添加函数
    def add_overview_lab(thumbnail_obj):
        k = thumbnail_obj.file_index
        ui.button(k, on_click=lambda: thumbnail_obj.handle_index_click()).classes("ml-1 text-white bg-purple-5").props(
            'round padding="0px 5px"'
        ).style("font-size: 8px;")

    # 根据传入的字符串生成对应的小标签
    def add_role_badge(role_text: str):
        color_data = {
            "光学": ["光", "cyan-3"],
            "结构": ["机", "blue-3"],
            "硬件": ["硬", "green-3"],
            "软件": ["软", "purple-3"],
            "工艺": ["艺", "orange-3"],
            "质量": ["质", "red-3"],
            "全员": ["全", "brown-5"],
        }
        color_str = color_data[role_text][1] if color_data[role_text] else "blue-grey-6"
        text_str = color_data[role_text][0] if color_data[role_text] else role_text[0]
        ui.badge(text=text_str, color=color_str).props("rounded").classes("p-1 text-[8px]/[8px]")

    async def batch_overview_maintenance_dialog():
        """研发结构专用：跨项目批量新增概述或修改当前版本的激活状态。"""
        if current_role not in BATCH_OVERVIEW_TOOL_ROLES:
            ui.notify("当前角色无权使用批量概述维护工具。", type="negative")
            return

        over_config = app.storage.general.get("over_config_data", {})
        editable_configs = collect_editable_overview_configs(
            over_config,
            str(current_role or ""),
            OVERVIEW_UI_RENDER_REGISTRY,
        )
        if not editable_configs:
            ui.notify("当前角色没有可编辑的概述配置。", type="warning")
            return

        config_by_label = {config["label"]: config for config in editable_configs}
        project_summary = app.storage.general.get("project_summary", {})
        summary_by_project = {
            str(summary.get("sub_project") or key): summary for key, summary in project_summary.items()
        }
        eligible_items = [
            (key, summary)
            for key, summary in project_summary.items()
            if summary.get("state") in BATCH_OVERVIEW_ALLOWED_PROJECT_STATES
        ]
        eligible_projects = sorted({str(summary.get("sub_project") or key) for key, summary in eligible_items})
        eligible_category_projects = [
            str(summary.get("project") or summary.get("sub_project") or key) for key, summary in eligible_items
        ]
        category_map = build_project_category_map(eligible_category_projects)
        if not eligible_projects:
            ui.notify("没有处于待定、研发或转产状态的项目。", type="warning")
            return

        current_category_project = str(summary_by_project.get(project_name, {}).get("project") or project_name)
        current_major = current_category_project.split("-", 1)[0] if "-" in current_category_project else "其它"
        default_major = current_major if current_major in category_map else "所有"
        if default_major == "其它":
            default_sub = (
                current_category_project if current_category_project in category_map.get("其它", []) else "所有"
            )
        elif default_major != "所有" and "-" in current_category_project:
            candidate_sub = current_category_project.split("-", 1)[1][:2]
            default_sub = candidate_sub if candidate_sub in category_map.get(default_major, []) else "所有"
        else:
            default_sub = "所有"
        default_model_range_options = build_project_model_range_options(
            eligible_category_projects,
            default_major,
            default_sub,
        )
        if default_major not in {"所有", "其它"} and "-" in current_category_project:
            current_model_part = current_category_project.split("-", 2)[1]
            candidate_model_range = f"{default_major}-{current_model_part[:4]}"
            default_model_range = (
                candidate_model_range if candidate_model_range in default_model_range_options else "所有"
            )
        else:
            default_model_range = "所有"

        roles = sorted({config["role"] for config in editable_configs})
        default_role = "结构" if "结构" in roles else roles[0]
        role_groups = sorted({config["group_name"] for config in editable_configs if config["role"] == default_role})
        default_group = role_groups[0]
        default_labels = [
            config["label"]
            for config in editable_configs
            if config["role"] == default_role and config["group_name"] == default_group
        ]

        state: dict[str, Any] = {
            "states": list(BATCH_OVERVIEW_ALLOWED_PROJECT_STATES),
            "major": default_major,
            "sub": default_sub,
            "model_range": default_model_range,
            "projects": [],
            "action": "add",
            "role": default_role,
            "group": default_group,
            "label": default_labels[0],
            "content": "",
            "notes": "",
            "file_data": None,
            "row_anchors": {},
            "chip_targets": [],
            "target_state": "active",
            "impact_mode": "none",
            "impact_selected": {},
            "test_data": {
                "test_nature_select": None,
                "test_nature_other_text": "",
                "state_select": None,
                "state_other_text": "",
                "node_select": None,
                "node_other_text": "",
                "instrument_select": None,
                "instrument_other_text": "",
            },
        }
        project_select: Any = None
        sub_select: Any = None
        model_range_select: Any = None
        group_select: Any = None
        label_select: Any = None
        editor_container: Any = None
        selected_count_label: Any = None
        submit_button: Any = None
        submit_spinner: Any = None

        def notify_batch_lines(
            lines: list[str],
            *,
            notification_type: Literal["positive", "negative", "warning", "info", "ongoing"] = "warning",
            timeout: int = 0,
        ):
            """通过安全 HTML 换行显示完整的批量操作通知。"""
            message = "<br>".join(html.escape(str(line)) for line in lines)
            ui.notify(
                message,
                type=notification_type,
                html=True,
                multi_line=True,
                timeout=timeout,
                close_button="✖",
            )

        def get_config():
            return config_by_label.get(state["label"])

        def group_options(role):
            return sorted({config["group_name"] for config in editable_configs if config["role"] == role})

        def label_options(role, group):
            return {
                config["label"]: f"{config.get('title', '未命名')}（{config.get('processing_type', 'text')}）"
                for config in editable_configs
                if config["role"] == role and config["group_name"] == group
            }

        def current_filtered_projects():
            return filter_batch_projects(
                project_summary,
                state["states"],
                state["major"],
                state["sub"],
                state["model_range"],
            )

        def refresh_project_options(reset_sub=False, reset_model_range=False):
            if reset_sub:
                sub_options = category_map.get(state["major"], ["所有"])
                state["sub"] = sub_options[0]
                sub_select.set_options(sub_options, value=state["sub"])
            if reset_sub or reset_model_range:
                model_range_options = build_project_model_range_options(
                    eligible_category_projects,
                    state["major"],
                    state["sub"],
                )
                state["model_range"] = model_range_options[0]
                model_range_select.set_options(model_range_options, value=state["model_range"])
            visible_projects = current_filtered_projects()
            options = {
                project: f"{project}（{summary_by_project.get(project, {}).get('state', '')}）"
                for project in visible_projects
            }
            state["projects"] = [project for project in state["projects"] if project in options]
            project_select.set_options(options, value=state["projects"])
            selected_count_label.text = f"当前筛选 {len(options)} 个项目，已选择 {len(state['projects'])} 个"
            render_editor()

        def select_all_visible_projects():
            state["projects"] = current_filtered_projects()
            project_select.set_value(state["projects"])
            selected_count_label.text = f"当前筛选 {len(state['projects'])} 个项目，已选择 {len(state['projects'])} 个"
            render_editor()

        def on_role_change(e):
            state["role"] = e.value
            groups = group_options(e.value)
            state["group"] = groups[0]
            group_select.set_options(groups, value=state["group"])
            labels = label_options(state["role"], state["group"])
            state["label"] = next(iter(labels))
            label_select.set_options(labels, value=state["label"])
            reset_operation_selection(clear_inputs=True)

        def on_group_change(e):
            state["group"] = e.value
            labels = label_options(state["role"], state["group"])
            state["label"] = next(iter(labels))
            label_select.set_options(labels, value=state["label"])
            reset_operation_selection(clear_inputs=True)

        def reset_operation_selection(clear_inputs=False):
            state["row_anchors"] = {}
            state["chip_targets"] = []
            state["impact_selected"] = {}
            state["file_data"] = None
            if clear_inputs:
                state["impact_mode"] = "none"
                state["content"] = ""
                state["notes"] = ""
                state["test_data"] = {
                    "test_nature_select": None,
                    "test_nature_other_text": "",
                    "state_select": None,
                    "state_other_text": "",
                    "node_select": None,
                    "node_other_text": "",
                    "instrument_select": None,
                    "instrument_other_text": "",
                }
            render_editor()

        def on_label_change(e):
            state["label"] = e.value
            reset_operation_selection(clear_inputs=True)

        def render_test_inputs(config):
            test_groups = (
                ("测试性质", "test_nature", config.get("test_nature_options", [])),
                ("条件/状态", "state", config.get("state_options", [])),
                ("节点/位置", "node", config.get("node_options", [])),
                ("工具/仪器", "instrument", config.get("instrument_options", [])),
            )
            with ui.card().classes("w-full p-2 bg-purple-50/50 border border-purple-100 shadow-none"):
                ui.label("测试项参数").classes("text-sm font-bold text-purple-900")
                for title, prefix, options in test_groups:
                    if not options:
                        continue
                    with ui.row().classes("w-full items-start gap-2"):
                        select = (
                            ui.select(options, label=title)
                            .bind_value(state["test_data"], f"{prefix}_select")
                            .props("outlined dense options-dense")
                            .classes("w-1/3")
                        )
                        other_input = (
                            ui.input(label=f"{title}特殊要求")
                            .bind_value(state["test_data"], f"{prefix}_other_text")
                            .props("outlined dense")
                            .classes("flex-grow")
                        )
                        other_input.set_visibility(state["test_data"].get(f"{prefix}_select") == "其它")

                        def toggle_other(event, element=other_input, key=f"{prefix}_other_text"):
                            element.set_visibility(event.value == "其它")
                            if event.value != "其它":
                                state["test_data"][key] = ""

                        select.on_value_change(toggle_other)

        def render_table_anchor_selectors(config):
            if not config.get("is_table_group") or config["label"] == config.get("first_col_label"):
                return
            with ui.card().classes("w-full p-2 border border-blue-100 bg-blue-50/40 shadow-none"):
                ui.label("逐项目选择新增内容所在的表格行（必选）").classes("text-sm font-bold text-blue-900")
                for project in state["projects"]:
                    req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                    first_chips = db_storage.get_deep_item([f"{project}_over_data", config["first_col_label"]], {})
                    options = {
                        chip.get("row_id"): str(chip.get("content", "未命名"))
                        for chip in first_chips.values()
                        if chip.get("row_id")
                        and chip.get("select_activ_dic", {}).get(req_max_ver, chip.get("enabled")) is True
                    }
                    state["row_anchors"].setdefault(project, None)
                    ui.select(options, label=project).bind_value(state["row_anchors"], project).props(
                        "outlined dense options-dense"
                    ).classes("w-full")
                    if not options:
                        ui.label(f"{project} 没有可用的激活基准行").classes("text-xs text-red-600 -mt-2")

        def render_state_targets(config):
            state_labels = {True: "激活", False: "失活", None: "待定"}
            options = {}
            for project in state["projects"]:
                req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                chips = db_storage.get_deep_item([f"{project}_over_data", config["label"]], {})
                for chip_id, chip in chips.items():
                    chip_state = chip.get("select_activ_dic", {}).get(req_max_ver, chip.get("enabled"))
                    key = json.dumps([project, chip_id], ensure_ascii=False)
                    content = str(chip.get("content", "未命名"))
                    if len(content) > 45:
                        content = f"{content[:45]}…"
                    options[key] = f"{project}｜{state_labels.get(chip_state, '未知')}｜{content}"
            state["chip_targets"] = [target for target in state["chip_targets"] if target in options]
            ui.select(options, label="选择需要修改状态的概述条目", multiple=True).bind_value(
                state, "chip_targets"
            ).props("outlined use-chips options-dense").classes("w-full")
            ui.radio({"active": "设为激活", "pending": "设为待定", "inactive": "设为失活"}).bind_value(
                state, "target_state"
            ).props("inline")
            ui.label(f"当前可选 {len(options)} 条；状态修改作用于各项目当前最高需求版本。\n").classes(
                "text-xs text-gray-500"
            )

        def render_impact_options(config):
            related_labels = list(dict.fromkeys(label for label in config.get("impact_list", []) if label))
            if not related_labels:
                ui.label("该概述未配置关联影响项，提交时将直接跳过关联影响步骤。\n").classes("text-xs text-gray-500")
                return
            ui.separator()
            ui.label("关联影响范围").classes("text-sm font-bold text-gray-700")
            ui.radio(
                {
                    "none": "本次不影响其它项",
                    "selected": "勾选的受影响",
                    "all": "全部受影响",
                }
            ).bind_value(state, "impact_mode").props("inline")
            impact_checks = ui.grid(columns=3).classes("w-full gap-0")
            impact_checks.bind_visibility_from(state, "impact_mode", backward=lambda value: value == "selected")
            with impact_checks:
                for related_label in related_labels:
                    state["impact_selected"].setdefault(related_label, False)
                    title = (
                        app.storage.general.get("over_config_data_flat", {})
                        .get(related_label, {})
                        .get("title", related_label)
                    )
                    ui.checkbox(title).bind_value(state["impact_selected"], related_label)

        async def handle_batch_file_upload(event, status_label):
            try:
                state["file_data"] = {
                    "name": Path(event.file.name).name,
                    "content_type": event.file.content_type,
                    "content": await event.file.read(),
                }
                status_label.text = f"已选择：{state['file_data']['name']}"
                status_label.classes(remove="text-gray-500", add="text-green-700")
            except Exception as ex:
                logger.error("读取批量概述上传文件失败", exc_info=True)
                ui.notify(f"读取上传文件失败：{ex}", type="negative")

        def handle_batch_file_removed(status_label):
            """同步清除待提交文件和状态提示。"""
            state["file_data"] = None
            status_label.text = "尚未选择文件"
            status_label.classes(remove="text-green-700", add="text-gray-500")

        def render_editor():
            if editor_container is None:
                return
            editor_container.clear()
            config = get_config()
            with editor_container:
                if not config:
                    ui.label("请选择具体概述项。\n").classes("text-gray-500")
                    return
                if not state["projects"]:
                    ui.label("请先在上方勾选至少一个目标项目。\n").classes("text-amber-800 font-bold")
                ptype = config.get("processing_type", "text")
                ui.label(
                    f"处理类型：{ptype}；配置允许状态：{', '.join(config.get('allowed_state', [])) or '未限制'}"
                ).classes("text-xs text-blue-grey-7")
                if state["action"] == "add":
                    if ptype in {"file", "image", "video"}:
                        ui.input(
                            label="不需要提交文件时填写（选填）",
                            placeholder="无",
                        ).bind_value(state, "content").props("outlined dense").classes("w-full")
                        upload_status = ui.label(
                            f"已选择：{state['file_data']['name']}" if state["file_data"] else "尚未选择文件"
                        ).classes("text-xs text-gray-500")
                        custom_upload(
                            multiple=True,
                            max_files=5,
                            on_upload=lambda e, label=upload_status: handle_batch_file_upload(e, label),
                            on_removed=lambda: handle_batch_file_removed(upload_status),
                            label="选择要添加的文件",
                        ).classes("w-full")
                        ui.label("服务器存在同名文件时将复用服务器已有文件，不会覆盖。\n").classes(
                            "text-xs text-orange-700"
                        )
                    else:
                        ui.input(
                            label=config.get("dialog_label", "概述内容"),
                            placeholder=config.get("dialog_placeholder", ""),
                        ).bind_value(state, "content").props("outlined").classes("w-full")
                    ui.textarea(label="注释（必填）").bind_value(state, "notes").props("outlined auto-grow").classes(
                        "w-full"
                    )
                    if ptype == "test":
                        render_test_inputs(config)
                    render_table_anchor_selectors(config)
                else:
                    render_state_targets(config)
                render_impact_options(config)

        def selected_related_labels(config):
            related_labels = list(dict.fromkeys(label for label in config.get("impact_list", []) if label))
            if state["impact_mode"] == "all":
                return related_labels
            if state["impact_mode"] == "selected":
                return [label for label in related_labels if state["impact_selected"].get(label) is True]
            return []

        def validate_test_data(config):
            for prefix, option_key in (
                ("test_nature", "test_nature_options"),
                ("state", "state_options"),
                ("node", "node_options"),
                ("instrument", "instrument_options"),
            ):
                if config.get(option_key) and not state["test_data"].get(f"{prefix}_select"):
                    return False, "测试项参数必须全部选择。"
                if (
                    state["test_data"].get(f"{prefix}_select") == "其它"
                    and not str(state["test_data"].get(f"{prefix}_other_text", "")).strip()
                ):
                    return False, "选择“其它”时必须填写对应特殊要求。"
            return True, ""

        async def prepare_media_file(config):
            file_data = state["file_data"]
            if not file_data:
                return False, "请先选择文件。", None
            filename = Path(file_data["name"]).name
            file_type = str(file_data.get("content_type") or "application/octet-stream")
            extension = Path(filename).suffix.lower()
            ptype = config.get("processing_type")
            if ptype == "file" and extension not in OVER_UPLOADS_FILE_TYPE:
                return False, f"{filename} 不是允许上传的文件类型。", None
            if ptype == "image" and "image" not in file_type:
                return False, f"{filename} 不是图片类型。", None
            if ptype == "video" and "video" not in file_type:
                return False, f"{filename} 不是视频类型。", None
            upload_path_value = str(config.get("upload_path", "")).strip()
            if not upload_path_value:
                return False, "该概述项未配置上传目录。", None
            upload_path = Path(upload_path_value)
            if not upload_path.is_dir():
                return False, f"上传目录不存在：{upload_path}", None
            target_path = upload_path / filename
            if not target_path.exists():
                try:
                    target_path.write_bytes(file_data["content"])
                except Exception as ex:
                    return False, f"文件保存失败：{ex}", None
            return (
                True,
                "",
                {
                    "content": filename,
                    "file_type": file_type,
                    "url_path": f"{FILES_URL_DIR}/{filename}",
                },
            )

        def refresh_pending_for_label(project, label):
            flat_config = app.storage.general.get("over_config_data_flat", {}).get(label, {})
            role = flat_config.get("role", "")
            latest_user = (
                app.storage.general.get("overview_role", {}).get(project, {}).get(role, {}).get("latest_user", "")
            )
            des_user = latest_user.split("：", 1)[1] if "：" in latest_user else latest_user
            if des_user and des_user != "——":
                update_overview_charge_pending_dic(
                    scope="local",
                    des_user=des_user,
                    project_name=project,
                    des_label=label,
                )

        def ensure_complete_table_row_bindings(config, projects):
            """在任何批量写入前确认所有表格子项均已绑定有效基准行。"""
            if (
                state["action"] != "add"
                or not config.get("is_table_group")
                or config["label"] == config.get("first_col_label")
            ):
                return True

            missing_projects = find_projects_without_row_anchors(projects, state["row_anchors"])
            invalid_projects = []
            for project in projects:
                if project in missing_projects:
                    continue
                row_id = state["row_anchors"].get(project)
                req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                first_chips = db_storage.get_deep_item(
                    [f"{project}_over_data", config["first_col_label"]],
                    {},
                )
                anchor_is_active = any(
                    chip.get("row_id") == row_id
                    and chip.get("select_activ_dic", {}).get(req_max_ver, chip.get("enabled")) is True
                    for chip in first_chips.values()
                )
                if not anchor_is_active:
                    invalid_projects.append(project)
                    state["row_anchors"][project] = None

            if not missing_projects and not invalid_projects:
                return True

            render_editor()
            details = []
            if missing_projects:
                details.append(f"尚未选择绑定行：{', '.join(missing_projects)}")
            if invalid_projects:
                details.append(f"原绑定行已失效或失活，请重新选择：{', '.join(invalid_projects)}")
            notify_batch_lines(
                ["请先为全部目标项目选择有效的表格绑定行，完成后才能批量处理。", *details],
            )
            return False

        async def submit_batch_request():
            """完成全部前置校验后提交审批，审批通过前不改动任何概述数据。"""
            if current_role not in BATCH_OVERVIEW_TOOL_ROLES:
                ui.notify("当前角色无权提交批量概述申请。", type="negative")
                return
            reviewer_roles = get_batch_overview_reviewer_roles(str(current_role or ""))
            if not reviewer_roles:
                ui.notify("当前角色尚未配置批量概述审批角色，无法提交申请。", type="negative")
                return
            config = get_config()
            if not config:
                ui.notify("请选择具体概述项。", type="warning")
                return
            live_summary_by_project = {
                str(summary.get("sub_project") or key): summary
                for key, summary in app.storage.general.get("project_summary", {}).items()
            }
            selected_projects = [
                project
                for project in state["projects"]
                if live_summary_by_project.get(project, {}).get("state") in BATCH_OVERVIEW_ALLOWED_PROJECT_STATES
            ]
            if not selected_projects:
                ui.notify("请至少选择一个符合状态限制的项目。", type="warning")
                return
            if current_role not in config.get("permission", {}).get("edit_role", []):
                ui.notify("当前角色没有该概述项的编辑权限。", type="negative")
                return
            if not ensure_complete_table_row_bindings(config, selected_projects):
                return
            related_config = list(dict.fromkeys(label for label in config.get("impact_list", []) if label))
            related_labels = selected_related_labels(config)
            if state["impact_mode"] == "selected" and related_config and not related_labels:
                ui.notify("请至少勾选一个确实受影响的概述项。", type="warning")
                return

            submit_button.disable()
            submit_spinner.set_visibility(True)
            request_id = str(uuid.uuid4())
            staged_file_path = ""
            request_saved = False
            try:
                content = str(state["content"] or "").strip()
                notes = str(state["notes"] or "").strip()
                ptype = str(config.get("processing_type", "text"))
                actual_type = ptype
                extra_data: dict[str, Any] = {}
                chip_targets: list[dict[str, str]] = []
                target_state: Optional[bool] = None

                if state["action"] == "add":
                    if not notes:
                        ui.notify("注释不能为空。", type="warning")
                        return
                    media_as_text = ptype in {"file", "image", "video"} and any(
                        re.search(pattern, content) for pattern in NONE_REGULAR
                    )
                    if ptype in {"file", "image", "video"} and not media_as_text:
                        file_data = state.get("file_data")
                        if not file_data:
                            ui.notify("请先选择文件。", type="warning")
                            return
                        filename = Path(str(file_data.get("name") or "")).name
                        file_type = str(file_data.get("content_type") or "application/octet-stream")
                        extension = Path(filename).suffix.lower()
                        if ptype == "file" and extension not in OVER_UPLOADS_FILE_TYPE:
                            ui.notify(f"{filename} 不是允许上传的文件类型。", type="warning")
                            return
                        if ptype == "image" and "image" not in file_type:
                            ui.notify(f"{filename} 不是图片类型。", type="warning")
                            return
                        if ptype == "video" and "video" not in file_type:
                            ui.notify(f"{filename} 不是视频类型。", type="warning")
                            return
                        upload_path = Path(str(config.get("upload_path") or ""))
                        if not upload_path.is_dir():
                            ui.notify(f"上传目录不存在：{upload_path}", type="warning")
                            return
                        staging_dir = BATCH_OVERVIEW_STAGING_DIR / request_id
                        staging_dir.mkdir(parents=True, exist_ok=True)
                        staging_path = staging_dir / filename
                        staging_path.write_bytes(file_data["content"])
                        staged_file_path = str(staging_path)
                        content = filename
                        extra_data.update({"file_type": file_type, "url_path": f"{FILES_URL_DIR}/{filename}"})
                    elif not validate_overview_content(content, config):
                        ui.notify("概述内容为空或不符合该项填写格式。", type="warning")
                        return

                    actual_type = "text" if media_as_text else ptype
                    if ptype in {"test", "search", "svn"} and any(
                        re.search(pattern, content) for pattern in NONE_REGULAR
                    ):
                        actual_type = "text"
                    if actual_type == "test":
                        valid, message = validate_test_data(config)
                        if not valid:
                            ui.notify(message, type="warning")
                            return
                        extra_data["test_select_data"] = copy.deepcopy(state["test_data"])
                    validation_errors = []
                    if actual_type == "search":
                        for project in selected_projects:
                            valid, _, _, _, message = await validate_search_path(content, config, [project])
                            if not valid:
                                validation_errors.append(f"{project}：{message}")
                    elif actual_type == "svn":
                        for project in selected_projects:
                            valid, _, _, message = await validate_svn_url(content, config, [project])
                            if not valid:
                                validation_errors.append(f"{project}：{message}")
                    if validation_errors:
                        notify_batch_lines(["以下项目校验未通过，申请尚未提交：", *validation_errors])
                        return
                else:
                    if not state["chip_targets"]:
                        ui.notify("请至少选择一条需要修改状态的概述。", type="warning")
                        return
                    target_state = {"active": True, "pending": None, "inactive": False}[state["target_state"]]
                    validation_errors = []
                    for encoded_target in state["chip_targets"]:
                        project, chip_id = json.loads(encoded_target)
                        if project not in selected_projects:
                            validation_errors.append(f"{project}：项目已不在当前选择范围")
                            continue
                        req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                        chip = db_storage.get_deep_item([f"{project}_over_data", config["label"], chip_id], {})
                        if not chip:
                            validation_errors.append(f"{project}：概述条目已不存在")
                            continue
                        if (
                            config.get("is_table_group")
                            and config["label"] != config.get("first_col_label")
                            and not is_table_child_state_allowed(
                                project,
                                config["first_col_label"],
                                chip.get("row_id"),
                                req_max_ver,
                                target_state,
                            )
                        ):
                            validation_errors.append(f"{project}：目标状态等级不能高于同行首列概述状态")
                            continue
                        if target_state is True and chip.get("type") == "search":
                            valid, _, _, _, message = await validate_search_path(
                                chip.get("content", ""), config, [project]
                            )
                            if not valid:
                                validation_errors.append(f"{project}：{message}")
                                continue
                        if target_state is True and chip.get("type") == "svn":
                            valid, _, _, message = await validate_svn_url(chip.get("content", ""), config, [project])
                            if not valid:
                                validation_errors.append(f"{project}：{message}")
                                continue
                        chip_targets.append({"project": project, "chip_id": chip_id})
                    if validation_errors:
                        notify_batch_lines(["以下目标校验未通过，申请尚未提交：", *validation_errors])
                        return

                group_labels = [
                    item.get("label")
                    for item in over_config.get(config["role"], {}).get(config["group_name"], [])
                    if item.get("label")
                ]
                now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                request_record = {
                    "id": request_id,
                    "submitter": current_user,
                    "submitter_role": current_role,
                    "reviewer_roles": reviewer_roles,
                    "status": "pending",
                    "created_at": now_text,
                    "updated_at": now_text,
                    "reject_reason": "",
                    "review_log": [],
                    "payload": {
                        "action": state["action"],
                        "projects": list(selected_projects),
                        "role": config.get("role", ""),
                        "group_name": config.get("group_name", ""),
                        "label": config.get("label", ""),
                        "title": config.get("title", config.get("label", "")),
                        "config": copy.deepcopy(config),
                        "content": content,
                        "notes": notes,
                        "actual_type": actual_type,
                        "extra_data": extra_data,
                        "staged_file_path": staged_file_path,
                        "row_anchors": copy.deepcopy(state["row_anchors"]),
                        "chip_targets": chip_targets,
                        "target_state": target_state,
                        "group_labels": group_labels,
                        "impact_mode": state["impact_mode"],
                        "related_labels": related_labels,
                    },
                }
                saved, _ = await create_batch_overview_request(request_record)
                if not saved:
                    ui.notify("批量概述申请保存失败，请稍后重试。", type="negative")
                    return
                request_saved = True
                batch_overview_dialog.close()
                ui.notify(
                    f"批量概述申请已提交，等待{'、'.join(reviewer_roles)}审批。",
                    type="positive",
                    position="center",
                )
            except Exception as ex:
                logger.error("提交批量概述申请失败", exc_info=True)
                ui.notify(f"提交申请失败：{ex}", type="negative", timeout=0, close_button="✖")
            finally:
                if staged_file_path and not request_saved:
                    staging_path = Path(staged_file_path)
                    staging_path.unlink(missing_ok=True)
                    try:
                        staging_path.parent.rmdir()
                    except OSError:
                        pass
                if not submit_button.is_deleted:
                    submit_button.enable()
                if not submit_spinner.is_deleted:
                    submit_spinner.set_visibility(False)

        async def execute_batch():
            if current_role not in BATCH_OVERVIEW_TOOL_ROLES:
                ui.notify("当前角色无权执行此操作。", type="negative")
                return
            config = get_config()
            if not config:
                ui.notify("请选择具体概述项。", type="warning")
                return
            live_summary_by_project = {
                str(summary.get("sub_project") or key): summary
                for key, summary in app.storage.general.get("project_summary", {}).items()
            }
            selected_projects = [
                project
                for project in state["projects"]
                if live_summary_by_project.get(project, {}).get("state") in BATCH_OVERVIEW_ALLOWED_PROJECT_STATES
            ]
            if not selected_projects:
                ui.notify("请至少选择一个符合状态限制的项目。", type="warning")
                return
            if current_role not in config.get("permission", {}).get("edit_role", []):
                ui.notify("当前角色没有该概述项的编辑权限。", type="negative")
                return
            if not ensure_complete_table_row_bindings(config, selected_projects):
                return
            related_config = list(dict.fromkeys(label for label in config.get("impact_list", []) if label))
            if state["impact_mode"] == "selected" and related_config and not selected_related_labels(config):
                ui.notify("请至少勾选一个确实受影响的概述项。", type="warning")
                return

            submit_button.disable()
            submit_spinner.set_visibility(True)
            successes = []
            skipped = []
            failed = []
            changed_pairs = set()
            creator = app.storage.user.get("current_user", "匿名用户")
            content = str(state["content"] or "").strip()
            notes = str(state["notes"] or "").strip()
            ptype = config.get("processing_type", "text")
            common_extra = {}

            try:
                if state["action"] == "add":
                    if not notes:
                        ui.notify("注释不能为空。", type="warning")
                        return
                    media_as_text = ptype in {"file", "image", "video"} and any(
                        re.search(pattern, content) for pattern in NONE_REGULAR
                    )
                    if ptype in {"file", "image", "video"} and not media_as_text:
                        valid, message, media = await prepare_media_file(config)
                        if not valid or media is None:
                            ui.notify(message, type="warning")
                            return
                        media_data = dict(media)
                        content = str(media_data.pop("content"))
                        common_extra.update(media_data)
                    else:
                        if not validate_overview_content(content, config):
                            ui.notify("概述内容为空或不符合该项填写格式。", type="warning")
                            return
                    actual_type = "text" if media_as_text else ptype
                    if ptype in {"test", "search", "svn"} and any(
                        re.search(pattern, content) for pattern in NONE_REGULAR
                    ):
                        actual_type = "text"
                    if actual_type == "test":
                        valid, message = validate_test_data(config)
                        if not valid:
                            ui.notify(message, type="warning")
                            return
                        common_extra["test_select_data"] = copy.deepcopy(state["test_data"])

                    for project in selected_projects:
                        try:
                            req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                            extra = copy.deepcopy(common_extra)
                            if actual_type == "search":
                                valid, url_path, file_type, _, message = await validate_search_path(
                                    content, config, [project]
                                )
                                if not valid:
                                    failed.append(f"{project}：{message}")
                                    continue
                                extra.update({"url_path": url_path, "file_type": file_type})
                            elif actual_type == "svn":
                                valid, url_path, file_type, message = await validate_svn_url(content, config, [project])
                                if not valid:
                                    failed.append(f"{project}：{message}")
                                    continue
                                extra.update(
                                    {
                                        "url_path": url_path,
                                        "file_type": file_type,
                                        "warehouse": config.get("state_path", {}).get(
                                            live_summary_by_project.get(project, {}).get("state")
                                        ),
                                    }
                                )
                            row_id = None
                            if config.get("is_table_group"):
                                if config["label"] == config.get("first_col_label"):
                                    row_id = str(uuid.uuid4())
                                else:
                                    row_id = state["row_anchors"].get(project)
                                    if not row_id:
                                        failed.append(f"{project}：未选择表格同行基准项")
                                        continue
                            chip = build_new_overview_chip(
                                project=project,
                                config=config,
                                content=content,
                                notes=notes,
                                creator=creator,
                                req_max_ver=req_max_ver,
                                row_id=row_id,
                                processing_type=actual_type,
                                extra_data=extra,
                            )
                            inserted, message = await insert_overview_chip(project, config["label"], chip)
                            if not inserted:
                                skipped.append(f"{project}：{message}")
                                continue
                            successes.append(
                                {
                                    "project": project,
                                    "label": config["label"],
                                    "chip_id": chip["id"],
                                    "content": content,
                                    "state": True,
                                    "operation_type": "add_chip",
                                }
                            )
                            changed_pairs.add((project, config["label"]))
                        except Exception as ex:
                            logger.error("批量新增概述单项目处理失败", exc_info=True)
                            failed.append(f"{project}：{ex}")
                else:
                    if not state["chip_targets"]:
                        ui.notify("请至少选择一条需要修改状态的概述。", type="warning")
                        return
                    target_state = {"active": True, "pending": None, "inactive": False}[state["target_state"]]
                    group_labels = [
                        item.get("label") for item in over_config.get(config["role"], {}).get(config["group_name"], [])
                    ]
                    for encoded_target in state["chip_targets"]:
                        try:
                            project, chip_id = json.loads(encoded_target)
                            if project not in selected_projects:
                                skipped.append(f"{project}：项目已不在当前选择范围")
                                continue
                            req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                            current_chip = db_storage.get_deep_item(
                                [f"{project}_over_data", config["label"], chip_id], {}
                            )
                            if not current_chip:
                                skipped.append(f"{project}：概述条目已不存在")
                                continue
                            row_id = current_chip.get("row_id")
                            if (
                                config.get("is_table_group")
                                and config["label"] != config.get("first_col_label")
                                and not is_table_child_state_allowed(
                                    project,
                                    config["first_col_label"],
                                    row_id,
                                    req_max_ver,
                                    target_state,
                                )
                            ):
                                failed.append(f"{project}：目标状态等级不能高于同行首列概述状态")
                                continue
                            if target_state is True and current_chip.get("type") == "search":
                                valid, _, _, _, message = await validate_search_path(
                                    current_chip.get("content", ""), config, [project]
                                )
                                if not valid:
                                    failed.append(f"{project}：{message}")
                                    continue
                            if target_state is True and current_chip.get("type") == "svn":
                                valid, _, _, message = await validate_svn_url(
                                    current_chip.get("content", ""), config, [project]
                                )
                                if not valid:
                                    failed.append(f"{project}：{message}")
                                    continue
                            changed, message, updated_chip = await update_overview_chip_state(
                                project,
                                config["label"],
                                chip_id,
                                req_max_ver,
                                target_state,
                                creator,
                            )
                            if not changed or updated_chip is None:
                                skipped.append(f"{project}：{message}")
                                continue
                            await archive_related_record(project, config["label"], chip_id, creator)
                            successes.append(
                                {
                                    "project": project,
                                    "label": config["label"],
                                    "chip_id": chip_id,
                                    "content": updated_chip.get("content", ""),
                                    "state": target_state,
                                    "operation_type": "activ_change",
                                }
                            )
                            changed_pairs.add((project, config["label"]))
                            if (
                                target_state is False
                                and config.get("is_table_group")
                                and config["label"] == config.get("first_col_label")
                                and row_id
                            ):
                                cascaded_labels = await cascade_deactivate_table_row(
                                    project,
                                    group_labels,
                                    config["label"],
                                    row_id,
                                    req_max_ver,
                                    creator,
                                )
                                changed_pairs.update((project, label) for label in cascaded_labels)
                        except Exception as ex:
                            logger.error("批量修改概述状态单条处理失败", exc_info=True)
                            failed.append(f"状态修改失败：{ex}")

                related_labels = selected_related_labels(config)
                if related_labels:
                    flat_config = app.storage.general.get("over_config_data_flat", {})
                    overview_role = app.storage.general.get("overview_role", {})
                    for operation in successes:
                        changed_related = await apply_related_overview_impacts(
                            project=operation["project"],
                            related_labels=related_labels,
                            source_content=operation["content"],
                            source_state=operation["state"],
                            operation_type=operation["operation_type"],
                            creator=creator,
                            config_flat=flat_config,
                            overview_role=overview_role,
                        )
                        changed_pairs.update((operation["project"], label) for label in changed_related)

                for changed_project, changed_label in changed_pairs:
                    OverviewVersionManager.bump(changed_project, changed_label)
                    refresh_pending_for_label(changed_project, changed_label)
                flat_config = app.storage.general.get("over_config_data_flat", {})
                changed_project_roles = {
                    (changed_project, flat_config.get(changed_label, {}).get("role", config["role"]))
                    for changed_project, changed_label in changed_pairs
                }
                for changed_project, changed_role in changed_project_roles:
                    overview_role_update(changed_project, changed_role)

                result_lines = build_batch_result_lines(len(successes), skipped, failed)
                notify_batch_lines(
                    result_lines,
                    notification_type="positive" if successes and not failed else "warning",
                    timeout=0 if skipped or failed else 6000,
                )
                if successes:
                    batch_overview_dialog.close()
            except Exception as ex:
                logger.error("批量维护概述失败", exc_info=True)
                ui.notify(
                    f"批量处理异常：{ex}。已完成的单项不会被覆盖，请根据结果重新检查。",
                    type="negative",
                    timeout=0,
                    close_button="✖",
                )
            finally:
                if not submit_button.is_deleted:
                    submit_button.enable()
                if not submit_spinner.is_deleted:
                    submit_spinner.set_visibility(False)

        async def request_execute_batch():
            config = get_config()
            if not config:
                ui.notify("请选择具体概述项。", type="warning")
                return
            if not ensure_complete_table_row_bindings(config, state["projects"]):
                return
            related_labels = list(dict.fromkeys(label for label in config.get("impact_list", []) if label))
            if not related_labels or state["impact_mode"] != "none":
                await submit_batch_request()
                return

            confirm_dialog = ui.dialog().props("persistent")

            async def confirm_no_impact():
                confirm_dialog.close()
                await submit_batch_request()

            with confirm_dialog, ui.card().classes("w-full max-w-[520px]"):
                ui.label("请谨慎确认").classes("text-lg font-bold text-negative")
                ui.label(
                    f"本次将批量处理 {len(state['projects'])} 个项目，并明确选择不影响其它概述项。"
                    "确认后不会生成关联影响记录；如有疑虑，请返回重新选择。"
                )
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("返回重新选择", on_click=confirm_dialog.close).props("flat color=grey")
                    ui.button("确认不影响其它项", on_click=confirm_no_impact).props("color=negative")
            confirm_dialog.open()

        batch_overview_dialog.clear()
        with batch_overview_dialog, ui.card().classes("w-[1100px] max-w-[96vw] h-[90vh] p-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("批量维护概述").classes("text-xl font-bold text-blue-900")
                ui.button(icon="close", on_click=batch_overview_dialog.close).props("flat round dense")
            ui.label("仅处理项目状态为：待定、研发、转产的项目").classes("text-xs text-red-600 -mt-2")

            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full gap-3 pr-2"):
                    with ui.card().classes("w-full p-3 border border-blue-100 bg-blue-50/30 shadow-none"):
                        ui.label("一、选择批量处理项目").classes("text-base font-bold text-blue-900")
                        status_select = (
                            ui.select(
                                list(BATCH_OVERVIEW_ALLOWED_PROJECT_STATES),
                                label="项目状态",
                                multiple=True,
                            )
                            .bind_value(state, "states")
                            .props("outlined dense use-chips")
                            .classes("w-full")
                        )
                        with ui.grid(columns=3).classes("w-full gap-2"):
                            major_select = (
                                ui.select(list(category_map), label="项目大类")
                                .bind_value(state, "major")
                                .props("outlined dense")
                            )
                            sub_select = (
                                ui.select(category_map[default_major], label="项目小类")
                                .bind_value(state, "sub")
                                .props("outlined dense")
                            )
                            model_range_select = (
                                ui.select(default_model_range_options, label="型号范围")
                                .bind_value(state, "model_range")
                                .props("outlined dense")
                            )
                        project_select = (
                            ui.select({}, label="勾选目标项目", multiple=True)
                            .bind_value(state, "projects")
                            .props("outlined dense use-chips options-dense")
                            .classes("w-full")
                        )
                        with ui.row().classes("w-full items-center justify-between"):
                            selected_count_label = ui.label("").classes("text-xs text-gray-600")
                            with ui.row().classes("gap-1"):
                                ui.button("全选当前筛选", on_click=select_all_visible_projects).props(
                                    "flat dense color=primary"
                                )
                                ui.button(
                                    "清空",
                                    on_click=lambda: (
                                        state.update(projects=[]),
                                        project_select.set_value([]),
                                        render_editor(),
                                    ),
                                ).props("flat dense color=grey")

                    with ui.card().classes("w-full p-3 border border-gray-200 shadow-none"):
                        ui.label("二、选择操作与概述项").classes("text-base font-bold text-gray-800")
                        action_select = (
                            ui.radio({"add": "批量新增概述", "state": "批量修改激活状态"})
                            .bind_value(state, "action")
                            .props("inline")
                        )
                        with ui.grid(columns=3).classes("w-full gap-2"):
                            role_select = (
                                ui.select(roles, label="技术维度").bind_value(state, "role").props("outlined dense")
                            )
                            group_select = (
                                ui.select(role_groups, label="概述分组")
                                .bind_value(state, "group")
                                .props("outlined dense")
                            )
                            label_select = (
                                ui.select(label_options(default_role, default_group), label="具体概述项")
                                .bind_value(state, "label")
                                .props("outlined dense options-dense")
                            )

                    with ui.card().classes("w-full p-3 border border-amber-100 bg-amber-50/20 shadow-none"):
                        ui.label("三、填写处理内容").classes("text-base font-bold text-amber-900")
                        editor_container = ui.column().classes("w-full gap-2")

            with ui.row().classes("w-full items-center justify-end gap-2 pt-2 border-t"):
                submit_spinner = ui.spinner("hourglass", size="sm", color="amber-8")
                submit_spinner.set_visibility(False)
                ui.button("取消", on_click=batch_overview_dialog.close).props("flat color=grey")
                submit_button = ui.button("提交审批申请", icon="approval", on_click=request_execute_batch).props(
                    "color=primary"
                )

        status_select.on_value_change(lambda _=None: refresh_project_options())
        major_select.on_value_change(lambda _=None: refresh_project_options(reset_sub=True))
        sub_select.on_value_change(lambda _=None: refresh_project_options(reset_model_range=True))
        model_range_select.on_value_change(lambda _=None: refresh_project_options())
        project_select.on_value_change(
            lambda _=None: (
                selected_count_label.set_text(
                    f"当前筛选 {len(current_filtered_projects())} 个项目，已选择 {len(state['projects'])} 个"
                ),
                render_editor(),
            )
        )
        action_select.on_value_change(lambda _=None: reset_operation_selection())
        role_select.on_value_change(on_role_change)
        group_select.on_value_change(on_group_change)
        label_select.on_value_change(on_label_change)
        refresh_project_options()
        batch_overview_dialog.open()

    def show_project_correction_archives(target_project: str) -> None:
        """查看当前项目的纠错历史，包含已经被删除而无法从 chip 进入的记录。"""
        records = get_project_correction_archives(target_project)
        correction_archive_dialog.clear()
        with correction_archive_dialog, ui.card().classes("w-[960px] max-w-[96vw] h-[88vh] p-4"):
            with ui.row().classes("w-full items-center justify-between"):
                ui.label(f"{target_project}｜概述纠错历史").classes("text-xl font-bold text-purple-900")
                ui.button(icon="close", on_click=correction_archive_dialog.close).props("flat round dense")
            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full gap-3 pr-2"):
                    if not records:
                        with ui.column().classes("w-full items-center py-12 text-gray-400"):
                            ui.icon("history", size="4em").classes("opacity-50")
                            ui.label("当前项目没有纠错历史")
                    for record in records:
                        result = record.get("result") or {}
                        with ui.card().classes("w-full p-3 shadow-base border border-purple-100 bg-purple-50/20"):
                            with ui.row().classes("w-full items-center justify-between"):
                                ui.label(
                                    f"{record.get('title', record.get('label', '未命名'))} ｜ "
                                    f"{'纠正原记录' if record.get('action') == 'correct' else '删除错误记录'}"
                                ).classes("font-bold")
                                ui.badge(str(record.get("status") or "未知"), color="purple").props("outline")
                            ui.label(
                                f"申请人：{record.get('submitter', '')} ｜ 审批人：{record.get('reviewer', '')} ｜ "
                                f"时间：{record.get('reviewed_at', record.get('updated_at', ''))}"
                            ).classes("text-xs text-gray-600")
                            ui.label(f"理由：{record.get('reason', '')}").classes("text-sm")
                            file_change = result.get("file_change") or {}
                            if file_change:
                                ui.label(
                                    f"文件：{file_change.get('before_name', '')} → {file_change.get('after_name', '')}"
                                ).classes("text-xs font-medium text-blue-800")
                                ui.label(
                                    f"SHA256：{file_change.get('before_sha256', '') or '无'} → "
                                    f"{file_change.get('after_sha256', '') or '无'}"
                                ).classes("text-[11px] font-mono break-all text-gray-500")
                            for change in result.get("changes", []):
                                changed = change.get("changed") is True
                                with ui.row().classes("w-full items-start gap-2 border-t border-purple-100 pt-1"):
                                    ui.badge(
                                        "已变化" if changed else "未变化",
                                        color="orange" if changed else "grey",
                                    ).props("outline")
                                    if "before_select" in change:
                                        before_text = str(change.get("before_select") or "未选择")
                                        after_text = str(change.get("after_select") or "未选择")
                                        if change.get("before_other"):
                                            before_text += f"；{change['before_other']}"
                                        if change.get("after_other"):
                                            after_text += f"；{change['after_other']}"
                                        ui.label(f"{change.get('title', '')}：{before_text} → {after_text}").classes(
                                            "text-xs text-gray-700"
                                        )
                                    else:
                                        ui.label(
                                            f"{change.get('title', '')}：{change.get('before', '')} → "
                                            f"{change.get('after', '')}"
                                        ).classes("text-xs text-gray-700")
                            deleted_snapshots = result.get("deleted_snapshots", [])
                            if deleted_snapshots:
                                ui.label(f"已归档删除快照（{len(deleted_snapshots)} 条）").classes(
                                    "text-xs font-bold text-red-700"
                                )
                                with ui.row().classes("w-full gap-2 flex-wrap"):
                                    for snapshot in deleted_snapshots:
                                        ui.chip(str(snapshot.get("content") or "无内容"), icon="delete").props(
                                            "outline color=negative dense"
                                        )
        correction_archive_dialog.open()

    # --- 更新 requirement.py 中的 modify_overview_content_dialog 函数 ---

    async def modify_overview_content_dialog(project_name):
        """
        研发经理专用的修改概述内容弹窗（增强版）
        - 支持 text/file/image/svn/search/video 的 content/notes 修改
        - 特别支持 test 类型的 test_select_data 修改
        - 修改不覆盖原 creator，但记录修改历史
        """
        # 1. 准备数据容器
        over_config = app.storage.general.get("over_config_data", {})

        # 状态变量
        state = {"role": None, "label": None, "chip_id": None, "current_chip_data": None, "btn_config": None}

        # 存储 test 类型动态生成的输入控件引用
        test_inputs = {}

        # 创建对话框
        general_dialog.clear()
        with general_dialog, ui.card().classes("w-[700px] max-w-full"):
            ui.label(f"修改概述内容 - {project_name}").classes("text-xl font-bold text-amber-900")
            ui.label("注意：此功能直接修改底层数据，修改记录将留档，原创建人保持不变。").classes(
                "text-xs text-red-500 mb-2"
            )

            # --- 级联选择区域 ---
            select_container = ui.column().classes("w-full gap-2")

            # --- 编辑区域 (初始隐藏) ---
            editor_scroll = ui.scroll_area().classes("w-full h-[50vh] border-t pt-2 border-gray-200 mt-2")
            editor_scroll.set_visibility(False)

            with editor_scroll:
                editor_container = ui.column().classes("w-full gap-3")

            # 辅助：获取当前 label 对应的配置信息
            def get_button_config(role, label):
                if role and role in over_config:
                    for group_data_li in over_config[role].values():
                        for item_val in group_data_li:
                            if item_val["label"] == label:
                                return item_val
                return None

            # 定义选择器的更新逻辑
            def on_role_change(e):
                state["role"] = e.value
                state["label"] = None
                state["chip_id"] = None
                state["btn_config"] = None
                label_select.value = None
                chip_select.value = None
                label_select.options = get_label_options(e.value)
                label_select.update()
                chip_select.options = {}
                chip_select.update()
                editor_scroll.set_visibility(False)

            def on_label_change(e):
                state["label"] = e.value
                state["chip_id"] = None
                state["btn_config"] = get_button_config(state["role"], state["label"])
                chip_select.value = None
                chip_select.options = get_chip_options(state["label"])
                chip_select.update()
                editor_scroll.set_visibility(False)

            def on_chip_change(e):
                state["chip_id"] = e.value
                # [修复 1] 显式获取并检查变量，确保路径列表是纯字符串
                current_label = state["label"]
                current_id = e.value
                # 【重要修复】：清空上一轮的控件引用，防止保存时读取到僵尸控件报错
                test_inputs.clear()
                if current_id and current_label:
                    # 获取该chip的完整数据
                    path_list = [f"{project_name}_over_data", str(current_label), str(current_id)]
                    CHIP_DATA = db_storage.get_deep_item(path_list, {})

                    state["current_chip_data"] = copy.deepcopy(CHIP_DATA)
                    chip_type = CHIP_DATA.get("type", "text")

                    # --- 1. 基础内容填充 ---
                    content_input.value = CHIP_DATA.get("content", "")
                    notes_input.value = CHIP_DATA.get("notes", "")

                    # --- 2. 类型特殊处理 ---
                    test_ui_container.clear()

                    if chip_type == "test":
                        render_test_editor(CHIP_DATA)
                        test_ui_container.set_visibility(True)
                        type_warning.set_visibility(False)
                    elif chip_type != "text":
                        test_ui_container.set_visibility(False)
                        type_warning.text = (
                            f"当前类型为【{chip_type}】，修改内容通常意味着修改文件名或路径，请确保服务器存在对应文件。"
                        )
                        type_warning.set_visibility(True)
                    else:
                        test_ui_container.set_visibility(False)
                        type_warning.set_visibility(False)

                    editor_scroll.set_visibility(True)

            # 渲染测试项编辑器
            def render_test_editor(CHIP_DATA):
                test_data = CHIP_DATA.get("test_select_data", {})
                config = state["btn_config"] or {}

                with test_ui_container:
                    ui.label("测试项参数配置:").classes(
                        "text-sm font-bold text-deep-purple-700 bg-deep-purple-50 p-1 rounded"
                    )

                    # 辅助函数：创建一组 选择框 + 补充输入框
                    def create_test_group(label, key_prefix, options):
                        if not options:
                            return

                        with ui.row().classes("w-full items-start gap-2"):
                            # 下拉选择
                            select = (
                                ui.select(options=options, label=label, value=test_data.get(f"{key_prefix}_select"))
                                .classes("w-1/3")
                                .props("outlined dense options-dense")
                            )

                            # 补充文本框
                            is_other = select.value == "其它"
                            other_input = (
                                ui.textarea(
                                    placeholder="特殊要求描述", value=test_data.get(f"{key_prefix}_other_text", "")
                                )
                                .classes("flex-grow")
                                .props("outlined dense rows=1")
                            )
                            other_input.set_visibility(is_other)

                            # 绑定联动
                            def on_sel_change(e, inp=other_input):
                                inp.set_visibility(e.value == "其它")
                                if e.value != "其它":
                                    inp.value = ""

                            # [修复 2] 使用 on_value_change 替代 on_change
                            select.on_value_change(on_sel_change)

                            # 存入引用以便保存时读取
                            test_inputs[f"{key_prefix}_select"] = select
                            test_inputs[f"{key_prefix}_other_text"] = other_input

                    # 渲染三组配置
                    create_test_group("测试性质", "test_nature", config.get("test_nature_options", []))
                    create_test_group("条件/状态", "state", config.get("state_options", []))
                    create_test_group("节点/位置", "node", config.get("node_options", []))
                    create_test_group("工具/仪器", "instrument", config.get("instrument_options", []))

            # 辅助：获取Label选项
            def get_label_options(role):
                options = {}
                if role and role in over_config:
                    for group_li in over_config[role].values():
                        for item_val in group_li:
                            options[item_val["label"]] = f"{item_val.get('title', '未命名')} ({item_val.get('label')})"
                return options

            # 辅助：获取Chip选项
            def get_chip_options(label):
                options = {}
                if label:
                    CHIPS = db_storage.get_deep_item([f"{project_name}_over_data", label], {})
                    for c_id, c_data in CHIPS.items():
                        content = c_data.get("content", "无内容")
                        display = content[:30] + "..." if len(content) > 30 else content
                        options[c_id] = display
                return options

            # --- 界面构建 ---
            with select_container:
                role_options = list(over_config.keys())
                ui.select(options=role_options, label="1. 选择角色分类", on_change=on_role_change).classes("w-full")
                label_select = ui.select(options={}, label="2. 选择概述分类", on_change=on_label_change).classes(
                    "w-full"
                )
                chip_select = ui.select(options={}, label="3. 选择具体概述条目", on_change=on_chip_change).classes(
                    "w-full"
                )

            with editor_container:
                type_warning = ui.label("").classes("text-xs text-orange-600 font-bold")

                # 测试项专用容器
                test_ui_container = ui.column().classes("w-full gap-2 p-2 border border-deep-purple-100 rounded")

                ui.label("概述内容 (Content):").classes("text-sm font-bold text-gray-600")
                content_input = ui.textarea(placeholder="修改内容").classes("w-full").props("outlined auto-grow")

                ui.label("注释 (Notes):").classes("text-sm font-bold text-gray-600")
                notes_input = ui.textarea(placeholder="修改注释").classes("w-full").props("outlined auto-grow")

                async def save_modification():
                    # [修复 3] 增加对 current_chip_data 的非空检查，消除 None 类型访问属性的报错
                    if not state["chip_id"] or not state["label"] or state["current_chip_data"] is None:
                        return

                    new_content = content_input.value.strip()
                    new_notes = notes_input.value.strip()

                    if not new_content:
                        ui.notify("内容不能为空", type="warning")
                        return

                    try:
                        # 确保路径是字符串列表
                        label_str = str(state["label"])
                        id_str = str(state["chip_id"])
                        base_path = [f"{project_name}_over_data", label_str, id_str]

                        # 1. 更新基础字段
                        await db_storage.set_deep_item(base_path + ["content"], new_content)
                        await db_storage.set_deep_item(base_path + ["notes"], new_notes)

                        # 2. 如果是 Test 类型，收集并保存 test_select_data
                        if state["current_chip_data"].get("type") == "test":
                            new_test_data = {}
                            for key, input_elem in test_inputs.items():
                                if input_elem:
                                    new_test_data[key] = input_elem.value

                            final_test_data = state["current_chip_data"].get("test_select_data", {}).copy()
                            final_test_data.update(new_test_data)

                            await db_storage.set_deep_item(base_path + ["test_select_data"], final_test_data)

                        ui.notify("修改已保存！", type="positive")

                    except Exception as e:
                        logger.error(f"修改概述失败: {e}")
                        ui.notify(f"保存失败: {e}", type="negative")

                with ui.row().classes("w-full justify-end mt-2 pb-4"):
                    ui.button("取消", on_click=general_dialog.close).props("flat color=grey")
                    ui.button("确认修改并保存", on_click=save_modification).props("color=red icon=save")

        general_dialog.open()

    # 需求显示界面框架构造函数
    async def overview_input_frame(json_data, temp_bool, pn=""):
        if json_data:
            project_name = json_data["1.0"]["project_name"]
        else:
            project_name = pn

        if project_name == "":
            ui.notify(
                "项目名获取失败，无法响应！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return

        # 判断服务器存储器概述数据字典里是否已经存在该项目键值对，没有则创建，用于后续储存该项目需求概述资料
        # 定义更新规则：如果当前存在有效数据则跳过写入，如果为 None 则初始化为空字典
        def init_if_missing(current_data):
            return db_storage.ATOMIC_NO_UPDATE if current_data is not None else {}

        # 将检查与初始化的动作合并为一个原子操作
        await db_storage.atomic_deep_update([f"{project_name}_over_data"], init_if_missing)

        # 只在正式概述页做一次性自愈：同一项目同一最高已审版本不重复扫描概述数据。
        if not temp_bool:
            req_max_ver = app.storage.general.get("project_req_max_ver", {}).get(project_name)
            checked_versions = app.storage.general.setdefault("overview_active_state_checked_versions", {})
            if req_max_ver and checked_versions.get(project_name) != req_max_ver:
                try:
                    if not json_data:
                        raise RequirementOverviewImpactConfigError("缺少正式概述整理数据，无法推导需求影响范围")
                    affected_labels, missing_node_ids, _change_node_ids = get_requirement_overview_impacts(
                        json_data,
                        req_max_ver,
                        project_name,
                    )
                    overview_success, changed_labels = await set_overview_active_state(
                        project_name,
                        req_max_ver,
                        affected_labels,
                    )
                except RequirementOverviewImpactConfigError as exc:
                    overview_success = False
                    changed_labels = set()
                    affected_labels = set()
                    missing_node_ids = set()
                    logger.error(
                        "打开概述页时无法解析精准影响范围: project=%s, version=%s, error=%s",
                        project_name,
                        req_max_ver,
                        exc,
                    )
                if overview_success:
                    checked_versions[project_name] = req_max_ver
                    for label in changed_labels:
                        OverviewVersionManager.bump(project_name, label)
                    refresh_overview_pending_labels(project_name, affected_labels)
                    if changed_labels:
                        logger.info(
                            "已按需求影响配置自动补齐概述激活版本记录: "
                            "project=%s, version=%s, labels=%s, unmapped_node_ids=%s",
                            project_name,
                            req_max_ver,
                            sorted(changed_labels),
                            sorted(missing_node_ids),
                        )
                else:
                    logger.error(
                        "打开概述页时补齐概述激活版本记录失败: project=%s, version=%s",
                        project_name,
                        req_max_ver,
                    )

        # --- 新增辅助函数：展示 Role 维度的历史记录 (Feature 1) ---
        def show_role_history_dialog(project_name, role):
            # 1. 收集该 Role 下所有 Label 的数据
            # 我们遍历数据库中该项目的所有 label，如果该 label 属于当前 role，则收集
            # 注意：这里需要知道 label -> role 的映射。
            # 我们可以遍历 app.storage.general["over_config_data"] 来找到该 role 下的所有 label key
            target_labels = []
            if role in app.storage.general.get("over_config_data", {}):
                for group_li in app.storage.general["over_config_data"][role].values():
                    for item in group_li:
                        target_labels.append((item["label"], item.get("title", "无标题")))

            all_history = []
            for label, title in target_labels:
                # 获取该 label 下的所有 chip
                CHIPS = db_storage.get_deep_item([f"{project_name}_over_data", label], {})
                for CHIP_INFO in CHIPS.values():
                    TIMESTAMPS = CHIP_INFO.get("timestamp", {})
                    creation_time = min(TIMESTAMPS.keys()) if TIMESTAMPS else "N/A"
                    all_history.append(
                        {
                            "label": label,  # 额外记录所属标签
                            "title": title,
                            "content": CHIP_INFO.get("content", "N/A"),
                            "req_ver": CHIP_INFO.get("req_ver", "0.0"),
                            "creation_time": creation_time,
                            "creator": CHIP_INFO.get("creator", "未知"),
                            "type": CHIP_INFO.get("type", ""),
                            "enabled": CHIP_INFO.get("enabled", True),
                        }
                    )

            # 2. 排序
            try:
                all_history.sort(key=lambda x: (float(x["req_ver"]), x["creation_time"]))
            except ValueError:
                all_history.sort(key=lambda x: (x["req_ver"], x["creation_time"]))

            # 3. 构建 UI
            general_dialog.clear()
            with general_dialog, ui.card().classes("w-[900px] max-w-full h-[80vh]"):
                with ui.row().classes("w-full justify-between items-center"):
                    ui.label(f"全项历史记录: {role}").classes("text-xl font-bold text-gray-800")
                    ui.button(icon="close", on_click=general_dialog.close).props("flat round dense")
                ui.label("概述文字颜色效果代表当前激活状态").classes("text-sm text-gray-500 mt-0 mb-1")
                ui.separator()

                with ui.scroll_area().classes("w-full flex-grow"):
                    if not all_history:
                        ui.label("暂无记录").classes("w-full text-center text-gray-500 mt-4")

                    current_ver = None
                    for item in all_history:
                        if item["req_ver"] != current_ver:
                            current_ver = item["req_ver"]
                            ui.label(f"需求版本V{current_ver}生效后提交的概述：").classes(
                                "text-base font-bold text-amber-900 mt-3 mb-1 bg-amber-50 px-2 py-1 rounded"
                            )

                        with ui.row().classes(
                            "w-full items-center p-2 border-b border-gray-100 hover:bg-gray-50 text-sm"
                        ):
                            # 时间与作者
                            with ui.column().classes("w-32 gap-0"):
                                ui.label(format_overview_timestamp(item["creation_time"])).classes(
                                    "text-xs text-gray-500"
                                )
                                ui.label(item["creator"]).classes("text-xs font-bold text-blue-600")

                            # 所属标签 (特有)
                            ui.label(f"在[{item['title']}]添加：").classes("text-xs font-bold text-amber-600 truncate")

                            # 内容
                            with ui.row().classes("flex-grow items-center gap-2"):
                                if item["type"] in ["file", "image", "svn", "search"]:
                                    ui.icon("attachment", size="xs", color="grey")
                                if item["enabled"]:
                                    color = "text-blue-400"
                                elif item["enabled"] == "null":
                                    color = "text-orange-400 italic"
                                else:
                                    color = "text-gray-400 line-through"
                                ui.label(item["content"]).classes(f"font-medium {color}")

            general_dialog.open()

        # 需求界面内容
        header.clear()
        with header:
            ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
            # )  # 左侧对齐
            ui.label("概述整理模块").classes(
                "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
            )  # 绝对定位居中

            with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
                ui.image(current_display_path)
                with ui.menu().props("auto-close"):
                    ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                    ui.separator().props("size=1px")
                    ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                    ui.menu_item("返回项目信息表", on_click=lambda: ui.navigate.to("/project_table"))
                    ui.menu_item("注销登录", on_click=lambda: logout())
                    ui.separator().props("size=1px")
                    ui.menu_item("对比需求", on_click=show_comparison_dialog)
                    ui.separator().props("size=1px")
                    ui.menu_item("概述纠错历史", on_click=lambda: show_project_correction_archives(project_name))
                    if current_role == "研发经理":
                        ui.separator().props("size=1px")
                        ui.menu_item("修改概述内容", on_click=lambda: modify_overview_content_dialog(project_name))
                    if current_role in BATCH_OVERVIEW_TOOL_ROLES:
                        ui.separator().props("size=1px")
                        ui.menu_item("批量维护概述", on_click=batch_overview_maintenance_dialog)

            with ui.row().classes("font-sans h-[calc(100vh-9rem)] items-stretch flex-nowrap w-full mt-3 text-black"):
                # 需求内容列
                with ui.column().classes("w-5/12 min-w-[400px]"):
                    requirements_loading_row = None
                    with ui.row().classes("relative w-full items-center justify-center min-h-[32px]"):
                        ui.label(f"{project_name} 需求内容").classes("text-xl text-center w-full")
                        if json_data:
                            requirements_loading_row = ui.row().classes(
                                "absolute left-2 top-1/2 -translate-y-1/2 items-center gap-1 flex-nowrap"
                            )
                            with requirements_loading_row:
                                ui.spinner("dots", size="1.5em", color="primary")
                                ui.label("需求内容加载中...").classes("text-xs text-gray-500 whitespace-nowrap")
                    with ui.column().classes("w-full overflow-y-auto p-1 gap-4"):
                        if json_data:
                            # === 步骤 1: 预处理 - 收集所有条目并获取其排序/分组信息 ===
                            version_keys = sorted([k for k in json_data if k.replace(".", "", 1).isdigit()], key=float)
                            # 储存最新版元素
                            ui_expansion = {}
                            ui_elements_latest = {}
                            render_batch_size = 20  # 每批处理的需求项数量；调大更快，调小更流畅
                            render_yield_delay = 0  # 仅让出事件循环，不额外增加固定等待时间
                            version_render_jobs = []
                            version_expansions = []

                            def sync_latest_version_markers(version, version_data):
                                """保持汇总视图中的最后变更版本标记，不依赖历史版本 UI 是否已经展开。"""
                                if version == "0":
                                    return
                                for item_data in version_data.get("added", {}).values():
                                    node_id = item_data.get("node_id")
                                    if node_id in ui_elements_latest and format_show_string(item_data) != "无":
                                        ui_elements_latest[node_id] = version
                                for item_data in version_data.get("modified", {}).values():
                                    node_id = item_data.get("new_data", {}).get("node_id")
                                    if node_id not in ui_elements_latest:
                                        continue
                                    old_text = format_show_string(item_data.get("old_data", {}))
                                    new_text = format_show_string(item_data.get("new_data", {}))
                                    if old_text != "无" or new_text != "无":
                                        ui_elements_latest[node_id] = version

                            async def render_version_content(
                                exp,
                                version,
                                version_data,
                                sorted_items,
                                ui_elements,
                                ui_cards,
                                group_id_li,
                            ):
                                content_items_processed = 0

                                async def finish_content_item():
                                    nonlocal content_items_processed
                                    content_items_processed += 1
                                    if content_items_processed % render_batch_size == 0:
                                        await asyncio.sleep(render_yield_delay)

                                with exp:
                                    with ui.column().classes("w-full gap-4") as exp_content:
                                        for item_index, item_info in enumerate(sorted_items):
                                            # 获取需求ID
                                            node_id = item_info["node_id"]
                                            # 获取分组ID
                                            group_id = item_info["option_group_id"]
                                            change_type = item_info["change_type"]

                                            if group_id == "":
                                                continue
                                            # 如果是新的分组，则添加卡元素
                                            if group_id not in group_id_li:
                                                # ui.separator().props("size=1px").classes("my-2 bg-grey-1 h-0.3 rounded-sm shadow-1")
                                                with ui.card().classes(
                                                    # f"bg-{'blue-50/50' if float(group_id) % 2 == 0 else 'amber-50/50'} rounded-md shadow-1 p-2 gap-2 w-full"
                                                    "rounded-md shadow-1 px-2 pt-2 pb-0 gap-2 w-full"
                                                ) as ui_card:
                                                    # ui.label(f"需求组编号：{int(float(group_id))}").classes(
                                                    #     "text-gray-500 text-[10px]/[16px] font-medium"
                                                    # )
                                                    ui.badge(f"{int(float(group_id))}", color="bg-gray-500/10").classes(
                                                        "bg-gray-500/30 py-0 px-1 rounded-md text-[8px]/[12px]"
                                                    ).style("position:absolute;top: -4px;left: -3px;")
                                                ui_cards[group_id] = ui_card
                                                group_id_li.append(group_id)
                                                # 将容器的可见性先设为False，有内容时再打开
                                                ui_card.visible = False

                                            # 创建UI容器和占位符
                                            with ui_cards[group_id]:
                                                with ui.column().classes(
                                                    "w-full gap-2 mb-1 text-[14px]/[20px] text-gray-500 bg-gradient-to-b from-gray-50/10 to-gray-300/10 rounded-md"
                                                ) as container:
                                                    # 将容器的可见性先设为False，有内容时再打开
                                                    container.visible = False
                                                    old_column = None
                                                    old_content = None
                                                    old_row = None
                                                    old_ref_row = None
                                                    # 只有修改项才需要“修改前”整套展示结构
                                                    if change_type == "modified":
                                                        with ui.column().classes(
                                                            "items-start w-full gap-0"
                                                        ) as old_column:
                                                            old_content = ui.markdown()
                                                            with ui.row().classes("items-start gap-0") as old_row:
                                                                ui.label("引用文件：")
                                                                old_ref_row = ui.row().classes("gap-0")
                                                            old_row.visible = False
                                                        old_column.visible = False
                                                    with ui.row().classes("items-start w-full gap-0"):
                                                        version_badge = ui.badge().classes("my-1 mr-1")
                                                        with ui.column().classes("items-start gap-0"):
                                                            content = ui.markdown()
                                                            with ui.row().classes("items-start gap-0") as new_row:
                                                                ui.label("引用文件：")
                                                                ref_row = ui.row().classes("gap-0")
                                                            new_row.visible = False
                                                        ui.space()
                                                        role_row = ui.row().classes("gap-0")
                                                    # history_container = ui.column().classes("w-full pl-4 gap-0")

                                                    # 存储UI元素引用
                                                    ui_elements[node_id] = {
                                                        "container": container,
                                                        "group_card": ui_cards[group_id],
                                                        "old_column": old_column,
                                                        "old_content": old_content,
                                                        "old_ref_row": old_ref_row,
                                                        "new_row": new_row,
                                                        "old_row": old_row,
                                                        "version_badge": version_badge,
                                                        "content": content,
                                                        "ref_row": ref_row,
                                                        "role_badge": role_row,
                                                        # "history_container": history_container,
                                                    }
                                                    # 单独创建最新版模块的元素字典
                                                    # if version == "0":
                                                    #     ui_elements_latest[node_id] = ui_elements[node_id]

                                            # 分批创建UI，避免一次性占满事件循环导致页面无响应
                                            if (item_index + 1) % render_batch_size == 0:
                                                await asyncio.sleep(render_yield_delay)

                                        # === 步骤 4: 按时间顺序填充和更新UI ===
                                        # for version in version_keys:
                                        # version_data = json_data[version]
                                        # version_num = version_data.get("version", "N/A")
                                        user = version_data.get("current_user", "")
                                        timestamp = (
                                            version_data.get("req_timestamp", "N/A").replace("T", " ").split(".")[0]
                                        )

                                        # 处理新增
                                        for item_data in version_data.get("added", {}).values():
                                            node_id = item_data.get("node_id")
                                            if node_id in ui_elements:
                                                target = ui_elements[node_id]
                                                show_str = format_show_string(item_data)
                                                if show_str != "无":
                                                    target["container"].visible = True  # 填充内容，设为可见
                                                    target["group_card"].visible = True  # 填充内容，设为可见
                                                    status = "新增"
                                                    if version == version_keys[1]:
                                                        status = "初版"
                                                    # 如果是最新版模块，则显示版本标签
                                                    elif version == "0":
                                                        ui_elements_latest[node_id] = "1.0"
                                                        target["version_badge"].bind_text_from(
                                                            ui_elements_latest, node_id
                                                        )
                                                        status = "1.0"
                                                    else:
                                                        if node_id in ui_elements_latest.keys():
                                                            ui_elements_latest[node_id] = version
                                                    target["version_badge"].set_text(f"{status}")
                                                    color = "blue-grey-2" if status == "初版" else "green-7"
                                                    # if node_id in ui_elements_latest.keys():
                                                    #     ui_elements_latest[node_id]["version_badge"].set_text(f"{version}")

                                                    # ui_expansion["latest"].update()
                                                    target["version_badge"].props(f"color={color}")
                                                    with target["version_badge"]:
                                                        # target["version_badge"].clear()
                                                        tooltip_text = (
                                                            f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                        )
                                                        with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                            ui.html(
                                                                tooltip_text, sanitize=False
                                                            )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize
                                                    target["content"].set_content(show_str)
                                                    if item_data["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["ref_row"]:
                                                            for t_lab in item_data["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                        target["new_row"].visible = True
                                                    if item_data["option_view"]:
                                                        with target["role_badge"]:
                                                            for role in item_data["option_view"].split("+"):
                                                                add_role_badge(role)

                                            # 分批填充内容，使已经生成的部分可以及时发送到浏览器
                                            await finish_content_item()

                                        # 处理删除
                                        for item_data in version_data.get("deleted", {}).values():
                                            node_id = item_data.get("node_id")
                                            if node_id in ui_elements:
                                                target = ui_elements[node_id]
                                                show_str = format_show_string(item_data)
                                                if show_str != "无":
                                                    target["container"].visible = True
                                                    target["group_card"].visible = True  # 填充内容，设为可见
                                                    target["version_badge"].set_text("删除")
                                                    target["version_badge"].props("color=red-7")
                                                    with target["version_badge"]:
                                                        # target["version_badge"].clear()
                                                        tooltip_text = (
                                                            f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                        )
                                                        with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                            ui.html(
                                                                tooltip_text, sanitize=False
                                                            )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                                                    target["content"].set_content(f"<del>{show_str}</del>")
                                                    target["content"].classes(add="text-gray-400")
                                                    if item_data["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["ref_row"]:
                                                            for t_lab in item_data["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                        target["new_row"].visible = True
                                                    if item_data["option_view"]:
                                                        with target["role_badge"]:
                                                            for role in item_data["option_view"].split("+"):
                                                                add_role_badge(role)
                                            await finish_content_item()
                                        # 处理修改
                                        for item_data in version_data.get("modified", {}).values():
                                            node_id = item_data.get("new_data", {}).get("node_id")
                                            if node_id in ui_elements:
                                                target = ui_elements[node_id]
                                                new_text = format_show_string(item_data["new_data"])
                                                old_text = format_show_string(item_data["old_data"])
                                                # 判断是首次填充还是追加历史
                                                # 之前是空的，现在首次填充
                                                if old_text == "无":
                                                    if new_text != "无":
                                                        target["container"].visible = True
                                                        target["group_card"].visible = True  # 填充内容，设为可见
                                                        target["version_badge"].set_text("新增")
                                                        # 更新最新版模块版本标签
                                                        if node_id in ui_elements_latest.keys():
                                                            ui_elements_latest[node_id] = version
                                                        target["version_badge"].props("color=green-7")
                                                        with target["version_badge"]:
                                                            # target["version_badge"].clear()
                                                            tooltip_text = f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                            with ui.tooltip("").classes(
                                                                "bg-gray-700 text-white min-w-40"
                                                            ):
                                                                ui.html(
                                                                    tooltip_text, sanitize=False
                                                                )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize
                                                        target["content"].set_content(new_text)
                                                        if item_data["new_data"]["ref_out"]:
                                                            # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                            with target["ref_row"]:
                                                                for t_lab in item_data["new_data"]["ref_out"]:
                                                                    thumbnail_obj = app.storage.client[
                                                                        "file_thumbnail_dic"
                                                                    ][t_lab]["file_obj"]
                                                                    add_overview_lab(thumbnail_obj)
                                                            target["new_row"].visible = True
                                                        if item_data["new_data"]["option_view"]:
                                                            with target["role_badge"]:
                                                                for role in item_data["new_data"]["option_view"].split(
                                                                    "+"
                                                                ):
                                                                    add_role_badge(role)
                                                else:  # 之前已有内容，追加更改
                                                    if new_text == "无":
                                                        target["container"].visible = True
                                                        target["group_card"].visible = True  # 填充内容，设为可见
                                                        target["version_badge"].set_text("作废")
                                                        # 更新最新版模块版本标签，作废的一般进入不了这个条件判断，保险先放着
                                                        if node_id in ui_elements_latest.keys():
                                                            ui_elements_latest[node_id] = version
                                                        target["version_badge"].props("color=red-7")
                                                        with target["version_badge"]:
                                                            # target["version_badge"].clear()
                                                            tooltip_text = f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                            with ui.tooltip("").classes(
                                                                "bg-gray-700 text-white min-w-40"
                                                            ):
                                                                ui.html(
                                                                    tooltip_text, sanitize=False
                                                                )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                                                        target["content"].set_content(f"<del>{old_text}</del>")
                                                        target["content"].classes(add="text-gray-400")
                                                        if item_data["old_data"]["ref_out"]:
                                                            # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                            with target["ref_row"]:
                                                                for t_lab in item_data["old_data"]["ref_out"]:
                                                                    thumbnail_obj = app.storage.client[
                                                                        "file_thumbnail_dic"
                                                                    ][t_lab]["file_obj"]
                                                                    add_overview_lab(thumbnail_obj)
                                                        if item_data["old_data"]["option_view"]:
                                                            with target["role_badge"]:
                                                                for role in item_data["old_data"]["option_view"].split(
                                                                    "+"
                                                                ):
                                                                    add_role_badge(role)
                                                    else:
                                                        target["container"].visible = True
                                                        target["group_card"].visible = True  # 填充内容，设为可见
                                                        target["old_column"].visible = True
                                                        target["old_content"].set_content(old_text)
                                                        if item_data["old_data"]["ref_out"]:
                                                            # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                            with target["old_ref_row"]:
                                                                for t_lab in item_data["old_data"]["ref_out"]:
                                                                    thumbnail_obj = app.storage.client[
                                                                        "file_thumbnail_dic"
                                                                    ][t_lab]["file_obj"]
                                                                    add_overview_lab(thumbnail_obj)
                                                            target["old_row"].visible = True
                                                        target["version_badge"].set_text("更改为")
                                                        # 更新最新版模块版本标签
                                                        if node_id in ui_elements_latest.keys():
                                                            ui_elements_latest[node_id] = version
                                                        target["version_badge"].props("color=orange-7")
                                                        with target["version_badge"]:
                                                            tooltip_text = f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                            with ui.tooltip("").classes(
                                                                "bg-gray-700 text-white min-w-40"
                                                            ):
                                                                ui.html(
                                                                    tooltip_text, sanitize=False
                                                                )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize
                                                        target["content"].set_content(new_text)
                                                        if item_data["new_data"]["ref_out"]:
                                                            # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                            with target["ref_row"]:
                                                                for t_lab in item_data["new_data"]["ref_out"]:
                                                                    thumbnail_obj = app.storage.client[
                                                                        "file_thumbnail_dic"
                                                                    ][t_lab]["file_obj"]
                                                                    add_overview_lab(thumbnail_obj)
                                                            target["new_row"].visible = True
                                                        if item_data["new_data"]["option_view"]:
                                                            with target["role_badge"]:
                                                                for role in item_data["new_data"]["option_view"].split(
                                                                    "+"
                                                                ):
                                                                    add_role_badge(role)

                                            await finish_content_item()

                                # 只给显示出来的card进行间隔上色
                                n = 1
                                for child in exp_content.default_slot.children:
                                    if not child.visible:
                                        continue
                                    if n == 1:
                                        child.classes("bg-blue-100/40 shadow-xs shadow-blue-300/30")
                                        n = 0
                                    else:
                                        child.classes("bg-amber-100/40 shadow-xs shadow-amber-300/30")
                                        n = 1

                            for version in version_keys:
                                all_items_info = {}
                                version_data = json_data[version]
                                # 从 added 和 deleted 和 modified.new_data 中收集
                                all_change_items = (
                                    [("added", item) for item in version_data.get("added", {}).values()]
                                    + [("deleted", item) for item in version_data.get("deleted", {}).values()]
                                    + [
                                        ("modified", item["new_data"])
                                        for item in version_data.get("modified", {}).values()
                                    ]
                                )
                                for change_type, item_data in all_change_items:
                                    node_id = item_data.get("node_id")
                                    if node_id and node_id not in all_items_info:
                                        all_items_info[node_id] = {
                                            "node_id": node_id,
                                            "num": item_data.get("num", 999),  # 默认值，确保未提供序号的排在最后
                                            "option_group_id": item_data.get("option_group_id", 999),
                                            "change_type": change_type,
                                        }

                                # === 步骤 2: 排序 - 根据分组ID和组内序号进行排序 ===
                                sorted_items = sorted(
                                    all_items_info.values(),
                                    key=lambda x: (int(float(x["option_group_id"])), int(float(x["num"]))),
                                )

                                # === 步骤 3: 搭建UI骨架 - 根据排序结果创建占位容器和分隔线 ===
                                ui_elements = {}
                                ui_cards = {}
                                group_id_li = []
                                original_str = ""
                                original_version = version_data.get("original_version", "0.0")
                                original_project = version_data.get("original_project", "")
                                # 非全新配置需求
                                if original_project != "":
                                    # 处理项目名来源信息
                                    if original_project == project_name:
                                        original_str = f"修改自：{original_project}"
                                    elif version == "1.0":
                                        original_str = f"复制自：{original_project}"
                                    else:
                                        original_str = f"衍生自：{original_project}"

                                    # 增加版本信息
                                    # 不是呈现最新版本栏，且衍生自某个版本
                                    if version != "0" and original_version != "0.0":
                                        original_str = f"{original_str}，V{original_version}"
                                    # 双重保险，即使有参照项目名（参照自己），只要参照版本为0.0（参照别的项目不会是0.0），包括特殊情况（全新输入再提交前改了名字），依旧判定为全新
                                    elif version != "0" and original_version == "0.0":
                                        original_str = "全新配置需求"
                                    # 属于呈现最新版本栏
                                    else:
                                        original_str = ""
                                # 全新配置需求
                                else:
                                    # 不是呈现最新版本栏
                                    if version != "0":
                                        original_str = "全新配置需求"

                                # 处理需求内容标题内容
                                version_label = f"版本V{version}变更内容"
                                # 获取需求提交日期
                                pass_time = (
                                    app.storage.general["wait_review"]
                                    .get(project_name, {})
                                    .get(version, {})
                                    .get("pass_time", "")
                                )
                                if pass_time and version != "0":
                                    req_time = datetime.fromisoformat(pass_time).strftime("%Y年%m月%d日%H时%M分%S秒")
                                    original_str = f"{original_str}，于{req_time}评审通过生效。"
                                if version == "0":
                                    version_label = f"需求汇总内容（更新到版本V{version_data['version']}）"
                                exp = ui.expansion(
                                    version_label,
                                    icon="text_snippet" if version == "0" else "difference",
                                    value=False,
                                    caption=f"{original_str}",
                                    group="group",
                                ).classes("gap-1 w-full bg-gray-100/30 rounded")
                                # 将最新版扩展元素存放，以便后续持续刷新
                                if version == "0":
                                    ui_expansion["latest"] = exp
                                exp.props("disable")
                                version_expansions.append(exp)
                                version_render_jobs.append(
                                    (exp, version, version_data, sorted_items, ui_elements, ui_cards, group_id_li)
                                )

                            async def load_all_requirement_versions():
                                # 先让页面和加载提示发送到浏览器，再开始后台构建需求内容
                                await asyncio.sleep(0.05)
                                try:
                                    for render_job in version_render_jobs:
                                        await render_version_content(*render_job)
                                    for history_version in version_keys:
                                        sync_latest_version_markers(history_version, json_data[history_version])
                                finally:
                                    if requirements_loading_row is not None:
                                        requirements_loading_row.delete()
                                    for expansion in version_expansions:
                                        expansion.props(remove="disable")

                            # 必须禁用 immediate，确保页面框架和加载提示先发送到浏览器，再启动后台构建
                            ui.timer(0.2, load_all_requirement_versions, once=True, immediate=False)
                        else:
                            ui.label("暂无需求数据").classes("text-gray-500 text-center w-full mt-10")
                ui.separator().props("vertical size=1px")

                # 概述内容列
                with ui.column().classes("w-7/12 min-w-[800px] items-center"):
                    # 只要有人查阅过项目的概述，就会创建该项目 项目工程师负责人 的条目
                    app.storage.general["project_engineer"].setdefault(project_name, "未指定")
                    # 优化容器：使用 justify-between 分隔左右，items-center 垂直居中，增加内边距和底部留白
                    with ui.row().classes("relative w-full items-center justify-between px-2 border-gray-200"):
                        # 1. 左侧操作区：状态 + 负责人
                        with ui.row().classes("items-center gap-3"):
                            if current_role in ["研发经理", "研发助理"]:
                                # 下拉框：移除 absolute，保留 small-select
                                ui.select(
                                    PROJECT_STATE_LIST,
                                    value=app.storage.general["project_summary"][project_name].get("state"),
                                    on_change=lambda e: set_project_state(project_name, e),
                                ).props("outlined dense options-dense").classes(
                                    "w-24 small-select"
                                )  # 给个固定宽或者让它自适应
                            else:
                                # Chip：移除 absolute
                                ui.chip(icon="star", color="amber-7").props("outline dense").classes(
                                    "text-sm m-0"
                                ).bind_text_from(app.storage.general["project_summary"][project_name], "state")
                            if current_role == "研发经理":
                                project_engineer = app.storage.general["project_engineer"].get(project_name, "未指定")
                                engineer_button = ui.button(project_engineer)
                                engineer_button.on_click(
                                    lambda pn=project_name, bt=engineer_button: set_project_engineer_dialog(pn, bt)
                                ).props("outline dense").classes("text-sm px-3")  # 移除 absolute, 增加 padding
                                engineer_button.bind_text_from(app.storage.general["project_engineer"], project_name)
                            else:
                                ui.chip(icon="engineering", color="blue-7").props("outline dense").classes(
                                    "text-sm m-0"
                                ).bind_text_from(app.storage.general["project_engineer"], project_name)

                        # 2. 中间标题区：使用绝对定位确保完美的视觉居中
                        ui.label(f"{project_name} 概述整理").classes(
                            "text-xl text-gray-800 absolute left-1/2 top-1/2 transform -translate-x-1/2 -translate-y-1/2"
                        )

                        # 3. 右侧操作区：打印按钮 + 开关
                        with ui.row().classes("items-center gap-4"):
                            ui.button(
                                "测试项",
                                icon="print",
                                on_click=lambda: ui.run_javascript(
                                    f'window.open("/report/test_summary/{project_name}", "_blank")'
                                ),
                            ).props("flat dense").classes("text-sm text-blue-800 hover:bg-blue-100 px-2")

                            if (
                                "研发" in app.storage.user.get("current_role", "")
                                or app.storage.user.get("current_role", "") == "NPI工程"
                                or app.storage.user.get("current_role", "") == "admin"
                            ):
                                app.storage.client.setdefault("record_switch", False)
                                # 开关：移除 absolute，增加 keep-color 保持颜色鲜艳
                                ui.switch("查阅失活概述").props("dense").classes("text-sm text-gray-600").bind_value(
                                    app.storage.client, "record_switch"
                                )
                    with ui.column().classes("w-full overflow-y-auto p-1 gap-2 rounded"):
                        overview_role_update(project_name, "initialize")
                        # 显示概述模块内容
                        num_chip_dic = {}
                        group_li_dic = {}
                        client_last_sync_time = 0

                        async def _update_num_chip_text():
                            nonlocal client_last_sync_time
                            # 1. 极低成本获取全局最后更新时间（如果没有，默认为1）
                            global_update_time = app.storage.general.get("overview_last_update", {}).get(
                                project_name, 1
                            )
                            # 2. 如果全局时间戳没有变，直接 return，阻断后续所有高昂的计算！
                            if client_last_sync_time == global_update_time:
                                return
                            # 3. 记录新的时间戳
                            client_last_sync_time = global_update_time
                            over_flat = app.storage.general.get("over_config_data_flat", {})
                            for role in app.storage.general["over_config_data"].keys():
                                num_chip_dic.setdefault(role, {})
                                raw_latest_user = (
                                    app.storage.general["overview_role"]
                                    .get(project_name, {})
                                    .get(role, {})
                                    .get("latest_user", "")
                                )
                                latest_user = (
                                    raw_latest_user.split("：")[1] if "：" in raw_latest_user else raw_latest_user
                                )
                                pending_dic = (
                                    app.storage.general["overview_charge_pending"]
                                    .get(latest_user, {})
                                    .get(project_name, {})
                                )
                                none_num = 0
                                pending_num = 0
                                need_num = 0
                                for k, p in pending_dic.items():
                                    if over_flat.get(k, {}).get("role", "") == role:
                                        if p == "缺必填":
                                            none_num += 1
                                        elif p == "有待定":
                                            pending_num += 1
                                        elif p == "缺需填":
                                            need_num += 1
                                num_chip_dic[role]["none_num_text"] = f"必填项缺：{none_num}项"
                                num_chip_dic[role]["pending_num_text"] = f"待定项：{pending_num}项"
                                num_chip_dic[role]["need_num_text"] = f"需填项缺：{need_num}项"
                                if none_num:
                                    num_chip_dic[role]["none_chip_visibility"] = True
                                else:
                                    num_chip_dic[role]["none_chip_visibility"] = False
                                if pending_num:
                                    num_chip_dic[role]["pending_chip_visibility"] = True
                                else:
                                    num_chip_dic[role]["pending_chip_visibility"] = False
                                if need_num:
                                    num_chip_dic[role]["need_chip_visibility"] = True
                                else:
                                    num_chip_dic[role]["need_chip_visibility"] = False

                                # --- 提取并构建 HTML 格式的 Tooltip 内容 ---
                                # tooltip_str_dic.setdefault(role, {})
                                none_group_li = [
                                    over_flat.get(k, {}).get("group_name", "")
                                    for k, v in pending_dic.items()
                                    if v == "缺必填"
                                ]
                                need_group_li = [
                                    over_flat.get(k, {}).get("group_name", "")
                                    for k, v in pending_dic.items()
                                    if v == "缺需填"
                                ]
                                pending_group_li = [
                                    over_flat.get(k, {}).get("group_name", "")
                                    for k, v in pending_dic.items()
                                    if v == "有待定"
                                ]
                                group_li_dic[role] = {
                                    "none_group_li": none_group_li,
                                    "need_group_li": need_group_li,
                                    "pending_group_li": pending_group_li,
                                }

                        def _chip_onclick(role_expansions, group_li):
                            for ex in role_expansions:
                                for group_name in group_li:
                                    if ex.text == group_name:
                                        ex.set_value(True)

                        await _update_num_chip_text()
                        for role, over_data in app.storage.general["over_config_data"].items():
                            with ui.card().classes("w-full px-3 gap-0"):
                                # 创建一个空列表，用于存储当前分组下的所有 expansion 对象
                                current_role_expansions = []
                                with ui.row().classes("relative flex-nowrap -space-x-2 items-center w-full"):
                                    ui.label(f"{role}概述：").classes("text-base text-left px-1 font-bold")
                                    ui.chip(icon="history", color="blue-5").props("outline").classes(
                                        "text-xs"
                                    ).bind_text(app.storage.general["overview_role"][project_name][role], "most_user")

                                    ui.chip(icon="add_reaction", color="cyan-5").props("outline").classes(
                                        "text-xs"
                                    ).bind_text(app.storage.general["overview_role"][project_name][role], "latest_user")

                                    need_num_chip = (
                                        ui.chip(icon="info", color="green-5")
                                        .props("outline")
                                        .classes("text-xs")
                                        .bind_text(num_chip_dic[role], "need_num_text")
                                        .bind_visibility_from(num_chip_dic[role], "need_chip_visibility")
                                    )
                                    pending_num_chip = (
                                        ui.chip(icon="help", color="orange-5")
                                        .props("outline")
                                        .classes("text-xs")
                                        .bind_text(num_chip_dic[role], "pending_num_text")
                                        .bind_visibility_from(num_chip_dic[role], "pending_chip_visibility")
                                    )
                                    none_num_chip = (
                                        ui.chip(icon="error", color="red-4")
                                        .props("outline")
                                        .classes("text-xs")
                                        .bind_text(num_chip_dic[role], "none_num_text")
                                        .bind_visibility_from(num_chip_dic[role], "none_chip_visibility")
                                    )
                                    need_num_chip.on_click(
                                        lambda exps=current_role_expansions, group_li=group_li_dic[role]["need_group_li"]: (
                                            _chip_onclick(exps, group_li)
                                        )
                                    )
                                    pending_num_chip.on_click(
                                        lambda exps=current_role_expansions, group_li=group_li_dic[role]["pending_group_li"]: (
                                            _chip_onclick(exps, group_li)
                                        )
                                    )
                                    none_num_chip.on_click(
                                        lambda exps=current_role_expansions, group_li=group_li_dic[role]["none_group_li"]: (
                                            _chip_onclick(exps, group_li)
                                        )
                                    )
                                    # --- 修改点：在 switch 左边增加历史记录按钮 (Feature 1) ---
                                    # 使用 absolute 定位放到 switch 左边，或者重新布局
                                    # 原有的 switch 是 absolute top-0 right-2
                                    # 我们把历史按钮放在 absolute top-0 right-20 (调整位置)
                                    ui.button(
                                        icon="history",
                                        on_click=lambda r=role: show_role_history_dialog(project_name, r),
                                    ).props("flat round dense color=grey-7").classes(
                                        "absolute -top-1 right-35"
                                    ).tooltip("查看该角色下所有版本的添加历史")

                                    # 方案2：异步分块全展开机制
                                    async def toggle_all_expansions_async(e, exps: list):
                                        for i, exp in enumerate(exps):
                                            exp.set_value(e.value)
                                            # 每处理 2 个面板，就强制让出控制权给浏览器渲染一帧，绝不卡死
                                            if i % 2 == 0:
                                                await asyncio.sleep(0.01)

                                    ui.switch("全展开").classes("absolute -top-2 right-2 text-sm").on_value_change(
                                        lambda e, exps=current_role_expansions: toggle_all_expansions_async(e, exps)
                                    )
                                exp_icon_dic = {
                                    "光学": "flare",
                                    "结构": "view_in_ar",
                                    "硬件": "memory",
                                    "软件": "terminal",
                                    "UI": "screenshot_monitor",
                                    "工艺": "handyman",
                                }
                                # 💡 在遍历 group_name 之前，定义一个集合来记录已经加载过的表格
                                initialized_tables = set()
                                for group_name, chip_data_li in over_data.items():
                                    # === 创建 Expansion ===
                                    exp = (
                                        ui.expansion(
                                            group_name,
                                            icon=exp_icon_dic.get(role, "list"),
                                            value=False,
                                            caption="",  # 可应用统计文字
                                        )
                                        .classes("gap-1 w-full bg-gray-100/30 rounded")
                                        .style("content-visibility: auto; contain-intrinsic-size: auto 300px;")
                                    )
                                    # 注：contain-intrinsic-size 给一个预估的最小高度，防止滚动条在滚动时剧烈抖动

                                    current_role_expansions.append(exp)
                                    exp.set_visibility(False)

                                    # 直接从 config.py 中读取渲染策略，找不到则默认 InteractiveButton
                                    render_type = OVERVIEW_UI_RENDER_REGISTRY.get(group_name, "InteractiveButton")

                                    # 权限预检：只要分组里有任意一项有权限，就显示这个折叠面板和里面的表格
                                    user_role = app.storage.user["current_role"]
                                    has_permission = False
                                    for data in chip_data_li:
                                        if (
                                            user_role in data["permission"]["read_role"]
                                            or user_role in data["permission"]["edit_role"]
                                        ):
                                            has_permission = True
                                            break
                                    if render_type == "OverviewTableGroup":
                                        if has_permission:
                                            exp.set_visibility(True)
                                            target_in_table = bool(correction_label) and any(
                                                str(item.get("label") or "") == str(correction_label)
                                                for item in chip_data_li
                                            )

                                            # 方案1：懒加载闭包函数（类型安全版）
                                            def init_lazy_table(
                                                e=None,
                                                proj=project_name,
                                                r=role,
                                                g_name=group_name,
                                                cfgs=chip_data_li,
                                                exp_ref=exp,
                                            ):
                                                # 使用 角色+组名 作为唯一键，防止不同角色下有同名分组
                                                unique_key = f"{r}_{g_name}"

                                                # 如果面板展开，且该表格尚未初始化过
                                                should_initialize = e is None or bool(getattr(e, "value", False))
                                                if should_initialize and unique_key not in initialized_tables:
                                                    initialized_tables.add(unique_key)  # 记录为已初始化
                                                    with exp_ref:
                                                        OverviewTableGroup(
                                                            project=proj,
                                                            role=r,
                                                            group_name=g_name,
                                                            configs=cfgs,
                                                            temp_bool=temp_bool,
                                                        )

                                            # 绑定到展开状态变化事件上
                                            exp.on_value_change(init_lazy_table)
                                            if target_in_table:
                                                exp.set_value(True)
                                                init_lazy_table()
                                    else:
                                        with exp:
                                            for data in chip_data_li:
                                                user_role = app.storage.user["current_role"]
                                                if (
                                                    user_role in data["permission"]["read_role"]
                                                    or user_role in data["permission"]["edit_role"]
                                                ):
                                                    exp.set_visibility(True)

                                                    # 提取必填属性
                                                    is_required_btn = data.get("nature", "必填")
                                                    if data["processing_type"] == "text":
                                                        InteractiveButton(
                                                            project=project_name,
                                                            role=role,
                                                            title=data["title"],
                                                            label=data["label"],
                                                            processing_type=data["processing_type"],
                                                            content_regular=data["content_regular"],
                                                            impact_list=data["impact_list"],
                                                            dialog_placeholder=data["dialog_placeholder"],
                                                            permission=data["permission"],
                                                            temp_bool=temp_bool,
                                                            nature=is_required_btn,
                                                            allowed_state=data["allowed_state"],
                                                            # delete_bool=False,
                                                        )
                                                    elif data["processing_type"] in ["file", "image"]:
                                                        InteractiveButton(
                                                            project=project_name,
                                                            role=role,
                                                            title=data["title"],
                                                            label=data["label"],
                                                            processing_type=data["processing_type"],
                                                            impact_list=data["impact_list"],
                                                            permission=data["permission"],
                                                            temp_bool=temp_bool,
                                                            upload_path=data["upload_path"],
                                                            nature=is_required_btn,
                                                            allowed_state=data["allowed_state"],
                                                            # delete_bool=False,
                                                        )
                                                    elif data["processing_type"] in ["search"]:
                                                        InteractiveButton(
                                                            project=project_name,
                                                            role=role,
                                                            title=data["title"],
                                                            label=data["label"],
                                                            processing_type=data["processing_type"],
                                                            dialog_placeholder=data["dialog_placeholder"],
                                                            impact_list=data["impact_list"],
                                                            permission=data["permission"],
                                                            temp_bool=temp_bool,
                                                            upload_path=data["upload_path"],
                                                            search_scope_regular=data["search_scope_regular"],
                                                            search_folder_according=data["search_folder_according"],
                                                            content_regular=data["content_regular"],
                                                            search_folder_according_li=data["search_folder_according"],
                                                            search_hierarchy=data["search_hierarchy"],
                                                            nature=is_required_btn,
                                                            allowed_state=data["allowed_state"],
                                                            # delete_bool=False,
                                                        )
                                                    elif data["processing_type"] in ["svn"]:
                                                        InteractiveButton(
                                                            project=project_name,
                                                            role=role,
                                                            title=data["title"],
                                                            label=data["label"],
                                                            processing_type=data["processing_type"],
                                                            impact_list=data["impact_list"],
                                                            permission=data["permission"],
                                                            temp_bool=temp_bool,
                                                            upload_path=data["upload_path"],
                                                            state_path=data["state_path"],
                                                            search_scope_regular=data["search_scope_regular"],
                                                            content_regular=data["content_regular"],
                                                            search_folder_according_li=data["search_folder_according"],
                                                            search_hierarchy=data["search_hierarchy"],
                                                            nature=is_required_btn,
                                                            allowed_state=data["allowed_state"],
                                                            # delete_bool=False,
                                                        )
                                                    elif data["processing_type"] in ["test"]:
                                                        InteractiveButton(
                                                            project=project_name,
                                                            role=role,
                                                            title=data["title"],
                                                            label=data["label"],
                                                            processing_type=data["processing_type"],
                                                            content_regular=data["content_regular"],
                                                            impact_list=data["impact_list"],
                                                            dialog_placeholder=data["dialog_placeholder"],
                                                            permission=data["permission"],
                                                            test_nature_options=data["test_nature_options"],
                                                            state_options=data["state_options"],
                                                            node_options=data["node_options"],
                                                            instrument_options=data["instrument_options"],
                                                            temp_bool=temp_bool,
                                                            nature=is_required_btn,
                                                            allowed_state=data["allowed_state"],
                                                            # delete_bool=False,
                                                        )
                                        if correction_label and any(
                                            str(item.get("label") or "") == str(correction_label)
                                            for item in chip_data_li
                                        ):
                                            exp.set_value(True)
                            ui.timer(2.0, _update_num_chip_text)
            with ui.row().classes("fixed bottom-0 left-0 right-0 bg-sky-50 p-3 items-center shadow-inner"):
                ui.label(text="参考文件：").classes("text-lg text-black m-0")
                # 创建一个按钮组件，组件里有一个空白行，待后续往里面放缩略图
                row_h = 9
                # get_img_group("上传", "/*", row_h)
                with ui.row().classes(f"h-{str(row_h + 1)}").classes("p-0 overflow-y-auto") as img_row:
                    # 将新创建的 img_row 实例存入 user storage
                    app.storage.client["page_elements"]["img_row"] = img_row
                    # 检查缩略图对象存放字典，有对象则会创建缩略图
                    req_thumbnail_display()

    # [逻辑优化] 定义一个异步函数，用于在页面加载后执行检查
    async def check_autosave_after_load(project_name, json_data):
        # 重新构造路径（因为这是一个闭包，或者重新获取）
        autosave_path = os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_AUTOSAVE.json")

        if os.path.exists(autosave_path):
            try:
                with open(autosave_path, "r", encoding="utf-8") as f:
                    autosave_json_data = json.load(f)

                # 比较时间戳
                # 注意：确保 json_data["req_timestamp"] 存在，如果不存在(如新建)可能需要处理异常
                current_ts = json_data.get("req_timestamp")
                autosave_ts = autosave_json_data.get("req_timestamp")

                if (
                    current_ts
                    and autosave_ts
                    and datetime.fromisoformat(autosave_ts) > datetime.fromisoformat(current_ts)
                ):
                    # --- 此时页面已渲染，可以安全地弹出对话框 ---
                    general_dialog.clear()
                    with general_dialog, ui.card():
                        ui.label("发现更新的草稿").classes("text-h6")
                        ui.label(
                            f"检测到自动保存的内容({datetime.fromisoformat(autosave_ts).strftime('%Y年%m月%d日_%H时%M分%S秒')})比当前文件({datetime.fromisoformat(current_ts).strftime('%Y年%m月%d日_%H时%M分%S秒')})较新，是否加载？"
                        )
                        with ui.row().classes("w-full justify-end"):
                            # 选择不加载：仅关闭弹窗，页面保持原状（已加载了 json_path 的数据）
                            ui.button(
                                "不加载(覆盖掉自动保存的内容)", on_click=lambda: general_dialog.submit(False)
                            ).props("color=amber-7")
                            # 选择加载：覆盖当前界面数据
                            ui.button("加载自动保存内容", on_click=lambda: general_dialog.submit(True)).props(
                                "color=primary"
                            )

                    result = await general_dialog
                    if result:
                        loads_requirements(autosave_json_data, False)
                        ui.notify("已恢复自动保存的内容", type="positive")
                    else:
                        # 用户选择不加载，此时不需要做任何事，因为页面初始已经加载了旧数据
                        # 但为了逻辑闭环，也可以弹个提示
                        ui.notify("保留当前文件内容", type="info")

            except Exception as e:
                logger.error(f"检查自动保存逻辑失败: {e}")
        # [关键新增] 无论用户选了"是"还是"否"，甚至如果没有进入if判断（没弹窗），
        # 只要检查流程结束，就允许后续的自动保存了
        app.storage.client["allow_autosave"] = True

    # --- 页面初始化逻辑开始 ---
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    # [新增] 定义自动保存文件的路径
    autosave_path = ""
    if project_name:  # 如果 URL 里有项目名
        autosave_path = os.path.join(REQ_DIR, f"temp/{current_user}/{project_name}_AUTOSAVE.json")
    # 如果跳转传入了json文件路径，则解析这个路径并借此生成界面，优先级高于仅传项目名，认为本次跳转目标清晰明确
    if type == "requirement" and os.path.exists(json_path):
        json_data = {}
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)

            # 1. 【关键】先无条件加载当前指定的文件，保证页面能显示出来
            loads_requirements(json_data, False)

            # 2. 【关键】使用 timer(0) 将检查逻辑推迟到客户端连接建立之后
            # 这样就不会阻塞页面初始化响应
            # 注意：这里我们不再需要在其他地方设置 allow_autosave = True，
            # 因为 check_autosave_after_load 函数最后会负责开启它。
            ui.timer(0.1, lambda: check_autosave_after_load(json_data["project_name"], json_data), once=True)

        except json.JSONDecodeError:
            logger.error(f"错误：文件 '{json_path}' 不是有效的 JSON 格式。", exc_info=True)
        except Exception:
            logger.error("读取需求文件时发生其他错误", exc_info=True)
    # [新增] 如果没指定文件，但存在自动保存文件，优先加载自动保存文件
    elif type == "requirement" and autosave_path and os.path.exists(autosave_path):
        try:
            with open(autosave_path, "r", encoding="utf-8") as f:
                json_data = json.load(f)
                loads_requirements(json_data, False)
                ui.notify(
                    "已为您恢复上次自动保存的需求内容",
                    type="info",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
        except Exception as e:
            logger.error(f"加载自动保存文件失败: {e}")
            # 失败了就回退到新建
            app.storage.client["project_name"] = project_name
            app.storage.client["target_project_name"] = project_name
            new_requirement()
        # [关键新增] 这种情况没有弹窗，直接加载完就可以开启保存了
        app.storage.client["allow_autosave"] = True
    # 如果跳转传入的仅为项目名，且不存在自动保存需求文件，则意味着服务器没有改项目配置文件，新建项目
    elif type == "requirement" and project_name:
        # 设置项目型号
        app.storage.client["project_name"] = project_name
        app.storage.client["target_project_name"] = project_name
        # 客户端储存里数据初始化，调用requirement_input_frame()显示需求确认项
        new_requirement()
        # [关键新增] 新建项目也可以直接开启保存
        app.storage.client["allow_autosave"] = True
    # 如果跳转传入了json文件路径，则解析这个路径并借此生成界面
    elif type in ["overview", "temp_overview"] and (os.path.exists(json_path) or project_name):
        temp_bool = False
        if type == "temp_overview":
            temp_bool = True
        json_data = {}
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    # 使用 json.load() 读取文件内容并解析
                    json_data = json.load(f)
            except json.JSONDecodeError:
                logger.error(f"错误：文件 '{json_path}' 不是有效的 JSON 格式。", exc_info=True)
            except Exception:
                logger.error("读取概述文件时发生其他错误", exc_info=True)
            # 获取概述文件里，版本最高的文件缩略图字典内容，复现文件缩略图
            file_information = json_data[get_max_numeric_key(json_data)]["file_dic"]
            app.storage.client["deleted_files"] = json_data[get_max_numeric_key(json_data)]["deleted_files"]
            app.storage.client["file_thumbnail_dic"] = {}
            for k, v in file_information.items():
                app.add_static_file(local_file=f"{UPLOADS_DIR}/{v['file_name_hash']}", url_path=v["file_url"])
                file_thumbnail = FileThumbnail(
                    file_url=v["file_url"],
                    file_type=v["file_type"],
                    file_name_suffix=v["file_name_suffix"],
                    file_lab=v["file_lab"],
                    parents_h=v["parents_h"],
                    auto_create=False,
                    delet_lab=False,
                    # on_add_ref_click=add_ref_button,
                )
                app.storage.client["file_thumbnail_dic"][k] = {
                    "file_obj": file_thumbnail,
                    "file_information": v,
                }
            await overview_input_frame(json_data, temp_bool, project_name)
        # 暂时开放给研发部进行提前整理概述内容
        elif project_name:
            await overview_input_frame({}, False, project_name)

        # loads_overviews()
    else:
        new_requirement()
        # [关键新增] 新建项目也可以直接开启保存
        app.storage.client["allow_autosave"] = True

    # [新增] 每 10 秒调用一次复用的保存函数，模式为 autosave
    # 只有当 entry_status 为 True (或者你希望任何时候都存) 时才保存，防止刚进来就覆盖
    if type == "requirement":
        app.storage.client["autosave_timer"] = ui.timer(
            10.0, lambda: output_config_data(app.storage.client["config_data"], "autosave"), immediate=False
        )
    # 添加全局键盘事件跟踪
    # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
    ui.keyboard(on_key=handle_key)
