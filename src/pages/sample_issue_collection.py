# -*- encoding: utf-8 -*-
"""样品问题收集、对策填写和延期审批页面。

该模块沿用生产异常单的并发写入和延期审批模式，但配置与数据均独立管理，避免与生产异常模块混用。
"""

import copy
import hashlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote

from nicegui import app, ui

from .. import db_storage
from ..components import ButtonUploader, FileThumbnail, get_upload_local_path
from ..config import IMG_DIR, PRESET_AVATARS, REQ_UPLOADS_FILE_TYPE, UPLOAD_URL_DIR, UPLOADS_DIR
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
    SAMPLE_FILTER_CLOSED_STATE,
    SAMPLE_FILTER_PENDING_CLOSE_STATE,
    SAMPLE_FILTER_PENDING_EXTENSION_STATE,
    SAMPLE_FILTER_STATES,
    SAMPLE_PUBLIC_BASE_URL,
    SAMPLE_STATUS_CORRECTIVE_ACTION_DONE,
    SAMPLE_STATUS_ISSUE_RECORDED,
    SAMPLE_STATUS_TEMPORARY_ACTION_DONE,
)
from ..utils import get_cache_busted_path, handle_key, logout, setup_global_activity_tracking
from ..wecom_service import resolve_wecom_recipients, send_wecom_text_message

logger = logging.getLogger(__name__)

SAMPLE_ISSUE_DATA_KEY = "sample_issue_collection_data"
SAMPLE_ISSUE_VERSION_KEY = "sample_issue_collection_version_stamp"
SAMPLE_ISSUE_ID_PREFIX = "SPI"
SAMPLE_ISSUE_ID_SEQUENCE_WIDTH = 3
SAMPLE_ISSUE_ID_SEQUENCE_MAX = 999
SAMPLE_ATTACHMENT_DIR_NAME = "sample_issue"
SAMPLE_ATTACHMENT_ACCEPT = ",".join(["image/*", *sorted(REQ_UPLOADS_FILE_TYPE)])
SAMPLE_ATTACHMENT_PARENTS_H = 12


def get_attachment_label_number(file_info: dict) -> int:
    """把附件显示序号转换为整数；历史脏值按 0 处理。"""
    try:
        return int(str(file_info.get("file_lab", "0")))
    except (TypeError, ValueError):
        return 0


def get_active_evidence_files(countermeasure: dict) -> list[dict]:
    """返回未被删除的附件信息，按页面显示序号排序。"""
    files = countermeasure.get("evidence_files", [])
    if not isinstance(files, list):
        return []
    active_files = [
        copy.deepcopy(file_info)
        for file_info in files
        if isinstance(file_info, dict) and not file_info.get("file_del_bool")
    ]
    return sorted(
        active_files,
        key=get_attachment_label_number,
    )


def sanitize_upload_path_segment(value: str, default: str) -> str:
    """把用户输入转换为可用于 Windows 文件夹或文件名前缀的片段。"""
    safe_value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    safe_value = safe_value.strip(" .")
    return safe_value or default


def get_sample_attachment_user_folder(uploader_name: str) -> str:
    """返回样品附件按上传人归档使用的文件夹名。"""
    return sanitize_upload_path_segment(uploader_name, "unknown")


def get_upload_file_hash_name(
    issue_id: str,
    uploader_name: str,
    original_filename: str,
    content: bytes,
) -> tuple[str, str, str]:
    """生成样品附件存储文件名，并返回原始主名、后缀和哈希文件名。"""
    safe_name = sanitize_upload_path_segment(os.path.basename(original_filename), "attachment")
    file_name, file_suffix = os.path.splitext(safe_name)
    file_name = file_name or "attachment"
    file_suffix = file_suffix.lstrip(".").lower()
    file_hash = hashlib.md5(content).hexdigest()
    safe_issue_id = sanitize_upload_path_segment(issue_id, "sample_issue")
    safe_uploader = get_sample_attachment_user_folder(uploader_name)
    return file_name, file_suffix, f"sample_issue_{safe_issue_id}_{safe_uploader}_{file_name}.{file_hash}.{file_suffix}"


def get_sample_attachment_storage_paths(uploader_name: str, file_name_hash: str) -> tuple[str, str]:
    """返回样品附件的本地保存路径和访问 URL。"""
    user_folder = get_sample_attachment_user_folder(uploader_name)
    target_dir = os.path.join(UPLOADS_DIR, SAMPLE_ATTACHMENT_DIR_NAME, user_folder)
    target_path = os.path.join(target_dir, file_name_hash)
    url_path = "/".join(
        [
            UPLOAD_URL_DIR.rstrip("/"),
            SAMPLE_ATTACHMENT_DIR_NAME,
            quote(user_folder, safe=""),
            quote(file_name_hash, safe=""),
        ]
    )
    return target_path, url_path


def get_sample_issue_id_prefix(reference_time: Optional[datetime] = None) -> str:
    """返回当天样品问题编号前缀，例如 SPI20260701。"""
    target_time = reference_time or datetime.now()
    return f"{SAMPLE_ISSUE_ID_PREFIX}{target_time.strftime('%Y%m%d')}"


def get_next_sample_issue_id(all_issues: Any, reference_time: Optional[datetime] = None) -> str:
    """按当天已有编号生成下一个 SPIyyyyMMddNNN 编号。"""
    prefix = get_sample_issue_id_prefix(reference_time)
    issue_id_pattern = re.compile(rf"^{re.escape(prefix)}(\d{{{SAMPLE_ISSUE_ID_SEQUENCE_WIDTH}}})$")
    max_sequence = 0

    if isinstance(all_issues, dict):
        for key, issue_data in all_issues.items():
            candidates = [key]
            if isinstance(issue_data, dict):
                candidates.append(issue_data.get("issue_id", ""))
            for candidate in candidates:
                match = issue_id_pattern.fullmatch(str(candidate or ""))
                if match:
                    max_sequence = max(max_sequence, int(match.group(1)))

    if max_sequence >= SAMPLE_ISSUE_ID_SEQUENCE_MAX:
        return ""
    return f"{prefix}{max_sequence + 1:0{SAMPLE_ISSUE_ID_SEQUENCE_WIDTH}d}"


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
        "status": SAMPLE_STATUS_ISSUE_RECORDED,
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
            "evidence_files": [],
            "extension_requests": [],
            "close_requests": [],
            "close_note": "",
            "closed_by": "",
            "closed_role": "",
            "closed_at": "",
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

    countermeasure = merged["countermeasure"]
    if not isinstance(countermeasure.get("evidence_files"), list):
        countermeasure["evidence_files"] = []
    if not isinstance(countermeasure.get("extension_requests"), list):
        countermeasure["extension_requests"] = []
    if not isinstance(countermeasure.get("close_requests"), list):
        countermeasure["close_requests"] = []
    return merged


def generate_initial_sample_issue_data(current_user: str, current_role: str) -> dict:
    """创建新样品问题草稿。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = get_sample_issue_template()
    data["basic_info"]["recorder_name"] = current_user
    data["created_by"] = current_user
    data["created_role"] = current_role
    data["created_at"] = now_str
    data["updated_by"] = current_user
    data["updated_at"] = now_str
    data["operation_log"].append(
        {"user": current_user, "role": current_role, "action": "创建样品问题", "time": now_str}
    )
    return data


def is_sample_editor(role: str) -> bool:
    """判断当前角色是否包含任一样品问题维护角色关键字。"""
    return any(role_key in str(role) for role_key in SAMPLE_EDITOR_ROLES)


def is_sample_extension_approver(role: str) -> bool:
    """判断当前角色是否可以审批样品问题延期申请。"""
    return any(role_key in str(role) for role_key in SAMPLE_EXTENSION_APPROVER_ROLES)


def is_sample_admin(role: str) -> bool:
    """删除样品问题属于高风险操作，仅允许角色值严格等于 admin。"""
    return str(role).strip().lower() == "admin"


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
    requests = countermeasure.get("extension_requests", [])
    if isinstance(requests, list):
        for request in reversed(requests):
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


def get_pending_close_request(countermeasure: dict) -> Optional[dict]:
    """返回当前待审批关闭申请。"""
    requests = countermeasure.get("close_requests", [])
    if isinstance(requests, list):
        for request in reversed(requests):
            if isinstance(request, dict) and request.get("status") == "待审批":
                return request
    return None


def find_close_request(countermeasure: dict, request_id: str) -> Optional[dict]:
    """按 id 查找关闭申请。"""
    requests = countermeasure.get("close_requests", [])
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
    if not isinstance(requests, list):
        return 0, 0
    approved_count = sum(1 for request in requests if isinstance(request, dict) and request.get("status") == "已通过")
    return approved_count, len(requests)


def get_close_counts(countermeasure: dict) -> tuple[int, int]:
    """返回（已通过关闭次数，总关闭申请次数）。"""
    requests = countermeasure.get("close_requests", [])
    if not isinstance(requests, list):
        return 0, 0
    approved_count = sum(1 for request in requests if isinstance(request, dict) and request.get("status") == "已通过")
    return approved_count, len(requests)


def is_sample_issue_closed(issue_data: dict) -> bool:
    """判断样品问题是否已经完成关闭审批。"""
    countermeasure = issue_data.get("countermeasure", {})
    if countermeasure.get("closed_at"):
        return True
    requests = countermeasure.get("close_requests", [])
    if not isinstance(requests, list):
        return False
    return any(isinstance(request, dict) and request.get("status") == "已通过" for request in requests)


def is_countermeasure_complete(issue_data: dict) -> bool:
    """判断对策责任人区块是否已填写完整。"""
    countermeasure = issue_data.get("countermeasure", {})
    required_keys = ["reason_analysis", "temporary_action", "corrective_preventive_action", "due_date"]
    return all(str(countermeasure.get(key, "")).strip() for key in required_keys)


def is_temporary_action_complete(issue_data: dict) -> bool:
    """判断原因分析和样品临时对策是否已经填写完整。"""
    countermeasure = issue_data.get("countermeasure", {})
    required_keys = ["reason_analysis", "temporary_action"]
    return all(str(countermeasure.get(key, "")).strip() for key in required_keys)


def calculate_sample_issue_status(issue_data: dict) -> str:
    """根据对策区块填写情况推导状态。"""
    countermeasure = issue_data.get("countermeasure", {})
    if is_sample_issue_closed(issue_data):
        return SAMPLE_FILTER_CLOSED_STATE
    if get_pending_close_request(countermeasure):
        return SAMPLE_FILTER_PENDING_CLOSE_STATE
    if is_countermeasure_complete(issue_data):
        return SAMPLE_STATUS_CORRECTIVE_ACTION_DONE
    if is_temporary_action_complete(issue_data):
        return SAMPLE_STATUS_TEMPORARY_ACTION_DONE
    return SAMPLE_STATUS_ISSUE_RECORDED


def sample_issue_matches_filter(issue_data: dict, filter_state: str) -> bool:
    """判断记录是否符合列表筛选条件。"""
    if filter_state == SAMPLE_FILTER_ALL_STATE:
        return True
    if filter_state == SAMPLE_FILTER_PENDING_EXTENSION_STATE:
        return bool(get_pending_extension_request(issue_data.get("countermeasure", {})))
    if filter_state == SAMPLE_FILTER_PENDING_CLOSE_STATE:
        return bool(get_pending_close_request(issue_data.get("countermeasure", {})))
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
            if (
                bool(get_pending_extension_request(issue_data.get("countermeasure", {})))
                or bool(get_pending_close_request(issue_data.get("countermeasure", {})))
            )
        )

    return sum(
        1
        for issue_data in all_issues.values()
        if isinstance(issue_data, dict)
        and not is_sample_issue_closed(merge_with_sample_issue_template(issue_data))
        and not get_pending_close_request(issue_data.get("countermeasure", {}))
        and is_current_responsible(issue_data.get("countermeasure", {}).get("owner", ""), current_user, current_role)
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
    additional_role_recipients = (
        await resolve_sample_notify_recipients(additional_targets) if additional_targets else ""
    )
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

    if is_new:
        outcome = {"changed": False, "code": "db_error", "record": None}

        def create_record(all_issues):
            current_issues = all_issues if isinstance(all_issues, dict) else {}
            issue_id = get_next_sample_issue_id(current_issues)
            if not issue_id:
                outcome["code"] = "sequence_exhausted"
                return db_storage.ATOMIC_NO_UPDATE
            if issue_id in current_issues:
                outcome["code"] = "already_exists"
                return db_storage.ATOMIC_NO_UPDATE

            new_record = copy.deepcopy(incoming)
            new_record["issue_id"] = issue_id
            new_record["_revision"] = 1
            new_record["status"] = calculate_sample_issue_status(new_record)
            new_record.setdefault("operation_log", []).append(
                {"user": user, "role": role, "action": "保存样品问题", "time": now_str}
            )
            current_issues[issue_id] = new_record
            outcome["changed"] = True
            outcome["code"] = "created"
            outcome["record"] = copy.deepcopy(new_record)
            return current_issues

        success = await db_storage.atomic_deep_update([SAMPLE_ISSUE_DATA_KEY], create_record)
        if success and outcome["changed"]:
            await db_storage.set_item(SAMPLE_ISSUE_VERSION_KEY, time.time())
        return SampleIssueUpdateResult(
            db_success=success,
            changed=bool(success and outcome["changed"]),
            code=outcome["code"] if success else "db_error",
            record=outcome["record"],
        )

    def save_record(current):
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
            for key in [
                "reason_analysis",
                "temporary_action",
                "corrective_preventive_action",
                "due_date",
                "evidence_files",
            ]:
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
        expected_revision=get_record_revision(issue_data),
        create=False,
    )


async def submit_sample_close_request(
    issue_id: str,
    user: str,
    role: str,
    close_note: str = "",
) -> SampleIssueUpdateResult:
    """由对策责任人申请关闭样品问题。"""
    note = close_note.strip()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    close_request = {
        "id": f"close_{uuid.uuid4().hex[:8]}",
        "status": "待审批",
        "note": note,
        "requester": user,
        "requester_role": role,
        "requested_at": now_str,
    }

    def add_close_request(current):
        countermeasure = current.get("countermeasure", {})
        if is_sample_issue_closed(current):
            return "already_closed", current
        if not is_current_responsible(countermeasure.get("owner", ""), user, role):
            return "permission_changed", current
        if get_pending_extension_request(countermeasure):
            return "pending_extension", current
        if get_pending_close_request(countermeasure):
            return "pending_close", current
        if not is_countermeasure_complete(current):
            return "incomplete_countermeasure", current

        countermeasure.setdefault("close_requests", []).append(copy.deepcopy(close_request))
        current["updated_by"] = user
        current["updated_at"] = now_str
        current.setdefault("operation_log", []).append(
            {"user": user, "role": role, "action": "申请关闭样品问题", "time": now_str}
        )
        return "updated", current

    return await atomic_sample_issue_update(issue_id, add_close_request)


async def approve_sample_close_request(
    issue_id: str,
    request_id: str,
    approved: bool,
    user: str,
    role: str,
) -> SampleIssueUpdateResult:
    """审批样品问题关闭申请；通过后整单进入已关闭状态。"""
    if not is_sample_extension_approver(role):
        return SampleIssueUpdateResult(db_success=False, changed=False, code="forbidden")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    action_text = "通过关闭申请" if approved else "驳回关闭申请"

    def update_close_request(current):
        countermeasure = current.get("countermeasure", {})
        stored_request = find_close_request(countermeasure, request_id)
        if not stored_request:
            return "request_not_found", current
        if stored_request.get("status") != "待审批":
            return "already_processed", current
        if approved and is_sample_issue_closed(current):
            return "already_closed", current

        stored_request["status"] = "已通过" if approved else "已驳回"
        stored_request["approver"] = user
        stored_request["approver_role"] = role
        stored_request["approved_at"] = now_str
        if approved:
            countermeasure["close_note"] = stored_request.get("note", "")
            countermeasure["closed_by"] = user
            countermeasure["closed_role"] = role
            countermeasure["closed_at"] = now_str
        current["updated_by"] = user
        current["updated_at"] = now_str
        current.setdefault("operation_log", []).append(
            {"user": user, "role": role, "action": action_text, "time": now_str}
        )
        return "updated", current

    return await atomic_sample_issue_update(issue_id, update_close_request)


async def delete_sample_issue_record(issue_id: str, role: str) -> SampleIssueUpdateResult:
    """由 admin 原子删除单张样品问题。"""
    if not is_sample_admin(role):
        return SampleIssueUpdateResult(db_success=False, changed=False, code="forbidden")

    outcome = {"changed": False, "code": "db_error", "record": None}

    def remove_record(all_issues):
        if not isinstance(all_issues, dict) or issue_id not in all_issues:
            outcome["code"] = "not_found"
            return db_storage.ATOMIC_NO_UPDATE

        outcome["record"] = copy.deepcopy(all_issues[issue_id])
        del all_issues[issue_id]
        outcome["changed"] = True
        outcome["code"] = "deleted"
        return all_issues

    success = await db_storage.atomic_deep_update([SAMPLE_ISSUE_DATA_KEY], remove_record)
    if success and outcome["changed"]:
        await db_storage.set_item(SAMPLE_ISSUE_VERSION_KEY, time.time())
    return SampleIssueUpdateResult(
        db_success=success,
        changed=bool(success and outcome["changed"]),
        code=outcome["code"] if success else "db_error",
        record=outcome["record"],
    )


@ui.page("/sample_issue_collection")
async def sample_issue_collection_page(issue_id: str = ""):
    """构建样品问题收集页面。"""
    setup_global_activity_tracking()
    app.storage.client.setdefault("key_state", {})
    ui.keyboard(on_key=handle_key)

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
    can_delete_record = is_sample_admin(current_role)

    async def handle_new_sample_issue():
        await open_sample_issue_detail_dialog()

    def validate_sample_issue_record(sample_data: dict, *, is_new_record: bool = False) -> bool:
        """执行保存前的基础校验。"""
        sample_data["issue_id"] = str(sample_data.get("issue_id", "")).strip()
        if is_new_record:
            sample_data["issue_id"] = ""
        else:
            if not sample_data["issue_id"]:
                ui.notify("样品问题编号缺失，请关闭窗口后重新打开", type="warning", position="bottom")
                return False
            if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", sample_data["issue_id"]):
                ui.notify("样品问题编号格式异常，请联系管理员", type="warning", position="bottom")
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

        local_data["countermeasure"].setdefault("evidence_files", [])
        local_data["countermeasure"].setdefault("extension_requests", [])
        local_data["countermeasure"].setdefault("close_requests", [])
        if not isinstance(local_data["countermeasure"].get("evidence_files"), list):
            local_data["countermeasure"]["evidence_files"] = []
        if not isinstance(local_data["countermeasure"].get("close_requests"), list):
            local_data["countermeasure"]["close_requests"] = []
        issue_closed = is_sample_issue_closed(local_data)
        can_edit_base = is_new or ((not issue_closed) and can_edit_sample_base(local_data, current_user, current_role))
        can_edit_countermeasure = (
            (not is_new)
            and (not issue_closed)
            and can_edit_sample_countermeasure(local_data, current_user, current_role)
        )
        can_operate_countermeasure = (
            (not is_new)
            and (not issue_closed)
            and is_current_responsible(
                local_data.get("countermeasure", {}).get("owner", ""),
                current_user,
                current_role,
            )
        )
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
            field = (
                ui.input(label, value=target.get(key, "")).props("outlined dense readonly").classes(f"{classes} mb-3")
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

        def initialize_attachment_state():
            """为 FileThumbnail 准备它依赖的客户端附件状态。"""
            active_files = get_active_evidence_files(local_data["countermeasure"])
            app.storage.client["file_thumbnail_dic"] = {}
            app.storage.client["files"] = [
                file_info.get("file_name_hash", "") for file_info in active_files if file_info.get("file_name_hash")
            ]
            app.storage.client["deleted_files"] = []
            app.storage.client["file_counter"] = max(
                [get_attachment_label_number(item) for item in active_files] or [0]
            )
            app.storage.client["ref_question_dic"] = {}
            app.storage.client.setdefault("key_state", {})
            app.storage.client.setdefault("page_elements", {})

        def sync_countermeasure_files_from_thumbnail_state():
            """把 FileThumbnail 的删除状态同步回当前表单快照。"""
            thumbnail_dic = app.storage.client.get("file_thumbnail_dic", {})
            deleted_files = set(app.storage.client.get("deleted_files", []))
            evidence_files = []
            for entry in thumbnail_dic.values():
                file_info = copy.deepcopy(entry.get("file_information", {}))
                if not file_info or file_info.get("file_del_bool"):
                    continue
                if file_info.get("file_name_hash") in deleted_files:
                    continue
                evidence_files.append(file_info)
            local_data["countermeasure"]["evidence_files"] = sorted(evidence_files, key=get_attachment_label_number)
            return local_data["countermeasure"]["evidence_files"]

        def create_attachment_thumbnail(file_info: dict, deletable: bool):
            """使用共用 FileThumbnail 渲染一个附件缩略图。"""
            file_name_hash = file_info.get("file_name_hash", "")
            file_url = file_info.get("file_url", "")
            if file_url:
                file_path = get_upload_local_path(file_url)
                if os.path.exists(file_path):
                    app.add_static_file(local_file=file_path, url_path=file_url)
            thumbnail = FileThumbnail(
                file_url=file_url,
                file_type=file_info.get("file_type", "application/octet-stream"),
                file_name_suffix=file_info.get("file_name_suffix", file_info.get("file_name", "附件")),
                file_lab=str(file_info.get("file_lab", "")),
                parents_h=int(file_info.get("parents_h", SAMPLE_ATTACHMENT_PARENTS_H)),
                delet_lab=deletable,
            )
            app.storage.client["file_thumbnail_dic"][thumbnail.file_index] = {
                "file_obj": thumbnail,
                "file_information": copy.deepcopy(file_info),
            }
            return thumbnail

        async def handle_countermeasure_file_upload(e, parents_h):
            """保存上传文件并追加到当前纠正预防措施附件列表。"""
            if not can_operate_countermeasure:
                return ui.notify("当前用户无附件上传权限", type="warning", position="bottom")

            try:
                file_type = e.file.content_type or "application/octet-stream"
                content = await e.file.read()
                file_name, file_suffix, file_name_hash = get_upload_file_hash_name(
                    local_data["issue_id"],
                    current_user,
                    e.file.name,
                    content,
                )
                if not file_suffix:
                    return ui.notify("无法识别文件后缀，上传已取消", type="warning", position="bottom")
                if not file_type.startswith("image/") and f".{file_suffix}" not in REQ_UPLOADS_FILE_TYPE:
                    return ui.notify(
                        f'文件 "{e.file.name}" 不是允许的附件类型，无法上传',
                        type="warning",
                        position="bottom",
                    )

                target_path, url_path = get_sample_attachment_storage_paths(current_user, file_name_hash)
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                if not os.path.isfile(target_path):
                    with open(target_path, "wb") as uploaded_file:
                        uploaded_file.write(content)

                app.add_static_file(local_file=target_path, url_path=url_path)
                if file_name_hash in app.storage.client.get(
                    "files", []
                ) and file_name_hash not in app.storage.client.get("deleted_files", []):
                    return ui.notify(f"文件已存在：{e.file.name}", type="warning", position="bottom")

                app.storage.client.setdefault("files", []).append(file_name_hash)
                app.storage.client["file_counter"] = int(app.storage.client.get("file_counter", 0)) + 1
                file_lab = str(app.storage.client["file_counter"])
                if file_name_hash in app.storage.client.get("deleted_files", []):
                    app.storage.client["deleted_files"].remove(file_name_hash)

                file_info = {
                    "file_del_bool": False,
                    "file_name": file_name,
                    "file_url": url_path,
                    "file_name_hash": file_name_hash,
                    "file_name_suffix": e.file.name,
                    "file_type": file_type,
                    "file_lab": file_lab,
                    "parents_h": parents_h,
                    "uploaded_by": current_user,
                    "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                current_attachment_row = app.storage.client["page_elements"].get("sample_issue_attachment_row")
                if current_attachment_row is None:
                    return ui.notify("附件区域尚未初始化，请关闭窗口后重试", type="warning", position="bottom")

                with current_attachment_row:
                    create_attachment_thumbnail(file_info, deletable=True)
                sync_countermeasure_files_from_thumbnail_state()
                ui.notify("附件已添加，请保存样品问题", type="positive", position="bottom")
            except Exception as exc:
                logger.exception("样品问题附件上传失败")
                ui.notify(f"上传文件 '{e.file.name}' 失败：{exc}", type="negative", position="bottom", multi_line=True)

        def render_countermeasure_attachments():
            """在纠正预防措施文本下方渲染附件上传按钮和缩略图。"""
            active_files = get_active_evidence_files(local_data["countermeasure"])
            if not can_operate_countermeasure and not active_files:
                return

            ui.label("附件").classes("text-sm font-bold text-gray-700")
            with ui.row().classes("w-full flex-wrap items-start gap-2") as attachment_row:
                app.storage.client["page_elements"]["sample_issue_attachment_row"] = attachment_row
                if can_operate_countermeasure:
                    ButtonUploader(
                        on_upload=handle_countermeasure_file_upload,
                        label="添加图片或文件",
                        input_any_suffix=SAMPLE_ATTACHMENT_ACCEPT,
                        classes_str=f"h-{SAMPLE_ATTACHMENT_PARENTS_H}",
                        props_str="outline color=primary dense",
                        parents_h=SAMPLE_ATTACHMENT_PARENTS_H,
                    )
                for file_info in active_files:
                    create_attachment_thumbnail(file_info, deletable=can_operate_countermeasure)

        async def save_current_record():
            if not can_save_record:
                return ui.notify("当前用户无保存权限", type="warning", position="bottom")
            if can_edit_countermeasure:
                sync_countermeasure_files_from_thumbnail_state()
            if not validate_sample_issue_record(local_data, is_new_record=is_new):
                return
            result = await save_sample_issue_record(local_data, current_user, current_role, is_new=is_new)
            if result.code == "already_exists":
                return ui.notify("自动生成编号冲突，请重新保存", type="warning", position="bottom")
            if result.code == "sequence_exhausted":
                return ui.notify("今日样品问题编号已达到 999，请联系管理员处理", type="warning", position="bottom")
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
            saved_issue_id = result.record.get("issue_id", "") if result.record else ""
            ui.notify(
                f"样品问题已保存：{saved_issue_id}" if saved_issue_id else "样品问题已保存",
                type="positive",
                position="bottom",
            )
            root_dialog.close()
            refresh_list()

        def open_delete_confirmation():
            """打开删除确认框；真正删除时仍会再次校验 admin 角色。"""
            if is_new or not can_delete_record:
                return ui.notify("当前角色无删除样品问题权限", type="warning", position="bottom")

            target_issue_id = local_data["issue_id"]

            async def confirm_delete():
                result = await delete_sample_issue_record(target_issue_id, current_role)
                if result.code == "forbidden":
                    return ui.notify("当前角色无删除样品问题权限", type="warning", position="bottom")
                if result.code == "not_found":
                    ui.notify("该样品问题已被删除", type="warning", position="bottom")
                    dialog.close()
                    root_dialog.close()
                    refresh_list()
                    return
                if not result.changed:
                    return ui.notify("样品问题删除失败，请刷新后重试", type="negative", position="bottom")

                ui.notify(f"样品问题 {target_issue_id} 已删除", type="positive", position="bottom")
                dialog.close()
                root_dialog.close()
                refresh_list()

            dialog.clear()
            with dialog, ui.card().classes("w-1/3 max-w-md p-5"):
                ui.label("确认删除样品问题").classes("text-lg font-bold text-red-700")
                ui.label(f"样品问题编号：{target_issue_id}").classes("font-mono font-bold text-gray-800")
                ui.label("删除后将无法从页面恢复，请确认该样品问题确实需要删除。").classes("text-sm text-gray-600")
                with ui.row().classes("w-full justify-end gap-3 mt-3"):
                    ui.button("取消", on_click=dialog.close).props("outline color=grey")
                    ui.button("确认删除", icon="delete_forever", on_click=confirm_delete).props("color=negative")
            dialog.open()

        async def open_extension_request_dialog():
            """由对策责任人发起延期申请。"""
            countermeasure = local_data["countermeasure"]
            if not can_operate_countermeasure:
                return ui.notify("仅对策责任人可申请延期", type="warning", position="bottom")
            if is_sample_issue_closed(local_data):
                return ui.notify("该样品问题已关闭，不能再申请延期", type="warning", position="bottom")
            pending_request = get_pending_extension_request(countermeasure)
            if pending_request:
                return ui.notify("该样品问题已有延期申请待审批", type="warning", position="bottom")
            if get_pending_close_request(countermeasure):
                return ui.notify("该样品问题已有关闭申请待审批，不能再申请延期", type="warning", position="bottom")
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
                    if is_sample_issue_closed(current):
                        return "already_closed", current
                    if not is_current_responsible(stored_countermeasure.get("owner", ""), current_user, current_role):
                        return "permission_changed", current
                    if get_pending_extension_request(stored_countermeasure):
                        return "pending_exists", current
                    if get_pending_close_request(stored_countermeasure):
                        return "pending_close", current
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
                if result.code == "pending_close":
                    return ui.notify("该样品问题已有关闭申请待审批，请先完成审批", type="warning", position="bottom")
                if result.code == "missing_due_date":
                    return ui.notify("预计完成日期为空，请刷新后重新申请", type="warning", position="bottom")
                if result.code == "due_date_changed":
                    return ui.notify("预计完成日期已被更新，请刷新后重新申请", type="warning", position="bottom")
                if result.code == "permission_changed":
                    return ui.notify("对策责任人已变更，当前用户不能再申请延期", type="warning", position="bottom")
                if result.code == "already_closed":
                    return ui.notify("该样品问题已关闭，不能再申请延期", type="warning", position="bottom")
                if result.code == "not_found":
                    return ui.notify("该样品问题已不存在，请刷新查看", type="warning", position="bottom")
                if not result.changed or not result.record:
                    return ui.notify("延期申请提交失败，请刷新后重试", type="negative", position="bottom")

                fresh_countermeasure = result.record.get("countermeasure", {})
                fresh_request = find_extension_request(fresh_countermeasure, extension_request["id"])
                if not fresh_request:
                    return ui.notify(
                        "延期申请已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom"
                    )
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

        async def submit_close_request_from_dialog():
            """提交样品问题关闭申请。"""
            if not can_operate_countermeasure:
                return ui.notify("仅对策责任人可申请关闭样品问题", type="warning", position="bottom")
            if get_pending_extension_request(local_data["countermeasure"]):
                return ui.notify("该样品问题存在待审批延期申请，请先完成审批", type="warning", position="bottom")
            if get_pending_close_request(local_data["countermeasure"]):
                return ui.notify("该样品问题已有关闭申请待审批", type="warning", position="bottom")
            if not is_countermeasure_complete(local_data):
                return ui.notify(
                    "请先保存完整的原因分析、临时对策、纠正预防措施和预计完成日期", type="warning", position="bottom"
                )

            result = await submit_sample_close_request(
                local_data["issue_id"],
                current_user,
                current_role,
            )
            if result.code == "incomplete_countermeasure":
                return ui.notify("数据库中的对策信息还不完整，请先保存后再申请关闭", type="warning", position="bottom")
            if result.code == "pending_extension":
                return ui.notify("该样品问题存在待审批延期申请，请先完成审批", type="warning", position="bottom")
            if result.code == "pending_close":
                return ui.notify("该样品问题已有关闭申请待审批，请刷新查看", type="warning", position="bottom")
            if result.code == "permission_changed":
                return ui.notify("对策责任人已变更，当前用户不能再申请关闭", type="warning", position="bottom")
            if result.code in {"already_closed", "not_found"}:
                return ui.notify("该样品问题已关闭或不存在，请刷新查看", type="warning", position="bottom")
            if not result.changed or not result.record:
                return ui.notify("关闭申请提交失败，请刷新后重试", type="negative", position="bottom")

            fresh_countermeasure = result.record.get("countermeasure", {})
            fresh_request = get_pending_close_request(fresh_countermeasure)
            if not fresh_request:
                return ui.notify("关闭申请已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom")
            basic = result.record.get("basic_info", {})
            content = (
                "样品问题关闭申请\n"
                f"样品问题：{result.record['issue_id']}\n"
                f"产品型号：{basic.get('product_model', '')}\n"
                f"样品单号：{basic.get('sample_order_no', '')}\n"
                f"问题点：{basic.get('issue_description', '')}\n"
                f"申请人：{current_user}\n"
                f"审批角色：{', '.join(SAMPLE_EXTENSION_APPROVER_ROLES)}"
            )
            await send_sample_extension_wecom_message(
                content,
                issue_id=result.record["issue_id"],
                business_key=f"{result.record['issue_id']}:{fresh_request['id']}:close_request",
                message_type="close_request",
            )
            ui.notify("关闭申请已提交", type="positive", position="bottom")
            refresh_list()
            await open_sample_issue_detail_dialog(local_data["issue_id"])

        async def approve_close_request_from_dialog(request: dict, approved: bool):
            """审批样品问题关闭申请。"""
            if not is_sample_extension_approver(current_role):
                return ui.notify("当前角色无关闭审批权限", type="warning", position="bottom")

            request_id = str(request.get("id", ""))
            result = await approve_sample_close_request(
                local_data["issue_id"],
                request_id,
                approved,
                current_user,
                current_role,
            )
            if result.code == "forbidden":
                return ui.notify("当前角色无关闭审批权限", type="warning", position="bottom")
            if result.code == "already_processed":
                return ui.notify("该关闭申请已被其他审批人处理，请刷新查看", type="warning", position="bottom")
            if result.code in {"request_not_found", "already_closed", "not_found"}:
                return ui.notify("该关闭申请已发生变化，请刷新查看", type="warning", position="bottom")
            if not result.changed or not result.record:
                return ui.notify("关闭审批失败，请刷新后重试", type="negative", position="bottom")

            fresh_countermeasure = result.record.get("countermeasure", {})
            fresh_request = find_close_request(fresh_countermeasure, request_id)
            if not fresh_request:
                return ui.notify("关闭审批已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom")
            basic = result.record.get("basic_info", {})
            content = (
                "样品问题关闭申请审批结果\n"
                f"样品问题：{result.record['issue_id']}\n"
                f"产品型号：{basic.get('product_model', '')}\n"
                f"样品单号：{basic.get('sample_order_no', '')}\n"
                f"审批结果：{'通过' if approved else '驳回'}\n"
                f"申请人：{fresh_request.get('requester', '-')}\n"
                f"审批人：{current_user}"
            )
            schedule_background_task(
                send_sample_extension_wecom_message(
                    content,
                    issue_id=result.record["issue_id"],
                    business_key=f"{result.record['issue_id']}:{fresh_request['id']}:close_approval",
                    message_type="close_approval",
                    additional_people=(
                        fresh_request.get("requester", "") if SAMPLE_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL else ""
                    ),
                    additional_targets=SAMPLE_EXTENSION_APPROVAL_NOTIFY_TARGETS if approved else None,
                ),
                "样品问题关闭审批企业微信通知",
            )
            ui.notify("关闭审批已处理", type="positive", position="bottom")
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
                    if can_operate_countermeasure:
                        ui.button("申请延期", icon="event", on_click=open_extension_request_dialog).props(
                            "outline color=orange"
                        )
                    elif not is_new:
                        ui.label("仅对策责任人可申请延期。").classes("text-xs text-gray-500")

        def render_close_controls():
            countermeasure = local_data["countermeasure"]
            pending_close = get_pending_close_request(countermeasure)
            approved_close_count, close_request_count = get_close_counts(countermeasure)
            recent_close = next(
                (
                    req
                    for req in reversed(countermeasure.get("close_requests", []))
                    if isinstance(req, dict) and req.get("status") != "待审批"
                ),
                None,
            )
            if not (can_operate_countermeasure or pending_close or is_sample_issue_closed(local_data) or recent_close):
                return

            with ui.element("div").classes("w-full border border-gray-200 rounded-md bg-gray-50 p-4"):
                with ui.row().classes("w-full justify-between items-center mb-3"):
                    with ui.row().classes("items-center gap-2"):
                        ui.label("关闭申请").classes("font-bold text-sm text-gray-700")
                        if is_sample_issue_closed(local_data):
                            ui.badge("已关闭", color="green")
                        elif pending_close:
                            ui.badge("待审批", color="purple").props("outline")
                        elif approved_close_count:
                            ui.badge(f"已通过 {approved_close_count} 次", color="green").props("outline")
                    ui.label(f"累计申请 {close_request_count} 次").classes("text-xs text-gray-500")

                if is_sample_issue_closed(local_data):
                    with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                        bind_input("关闭审批人", countermeasure, "closed_by", "w-full md:w-1/3", readonly=True)
                        bind_input("关闭时间", countermeasure, "closed_at", "w-full md:w-1/3", readonly=True)
                    if str(countermeasure.get("close_note", "")).strip():
                        bind_textarea("关闭说明", countermeasure, "close_note", readonly=True)
                    return

                if pending_close:
                    ui.label(f"待审批关闭申请：{pending_close.get('requested_at', '-')}").classes(
                        "text-sm font-bold text-purple-800"
                    )
                    ui.label(f"申请人：{pending_close.get('requester', '-')}").classes("text-sm text-purple-700")
                    if is_sample_extension_approver(current_role):

                        async def reject_close(event=None, r=pending_close):
                            await approve_close_request_from_dialog(r, False)

                        async def approve_close(event=None, r=pending_close):
                            await approve_close_request_from_dialog(r, True)

                        with ui.row().classes("justify-end gap-2 mt-2"):
                            ui.button("驳回关闭", icon="close", on_click=reject_close).props(
                                "outline color=negative dense"
                            )
                            ui.button("通过关闭", icon="check", on_click=approve_close).props("color=green dense")
                    return

                if recent_close:
                    ui.label(
                        f"最近关闭审批：{recent_close.get('status')}，申请人：{recent_close.get('requester', '-')}"
                    ).classes("text-xs text-gray-500 mb-2")

                if can_operate_countermeasure:

                    async def apply_close(event=None):
                        await submit_close_request_from_dialog()

                    ui.button("申请关闭该问题", icon="check_circle", on_click=apply_close).props("color=green")

        initialize_attachment_state()
        root_dialog.clear()
        with root_dialog, ui.card().classes("w-full h-[100vh] flex flex-col p-0 overflow-hidden bg-gray-100"):
            with ui.row().classes("w-full bg-white px-4 py-3 border-b border-gray-300 justify-between items-center"):
                with ui.row().classes("items-center gap-3 min-w-0"):
                    status = calculate_sample_issue_status(local_data)
                    detail_status_color = {
                        SAMPLE_FILTER_CLOSED_STATE: "green",
                        SAMPLE_FILTER_PENDING_CLOSE_STATE: "purple",
                        SAMPLE_STATUS_CORRECTIVE_ACTION_DONE: "green",
                        SAMPLE_STATUS_TEMPORARY_ACTION_DONE: "orange",
                    }.get(status, "grey")
                    ui.badge(status, color=detail_status_color)
                    ui.label(local_data["issue_id"] or "新样品问题").classes(
                        "font-mono font-bold text-lg text-gray-800"
                    )
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
                            bind_input(
                                "产品型号", basic, "product_model", "w-full md:w-[32%]", readonly=not can_edit_base
                            )
                            bind_input(
                                "样品单号", basic, "sample_order_no", "w-full md:w-[32%]", readonly=not can_edit_base
                            )
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
                            ui.label(
                                f"最近更新：{local_data.get('updated_by', '')} / {local_data.get('updated_at', '')}"
                            )

                    with section("对策责任人填写信息"):
                        bind_textarea(
                            "原因分析", countermeasure, "reason_analysis", readonly=not can_edit_countermeasure
                        )
                        bind_textarea(
                            "样品临时对策", countermeasure, "temporary_action", readonly=not can_edit_countermeasure
                        )
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
                        render_countermeasure_attachments()
                        if is_new:
                            ui.label("保存后，对策责任人可填写该区块、申请延期或申请关闭。").classes(
                                "text-xs text-gray-500"
                            )
                        else:
                            render_extension_controls()
                            render_close_controls()

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
                if can_delete_record and not is_new:
                    ui.button("删除样品问题", icon="delete_forever", on_click=open_delete_confirmation).props(
                        "outline color=negative"
                    )
                ui.button("关闭窗口", on_click=root_dialog.close).props("outline color=grey")
                if can_save_record:
                    ui.button("保存样品问题", icon="save", on_click=save_current_record).props("color=primary")

        root_dialog.open()

    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
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
        if status in {SAMPLE_FILTER_CLOSED_STATE, SAMPLE_STATUS_CORRECTIVE_ACTION_DONE}:
            return "green"
        if status == SAMPLE_FILTER_PENDING_CLOSE_STATE:
            return "purple"
        if status == SAMPLE_STATUS_TEMPORARY_ACTION_DONE:
            return "orange"
        return "grey"

    def status_border_color(status: str) -> str:
        return {
            SAMPLE_FILTER_CLOSED_STATE: "#22c55e",
            SAMPLE_FILTER_PENDING_CLOSE_STATE: "#a855f7",
            SAMPLE_STATUS_CORRECTIVE_ACTION_DONE: "#22c55e",
            SAMPLE_STATUS_TEMPORARY_ACTION_DONE: "#f97316",
            SAMPLE_STATUS_ISSUE_RECORDED: "#64748b",
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
                            pending_close = get_pending_close_request(countermeasure)
                            is_my_pending = (
                                is_current_responsible(countermeasure.get("owner", ""), current_user, current_role)
                                and not is_sample_issue_closed(issue_data)
                                and not pending_close
                            )
                            is_approval_pending = is_sample_extension_approver(current_role) and (
                                bool(pending_extension) or bool(pending_close)
                            )

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
                                            if pending_close and status != SAMPLE_FILTER_PENDING_CLOSE_STATE:
                                                ui.badge("关闭申请中", color="purple").props("outline")
                                            if is_my_pending or is_approval_pending:
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
