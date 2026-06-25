# -*- encoding: utf-8 -*-
"""样品问题收集、对策填写和延期审批页面。

该模块沿用生产异常单的并发写入和延期审批模式，但配置与数据均独立管理，避免与生产异常模块混用。
"""

import copy
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from nicegui import app, ui

from .. import db_storage
from ..config import IMG_DIR, PRESET_AVATARS
from ..issue_workflow_utils import (
    is_current_responsible,
    merge_wecom_recipients,
    parse_date,
    schedule_background_task,
    split_people,
)
from ..sample_issue_config import (
    SAMPLE_DEFAULT_NOTIFY_TARGETS,
    SAMPLE_EDITOR_ROLES,
    SAMPLE_EXTENSION_APPROVAL_NOTIFY_TARGETS,
    SAMPLE_EXTENSION_APPROVER_ROLES,
    SAMPLE_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL,
    SAMPLE_EXTENSION_NOTIFY_TARGETS,
    SAMPLE_FILTER_ALL_STATE,
    SAMPLE_FILTER_PENDING_EXTENSION_STATE,
    SAMPLE_FILTER_STATES,
    SAMPLE_PUBLIC_BASE_URL,
)
from ..utils import get_cache_busted_path, logout, setup_global_activity_tracking
from ..wecom_service import resolve_wecom_recipients, send_wecom_text_message

logger = logging.getLogger(__name__)

SAMPLE_ISSUE_DATA_KEY = "sample_issue_collection_data"
SAMPLE_ISSUE_VERSION_KEY = "sample_issue_collection_version_stamp"


@dataclass
class SampleIssueUpdateResult:
    """描述一次样品问题原子更新的结果。"""

    db_success: bool
    changed: bool
    code: str
    record: Optional[dict] = None


def get_sample_issue_template() -> dict:
    """返回一张完整的空样品问题记录。"""
    return {
        "issue_id": "",
        "_revision": 0,
        "status": "问题录入",
        "basic_info": {
            "product_model": "",
            "issue_description": "",
            "sample_order_no": "",
            "record_date": datetime.now().strftime("%Y-%m-%d"),
            "assembled_qty": "",
            "issue_qty": "",
            "recorder_name": "",
        },
        "countermeasure": {
            "owner": "",
            "reason_analysis": "",
            "temporary_action": "",
            "corrective_preventive_action": "",
            "due_date": "",
            "extension_requests": [],
        },
        "created_by": "",
        "created_role": "",
        "created_at": "",
        "updated_by": "",
        "updated_at": "",
        "operation_log": [],
    }


def merge_with_sample_issue_template(db_data: dict) -> dict:
    """用模板补齐旧数据，并返回独立副本。"""
    merged = copy.deepcopy(get_sample_issue_template())
    if not isinstance(db_data, dict):
        return merged

    for key, value in db_data.items():
        if key in ["basic_info", "countermeasure"] and isinstance(value, dict):
            merged[key].update(copy.deepcopy(value))
        elif key == "operation_log":
            merged[key] = copy.deepcopy(value) if isinstance(value, list) else []
        elif key in merged:
            merged[key] = copy.deepcopy(value)
        else:
            merged[key] = copy.deepcopy(value)

    if not isinstance(merged["countermeasure"].get("extension_requests"), list):
        merged["countermeasure"]["extension_requests"] = []
    return merged


def generate_sample_issue_id() -> str:
    """生成页面内部使用的稳定记录号。"""
    return f"SPI-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"


def generate_initial_sample_issue_data(current_user: str, current_role: str) -> dict:
    """创建新样品问题草稿。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = get_sample_issue_template()
    data["issue_id"] = generate_sample_issue_id()
    data["basic_info"]["recorder_name"] = current_user
    data["created_by"] = current_user
    data["created_role"] = current_role
    data["created_at"] = now_str
    data["updated_by"] = current_user
    data["updated_at"] = now_str
    data["operation_log"].append({"user": current_user, "role": current_role, "action": "创建样品问题", "time": now_str})
    return data


def is_sample_editor(role: str) -> bool:
    """判断当前角色是否包含任一样品问题维护角色关键字。"""
    return any(role_key in str(role) for role_key in SAMPLE_EDITOR_ROLES)


def is_sample_extension_approver(role: str) -> bool:
    """判断当前角色是否可以审批样品问题延期申请。"""
    return any(role_key in str(role) for role_key in SAMPLE_EXTENSION_APPROVER_ROLES)


def can_edit_sample_base(issue_data: dict, current_user: str, current_role: str) -> bool:
    """创建人和配置编辑角色可维护录入区块。"""
    return is_sample_editor(current_role) or issue_data.get("created_by") == current_user


def can_edit_sample_countermeasure(issue_data: dict, current_user: str, current_role: str) -> bool:
    """对策责任人和配置编辑角色可维护对策区块。"""
    owner = issue_data.get("countermeasure", {}).get("owner", "")
    return is_sample_editor(current_role) or is_current_responsible(owner, current_user, current_role)


def get_record_revision(issue_data: Optional[dict]) -> int:
    """安全读取乐观锁版本号。"""
    try:
        return max(0, int((issue_data or {}).get("_revision", 0)))
    except (TypeError, ValueError):
        return 0


def get_pending_extension_request(countermeasure: dict) -> Optional[dict]:
    """返回当前待审批延期申请。"""
    for request in reversed(countermeasure.get("extension_requests", [])):
        if isinstance(request, dict) and request.get("status") == "待审批":
            return request
    return None


def find_extension_request(countermeasure: dict, request_id: str) -> Optional[dict]:
    """按 id 查找延期申请。"""
    requests = countermeasure.get("extension_requests", [])
    if not isinstance(requests, list):
        return None
    return next(
        (
            request
            for request in requests
            if isinstance(request, dict) and str(request.get("id", "")) == str(request_id)
        ),
        None,
    )


def get_extension_counts(countermeasure: dict) -> tuple[int, int]:
    """返回（已通过次数，总申请次数）。"""
    requests = countermeasure.get("extension_requests", [])
    approved_count = sum(1 for request in requests if isinstance(request, dict) and request.get("status") == "已通过")
    return approved_count, len(requests) if isinstance(requests, list) else 0


def is_countermeasure_complete(issue_data: dict) -> bool:
    """判断对策责任人区块是否已填写完整。"""
    countermeasure = issue_data.get("countermeasure", {})
    required_keys = ["reason_analysis", "temporary_action", "corrective_preventive_action", "due_date"]
    return all(str(countermeasure.get(key, "")).strip() for key in required_keys)


def calculate_sample_issue_status(issue_data: dict) -> str:
    """根据对策区块填写情况推导状态。"""
    if is_countermeasure_complete(issue_data):
        return "措施执行中"

    countermeasure = issue_data.get("countermeasure", {})
    if any(
        str(countermeasure.get(key, "")).strip()
        for key in ["reason_analysis", "temporary_action", "corrective_preventive_action", "due_date"]
    ):
        return "对策填写中"
    return "问题录入"


def sample_issue_matches_filter(issue_data: dict, filter_state: str) -> bool:
    """判断记录是否符合列表筛选条件。"""
    if filter_state == SAMPLE_FILTER_ALL_STATE:
        return True
    if filter_state == SAMPLE_FILTER_PENDING_EXTENSION_STATE:
        return bool(get_pending_extension_request(issue_data.get("countermeasure", {})))
    return calculate_sample_issue_status(issue_data) == filter_state


def get_sample_due_text(issue_data: dict) -> str:
    """列表显示纠正预防措施预计完成日期。"""
    due_date = issue_data.get("countermeasure", {}).get("due_date", "")
    return due_date or "暂无"


def get_sample_dashboard_pending_count(all_issues: Any, current_user: str, current_role: str) -> int:
    """计算主页“样品问题收集”卡片对当前用户显示的待办角标数量。"""
    if not isinstance(all_issues, dict):
        return 0

    if is_sample_extension_approver(current_role):
        return sum(
            1
            for issue_data in all_issues.values()
            if isinstance(issue_data, dict)
            for request in issue_data.get("countermeasure", {}).get("extension_requests", [])
            if isinstance(request, dict) and request.get("status") == "待审批"
        )

    return sum(
        1
        for issue_data in all_issues.values()
        if isinstance(issue_data, dict)
        and is_current_responsible(issue_data.get("countermeasure", {}).get("owner", ""), current_user, current_role)
        and (
            not is_countermeasure_complete(merge_with_sample_issue_template(issue_data))
            or bool(get_pending_extension_request(issue_data.get("countermeasure", {})))
        )
    )


def get_sample_issue_collection_url(issue_id: str = "") -> str:
    """生成企业微信消息中的直达链接。"""
    page_url = f"{SAMPLE_PUBLIC_BASE_URL}/sample_issue_collection"
    return f"{page_url}?issue_id={quote(issue_id, safe='')}" if issue_id else page_url


async def resolve_sample_notify_recipients(targets) -> str:
    """按企业微信通讯录规则解析收件人。"""
    touser = await resolve_wecom_recipients(targets, fallback_touser="")
    if not touser:
        logger.error("样品问题通知规则未匹配到任何企业微信成员：%s", targets)
    return touser


async def format_people_for_wecom(value: str) -> str:
    """把人员姓名解析成企业微信账号；解析不到时保留直接输入值作为发送兜底。"""
    people = split_people(value)
    if not people:
        return await resolve_sample_notify_recipients(SAMPLE_DEFAULT_NOTIFY_TARGETS)
    direct_value = "|".join(people)
    return await resolve_wecom_recipients(
        [{"names": people}],
        fallback_touser=direct_value,
    )


async def send_sample_extension_wecom_message(
    content: str,
    *,
    issue_id: str,
    business_key: str,
    message_type: str,
    additional_people: str = "",
    additional_targets=None,
) -> tuple[bool, str]:
    """发送样品问题延期相关企业微信通知。"""
    role_recipients = await resolve_sample_notify_recipients(SAMPLE_EXTENSION_NOTIFY_TARGETS)
    additional_role_recipients = await resolve_sample_notify_recipients(additional_targets) if additional_targets else ""
    people_recipients = await format_people_for_wecom(additional_people) if additional_people else ""
    touser = merge_wecom_recipients(role_recipients, additional_role_recipients, people_recipients)
    if not touser:
        return False, "样品问题延期通知规则未匹配到企业微信成员"
    return await send_wecom_text_message(
        content,
        touser,
        module="sample_issue_collection",
        business_key=business_key,
        message_type=message_type,
        link_url=get_sample_issue_collection_url(issue_id),
    )


async def atomic_sample_issue_update(
    issue_id: str,
    update_function,
    *,
    expected_revision: Optional[int] = None,
    create: bool = False,
) -> SampleIssueUpdateResult:
    """样品问题模块统一数据库写入入口。"""
    outcome = {"changed": False, "code": "db_error", "record": None}

    def apply_update(current):
        current_exists = isinstance(current, dict) and bool(current.get("issue_id"))
        if create:
            if current is not None:
                outcome["code"] = "already_exists"
                return db_storage.ATOMIC_NO_UPDATE
            record = get_sample_issue_template()
        else:
            if not current_exists:
                outcome["code"] = "not_found"
                return db_storage.ATOMIC_NO_UPDATE
            record = merge_with_sample_issue_template(current)

        if expected_revision is not None and get_record_revision(record) != expected_revision:
            outcome["code"] = "revision_conflict"
            outcome["record"] = copy.deepcopy(record)
            return db_storage.ATOMIC_NO_UPDATE

        code, updated = update_function(record)
        outcome["code"] = code
        if code != "updated":
            outcome["record"] = copy.deepcopy(record)
            return db_storage.ATOMIC_NO_UPDATE

        updated = merge_with_sample_issue_template(updated)
        updated["_revision"] = get_record_revision(record) + 1
        updated["status"] = calculate_sample_issue_status(updated)
        outcome["changed"] = True
        outcome["record"] = copy.deepcopy(updated)
        return updated

    success = await db_storage.atomic_deep_update([SAMPLE_ISSUE_DATA_KEY, issue_id], apply_update)
    if success and outcome["changed"]:
        await db_storage.set_item(SAMPLE_ISSUE_VERSION_KEY, time.time())
    return SampleIssueUpdateResult(
        db_success=success,
        changed=bool(success and outcome["changed"]),
        code=outcome["code"] if success else "db_error",
        record=outcome["record"],
    )


async def save_sample_issue_record(issue_data: dict, user: str, role: str, *, is_new: bool) -> SampleIssueUpdateResult:
    """保存样品问题记录，并在事务内按录入区块/对策区块重新校验权限。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    incoming = merge_with_sample_issue_template(issue_data)
    incoming["updated_by"] = user
    incoming["updated_at"] = now_str

    def save_record(current):
        if is_new:
            incoming.setdefault("operation_log", []).append(
                {"user": user, "role": role, "action": "保存样品问题", "time": now_str}
            )
            return "updated", copy.deepcopy(incoming)

        stored = merge_with_sample_issue_template(current)
        can_edit_base = can_edit_sample_base(stored, user, role)
        can_edit_countermeasure = can_edit_sample_countermeasure(stored, user, role)
        if not can_edit_base and not can_edit_countermeasure:
            return "forbidden", stored

        updated = copy.deepcopy(stored)
        if can_edit_base:
            updated["basic_info"] = copy.deepcopy(incoming["basic_info"])
            updated["countermeasure"]["owner"] = incoming["countermeasure"].get("owner", "")

        if can_edit_countermeasure:
            for key in ["reason_analysis", "temporary_action", "corrective_preventive_action", "due_date"]:
                updated["countermeasure"][key] = incoming["countermeasure"].get(key, "")

        updated["updated_by"] = user
        updated["updated_at"] = now_str
        updated.setdefault("operation_log", []).append(
            {"user": user, "role": role, "action": "保存样品问题", "time": now_str}
        )
        return "updated", updated

    return await atomic_sample_issue_update(
        incoming["issue_id"],
        save_record,
        expected_revision=None if is_new else get_record_revision(issue_data),
        create=is_new,
    )


@ui.page("/sample_issue_collection")
async def sample_issue_collection_page(issue_id: str = ""):
    """构建样品问题收集页面。"""
    setup_global_activity_tracking()

    ui.add_head_html("""
        <style>
            .q-dialog__inner--minimized>div { max-width: 4000px; }
            html, body { overflow: hidden !important; }
        </style>
    """)

    if not app.storage.user.get("current_user"):
        redirect_target = f"/sample_issue_collection?issue_id={issue_id}" if issue_id else "/sample_issue_collection"
        ui.navigate.to(f"/login?redirect_to={quote(redirect_target, safe='')}")
        return

    current_user = app.storage.user.get("current_user", "未知用户")
    current_role = app.storage.user.get("current_role", "未知角色")
    current_display_path = get_cache_busted_path(
        app.storage.general.get("user_preferences", {}).get(current_user, {}).get("avatar", PRESET_AVATARS[0])
    )

    page_state = {"search_keyword": "", "filter_state": SAMPLE_FILTER_ALL_STATE}
    dialog = ui.dialog().props("persistent")
    root_dialog = ui.dialog().props("maximized persistent")

    async def handle_new_sample_issue():
        await open_sample_issue_detail_dialog()

    def validate_sample_issue_record(sample_data: dict) -> bool:
        """执行保存前的基础校验。"""
        sample_data["issue_id"] = str(sample_data.get("issue_id", "")).strip() or generate_sample_issue_id()
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", sample_data["issue_id"]):
            ui.notify("内部问题编号格式异常，请重新打开录入窗口", type="warning", position="bottom")
            return False

        basic = sample_data.get("basic_info", {})
        countermeasure = sample_data.get("countermeasure", {})
        required_fields = [
            ("产品型号", basic.get("product_model", "")),
            ("问题点描述", basic.get("issue_description", "")),
            ("样品单号", basic.get("sample_order_no", "")),
            ("记录日期", basic.get("record_date", "")),
            ("组装样机数量", basic.get("assembled_qty", "")),
            ("出现问题样机数量", basic.get("issue_qty", "")),
            ("记录人姓名", basic.get("recorder_name", "")),
            ("对策责任人姓名", countermeasure.get("owner", "")),
        ]
        missing = [label for label, value in required_fields if not str(value).strip()]
        if missing:
            ui.notify(f"请填写：{'、'.join(missing)}", type="warning", position="bottom", multi_line=True)
            return False
        return True

    async def open_sample_issue_detail_dialog(target_issue_id=None):
        """读取记录快照并打开详情窗口。"""
        is_new = target_issue_id is None
        all_issues = db_storage.get_item(SAMPLE_ISSUE_DATA_KEY, {})
        if is_new:
            local_data = generate_initial_sample_issue_data(current_user, current_role)
        else:
            local_data = merge_with_sample_issue_template(all_issues.get(target_issue_id, {}))
            if not local_data.get("issue_id"):
                return ui.notify("未找到该样品问题记录", type="negative", position="bottom")

        local_data["countermeasure"].setdefault("extension_requests", [])
        can_edit_base = is_new or can_edit_sample_base(local_data, current_user, current_role)
        can_edit_countermeasure = (not is_new) and can_edit_sample_countermeasure(local_data, current_user, current_role)
        can_save_record = can_edit_base or can_edit_countermeasure

        def bind_input(label, target, key, classes="w-full", readonly=False):
            props = "outlined dense"
            if readonly:
                props += " readonly"
            field = ui.input(label, value=target.get(key, "")).props(props).classes(f"{classes} mb-3")
            if not readonly:
                field.on_value_change(lambda e, t=target, k=key: t.__setitem__(k, e.value))
            return field

        def bind_date(label, target, key, classes="w-full", readonly=False):
            field = ui.input(label, value=target.get(key, "")).props("outlined dense readonly").classes(
                f"{classes} mb-3"
            )
            if readonly:
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

        def bind_textarea(label, target, key, classes="w-full", readonly=False):
            props = "outlined autogrow"
            if readonly:
                props += " readonly"
            field = ui.textarea(label, value=target.get(key, "")).props(props).classes(f"{classes} mb-3")
            if not readonly:
                field.on_value_change(lambda e, t=target, k=key: t.__setitem__(k, e.value))
            return field

        def section(title: str):
            with ui.element("div").classes("w-full bg-white border border-gray-200 rounded-md p-4"):
                ui.label(title).classes("text-base font-bold text-gray-800 mb-3")
                return ui.column().classes("w-full gap-3")

        async def save_current_record():
            if not can_save_record:
                return ui.notify("当前用户无保存权限", type="warning", position="bottom")
            if not validate_sample_issue_record(local_data):
                return
            result = await save_sample_issue_record(local_data, current_user, current_role, is_new=is_new)
            if result.code == "already_exists":
                return ui.notify("样品问题编号已存在，请重新打开录入窗口", type="warning", position="bottom")
            if result.code == "forbidden":
                return ui.notify("当前用户无保存权限", type="warning", position="bottom")
            if result.code == "revision_conflict":
                return ui.notify(
                    "保存已取消：该样品问题已被其他用户更新，请关闭窗口后重新打开再修改",
                    type="warning",
                    position="bottom",
                    multi_line=True,
                )
            if not result.changed:
                return ui.notify("样品问题保存失败，请刷新后重试", type="negative", position="bottom")
            ui.notify("样品问题已保存", type="positive", position="bottom")
            root_dialog.close()
            refresh_list()

        async def open_extension_request_dialog():
            """由对策责任人发起延期申请。"""
            countermeasure = local_data["countermeasure"]
            pending_request = get_pending_extension_request(countermeasure)
            if pending_request:
                return ui.notify("该样品问题已有延期申请待审批", type="warning", position="bottom")
            if not countermeasure.get("due_date", ""):
                return ui.notify("请先填写纠正预防措施预计完成日期", type="warning", position="bottom")

            request_state = {
                "new_due_date": countermeasure.get("due_date", ""),
                "reason": "",
            }

            async def submit_extension_request():
                old_due_date = countermeasure.get("due_date", "")
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
                    "old_due_date": "",
                    "new_due_date": request_state["new_due_date"],
                    "reason": request_state["reason"].strip(),
                    "requester": current_user,
                    "requester_role": current_role,
                    "requested_at": now_str,
                }

                def add_extension_request(current):
                    stored_countermeasure = current.get("countermeasure", {})
                    if not can_edit_sample_countermeasure(current, current_user, current_role):
                        return "permission_changed", current
                    if get_pending_extension_request(stored_countermeasure):
                        return "pending_exists", current
                    stored_due_date = parse_date(stored_countermeasure.get("due_date", ""))
                    if not stored_due_date:
                        return "missing_due_date", current
                    if new_date <= stored_due_date:
                        return "due_date_changed", current

                    extension_request["old_due_date"] = stored_countermeasure.get("due_date", "")
                    stored_countermeasure.setdefault("extension_requests", []).append(copy.deepcopy(extension_request))
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
                    return "updated", current

                result = await atomic_sample_issue_update(local_data["issue_id"], add_extension_request)
                if result.code == "pending_exists":
                    return ui.notify("该样品问题已有延期申请待审批，请刷新查看", type="warning", position="bottom")
                if result.code == "missing_due_date":
                    return ui.notify("预计完成日期为空，请刷新后重新申请", type="warning", position="bottom")
                if result.code == "due_date_changed":
                    return ui.notify("预计完成日期已被更新，请刷新后重新申请", type="warning", position="bottom")
                if result.code == "permission_changed":
                    return ui.notify("对策责任人已变更，当前用户不能再申请延期", type="warning", position="bottom")
                if result.code == "not_found":
                    return ui.notify("该样品问题已不存在，请刷新查看", type="warning", position="bottom")
                if not result.changed or not result.record:
                    return ui.notify("延期申请提交失败，请刷新后重试", type="negative", position="bottom")

                fresh_countermeasure = result.record.get("countermeasure", {})
                fresh_request = find_extension_request(fresh_countermeasure, extension_request["id"])
                if not fresh_request:
                    return ui.notify("延期申请已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom")
                approved_extension_count, request_count = get_extension_counts(fresh_countermeasure)
                basic = result.record.get("basic_info", {})
                content = (
                    "样品问题纠正预防措施延期申请\n"
                    f"样品问题：{result.record['issue_id']}\n"
                    f"产品型号：{basic.get('product_model', '')}\n"
                    f"样品单号：{basic.get('sample_order_no', '')}\n"
                    f"问题点：{basic.get('issue_description', '')}\n"
                    f"申请人：{current_user}\n"
                    f"本次为第 {request_count} 次延期申请\n"
                    f"此前已通过延期：{approved_extension_count} 次\n"
                    f"原预计日期：{fresh_request.get('old_due_date') or '-'}\n"
                    f"申请延期至：{fresh_request['new_due_date']}\n"
                    f"延期原因：{fresh_request['reason']}\n"
                    f"审批角色：{', '.join(SAMPLE_EXTENSION_APPROVER_ROLES)}"
                )
                await send_sample_extension_wecom_message(
                    content,
                    issue_id=result.record["issue_id"],
                    business_key=f"{result.record['issue_id']}:{fresh_request['id']}",
                    message_type="extension_request",
                )
                ui.notify("延期申请已提交", type="positive", position="bottom")
                dialog.close()
                refresh_list()
                await open_sample_issue_detail_dialog(local_data["issue_id"])

            dialog.clear()
            with dialog, ui.card().classes("w-1/3 max-w-lg p-5"):
                ui.label("申请延期").classes("text-lg font-bold text-gray-800")
                ui.label(local_data.get("issue_id", "")).classes("text-sm text-gray-600")
                bind_date("新的预计完成日期", request_state, "new_due_date", readonly=False)
                bind_textarea("延期原因", request_state, "reason", readonly=False)
                with ui.row().classes("w-full justify-end gap-3 mt-2"):
                    ui.button("取消", on_click=dialog.close).props("outline color=grey")
                    ui.button("提交申请", icon="schedule", on_click=submit_extension_request).props("color=primary")
            dialog.open()

        async def approve_extension_request(request: dict, approved: bool):
            """审批一条延期申请；通过时修改预计完成日期。"""
            if not is_sample_extension_approver(current_role):
                return ui.notify("当前角色无延期审批权限", type="warning", position="bottom")

            request_id = str(request.get("id", ""))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            action_text = "通过延期申请" if approved else "驳回延期申请"

            def update_extension_request(current):
                stored_countermeasure = current.get("countermeasure", {})
                stored_request = find_extension_request(stored_countermeasure, request_id)
                if not stored_request:
                    return "request_not_found", current
                if stored_request.get("status") != "待审批":
                    return "already_processed", current

                if approved:
                    current_due_date = parse_date(stored_countermeasure.get("due_date", ""))
                    requested_old_due_date = parse_date(stored_request.get("old_due_date", ""))
                    if current_due_date != requested_old_due_date:
                        return "due_date_changed", current

                stored_request["status"] = "已通过" if approved else "已驳回"
                stored_request["approver"] = current_user
                stored_request["approver_role"] = current_role
                stored_request["approved_at"] = now_str
                if approved:
                    stored_countermeasure["due_date"] = stored_request.get(
                        "new_due_date", stored_countermeasure.get("due_date", "")
                    )
                current["updated_by"] = current_user
                current["updated_at"] = now_str
                current.setdefault("operation_log", []).append(
                    {"user": current_user, "role": current_role, "action": action_text, "time": now_str}
                )
                return "updated", current

            result = await atomic_sample_issue_update(local_data["issue_id"], update_extension_request)
            if result.code == "already_processed":
                return ui.notify("该延期申请已被其他审批人处理，请刷新查看", type="warning", position="bottom")
            if result.code == "due_date_changed":
                return ui.notify("预计完成日期已发生变化，不能直接通过原延期申请", type="warning", position="bottom")
            if result.code in {"request_not_found", "not_found"}:
                return ui.notify("该延期申请已发生变化，请刷新查看", type="warning", position="bottom")
            if not result.changed or not result.record:
                return ui.notify("延期审批失败，请刷新后重试", type="negative", position="bottom")

            fresh_countermeasure = result.record.get("countermeasure", {})
            fresh_request = find_extension_request(fresh_countermeasure, request_id)
            if not fresh_request:
                return ui.notify("延期审批已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom")
            approved_extension_count, request_count = get_extension_counts(fresh_countermeasure)
            basic = result.record.get("basic_info", {})
            content = (
                "样品问题延期申请审批结果\n"
                f"样品问题：{result.record['issue_id']}\n"
                f"产品型号：{basic.get('product_model', '')}\n"
                f"样品单号：{basic.get('sample_order_no', '')}\n"
                f"审批结果：{'通过' if approved else '驳回'}\n"
                f"累计延期申请：{request_count} 次\n"
                f"当前已通过延期：{approved_extension_count} 次\n"
                f"原预计日期：{fresh_request.get('old_due_date', '-')}\n"
                f"申请延期至：{fresh_request.get('new_due_date', '-')}\n"
                f"审批人：{current_user}"
            )
            schedule_background_task(
                send_sample_extension_wecom_message(
                    content,
                    issue_id=result.record["issue_id"],
                    business_key=f"{result.record['issue_id']}:{fresh_request['id']}:approval",
                    message_type="extension_approval",
                    additional_people=(
                        fresh_request.get("requester", "") if SAMPLE_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL else ""
                    ),
                    additional_targets=SAMPLE_EXTENSION_APPROVAL_NOTIFY_TARGETS if approved else None,
                ),
                "样品问题延期审批企业微信通知",
            )
            ui.notify("延期审批已处理", type="positive", position="bottom")
            refresh_list()
            await open_sample_issue_detail_dialog(local_data["issue_id"])

        def render_extension_controls():
            countermeasure = local_data["countermeasure"]
            pending_extension = get_pending_extension_request(countermeasure)
            approved_extension_count, extension_request_count = get_extension_counts(countermeasure)

            with ui.element("div").classes("w-full border border-gray-200 rounded-md bg-gray-50 p-4"):
                with ui.row().classes("w-full justify-between items-center mb-3"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("延期申请").classes("font-bold text-sm text-gray-700")
                        ui.badge(f"已延期 {approved_extension_count} 次", color="blue").props("outline")
                    ui.label(f"累计申请 {extension_request_count} 次").classes("text-xs text-gray-500")

                if pending_extension:
                    ui.label(
                        f"待审批：{pending_extension.get('old_due_date', '-')} → {pending_extension.get('new_due_date', '-')}"
                    ).classes("text-sm font-bold text-orange-800")
                    ui.label(
                        f"申请人：{pending_extension.get('requester', '-')} ｜ 原因：{pending_extension.get('reason', '-')}"
                    ).classes("text-sm text-orange-700")
                    if is_sample_extension_approver(current_role):

                        async def reject_extension(event=None, r=pending_extension):
                            await approve_extension_request(r, False)

                        async def approve_extension(event=None, r=pending_extension):
                            await approve_extension_request(r, True)

                        with ui.row().classes("justify-end gap-2 mt-2"):
                            ui.button("驳回延期", icon="close", on_click=reject_extension).props(
                                "outline color=negative dense"
                            )
                            ui.button("通过延期", icon="check", on_click=approve_extension).props("color=green dense")
                else:
                    recent_extension = next(
                        (
                            req
                            for req in reversed(countermeasure.get("extension_requests", []))
                            if isinstance(req, dict) and req.get("status") != "待审批"
                        ),
                        None,
                    )
                    if recent_extension:
                        ui.label(
                            f"最近延期审批：{recent_extension.get('status')}，"
                            f"{recent_extension.get('old_due_date', '-')} → {recent_extension.get('new_due_date', '-')}"
                        ).classes("text-xs text-gray-500 mb-2")
                    if can_edit_countermeasure:
                        ui.button("申请延期", icon="event", on_click=open_extension_request_dialog).props(
                            "outline color=orange"
                        )
                    elif not is_new:
                        ui.label("仅对策责任人可申请延期。").classes("text-xs text-gray-500")

        root_dialog.clear()
        with root_dialog, ui.card().classes("w-full h-[100vh] flex flex-col p-0 overflow-hidden bg-gray-100"):
            with ui.row().classes("w-full bg-white px-4 py-3 border-b border-gray-300 justify-between items-center"):
                with ui.row().classes("items-center gap-3 min-w-0"):
                    status = calculate_sample_issue_status(local_data)
                    ui.badge(status, color="orange" if status != "措施执行中" else "green")
                    ui.label(local_data["issue_id"] or "新样品问题").classes("font-mono font-bold text-lg text-gray-800")
                    ui.label(local_data["basic_info"].get("product_model") or "未填写产品型号").classes(
                        "text-base font-bold text-gray-700"
                    )
                ui.button(icon="close", on_click=root_dialog.close).props("flat round dense")

            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
                    basic = local_data["basic_info"]
                    countermeasure = local_data["countermeasure"]

                    with section("问题点录入信息"):
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input("产品型号", basic, "product_model", "w-full md:w-[32%]", readonly=not can_edit_base)
                            bind_input("样品单号", basic, "sample_order_no", "w-full md:w-[32%]", readonly=not can_edit_base)
                            bind_date("记录日期", basic, "record_date", "w-full md:w-[32%]", readonly=not can_edit_base)
                        bind_textarea("问题点描述", basic, "issue_description", readonly=not can_edit_base)
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input(
                                "组装样机数量",
                                basic,
                                "assembled_qty",
                                "w-full md:w-[24%]",
                                readonly=not can_edit_base,
                            )
                            bind_input(
                                "出现问题样机数量",
                                basic,
                                "issue_qty",
                                "w-full md:w-[24%]",
                                readonly=not can_edit_base,
                            )
                            bind_input(
                                "记录人姓名",
                                basic,
                                "recorder_name",
                                "w-full md:w-[24%]",
                                readonly=not can_edit_base,
                            )
                            bind_input(
                                "对策责任人姓名",
                                countermeasure,
                                "owner",
                                "w-full md:w-[24%]",
                                readonly=not can_edit_base,
                            )
                        with ui.row().classes("w-full gap-3 text-xs text-gray-500"):
                            ui.label(f"创建：{local_data.get('created_by', '')} / {local_data.get('created_at', '')}")
                            ui.label(f"最近更新：{local_data.get('updated_by', '')} / {local_data.get('updated_at', '')}")

                    with section("对策责任人填写信息"):
                        bind_textarea("原因分析", countermeasure, "reason_analysis", readonly=not can_edit_countermeasure)
                        bind_textarea("样品临时对策", countermeasure, "temporary_action", readonly=not can_edit_countermeasure)
                        bind_textarea(
                            "纠正预防措施",
                            countermeasure,
                            "corrective_preventive_action",
                            readonly=not can_edit_countermeasure,
                        )
                        bind_date(
                            "纠正预防措施预计完成日期",
                            countermeasure,
                            "due_date",
                            "w-full md:w-[32%]",
                            readonly=not can_edit_countermeasure,
                        )
                        if is_new:
                            ui.label("保存后，对策责任人可填写该区块并申请延期。").classes("text-xs text-gray-500")
                        else:
                            render_extension_controls()

                    with ui.expansion("操作留痕", icon="history", value=False).classes(
                        "w-full bg-white border border-gray-200 rounded-md"
                    ):
                        with ui.column().classes("w-full gap-2 p-4 pt-0"):
                            logs = local_data.get("operation_log", [])
                            if not logs:
                                ui.label("暂无操作记录").classes("text-sm text-gray-400")
                            for log in reversed(logs[-20:]):
                                ui.label(
                                    f"{log.get('time', '')}  {log.get('user', '')}({log.get('role', '')})  {log.get('action', '')}"
                                ).classes("text-sm text-gray-600")

            with ui.row().classes("w-full bg-white border-t border-gray-200 p-3 justify-end gap-3"):
                ui.button("关闭窗口", on_click=root_dialog.close).props("outline color=grey")
                if can_save_record:
                    ui.button("保存样品问题", icon="save", on_click=save_current_record).props("color=primary")

        root_dialog.open()

    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-600 h-14 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("样品问题收集").classes("text-white text-xl font-bold absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {current_user}")
                ui.separator()
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    def status_color(status: str) -> str:
        if status == "措施执行中":
            return "green"
        if status == "对策填写中":
            return "orange"
        return "grey"

    def status_border_color(status: str) -> str:
        return {
            "措施执行中": "#22c55e",
            "对策填写中": "#f97316",
            "问题录入": "#64748b",
        }.get(status, "#64748b")

    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.column().classes("w-full h-full p-4 gap-4"):
            with ui.row().classes("w-full justify-between items-center bg-white p-4 shadow-sm rounded-md"):
                with ui.row().classes("gap-3 items-center"):
                    ui.input("搜索型号/样品单号/问题/责任人").props("dense outlined").bind_value(
                        page_state, "search_keyword"
                    ).classes("w-72")
                    ui.select(SAMPLE_FILTER_STATES, label="状态筛选").props("dense outlined").bind_value(
                        page_state, "filter_state"
                    ).classes("w-44")
                    ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("outline color=primary")
                ui.button("录入样品问题", icon="add_box", on_click=handle_new_sample_issue).props("color=red-7")

            with ui.element("div").classes("w-full flex-grow overflow-y-auto overflow-x-hidden p-1"):
                list_container = ui.column().classes("w-full gap-3")

                def refresh_list():
                    """从数据库读取、筛选并绘制样品问题列表。"""
                    list_container.clear()
                    all_issues = db_storage.get_item(SAMPLE_ISSUE_DATA_KEY, {})
                    keyword = page_state["search_keyword"].lower().strip()
                    filter_state = page_state["filter_state"]

                    valid_issues = [
                        merge_with_sample_issue_template(issue)
                        for issue in all_issues.values()
                        if issue and isinstance(issue, dict)
                    ]
                    valid_issues = sorted(
                        valid_issues,
                        key=lambda item: item.get("updated_at") or item.get("created_at") or "",
                        reverse=True,
                    )

                    with list_container:
                        if not valid_issues:
                            ui.label("暂无样品问题记录").classes("text-gray-500 m-auto mt-10")
                            return

                        rendered_count = 0
                        for issue_data in valid_issues:
                            basic = issue_data.get("basic_info", {})
                            countermeasure = issue_data.get("countermeasure", {})
                            status = calculate_sample_issue_status(issue_data)
                            searchable = " ".join(
                                [
                                    issue_data.get("issue_id", ""),
                                    basic.get("product_model", ""),
                                    basic.get("issue_description", ""),
                                    basic.get("sample_order_no", ""),
                                    basic.get("recorder_name", ""),
                                    countermeasure.get("owner", ""),
                                ]
                            ).lower()
                            if keyword and keyword not in searchable:
                                continue
                            if not sample_issue_matches_filter(issue_data, filter_state):
                                continue

                            rendered_count += 1
                            pending_extension = get_pending_extension_request(countermeasure)
                            is_my_pending = is_current_responsible(
                                countermeasure.get("owner", ""), current_user, current_role
                            ) and (not is_countermeasure_complete(issue_data) or bool(pending_extension))

                            with ui.element("div").classes(
                                "w-full bg-white border border-gray-200 border-l-4 rounded-md p-4 shadow-sm "
                                "hover:bg-amber-50 cursor-pointer transition-colors"
                            ) as card:

                                async def open_card_detail(_, i_id=issue_data["issue_id"]):
                                    await open_sample_issue_detail_dialog(i_id)

                                card.style(f"border-left-color: {status_border_color(status)}")
                                card.on("click", open_card_detail)
                                with ui.row().classes("w-full justify-between items-start gap-4"):
                                    with ui.column().classes("gap-1 min-w-0"):
                                        with ui.row().classes("items-center gap-2"):
                                            ui.label(issue_data["issue_id"]).classes(
                                                "font-mono font-bold text-lg text-gray-800"
                                            )
                                            ui.badge(status, color=status_color(status)).props("outline")
                                            if pending_extension:
                                                ui.badge("延期申请中", color="orange").props("outline")
                                            if is_my_pending:
                                                ui.chip("待我处理", icon="notifications_active", color="red").props(
                                                    "dense outline size=sm"
                                                )
                                        ui.label(basic.get("product_model", "未填写产品型号")).classes(
                                            "font-bold text-gray-800"
                                        )
                                        ui.label(
                                            f"样品单号：{basic.get('sample_order_no', '') or '-'} ｜ "
                                            f"记录日期：{basic.get('record_date', '') or '-'} ｜ "
                                            f"问题样机：{basic.get('issue_qty', '') or '-'}/{basic.get('assembled_qty', '') or '-'}"
                                        ).classes("text-sm text-gray-600")
                                        if basic.get("issue_description"):
                                            ui.label(basic.get("issue_description", "")[:120]).classes(
                                                "text-sm text-gray-500 line-clamp-2"
                                            )
                                    with ui.column().classes("items-end gap-1 shrink-0"):
                                        ui.label(f"记录人：{basic.get('recorder_name', '') or '-'}").classes(
                                            "text-xs text-gray-500"
                                        )
                                        ui.label(f"对策责任人：{countermeasure.get('owner', '') or '-'}").classes(
                                            "text-xs text-orange-700"
                                        )
                                        ui.label(f"预计完成：{get_sample_due_text(issue_data)}").classes(
                                            "text-xs text-gray-500"
                                        )

                        if rendered_count == 0:
                            ui.label("没有符合筛选条件的样品问题").classes("text-gray-500 m-auto mt-10")

                def check_and_refresh_list():
                    """检测后台或其他用户写入的版本时间戳，必要时自动刷新列表。"""
                    current_stamp = db_storage.get_item(SAMPLE_ISSUE_VERSION_KEY, 0.0)
                    if page_state.get("version_stamp", 0.0) != 0.0 and current_stamp != page_state["version_stamp"]:
                        page_state["version_stamp"] = current_stamp
                        refresh_list()
                    elif page_state.get("version_stamp", 0.0) == 0.0:
                        page_state["version_stamp"] = current_stamp

                refresh_list()
                ui.timer(5.0, check_and_refresh_list)

    if issue_id:
        await open_sample_issue_detail_dialog(issue_id)
