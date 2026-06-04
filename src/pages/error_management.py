# -*- encoding: utf-8 -*-
import asyncio
import copy  # copy: Python标准库，用于创建对象的副本
import logging
import os
import time
import uuid  # uuid: Python标准库，用于生成全局唯一的标识符
from datetime import datetime, timedelta

from nicegui import app, ui  # nicegui: 第三方轻量级Python Web框架，用于纯Python编写前端UI

from .. import db_storage
from ..config import (
    ERROR_EXTENSION_APPROVER_ROLES,
    ERROR_EXTENSION_NOTIFY_TARGETS,
    ERROR_EXTENSION_NOTIFY_TOUSER,
    IMG_DIR,
    PRESET_AVATARS,
    UPLOAD_URL_DIR,
    UPLOADS_DIR,
    WECOM_DEFAULT_TOUSER,
)
from ..utils import get_cache_busted_path, logout, setup_global_activity_tracking
from ..wecom_service import resolve_wecom_recipients, retry_failed_wecom_messages, send_wecom_text_message

logger = logging.getLogger(__name__)

ERROR_DATA_KEY = "error_management_data"
ERROR_VERSION_KEY = "error_management_version_stamp"
ERROR_EDITOR_ROLES = ["研发经理", "admin", "研发助理"]
ERROR_PRODUCT_STATES = ["试产", "量产"]
ERROR_FILTER_STATES = ["全部", "异常录入", "原因分析中", "应急处理中", "纠正预防执行中", "已关闭"]
ERROR_REMINDER_RULES = [
    {"key": "due_7_days", "label": "约定完成日期前7天", "days_until_due": 7},
    {"key": "due_3_days", "label": "约定完成日期前3天", "days_until_due": 3},
    {"key": "due_today", "label": "约定完成日期当天", "days_until_due": 0},
    {"key": "overdue", "label": "约定完成日期逾期", "max_days_until_due": -1},
]


async def _send_wecom_text_message(content: str, touser: str = WECOM_DEFAULT_TOUSER) -> tuple[bool, str]:
    return await send_wecom_text_message(
        content,
        touser,
        module="error_management",
        business_key="manual_test",
        message_type="manual_test",
    )


def schedule_background_task(coro, task_name: str) -> None:
    task = asyncio.create_task(coro)

    def log_task_exception(done_task):
        try:
            done_task.result()
        except Exception:
            logger.exception("%s后台任务执行失败", task_name)

    task.add_done_callback(log_task_exception)


async def send_error_extension_wecom_message(
    content: str,
    *,
    business_key: str,
    message_type: str,
) -> tuple[bool, str]:
    touser = await resolve_wecom_recipients(
        ERROR_EXTENSION_NOTIFY_TARGETS,
        fallback_touser=ERROR_EXTENSION_NOTIFY_TOUSER,
    )
    return await send_wecom_text_message(
        content,
        touser,
        module="error_management",
        business_key=business_key,
        message_type=message_type,
    )


def get_error_template() -> dict:
    return {
        "error_id": "",
        "status": "异常录入",
        "basic_info": {
            "product_name": "",
            "material_no": "",
            "order_no": "",
            "production_qty": "",
            "publish_date": datetime.now().strftime("%Y-%m-%d"),
            "product_state": "试产",
        },
        "descriptions": [],
        "analyses": [],
        "emergency_actions": [],
        "preventive_actions": [],
        "created_by": "",
        "created_role": "",
        "created_at": "",
        "updated_by": "",
        "updated_at": "",
        "closed_at": "",
        "reminder_log": {},
        "operation_log": [],
    }


def merge_with_error_template(db_data: dict) -> dict:
    merged = copy.deepcopy(get_error_template())
    if not isinstance(db_data, dict):
        return merged

    for key, value in db_data.items():
        if key in ["basic_info", "reminder_log"] and isinstance(value, dict):
            merged[key].update(copy.deepcopy(value))
        elif key in ["descriptions", "analyses", "emergency_actions", "preventive_actions", "operation_log"]:
            merged[key] = copy.deepcopy(value) if isinstance(value, list) else []
        elif key in merged:
            merged[key] = copy.deepcopy(value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def generate_error_id(all_errors: dict) -> str:
    today_str = datetime.now().strftime("%y%m%d")
    prefix = f"ERR{today_str}"
    max_count = 0
    for error_id in all_errors.keys():
        if error_id.startswith(prefix):
            try:
                max_count = max(max_count, int(error_id[-2:]))
            except ValueError:
                pass
    return f"{prefix}{str(max_count + 1).zfill(2)}"


def generate_initial_error_data(current_user: str, current_role: str, all_errors: dict) -> dict:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = get_error_template()
    data["error_id"] = generate_error_id(all_errors)
    data["created_by"] = current_user
    data["created_role"] = current_role
    data["created_at"] = now_str
    data["updated_by"] = current_user
    data["updated_at"] = now_str
    data["operation_log"].append({"user": current_user, "role": current_role, "action": "创建异常单", "time": now_str})
    return data


def calculate_error_status(error_data: dict) -> str:
    preventive_actions = error_data.get("preventive_actions", [])
    if preventive_actions and all(item.get("status") == "已关闭" for item in preventive_actions):
        return "已关闭"
    if preventive_actions:
        return "纠正预防执行中"
    if error_data.get("emergency_actions"):
        return "应急处理中"
    if error_data.get("analyses"):
        return "原因分析中"
    return "异常录入"


def is_error_editor(role: str) -> bool:
    return any(role_key in str(role) for role_key in ERROR_EDITOR_ROLES)


def is_error_extension_approver(role: str) -> bool:
    return any(role_key in str(role) for role_key in ERROR_EXTENSION_APPROVER_ROLES)


def split_people(value: str) -> list[str]:
    if not value:
        return []
    normalized = value
    for sep in ["，", ",", "、", ";", "；", "\n"]:
        normalized = normalized.replace(sep, "|")
    return [item.strip() for item in normalized.split("|") if item.strip()]


def format_people_for_wecom(value: str) -> str:
    people = split_people(value)
    return "|".join(people) if people else WECOM_DEFAULT_TOUSER


def parse_date(value: str):
    if not value:
        return None
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def is_current_responsible(owner_text: str, current_user: str, current_role: str) -> bool:
    for owner in split_people(owner_text):
        if owner in [current_user, current_role] or owner in str(current_role) or owner in str(current_user):
            return True
    return False


def ensure_item_id(item: dict, prefix: str) -> dict:
    item.setdefault("id", f"{prefix}_{uuid.uuid4().hex[:8]}")
    return item


def get_pending_extension_request(action: dict) -> dict | None:
    for request in reversed(action.get("extension_requests", [])):
        if request.get("status") == "待审批":
            return request
    return None


def get_next_due_text(error_data: dict) -> str:
    due_dates = []
    for item in error_data.get("preventive_actions", []):
        due_date = parse_date(item.get("due_date", ""))
        if due_date:
            due_dates.append(due_date)
    if not due_dates:
        return "暂无"
    return max(due_dates).strftime("%Y-%m-%d")


async def save_error_record(error_data: dict, user: str, role: str) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = merge_with_error_template(error_data)
    record["status"] = calculate_error_status(record)
    record["updated_by"] = user
    record["updated_at"] = now_str
    if record["status"] == "已关闭" and not record.get("closed_at"):
        record["closed_at"] = now_str
    elif record["status"] != "已关闭":
        record["closed_at"] = ""
    record.setdefault("operation_log", []).append({"user": user, "role": role, "action": "保存异常单", "time": now_str})
    await db_storage.set_deep_item([ERROR_DATA_KEY, record["error_id"]], record)
    await db_storage.set_item(ERROR_VERSION_KEY, time.time())


async def atomic_error_update(error_id: str, update_function) -> bool:
    def apply_update(current):
        record = merge_with_error_template(current or {})
        updated = update_function(record)
        updated["status"] = calculate_error_status(updated)
        return updated

    success = await db_storage.atomic_deep_update([ERROR_DATA_KEY, error_id], apply_update)
    if success:
        await db_storage.set_item(ERROR_VERSION_KEY, time.time())
    return success


async def check_and_send_error_reminders(show_result: bool = False) -> tuple[int, int]:
    retry_success_count, retry_fail_count = await retry_failed_wecom_messages()
    all_errors = db_storage.get_item(ERROR_DATA_KEY, {})
    today = datetime.now().date()
    today_key = today.strftime("%Y-%m-%d")
    sent_count = 0
    fail_count = 0

    for raw_error in all_errors.values():
        error_data = merge_with_error_template(raw_error)
        if error_data.get("status") == "已关闭":
            continue

        for action in error_data.get("preventive_actions", []):
            if action.get("status") == "已关闭":
                continue

            due_date = parse_date(action.get("due_date", ""))
            owner = action.get("owner", "")
            if not due_date or not owner:
                continue

            days_until_due = (due_date - today).days
            for rule in ERROR_REMINDER_RULES:
                should_send = False
                if "days_until_due" in rule:
                    should_send = days_until_due == rule["days_until_due"]
                elif "max_days_until_due" in rule:
                    should_send = days_until_due <= rule["max_days_until_due"]
                if not should_send:
                    continue

                marker = f"{action.get('id')}:{rule['key']}:{today_key}"
                if marker in error_data.get("reminder_log", {}):
                    continue

                claim_id = uuid.uuid4().hex

                def claim_reminder(current, marker=marker, rule=rule, claim_id=claim_id):
                    reminder_log = current.setdefault("reminder_log", {})
                    existing_marker = reminder_log.get(marker)
                    can_claim = marker not in reminder_log
                    if existing_marker and existing_marker.get("state") == "sending":
                        try:
                            sending_time = datetime.strptime(existing_marker.get("time", ""), "%Y-%m-%d %H:%M:%S")
                            can_claim = datetime.now() - sending_time > timedelta(minutes=10)
                        except ValueError:
                            can_claim = True
                    if can_claim:
                        reminder_log[marker] = {
                            "rule": rule["label"],
                            "state": "sending",
                            "claim_id": claim_id,
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    return current

                await atomic_error_update(error_data["error_id"], claim_reminder)
                fresh_marker = db_storage.get_deep_item(
                    [ERROR_DATA_KEY, error_data["error_id"], "reminder_log", marker], {}
                )
                if fresh_marker.get("claim_id") != claim_id:
                    continue

                content = (
                    "生产异常纠正预防措施提醒\n"
                    f"异常单：{error_data.get('error_id')}\n"
                    f"产品：{error_data.get('basic_info', {}).get('product_name', '')}\n"
                    f"措施：{action.get('content', '')}\n"
                    f"负责人：{owner}\n"
                    f"预计完成日期：{action.get('due_date', '')}\n"
                    f"提醒策略：{rule['label']}"
                )
                success, message = await send_wecom_text_message(
                    content,
                    format_people_for_wecom(owner),
                    module="error_management",
                    business_key=f"{error_data.get('error_id')}:{action.get('id')}:{rule['key']}",
                    message_type="preventive_reminder",
                )
                if success:
                    sent_count += 1
                else:
                    fail_count += 1

                def add_reminder_log(current, marker=marker, rule=rule, success=success, message=message):
                    current.setdefault("reminder_log", {})[marker] = {
                        "rule": rule["label"],
                        "state": "sent" if success else "failed_retrying",
                        "success": success,
                        "message": message,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    return current

                await atomic_error_update(error_data["error_id"], add_reminder_log)

    if show_result:
        ui.notify(
            f"提醒检查完成：新发成功 {sent_count} 条，失败进入重试 {fail_count} 条；历史重试成功 {retry_success_count} 条，仍失败 {retry_fail_count} 条",
            type="info",
            position="bottom",
        )
    return sent_count, fail_count


def save_uploaded_evidence_file(error_id: str, action_id: str, original_filename: str, content: bytes) -> dict:
    safe_name = os.path.basename(original_filename).replace("\\", "_").replace("/", "_")
    stored_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}_{safe_name}"
    relative_dir = f"error_management/{error_id}/{action_id}"
    target_dir = os.path.join(UPLOADS_DIR, "error_management", error_id, action_id)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, stored_name)
    with open(target_path, "wb") as file:
        file.write(content)
    return {
        "name": original_filename,
        "stored_name": stored_name,
        "url": f"{UPLOAD_URL_DIR}/{relative_dir}/{stored_name}",
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==========================================
# 主路由页面定义
# ==========================================
# @ui.page: NiceGUI框架的路由装饰器，用于定义页面路径
@ui.page("/error_management")
async def error_management_page():
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

    can_edit_all = is_error_editor(current_role)
    reminder_guard = {"running": False}

    async def send_test_wecom_notification(e):
        e.sender.disable()
        try:
            content = (
                "生产异常管理模块测试通知\n"
                f"发送人：{current_user}\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            ui.notify("正在发送企业微信测试通知...", type="info", position="bottom", timeout=1500)
            success, message = await _send_wecom_text_message(content)
            ui.notify(message, type="positive" if success else "negative", position="bottom", multi_line=True)
        finally:
            e.sender.enable()

    async def run_reminder_check(show_result: bool = False):
        if reminder_guard["running"]:
            return
        reminder_guard["running"] = True
        try:
            await check_and_send_error_reminders(show_result)
        finally:
            reminder_guard["running"] = False

    async def handle_manual_reminder_check():
        await run_reminder_check(True)

    async def handle_new_error_record():
        await open_error_detail_dialog()

    def validate_error_record(error_data: dict) -> bool:
        basic = error_data.get("basic_info", {})
        if not basic.get("product_name", "").strip():
            ui.notify("请填写产品名称", type="warning", position="bottom")
            return False
        if not any(item.get("content", "").strip() for item in error_data.get("descriptions", [])):
            ui.notify("请至少填写一条异常情况说明", type="warning", position="bottom")
            return False
        return True

    async def open_error_detail_dialog(error_id=None):
        is_new = error_id is None
        if is_new and not can_edit_all:
            return ui.notify("当前角色无异常单录入权限", type="warning", position="bottom")

        all_errors = db_storage.get_item(ERROR_DATA_KEY, {})
        if is_new:
            local_data = generate_initial_error_data(current_user, current_role, all_errors)
        else:
            local_data = merge_with_error_template(all_errors.get(error_id, {}))
            if not local_data.get("error_id"):
                return ui.notify("未找到该异常单", type="negative", position="bottom")

        for item in local_data["descriptions"]:
            ensure_item_id(item, "desc")
        for item in local_data["analyses"]:
            ensure_item_id(item, "analysis")
        for item in local_data["emergency_actions"]:
            ensure_item_id(item, "emergency")
        for item in local_data["preventive_actions"]:
            ensure_item_id(item, "preventive")
            item.setdefault("status", "待执行")
            item.setdefault("evidence_files", [])
            item.setdefault("close_note", "")
            item.setdefault("extension_requests", [])

        read_only = not can_edit_all

        def bind_input(label, target, key, classes="w-full", readonly=None):
            field_readonly = read_only if readonly is None else readonly
            props = "outlined dense"
            if field_readonly:
                props += " readonly"
            field = ui.input(label, value=target.get(key, "")).props(props).classes(f"{classes} mb-3")
            if not field_readonly:
                field.on_value_change(lambda e, t=target, k=key: t.__setitem__(k, e.value))
            return field

        def bind_date(label, target, key, classes="w-full", readonly=None):
            field_readonly = read_only if readonly is None else readonly
            field = (
                ui.input(label, value=target.get(key, "")).props("outlined dense readonly").classes(f"{classes} mb-3")
            )

            if field_readonly:
                return field

            def set_date(e, input_field=field, data=target, data_key=key):
                data[data_key] = e.value or ""
                input_field.value = data[data_key]
                input_field.update()
                menu.close()

            with ui.menu().props("no-parent-event") as menu:
                ui.date(value=target.get(key, ""), mask="YYYY-MM-DD", on_change=set_date)

            field.on("click", lambda _, m=menu: m.open())
            with field.add_slot("append"):
                ui.icon("event").classes("cursor-pointer").on("click", lambda _, m=menu: m.open())

            return field

        def bind_textarea(label, target, key, classes="w-full", readonly=None):
            field_readonly = read_only if readonly is None else readonly
            props = "outlined autogrow"
            if field_readonly:
                props += " readonly"
            field = ui.textarea(label, value=target.get(key, "")).props(props).classes(f"{classes} mb-3")
            if not field_readonly:
                field.on_value_change(lambda e, t=target, k=key: t.__setitem__(k, e.value))
            return field

        def bind_select(label, target, key, options, classes="w-full"):
            props = "outlined dense"
            if read_only:
                props += " disable"
            current_value = target.get(key, "")
            select_options = list(options)
            if current_value and current_value not in select_options:
                select_options = [current_value, *select_options]
            elif not current_value and select_options:
                current_value = select_options[0]
                target[key] = current_value
            field = ui.select(select_options, label=label, value=current_value).props(props).classes(f"{classes} mb-3")
            if not read_only:
                field.on_value_change(lambda e, t=target, k=key: t.__setitem__(k, e.value))
            return field

        def section(title: str):
            with ui.element("div").classes("w-full bg-white border border-gray-200 rounded-md p-4"):
                ui.label(title).classes("text-base font-bold text-gray-800 mb-3")
                return ui.column().classes("w-full gap-3")

        async def save_current_record():
            if not can_edit_all:
                return ui.notify("当前角色无保存权限", type="warning", position="bottom")
            if not validate_error_record(local_data):
                return
            await save_error_record(local_data, current_user, current_role)
            ui.notify("异常单已保存", type="positive", position="bottom")
            root_dialog.close()
            refresh_list()

        def render_standard_items(container, list_key, title, fields, prefix, empty_text):
            items = local_data[list_key]
            container.clear()
            with container:
                if not items:
                    ui.label(empty_text).classes("text-sm text-gray-400")
                for index, item in enumerate(items):
                    ensure_item_id(item, prefix)
                    with ui.element("div").classes("w-full border border-gray-200 rounded-md bg-gray-50 p-4"):
                        with ui.row().classes("w-full justify-between items-center mb-3"):
                            ui.label(f"{title} {index + 1}").classes("font-bold text-sm text-gray-700")
                            if can_edit_all:
                                ui.button(
                                    icon="delete",
                                    on_click=lambda _, i=index: (
                                        items.pop(i),
                                        render_standard_items(container, list_key, title, fields, prefix, empty_text),
                                    ),
                                ).props("flat round dense color=red")
                        for field in fields:
                            key, label, field_type = field
                            if field_type == "textarea":
                                bind_textarea(label, item, key)
                            elif field_type == "date":
                                bind_date(label, item, key)
                            else:
                                bind_input(label, item, key)
                if can_edit_all:

                    def add_item():
                        new_item = ensure_item_id({key: "" for key, _, _ in fields}, prefix)
                        items.append(new_item)
                        render_standard_items(container, list_key, title, fields, prefix, empty_text)

                    ui.button(f"添加{title}", icon="add", on_click=add_item).props("outline dense color=primary")

        async def open_extension_request_dialog(action: dict):
            pending_request = get_pending_extension_request(action)
            if pending_request:
                return ui.notify("该措施已有延期申请待审批", type="warning", position="bottom")

            request_state = {
                "new_due_date": action.get("due_date", ""),
                "reason": "",
            }

            async def submit_extension_request():
                old_due_date = action.get("due_date", "")
                old_date = parse_date(old_due_date)
                new_date = parse_date(request_state["new_due_date"])
                if not new_date:
                    return ui.notify("请选择新的预计完成日期", type="warning", position="bottom")
                if old_date and new_date <= old_date:
                    return ui.notify("延期日期必须晚于当前预计完成日期", type="warning", position="bottom")
                if not request_state["reason"].strip():
                    return ui.notify("请填写延期原因", type="warning", position="bottom")

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                extension_request = {
                    "id": f"ext_{uuid.uuid4().hex[:8]}",
                    "status": "待审批",
                    "old_due_date": old_due_date,
                    "new_due_date": request_state["new_due_date"],
                    "reason": request_state["reason"].strip(),
                    "requester": current_user,
                    "requester_role": current_role,
                    "requested_at": now_str,
                }

                def add_extension_request(current):
                    for stored_action in current.get("preventive_actions", []):
                        if stored_action.get("id") == action.get("id"):
                            stored_action.setdefault("extension_requests", []).append(copy.deepcopy(extension_request))
                            break
                    current["updated_by"] = current_user
                    current["updated_at"] = now_str
                    current.setdefault("operation_log", []).append(
                        {
                            "user": current_user,
                            "role": current_role,
                            "action": f"申请延期至 {extension_request['new_due_date']}",
                            "time": now_str,
                        }
                    )
                    return current

                success = await atomic_error_update(local_data["error_id"], add_extension_request)
                if not success:
                    return ui.notify("延期申请提交失败，请刷新后重试", type="negative", position="bottom")

                content = (
                    "生产异常纠正预防措施延期申请\n"
                    f"异常单：{local_data['error_id']}\n"
                    f"产品：{local_data.get('basic_info', {}).get('product_name', '')}\n"
                    f"措施：{action.get('content', '')}\n"
                    f"申请人：{current_user}\n"
                    f"原预计日期：{old_due_date or '-'}\n"
                    f"申请延期至：{extension_request['new_due_date']}\n"
                    f"延期原因：{extension_request['reason']}\n"
                    f"审批角色：{', '.join(ERROR_EXTENSION_APPROVER_ROLES)}"
                )
                await send_error_extension_wecom_message(
                    content,
                    business_key=f"{local_data['error_id']}:{action.get('id')}:{extension_request['id']}",
                    message_type="extension_request",
                )
                ui.notify("延期申请已提交", type="positive", position="bottom")
                dialog.close()
                refresh_list()
                await open_error_detail_dialog(local_data["error_id"])

            dialog.clear()
            with dialog, ui.card().classes("w-1/3 max-w-lg p-5"):
                ui.label("申请延期").classes("text-lg font-bold text-gray-800")
                ui.label(action.get("content", "")).classes("text-sm text-gray-600")
                bind_date("新的预计完成日期", request_state, "new_due_date", readonly=False)
                bind_textarea("延期原因", request_state, "reason", readonly=False)
                with ui.row().classes("w-full justify-end gap-3 mt-2"):
                    ui.button("取消", on_click=dialog.close).props("outline color=grey")
                    ui.button("提交申请", icon="schedule", on_click=submit_extension_request).props("color=primary")
            dialog.open()

        async def approve_extension_request(action: dict, request: dict, approved: bool):
            if not is_error_extension_approver(current_role):
                return ui.notify("当前角色无延期审批权限", type="warning", position="bottom")

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            action_text = "通过延期申请" if approved else "驳回延期申请"

            def update_extension_request(current):
                for stored_action in current.get("preventive_actions", []):
                    if stored_action.get("id") != action.get("id"):
                        continue
                    for stored_request in stored_action.get("extension_requests", []):
                        if stored_request.get("id") == request.get("id") and stored_request.get("status") == "待审批":
                            stored_request["status"] = "已通过" if approved else "已驳回"
                            stored_request["approver"] = current_user
                            stored_request["approver_role"] = current_role
                            stored_request["approved_at"] = now_str
                            if approved:
                                stored_action["due_date"] = stored_request.get(
                                    "new_due_date", stored_action.get("due_date", "")
                                )
                            break
                    break
                current["updated_by"] = current_user
                current["updated_at"] = now_str
                current.setdefault("operation_log", []).append(
                    {"user": current_user, "role": current_role, "action": action_text, "time": now_str}
                )
                return current

            success = await atomic_error_update(local_data["error_id"], update_extension_request)
            if not success:
                return ui.notify("延期审批失败，请刷新后重试", type="negative", position="bottom")

            content = (
                "生产异常延期申请审批结果\n"
                f"异常单：{local_data['error_id']}\n"
                f"产品：{local_data.get('basic_info', {}).get('product_name', '')}\n"
                f"措施：{action.get('content', '')}\n"
                f"审批结果：{'通过' if approved else '驳回'}\n"
                f"原预计日期：{request.get('old_due_date', '-')}\n"
                f"申请延期至：{request.get('new_due_date', '-')}\n"
                f"审批人：{current_user}"
            )
            schedule_background_task(
                send_error_extension_wecom_message(
                    content,
                    business_key=f"{local_data['error_id']}:{action.get('id')}:{request.get('id')}:approval",
                    message_type="extension_approval",
                ),
                "延期审批企业微信通知",
            )
            ui.notify("延期审批已处理", type="positive", position="bottom")
            refresh_list()
            await open_error_detail_dialog(local_data["error_id"])

        def render_preventive_items(container):
            items = local_data["preventive_actions"]
            container.clear()
            with container:
                if not items:
                    ui.label("暂无纠正预防措施").classes("text-sm text-gray-400")
                for index, item in enumerate(items):
                    ensure_item_id(item, "preventive")
                    item.setdefault("status", "待执行")
                    item.setdefault("evidence_files", [])
                    item.setdefault("close_note", "")
                    item.setdefault("extension_requests", [])
                    can_close = (
                        not is_new
                        and item.get("status") != "已关闭"
                        and (can_edit_all or is_current_responsible(item.get("owner", ""), current_user, current_role))
                    )
                    pending_extension = get_pending_extension_request(item)
                    can_apply_extension = can_close and not pending_extension
                    with ui.element("div").classes("w-full border border-gray-200 rounded-md bg-gray-50 p-4"):
                        with ui.row().classes("w-full justify-between items-center mb-3"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(f"纠正预防措施 {index + 1}").classes("font-bold text-sm text-gray-700")
                                ui.badge(
                                    item.get("status", "待执行"),
                                    color="green" if item.get("status") == "已关闭" else "orange",
                                )
                            if can_edit_all:
                                ui.button(
                                    icon="delete",
                                    on_click=lambda _, i=index: (items.pop(i), render_preventive_items(container)),
                                ).props("flat round dense color=red")

                        bind_textarea("纠正预防措施", item, "content")
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input("负责人", item, "owner", "w-full md:w-1/3")
                            bind_date("预计完成日期", item, "due_date", "w-full md:w-1/3")
                            bind_input("状态", item, "status", "w-full md:w-1/3", readonly=True)

                        if pending_extension:
                            with ui.element("div").classes(
                                "w-full border border-orange-200 bg-orange-50 rounded-md p-3 mb-3"
                            ):
                                ui.label(
                                    f"延期申请待审批：{pending_extension.get('old_due_date', '-')}"
                                    f" → {pending_extension.get('new_due_date', '-')}"
                                ).classes("text-sm font-bold text-orange-800")
                                ui.label(
                                    f"申请人：{pending_extension.get('requester', '-')} ｜ "
                                    f"原因：{pending_extension.get('reason', '-')}"
                                ).classes("text-sm text-orange-700")
                                if is_error_extension_approver(current_role):
                                    async def reject_extension(event=None, a=item, r=pending_extension):
                                        await approve_extension_request(a, r, False)

                                    async def approve_extension(event=None, a=item, r=pending_extension):
                                        await approve_extension_request(a, r, True)

                                    with ui.row().classes("justify-end gap-2 mt-2"):
                                        ui.button(
                                            "驳回延期",
                                            icon="close",
                                            on_click=reject_extension,
                                        ).props("outline color=negative dense")
                                        ui.button(
                                            "通过延期",
                                            icon="check",
                                            on_click=approve_extension,
                                        ).props("color=green dense")
                        else:
                            recent_extension = next(
                                (
                                    req
                                    for req in reversed(item.get("extension_requests", []))
                                    if req.get("status") != "待审批"
                                ),
                                None,
                            )
                            if recent_extension:
                                ui.label(
                                    f"最近延期审批：{recent_extension.get('status')}，"
                                    f"{recent_extension.get('old_due_date', '-')} → {recent_extension.get('new_due_date', '-')}"
                                ).classes("text-xs text-gray-500 mb-2")

                        if item.get("evidence_files"):
                            with ui.column().classes("w-full gap-1 mt-1"):
                                ui.label("证据留痕").classes("text-xs font-bold text-gray-500")
                                for evidence in item.get("evidence_files", []):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.icon("attach_file", size="xs").classes("text-gray-500")
                                        ui.link(evidence.get("name", "证据文件"), evidence.get("url", "#")).classes(
                                            "text-sm text-blue-700"
                                        )
                                        ui.label(evidence.get("uploaded_at", "")).classes("text-xs text-gray-400")

                        if item.get("status") == "已关闭":
                            with ui.row().classes("w-full gap-4 flex-wrap items-start mt-2"):
                                bind_input("关闭人", item, "closed_by", "w-full md:w-1/3", readonly=True)
                                bind_input("关闭时间", item, "closed_at", "w-full md:w-1/3", readonly=True)
                            bind_textarea("关闭说明", item, "close_note", readonly=True)
                        elif can_close:
                            ui.separator().classes("my-2")
                            bind_textarea("关闭说明", item, "close_note", readonly=False)
                            ui.label("可填写 ECN 编号、会议结论、沟通记录或其它执行结果。").classes(
                                "text-xs text-gray-500 -mt-2 mb-2"
                            )

                            async def close_preventive_action(event=None, action=item):
                                if not action.get("close_note", "").strip():
                                    return ui.notify("请填写关闭说明", type="warning", position="bottom")
                                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                action["status"] = "已关闭"
                                action["closed_by"] = current_user
                                action["closed_role"] = current_role
                                action["closed_at"] = now_str

                                def update_action(current):
                                    for stored_action in current.get("preventive_actions", []):
                                        if stored_action.get("id") == action.get("id"):
                                            stored_action.update(copy.deepcopy(action))
                                            break
                                    current.setdefault("operation_log", []).append(
                                        {
                                            "user": current_user,
                                            "role": current_role,
                                            "action": f"关闭纠正预防措施：{action.get('content', '')[:30]}",
                                            "time": now_str,
                                        }
                                    )
                                    current["updated_by"] = current_user
                                    current["updated_at"] = now_str
                                    if calculate_error_status(current) == "已关闭":
                                        current["closed_at"] = now_str
                                    return current

                                success = await atomic_error_update(local_data["error_id"], update_action)
                                if success:
                                    ui.notify("纠正预防措施已关闭", type="positive", position="bottom")
                                    refresh_list()
                                    await open_error_detail_dialog(local_data["error_id"])
                                else:
                                    ui.notify("关闭失败，请刷新后重试", type="negative", position="bottom")

                            with ui.row().classes("items-center justify-end gap-3 mt-2"):
                                if can_apply_extension:
                                    async def apply_extension(event=None, a=item):
                                        await open_extension_request_dialog(a)

                                    ui.button(
                                        "申请延期",
                                        icon="event",
                                        on_click=apply_extension,
                                    ).props("outline color=orange")
                                elif pending_extension:
                                    ui.button("延期审批中", icon="hourglass_empty").props(
                                        "outline color=orange disable"
                                    )
                                ui.button("关闭该措施", icon="check_circle", on_click=close_preventive_action).props(
                                    "color=green"
                                )

                if can_edit_all:

                    def add_preventive_item():
                        items.append(
                            ensure_item_id(
                                {
                                    "content": "",
                                    "owner": "",
                                    "due_date": "",
                                    "status": "待执行",
                                    "evidence_files": [],
                                    "close_note": "",
                                    "extension_requests": [],
                                },
                                "preventive",
                            )
                        )
                        render_preventive_items(container)

                    ui.button("添加纠正预防措施", icon="add", on_click=add_preventive_item).props(
                        "outline dense color=primary"
                    )

        root_dialog.clear()
        with root_dialog, ui.card().classes("w-full h-[100vh] flex flex-col p-0 overflow-hidden bg-gray-100"):
            with ui.row().classes("w-full bg-white px-4 py-3 border-b border-gray-300 justify-between items-center"):
                with ui.row().classes("items-center gap-3"):
                    ui.badge(
                        calculate_error_status(local_data),
                        color="green" if calculate_error_status(local_data) == "已关闭" else "orange",
                    )
                    ui.label(local_data["error_id"]).classes("font-mono font-bold text-lg text-gray-800")
                    ui.label(local_data["basic_info"].get("product_name") or "未命名异常单").classes(
                        "text-base font-bold text-gray-700"
                    )
                ui.button(icon="close", on_click=root_dialog.close).props("flat round dense")

            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
                    with section("基础信息"):
                        basic = local_data["basic_info"]
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input("产品名称", basic, "product_name", "w-full md:w-[32%]")
                            bind_input("料号", basic, "material_no", "w-full md:w-[32%]")
                            bind_input("订单号", basic, "order_no", "w-full md:w-[32%]")
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input("投产数量", basic, "production_qty", "w-full md:w-[32%]")
                            bind_date("发文日期", basic, "publish_date", "w-full md:w-[32%]")
                            bind_select("产品状态", basic, "product_state", ERROR_PRODUCT_STATES, "w-full md:w-[32%]")
                        with ui.row().classes("w-full gap-3 text-xs text-gray-500"):
                            ui.label(f"创建：{local_data.get('created_by', '')} / {local_data.get('created_at', '')}")
                            ui.label(
                                f"最近更新：{local_data.get('updated_by', '')} / {local_data.get('updated_at', '')}"
                            )

                    with section("异常情况说明"):
                        desc_container = ui.column().classes("w-full gap-3")
                        render_standard_items(
                            desc_container,
                            "descriptions",
                            "异常说明",
                            [("content", "异常情况说明", "textarea"), ("speaker", "说明人", "input")],
                            "desc",
                            "暂无异常情况说明",
                        )

                    with section("初步原因分析"):
                        analysis_container = ui.column().classes("w-full gap-3")
                        render_standard_items(
                            analysis_container,
                            "analyses",
                            "原因分析",
                            [
                                ("content", "初步原因分析", "textarea"),
                                ("analyst", "分析人", "input"),
                                ("analysis_date", "分析日期", "date"),
                            ],
                            "analysis",
                            "暂无初步原因分析",
                        )

                    with section("应急对策"):
                        emergency_container = ui.column().classes("w-full gap-3")
                        render_standard_items(
                            emergency_container,
                            "emergency_actions",
                            "应急对策",
                            [
                                ("content", "应急对策", "textarea"),
                                ("output_person", "对策输出人", "input"),
                                ("output_date", "输出日期", "date"),
                            ],
                            "emergency",
                            "暂无应急对策",
                        )

                    with section("纠正预防措施"):
                        preventive_container = ui.column().classes("w-full gap-3")
                        render_preventive_items(preventive_container)

                    with section("操作留痕"):
                        logs = local_data.get("operation_log", [])
                        if not logs:
                            ui.label("暂无操作记录").classes("text-sm text-gray-400")
                        for log in reversed(logs[-20:]):
                            ui.label(
                                f"{log.get('time', '')}  {log.get('user', '')}({log.get('role', '')})  {log.get('action', '')}"
                            ).classes("text-sm text-gray-600")

            with ui.row().classes("w-full bg-white border-t border-gray-200 p-3 justify-end gap-3"):
                ui.button("关闭窗口", on_click=root_dialog.close).props("outline color=grey")
                if can_edit_all:
                    ui.button("保存异常单", icon="save", on_click=save_current_record).props("color=primary")

        root_dialog.open()

    # ==========================================
    # 主页面 UI (头部与列表总览)
    # ==========================================
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-600 h-14 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("生产异常管理模块").classes(
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

    def status_color(status: str) -> str:
        if status == "已关闭":
            return "green"
        if status == "纠正预防执行中":
            return "orange"
        if status == "应急处理中":
            return "blue"
        if status == "原因分析中":
            return "purple"
        return "grey"

    def status_border_color(status: str) -> str:
        return {
            "已关闭": "#22c55e",
            "纠正预防执行中": "#f97316",
            "应急处理中": "#3b82f6",
            "原因分析中": "#a855f7",
            "异常录入": "#64748b",
        }.get(status, "#64748b")

    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.column().classes("w-full h-full p-4 gap-4"):
            with ui.row().classes("w-full justify-between items-center bg-white p-4 shadow-sm rounded-md"):
                with ui.row().classes("gap-3 items-center"):
                    ui.input("搜索产品/料号/订单/负责人").props("dense outlined").bind_value(
                        page_state, "search_keyword"
                    ).classes("w-64")
                    ui.select(ERROR_FILTER_STATES, label="状态筛选").props("dense outlined").bind_value(
                        page_state, "filter_state"
                    ).classes("w-44")
                    ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("outline color=primary")
                with ui.row().classes("gap-2 items-center"):
                    ui.button(
                        "检查提醒",
                        icon="notifications_active",
                        on_click=handle_manual_reminder_check,
                    ).props("outline color=orange")
                    ui.button(
                        "企微测试",
                        icon="send",
                        on_click=send_test_wecom_notification,
                    ).props("outline color=primary")
                    if can_edit_all:
                        ui.button(
                            "录入异常单",
                            icon="add_box",
                            on_click=handle_new_error_record,
                        ).props("color=red-7")

            with ui.element("div").classes("w-full flex-grow overflow-y-auto overflow-x-hidden p-1"):
                list_container = ui.column().classes("w-full gap-3")

                def refresh_list():
                    list_container.clear()
                    all_errors = db_storage.get_item(ERROR_DATA_KEY, {})
                    keyword = page_state["search_keyword"].lower().strip()
                    filter_state = page_state["filter_state"]

                    valid_errors = [
                        merge_with_error_template(error)
                        for error in all_errors.values()
                        if error and isinstance(error, dict)
                    ]
                    valid_errors = sorted(
                        valid_errors,
                        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
                        reverse=True,
                    )

                    with list_container:
                        if not valid_errors:
                            ui.label("暂无生产异常记录").classes("text-gray-500 m-auto mt-10")
                            return

                        rendered_count = 0
                        for error_data in valid_errors:
                            basic = error_data.get("basic_info", {})
                            status = calculate_error_status(error_data)
                            owner_text = "、".join(
                                [
                                    item.get("owner", "")
                                    for item in error_data.get("preventive_actions", [])
                                    if item.get("owner", "") and item.get("status") != "已关闭"
                                ]
                            )
                            searchable = " ".join(
                                [
                                    error_data.get("error_id", ""),
                                    basic.get("product_name", ""),
                                    basic.get("material_no", ""),
                                    basic.get("order_no", ""),
                                    owner_text,
                                ]
                            ).lower()
                            if keyword and keyword not in searchable:
                                continue
                            if filter_state != "全部" and status != filter_state:
                                continue

                            rendered_count += 1
                            total_preventive = len(error_data.get("preventive_actions", []))
                            closed_preventive = len(
                                [
                                    item
                                    for item in error_data.get("preventive_actions", [])
                                    if item.get("status") == "已关闭"
                                ]
                            )
                            is_my_pending = any(
                                item.get("status") != "已关闭"
                                and is_current_responsible(item.get("owner", ""), current_user, current_role)
                                for item in error_data.get("preventive_actions", [])
                            )

                            with ui.element("div").classes(
                                "w-full bg-white border border-gray-200 border-l-4 rounded-md p-4 shadow-sm "
                                "hover:bg-amber-50 cursor-pointer transition-colors"
                            ) as card:

                                async def open_card_detail(_, e_id=error_data["error_id"]):
                                    await open_error_detail_dialog(e_id)

                                card.style(f"border-left-color: {status_border_color(status)}")
                                card.on("click", open_card_detail)
                                with ui.row().classes("w-full justify-between items-start gap-4"):
                                    with ui.column().classes("gap-1 min-w-0"):
                                        with ui.row().classes("items-center gap-2"):
                                            ui.label(error_data["error_id"]).classes(
                                                "font-mono font-bold text-lg text-gray-800"
                                            )
                                            ui.badge(status, color=status_color(status)).props("outline")
                                            if is_my_pending:
                                                ui.chip("待我处理", icon="notifications_active", color="red").props(
                                                    "dense outline size=sm"
                                                )
                                        ui.label(basic.get("product_name", "未填写产品名称")).classes(
                                            "font-bold text-gray-800"
                                        )
                                        ui.label(
                                            f"料号：{basic.get('material_no', '') or '-'} ｜ 订单号：{basic.get('order_no', '') or '-'} ｜ 产品状态：{basic.get('product_state', '') or '-'}"
                                        ).classes("text-sm text-gray-600")
                                        first_desc = next(
                                            (
                                                item.get("content", "")
                                                for item in error_data.get("descriptions", [])
                                                if item.get("content")
                                            ),
                                            "",
                                        )
                                        if first_desc:
                                            ui.label(first_desc[:120]).classes("text-sm text-gray-500 line-clamp-2")
                                    with ui.column().classes("items-end gap-1 shrink-0"):
                                        ui.label(f"发文日期：{basic.get('publish_date', '') or '-'}").classes(
                                            "text-xs text-gray-500"
                                        )
                                        ui.label(f"预计完成：{get_next_due_text(error_data)}").classes(
                                            "text-xs text-gray-500"
                                        )
                                        ui.label(f"纠正预防：{closed_preventive}/{total_preventive}").classes(
                                            "text-xs text-gray-500"
                                        )
                                        if owner_text:
                                            ui.label(f"负责人：{owner_text}").classes("text-xs text-orange-700")

                        if rendered_count == 0:
                            ui.label("没有符合筛选条件的异常单").classes("text-gray-500 m-auto mt-10")

                def check_and_refresh_list():
                    current_stamp = db_storage.get_item(ERROR_VERSION_KEY, 0.0)
                    if page_state.get("version_stamp", 0.0) != 0.0 and current_stamp != page_state["version_stamp"]:
                        page_state["version_stamp"] = current_stamp
                        refresh_list()
                    elif page_state.get("version_stamp", 0.0) == 0.0:
                        page_state["version_stamp"] = current_stamp

                refresh_list()
                ui.timer(5.0, check_and_refresh_list)
