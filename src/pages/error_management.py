# -*- encoding: utf-8 -*-
"""生产异常单的录入、跟进、延期审批、关闭和企业微信提醒页面。

数据存储结构概览：
- ``error_management_data`` 是数据库中的顶层字典，键为用户填写的异常单号，值为一张完整异常单。
- 一张异常单包含基础信息，以及说明、分析、应急对策、纠正预防措施等列表。
- 纠正预防措施拥有独立 id，延期申请又拥有自己的 id，便于并发更新时准确定位。
- ``_revision`` 是整张异常单的版本号，用于阻止较早打开的表单覆盖其他用户或后台任务的新修改。

权限分为两层：配置中的编辑角色可以维护整张异常单，其中只有 admin 可以修改异常单号或删除
整张异常单；普通用户仅能对自己负责的纠正预防措施申请延期或提交关闭说明。延期、审批、关闭
等局部业务动作会在原子更新回调里重新检查权限和最新状态，不能只依赖页面按钮是否可见。
"""

import copy
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

from nicegui import app, ui

from .. import db_storage
from ..config import (
    IMG_DIR,
    PRESET_AVATARS,
    UPLOAD_URL_DIR,
    UPLOADS_DIR,
)
from ..error_management_config import (
    ERROR_DEFAULT_NOTIFY_TARGETS,
    ERROR_EDITOR_ROLES,
    ERROR_EXTENSION_APPROVAL_NOTIFY_TARGETS,
    ERROR_EXTENSION_APPROVER_ROLES,
    ERROR_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL,
    ERROR_EXTENSION_NOTIFY_TARGETS,
    ERROR_FILTER_ALL_STATE,
    ERROR_FILTER_CLOSED_STATE,
    ERROR_FILTER_OPEN_STATE,
    ERROR_FILTER_PENDING_CLOSE_STATE,
    ERROR_FILTER_PENDING_EXTENSION_STATE,
    ERROR_FILTER_STATES,
    ERROR_PRODUCT_STATES,
    ERROR_PUBLIC_BASE_URL,
    ERROR_REMINDER_RULES,
)
from ..issue_workflow_utils import (
    is_current_responsible,
    merge_wecom_recipients,
    parse_date,
    schedule_background_task,
    split_people,
    unique_nonempty_texts,
)
from ..utils import apply_chinese_date_locale, get_cache_busted_path, logout, setup_global_activity_tracking
from ..wecom_service import (
    find_unknown_wecom_names,
    resolve_wecom_recipients,
    retry_failed_wecom_messages,
    send_wecom_text_message,
)

logger = logging.getLogger(__name__)

# 数据键保存全部异常单；版本时间戳只用于通知已打开页面刷新列表，不承担并发控制。
ERROR_DATA_KEY = "error_management_data"
ERROR_VERSION_KEY = "error_management_version_stamp"
ERROR_CLOSURE_NATURE_CATALOG_KEY = "error_closure_nature_catalog"
ERROR_GRID_PAGE_SIZE = 30


@dataclass
class ErrorUpdateResult:
    """统一描述一次异常单原子更新的结果，供页面把不同冲突转换成明确提示。"""

    db_success: bool
    changed: bool
    code: str
    record: Optional[dict] = None


async def resolve_error_notify_recipients(targets) -> str:
    """按企业微信通讯录规则解析异常模块收件人，不再回落到固定个人账号。"""
    touser = await resolve_wecom_recipients(targets, fallback_touser="")
    if not touser:
        logger.error("生产异常通知规则未匹配到任何企业微信成员：%s", targets)
    return touser


async def send_error_extension_wecom_message(
    content: str,
    *,
    error_id: str,
    business_key: str,
    message_type: str,
    additional_people: str = "",
    additional_targets=None,
) -> tuple[bool, str]:
    """通知配置指定角色，并可额外合并申请人等动态人员。"""
    role_recipients = await resolve_error_notify_recipients(ERROR_EXTENSION_NOTIFY_TARGETS)
    additional_role_recipients = await resolve_error_notify_recipients(additional_targets) if additional_targets else ""
    people_recipients = await format_people_for_wecom(additional_people) if additional_people else ""
    touser = merge_wecom_recipients(role_recipients, additional_role_recipients, people_recipients)
    if not touser:
        return False, "生产异常延期通知规则未匹配到企业微信成员"
    return await send_wecom_text_message(
        content,
        touser,
        module="error_management",
        business_key=business_key,
        message_type=message_type,
        link_url=get_error_management_url(error_id),
    )


def get_error_template() -> dict:
    """返回一张完整的空异常单。

    新建记录和读取历史记录都会经过此模板。这样后续新增字段时，旧数据也能在页面中获得安全默认值，
    不需要立即批量修改数据库中的所有历史异常单。
    """
    return {
        "error_id": "",
        "_revision": 0,
        "status": "异常录入",
        "basic_info": {
            "product_name": "",
            "material_no": "",
            "order_no": "",
            "production_qty": "",
            "publish_date": datetime.now().strftime("%Y-%m-%d"),
            "product_state": ERROR_PRODUCT_STATES[0],
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
    """用模板补齐旧记录，同时深拷贝数据，避免页面编辑直接污染数据库内存缓存。"""
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
    for action in merged.get("preventive_actions", []):
        if not isinstance(action, dict):
            continue
        action.setdefault("evidence_files", [])
        action.setdefault("close_note", "")
        action.setdefault("close_requests", [])
        action.setdefault("closure_nature", "")
        action.setdefault("extension_requests", [])
        if not isinstance(action.get("close_requests"), list):
            action["close_requests"] = []
        if not isinstance(action.get("extension_requests"), list):
            action["extension_requests"] = []
    return merged


def generate_initial_error_data(current_user: str, current_role: str) -> dict:
    """创建新异常单草稿，并写入第一条操作留痕。异常单号仍由用户在表单中填写。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data = get_error_template()
    data["created_by"] = current_user
    data["created_role"] = current_role
    data["created_at"] = now_str
    data["updated_by"] = current_user
    data["updated_at"] = now_str
    data["operation_log"].append({"user": current_user, "role": current_role, "action": "创建异常单", "time": now_str})
    return data


def calculate_error_status(error_data: dict) -> str:
    """根据业务节点自动推导整单状态，状态不由用户直接选择。

    推导顺序从最靠后的流程节点开始；存在纠正预防措施时，即使前面也有应急对策，
    整单状态仍应显示为“纠正预防执行中”。
    """
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
    """判断当前角色是否包含任一配置的整单编辑角色关键字。"""
    return any(role_key in str(role) for role_key in ERROR_EDITOR_ROLES)


def is_error_extension_approver(role: str) -> bool:
    """判断当前角色是否可以审批延期申请。"""
    return any(role_key in str(role) for role_key in ERROR_EXTENSION_APPROVER_ROLES)


def is_error_rd_manager(role: str) -> bool:
    """判断是否为研发经理角色，用于专属待办和人工提醒检查入口。"""
    return "研发经理" in str(role)


def is_error_admin(role: str) -> bool:
    """删除整张异常单属于高风险操作，仅允许角色值严格等于 admin。"""
    return str(role).strip().lower() == "admin"


async def format_people_for_wecom(value: str) -> str:
    """把负责人姓名解析成企业微信账号；解析不到时保留直接输入值作为发送兜底。"""
    people = split_people(value)
    if not people:
        return await resolve_error_notify_recipients(ERROR_DEFAULT_NOTIFY_TARGETS)
    direct_value = "|".join(people)
    return await resolve_wecom_recipients(
        [{"names": people}],
        fallback_touser=direct_value,
    )


def ensure_item_id(item: dict, prefix: str) -> dict:
    """为历史列表项补充稳定 id；局部原子更新必须依靠 id，而不能依赖可能变化的列表序号。"""
    item.setdefault("id", f"{prefix}_{uuid.uuid4().hex[:8]}")
    return item


def get_item_id(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    item_id = item["id"] if "id" in item else ""
    return str(item_id) if item_id is not None else ""


def get_pending_extension_request(action: dict) -> Optional[dict]:
    """返回最近一条待审批申请；业务规则保证同一措施最多有一条待审批申请。"""
    for request in reversed(action.get("extension_requests", [])):
        if request.get("status") == "待审批":
            return request
    return None


def get_extension_counts(action: dict) -> tuple[int, int]:
    """返回（已通过次数, 总申请次数），驳回申请只计入总申请次数。"""
    requests = action.get("extension_requests", [])
    approved_count = sum(1 for request in requests if request.get("status") == "已通过")
    return approved_count, len(requests)


def get_pending_close_request(action: dict) -> Optional[dict]:
    """返回指定纠正预防措施当前待审批关闭申请。"""
    requests = action.get("close_requests", [])
    if isinstance(requests, list):
        for request in reversed(requests):
            if isinstance(request, dict) and request.get("status") == "待审批":
                return request
    return None


def find_close_request(action: dict, request_id: str) -> Optional[dict]:
    """在指定纠正预防措施中按 id 查找关闭申请。"""
    requests = action.get("close_requests", [])
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


def get_close_counts(action: dict) -> tuple[int, int]:
    """返回（已通过关闭次数, 总关闭申请次数）。"""
    requests = action.get("close_requests", [])
    if not isinstance(requests, list):
        return 0, 0
    approved_count = sum(1 for request in requests if isinstance(request, dict) and request.get("status") == "已通过")
    return approved_count, len(requests)


def normalize_closure_nature(value: Any) -> str:
    """统一措施性质中的首尾空白和连续空格，减少同义重复项。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def get_error_closure_nature_catalog_options(catalog: Any) -> list[str]:
    """按使用次数优先返回独立措施性质词库中的可选项。"""
    ranked_options = []
    if isinstance(catalog, dict):
        for catalog_key, raw_entry in catalog.items():
            if isinstance(raw_entry, dict):
                name = normalize_closure_nature(raw_entry.get("name") or catalog_key)
                try:
                    use_count = max(0, int(raw_entry.get("use_count", 0)))
                except (TypeError, ValueError):
                    use_count = 0
            else:
                name = normalize_closure_nature(raw_entry or catalog_key)
                use_count = 0
            if name:
                ranked_options.append((name, use_count))
    elif isinstance(catalog, list):
        ranked_options.extend((normalize_closure_nature(value), 0) for value in catalog)

    ranked_options = [(name, count) for name, count in ranked_options if name]
    ranked_options.sort(key=lambda item: (-item[1], item[0]))
    return unique_nonempty_texts(name for name, _ in ranked_options)


def get_error_closure_nature_options(all_errors: Any, catalog: Any = None) -> list[str]:
    """优先返回独立词库选项，并用历史关闭审批中的性质补齐旧数据。"""
    if not isinstance(all_errors, dict):
        all_errors = {}
    values = []
    for raw_error in all_errors.values():
        error_data = merge_with_error_template(raw_error) if isinstance(raw_error, dict) else {}
        for action in error_data.get("preventive_actions", []):
            if not isinstance(action, dict):
                continue
            values.append(action.get("closure_nature", ""))
            for request in action.get("close_requests", []):
                if isinstance(request, dict) and request.get("status") == "已通过":
                    values.append(request.get("closure_nature", ""))

    options = []
    seen = set()
    for value in [*get_error_closure_nature_catalog_options(catalog), *unique_nonempty_texts(values)]:
        name = normalize_closure_nature(value)
        normalized_key = name.casefold()
        if name and normalized_key not in seen:
            options.append(name)
            seen.add(normalized_key)
    return options


async def record_error_closure_nature(
    closure_nature: str,
    user: str,
    role: str,
    used_at: str = "",
) -> bool:
    """在独立词库中原子记录措施性质及其累计使用次数。"""
    nature = normalize_closure_nature(closure_nature)
    if not nature:
        return False
    timestamp = used_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    catalog_key = nature.casefold()

    def update_catalog(current):
        catalog = copy.deepcopy(current) if isinstance(current, dict) else {}
        raw_entry = catalog.get(catalog_key, {})
        entry = copy.deepcopy(raw_entry) if isinstance(raw_entry, dict) else {}
        try:
            use_count = max(0, int(entry.get("use_count", 0)))
        except (TypeError, ValueError):
            use_count = 0
        entry.update(
            {
                "name": normalize_closure_nature(entry.get("name")) or nature,
                "use_count": use_count + 1,
                "last_used_at": timestamp,
                "last_used_by": user,
                "last_used_role": role,
            }
        )
        entry.setdefault("created_at", timestamp)
        entry.setdefault("created_by", user)
        catalog[catalog_key] = entry
        return catalog

    return await db_storage.atomic_deep_update([ERROR_CLOSURE_NATURE_CATALOG_KEY], update_catalog)


def get_owner_extension_summary(error_data: dict) -> list[tuple[str, int]]:
    """汇总每位负责人已获批的延期次数，用于总览卡片展示。"""
    owner_counts = {}
    for action in error_data.get("preventive_actions", []):
        approved_count, _ = get_extension_counts(action)
        for owner in split_people(action.get("owner", "")):
            owner_counts[owner] = owner_counts.get(owner, 0) + approved_count
    return sorted(owner_counts.items(), key=lambda item: (-item[1], item[0]))


def is_error_pending_for_user(error_data: dict, current_user: str, current_role: str) -> bool:
    """按主页角标口径判断一张异常单是否需要当前用户处理。"""
    actions = error_data.get("preventive_actions", [])
    if is_error_extension_approver(current_role):
        return any(
            isinstance(action, dict)
            and action.get("status") != "已关闭"
            and (get_pending_extension_request(action) or get_pending_close_request(action))
            for action in actions
        )

    return any(
        isinstance(action, dict)
        and action.get("status") != "已关闭"
        and is_current_responsible(action.get("owner", ""), current_user, current_role)
        for action in actions
    )


def has_error_overdue_without_request_for_reviewer(
    error_data: dict,
    current_role: str,
    today: Optional[date] = None,
) -> bool:
    """判断评审角色是否需要关注任一已逾期且尚无延期或关闭申请的措施。"""
    if not is_error_extension_approver(current_role):
        return False

    reference_date = today or datetime.now().date()
    for action in error_data.get("preventive_actions", []):
        if not isinstance(action, dict) or action.get("status") == "已关闭":
            continue
        raw_due_date = action.get("due_date", "")
        due_date = parse_date(raw_due_date if isinstance(raw_due_date, str) else "")
        if (
            due_date
            and due_date < reference_date
            and not get_pending_extension_request(action)
            and not get_pending_close_request(action)
        ):
            return True
    return False


def get_error_card_sort_key(
    error_data: dict,
    current_user: str,
    current_role: str,
    today: Optional[date] = None,
) -> tuple[int, str]:
    """返回卡片排序键：待我处理最高，评审关注的逾期未申请事项其次。"""
    updated_at = error_data.get("updated_at") or error_data.get("created_at") or ""
    if is_error_pending_for_user(error_data, current_user, current_role):
        priority = 2
    elif has_error_overdue_without_request_for_reviewer(error_data, current_role, today):
        priority = 1
    else:
        priority = 0
    return priority, str(updated_at)


def get_error_dashboard_pending_count(all_errors: Any, current_user: str, current_role: str) -> int:
    """计算总页面“异常单跟进”卡片对当前用户显示的待办角标数量。

    - 审批角色：统计所有纠正预防措施中的待审批延期和关闭申请条数。
    - 其它角色：统计至少有一条未关闭措施由当前用户或当前角色负责的异常单数量；同一异常单
      即使有多条措施由该用户负责，也只计为一个待处理异常。
    """
    if not isinstance(all_errors, dict):
        return 0

    if is_error_extension_approver(current_role):
        return sum(
            1
            for error_data in all_errors.values()
            if isinstance(error_data, dict)
            for action in error_data.get("preventive_actions", [])
            if isinstance(action, dict) and action.get("status") != "已关闭"
            for request in [
                *(action.get("extension_requests", []) if isinstance(action.get("extension_requests"), list) else []),
                *(action.get("close_requests", []) if isinstance(action.get("close_requests"), list) else []),
            ]
            if isinstance(request, dict) and request.get("status") == "待审批"
        )

    return sum(
        1
        for error_data in all_errors.values()
        if isinstance(error_data, dict) and is_error_pending_for_user(error_data, current_user, current_role)
    )


def error_matches_filter(error_data: dict, filter_state: str) -> bool:
    """判断异常单是否符合总览页筛选条件。

    “延期申请中”和“关闭申请中”是跨主状态的特殊筛选，只要存在对应的待审批申请就命中。
    其它筛选项仍与自动推导的异常单主状态进行匹配。
    """
    if filter_state == ERROR_FILTER_ALL_STATE:
        return True
    if filter_state == ERROR_FILTER_OPEN_STATE:
        return calculate_error_status(error_data) != ERROR_FILTER_CLOSED_STATE
    if filter_state == ERROR_FILTER_PENDING_EXTENSION_STATE:
        return has_pending_error_extension(error_data)
    if filter_state == ERROR_FILTER_PENDING_CLOSE_STATE:
        return has_pending_error_close(error_data)
    return calculate_error_status(error_data) == filter_state


def has_pending_error_extension(error_data: dict) -> bool:
    """判断异常单是否存在任一待审批延期申请。"""
    return any(
        isinstance(action, dict) and get_pending_extension_request(action)
        for action in error_data.get("preventive_actions", [])
    )


def has_pending_error_close(error_data: dict) -> bool:
    """判断异常单是否存在任一待审批关闭申请。"""
    return any(
        isinstance(action, dict) and get_pending_close_request(action)
        for action in error_data.get("preventive_actions", [])
    )


def get_error_card_status(error_data: dict) -> str:
    """返回总览卡片优先展示的状态标签。"""
    if has_pending_error_close(error_data):
        return ERROR_FILTER_PENDING_CLOSE_STATE
    if has_pending_error_extension(error_data):
        return ERROR_FILTER_PENDING_EXTENSION_STATE
    return calculate_error_status(error_data)


def get_error_management_url(error_id: str = "") -> str:
    """生成企业微信消息中的直达链接；带 error_id 时登录后会自动打开对应详情。"""
    page_url = f"{ERROR_PUBLIC_BASE_URL}/error_management"
    return f"{page_url}?error_id={quote(error_id, safe='')}" if error_id else page_url


def get_next_due_text(error_data: dict) -> str:
    """总览卡片显示所有纠正预防措施中最晚的预计完成日期。"""
    due_dates = []
    for item in error_data.get("preventive_actions", []):
        due_date = parse_date(item.get("due_date", ""))
        if due_date:
            due_dates.append(due_date)
    if not due_dates:
        return "暂无"
    return max(due_dates).strftime("%Y-%m-%d")


def build_error_grid_row(error_data: dict, current_user: str, current_role: str) -> dict[str, object]:
    """把生产异常记录整理为首页 AG Grid 行数据。"""
    data = merge_with_error_template(error_data)
    basic = data["basic_info"]
    preventive_actions = [item for item in data.get("preventive_actions", []) if isinstance(item, dict)]
    active_owners = [
        str(item.get("owner", "")).strip()
        for item in preventive_actions
        if item.get("status") != "已关闭" and str(item.get("owner", "")).strip()
    ]
    first_description = next(
        (
            str(item.get("content", "")).strip()
            for item in data.get("descriptions", [])
            if isinstance(item, dict) and str(item.get("content", "")).strip()
        ),
        "",
    )
    closed_preventive = sum(1 for item in preventive_actions if item.get("status") == "已关闭")
    card_status = get_error_card_status(data)
    is_my_pending = is_error_pending_for_user(data, current_user, current_role)
    is_reviewer_overdue = has_error_overdue_without_request_for_reviewer(data, current_role)
    attention_labels = []
    if is_my_pending:
        attention_labels.append("待我处理")
    if is_reviewer_overdue:
        attention_labels.append("逾期未申请")
    owner_extension_text = "、".join(f"{owner} {count}次" for owner, count in get_owner_extension_summary(data))
    if is_my_pending:
        row_tone = "pending"
    elif is_reviewer_overdue:
        row_tone = "warning"
    elif card_status == ERROR_FILTER_CLOSED_STATE:
        row_tone = "completed"
    elif card_status == ERROR_FILTER_PENDING_CLOSE_STATE:
        row_tone = "pending_close"
    elif card_status == ERROR_FILTER_PENDING_EXTENSION_STATE:
        row_tone = "warning"
    else:
        row_tone = "normal"
    return {
        "record_id": data["error_id"],
        "detail_action": "详情",
        "error_id": data["error_id"],
        "status": card_status,
        "attention": "、".join(attention_labels),
        "product_name": basic.get("product_name", ""),
        "material_no": basic.get("material_no", ""),
        "order_no": basic.get("order_no", ""),
        "product_state": basic.get("product_state", ""),
        "description": first_description,
        "publish_date": basic.get("publish_date", ""),
        "next_due_date": get_next_due_text(data),
        "preventive_progress": f"{closed_preventive}/{len(preventive_actions)}",
        "owners": "、".join(active_owners),
        "owner_extensions": owner_extension_text,
        "updated_at": data.get("updated_at", ""),
        "row_tone": row_tone,
    }


def get_error_grid_columns() -> list[dict[str, object]]:
    """返回生产异常首页列定义；顺序、显隐和筛选直接在此处配置。"""
    text_filter = "agTextColumnFilter"
    date_filter = "agDateColumnFilter"
    # 列表顺序就是页面列顺序；不显示可注释对应行；不需要筛选可把 filter 改为 False。
    columns: list[dict[str, object]] = [
        {
            "headerName": "操作",
            "field": "detail_action",
            "filter": False,
            "pinned": "left",
            "width": 60,
            "sortable": False,
            "lockPosition": "left",
            "suppressMovable": True,
            "cellStyle": {"color": "#2563eb", "fontWeight": "bold", "cursor": "pointer"},
        },
        {"headerName": "异常单号", "field": "error_id", "filter": text_filter, "width": 120},
        {"headerName": "当前状态", "field": "status", "filter": text_filter, "width": 130},
        {"headerName": "关注事项", "field": "attention", "filter": text_filter, "width": 120},
        {"headerName": "产品型号", "field": "product_name", "filter": text_filter, "width": 160},
        {"headerName": "料号", "field": "material_no", "filter": text_filter, "width": 120},
        {"headerName": "订单号", "field": "order_no", "filter": text_filter, "width": 160},
        {"headerName": "产品状态", "field": "product_state", "filter": text_filter, "width": 120},
        {"headerName": "异常描述", "field": "description", "filter": text_filter, "width": 340},
        {"headerName": "发文日期", "field": "publish_date", "filter": date_filter, "width": 120},
        {"headerName": "预计完成", "field": "next_due_date", "filter": text_filter, "width": 120},
        {"headerName": "纠正预防进度", "field": "preventive_progress", "filter": text_filter, "width": 140},
        {"headerName": "负责人", "field": "owners", "filter": text_filter, "width": 90},
        {"headerName": "负责人延期", "field": "owner_extensions", "filter": text_filter, "width": 150},
    ]
    for column in columns:
        cell_style = column.setdefault("cellStyle", {})
        if isinstance(cell_style, dict):
            cell_style["textAlign"] = "center"
        if "width" in column:
            column["minWidth"] = column["width"]
        column["headerClass"] = "error-grid-header-center"
        column["wrapHeaderText"] = True
        column["autoHeaderHeight"] = True
    return columns


def get_record_revision(error_data: Optional[dict]) -> int:
    """安全读取乐观锁版本号，损坏或缺失的历史值视为第 0 版。"""
    try:
        return max(0, int((error_data or {}).get("_revision", 0)))
    except (TypeError, ValueError):
        return 0


def find_preventive_action(error_data: dict, action_id: str) -> Optional[dict]:
    """在数据库最新记录中按稳定 id 查找纠正预防措施。"""
    actions = error_data.get("preventive_actions", [])
    if not isinstance(actions, list):
        return None
    return next((action for action in actions if isinstance(action, dict) and get_item_id(action) == action_id), None)


def find_extension_request(action: dict, request_id: str) -> Optional[dict]:
    """在指定措施中按稳定 id 查找延期申请。"""
    requests = action.get("extension_requests", [])
    if not isinstance(requests, list):
        return None
    return next(
        (request for request in requests if isinstance(request, dict) and get_item_id(request) == request_id), None
    )


def reminder_rule_matches(due_date, today, rule: dict) -> bool:
    """判断某个预计完成日期是否命中一条 JSON 配置的提醒策略。"""
    days_until_due = (due_date - today).days
    if "days_until_due" in rule:
        return days_until_due == rule["days_until_due"]
    if "max_days_until_due" in rule:
        return days_until_due <= rule["max_days_until_due"]
    return False


async def save_error_record(
    error_data: dict,
    user: str,
    role: str,
    *,
    is_new: bool,
    original_error_id: str = "",
) -> ErrorUpdateResult:
    """保存整张表单。

    编辑已有记录时把页面打开时的 ``_revision`` 作为期望版本传入；如果期间有其他用户或后台提醒
    更新过记录，保存会返回 ``revision_conflict``，避免旧页面覆盖新内容。admin 修改已有异常单号时，
    会改为原子迁移顶层数据库键，确保旧键删除、新键写入和操作日志保存属于同一次事务。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    record = merge_with_error_template(error_data)
    source_error_id = str(original_error_id or record["error_id"]).strip()
    target_error_id = str(record["error_id"]).strip()
    record["error_id"] = target_error_id
    record["updated_by"] = user
    record["updated_at"] = now_str
    record.setdefault("operation_log", []).append({"user": user, "role": role, "action": "保存异常单", "time": now_str})

    if not is_new and source_error_id != target_error_id:
        return await rename_error_record(
            source_error_id,
            record,
            user,
            role,
            expected_revision=get_record_revision(error_data),
            renamed_at=now_str,
        )

    def save_record(_current):
        return "updated", copy.deepcopy(record)

    return await atomic_error_update(
        record["error_id"],
        save_record,
        expected_revision=None if is_new else get_record_revision(error_data),
        create=is_new,
    )


async def rename_error_record(
    original_error_id: str,
    error_data: dict,
    user: str,
    role: str,
    *,
    expected_revision: Optional[int] = None,
    renamed_at: str = "",
) -> ErrorUpdateResult:
    """仅允许 admin 原子迁移异常单数据库键，并同步记录单号变更日志。"""
    if not is_error_admin(role):
        return ErrorUpdateResult(db_success=False, changed=False, code="forbidden")

    source_error_id = str(original_error_id or "").strip()
    record = merge_with_error_template(error_data)
    target_error_id = str(record.get("error_id", "")).strip()
    if not source_error_id or not target_error_id:
        return ErrorUpdateResult(db_success=False, changed=False, code="invalid_error_id")
    if source_error_id == target_error_id:
        return ErrorUpdateResult(db_success=True, changed=False, code="unchanged", record=record)

    now_str = renamed_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    outcome = {"changed": False, "code": "db_error", "record": None}

    def rename_record(all_errors):
        # 单号是顶层字典键，因此必须在同一个顶层事务内同时检查并迁移新旧键。
        if not isinstance(all_errors, dict) or source_error_id not in all_errors:
            outcome["code"] = "not_found"
            return db_storage.ATOMIC_NO_UPDATE
        if target_error_id in all_errors:
            outcome["code"] = "already_exists"
            return db_storage.ATOMIC_NO_UPDATE

        current = merge_with_error_template(all_errors[source_error_id])
        if expected_revision is not None and get_record_revision(current) != expected_revision:
            outcome["code"] = "revision_conflict"
            outcome["record"] = copy.deepcopy(current)
            return db_storage.ATOMIC_NO_UPDATE

        updated = merge_with_error_template(record)
        updated["error_id"] = target_error_id
        updated["updated_by"] = user
        updated["updated_at"] = now_str
        updated.setdefault("operation_log", []).append(
            {
                "user": user,
                "role": role,
                "action": f"修改异常单号：{source_error_id} → {target_error_id}",
                "time": now_str,
            }
        )
        updated["_revision"] = get_record_revision(current) + 1
        updated["status"] = calculate_error_status(updated)
        if updated["status"] == "已关闭" and not updated.get("closed_at"):
            updated["closed_at"] = now_str
        elif updated["status"] != "已关闭":
            updated["closed_at"] = ""

        del all_errors[source_error_id]
        all_errors[target_error_id] = updated
        outcome["changed"] = True
        outcome["code"] = "updated"
        outcome["record"] = copy.deepcopy(updated)
        return all_errors

    success = await db_storage.atomic_deep_update([ERROR_DATA_KEY], rename_record)
    if success and outcome["changed"]:
        await db_storage.set_item(ERROR_VERSION_KEY, time.time())
    return ErrorUpdateResult(
        db_success=success,
        changed=bool(success and outcome["changed"]),
        code=outcome["code"] if success else "db_error",
        record=outcome["record"],
    )


async def atomic_error_update(
    error_id: str,
    update_function,
    *,
    expected_revision: Optional[int] = None,
    create: bool = False,
) -> ErrorUpdateResult:
    """生产异常模块唯一的数据库写入入口。

    ``db_storage.atomic_deep_update`` 会在 SQLite 写事务中读取最新异常单，再执行 ``update_function``。
    回调必须返回 ``("updated", 新记录)`` 才会写入；其它业务状态码会配合 ``ATOMIC_NO_UPDATE``
    放弃写入。此模式保证延期审批、措施关闭、提醒认领等并发动作不会互相覆盖。

    ``expected_revision`` 用于整单表单保存的乐观锁；``create`` 用于保证相同异常单号只能创建一次。
    """
    outcome = {"changed": False, "code": "db_error", "record": None}

    def apply_update(current):
        # 下列存在性、版本和业务条件判断全部位于事务内，判断依据始终是数据库最新值。
        current_exists = isinstance(current, dict) and bool(current.get("error_id"))
        if create:
            if current is not None:
                outcome["code"] = "already_exists"
                return db_storage.ATOMIC_NO_UPDATE
            record = get_error_template()
        else:
            if not current_exists:
                outcome["code"] = "not_found"
                return db_storage.ATOMIC_NO_UPDATE
            record = merge_with_error_template(current)

        if expected_revision is not None and get_record_revision(record) != expected_revision:
            outcome["code"] = "revision_conflict"
            outcome["record"] = copy.deepcopy(record)
            return db_storage.ATOMIC_NO_UPDATE

        code, updated = update_function(record)
        outcome["code"] = code
        if code != "updated":
            outcome["record"] = copy.deepcopy(record)
            return db_storage.ATOMIC_NO_UPDATE

        updated = merge_with_error_template(updated)
        # 每次成功修改都递增版本，并统一重算整单状态，避免各个操作入口各自维护状态造成偏差。
        updated["_revision"] = get_record_revision(record) + 1
        updated["status"] = calculate_error_status(updated)
        if updated["status"] == "已关闭" and not updated.get("closed_at"):
            updated["closed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elif updated["status"] != "已关闭":
            updated["closed_at"] = ""
        outcome["changed"] = True
        outcome["record"] = copy.deepcopy(updated)
        return updated

    success = await db_storage.atomic_deep_update([ERROR_DATA_KEY, error_id], apply_update)
    if success and outcome["changed"]:
        # 列表页每 5 秒观察此时间戳；变化时只重绘列表，不用于判断数据是否可写。
        await db_storage.set_item(ERROR_VERSION_KEY, time.time())
    return ErrorUpdateResult(
        db_success=success,
        changed=bool(success and outcome["changed"]),
        code=outcome["code"] if success else "db_error",
        record=outcome["record"],
    )


async def submit_error_preventive_close_request(
    error_id: str,
    action_id: str,
    user: str,
    role: str,
    close_note: str,
) -> ErrorUpdateResult:
    """由纠正预防措施负责人提交关闭申请，等待审批角色确认。"""
    note = close_note.strip()
    if not note:
        return ErrorUpdateResult(db_success=False, changed=False, code="missing_close_note")

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
        stored_action = find_preventive_action(current, action_id)
        if not stored_action:
            return "action_not_found", current
        if stored_action.get("status") == "已关闭":
            return "already_closed", current
        if not is_current_responsible(stored_action.get("owner", ""), user, role) and not is_error_editor(role):
            return "permission_changed", current
        if get_pending_extension_request(stored_action):
            return "pending_extension", current
        if get_pending_close_request(stored_action):
            return "pending_close", current

        stored_action["close_note"] = note
        stored_action.setdefault("close_requests", []).append(copy.deepcopy(close_request))
        current["updated_by"] = user
        current["updated_at"] = now_str
        current.setdefault("operation_log", []).append(
            {"user": user, "role": role, "action": "申请关闭纠正预防措施", "time": now_str}
        )
        return "updated", current

    return await atomic_error_update(error_id, add_close_request)


async def approve_error_preventive_close_request(
    error_id: str,
    action_id: str,
    request_id: str,
    approved: bool,
    user: str,
    role: str,
    closure_nature: str = "",
) -> ErrorUpdateResult:
    """审批纠正预防措施关闭申请；通过时写入措施性质并关闭该措施。"""
    if not is_error_extension_approver(role):
        return ErrorUpdateResult(db_success=False, changed=False, code="forbidden")

    nature = normalize_closure_nature(closure_nature)
    if approved and not nature:
        return ErrorUpdateResult(db_success=False, changed=False, code="missing_closure_nature")

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    action_text = "通过纠正预防措施关闭申请" if approved else "驳回纠正预防措施关闭申请"

    def update_close_request(current):
        stored_action = find_preventive_action(current, action_id)
        if not stored_action:
            return "action_not_found", current
        stored_request = find_close_request(stored_action, request_id)
        if not stored_request:
            return "request_not_found", current
        if stored_request.get("status") != "待审批":
            return "already_processed", current
        if approved and stored_action.get("status") == "已关闭":
            return "already_closed", current

        stored_request["status"] = "已通过" if approved else "已驳回"
        stored_request["approver"] = user
        stored_request["approver_role"] = role
        stored_request["approved_at"] = now_str
        if approved:
            stored_request["closure_nature"] = nature
            stored_action["status"] = "已关闭"
            stored_action["close_note"] = stored_request.get("note", "")
            stored_action["closure_nature"] = nature
            stored_action["closed_by"] = user
            stored_action["closed_role"] = role
            stored_action["closed_at"] = now_str
        current["updated_by"] = user
        current["updated_at"] = now_str
        current.setdefault("operation_log", []).append(
            {"user": user, "role": role, "action": action_text, "time": now_str}
        )
        return "updated", current

    result = await atomic_error_update(error_id, update_close_request)
    if approved and result.changed:
        catalog_saved = await record_error_closure_nature(nature, user, role, now_str)
        if not catalog_saved:
            logger.error("措施性质词库保存失败：%s", nature)
    return result


async def delete_error_record(error_id: str, role: str) -> ErrorUpdateResult:
    """由 admin 原子删除单张异常单。

    删除时更新整个 ``error_management_data`` 顶层字典，而不是调用依赖当前实例缓存的深层删除。
    ``atomic_deep_update`` 会在事务内读取最新字典，因此其它实例同时新增或修改的异常单会被保留。
    """
    if not is_error_admin(role):
        return ErrorUpdateResult(db_success=False, changed=False, code="forbidden")

    outcome = {"changed": False, "code": "db_error", "record": None}

    def remove_record(all_errors):
        if not isinstance(all_errors, dict) or error_id not in all_errors:
            outcome["code"] = "not_found"
            return db_storage.ATOMIC_NO_UPDATE

        outcome["record"] = copy.deepcopy(all_errors[error_id])
        del all_errors[error_id]
        outcome["changed"] = True
        outcome["code"] = "deleted"
        return all_errors

    success = await db_storage.atomic_deep_update([ERROR_DATA_KEY], remove_record)
    if success and outcome["changed"]:
        await db_storage.set_item(ERROR_VERSION_KEY, time.time())
    return ErrorUpdateResult(
        db_success=success,
        changed=bool(success and outcome["changed"]),
        code=outcome["code"] if success else "db_error",
        record=outcome["record"],
    )


async def check_and_send_error_reminders(show_result: bool = False) -> tuple[int, int]:
    """检查所有未关闭措施并发送到期提醒。

    去重标记格式为 ``措施id:规则key:日期``。发送前先通过原子更新把标记写成 ``sending`` 并附带
    唯一 claim_id，只有成功认领的任务才可以发送；因此多人打开页面或多个服务实例同时检查时，
    同一条提醒也只会由一个任务发送。卡在 sending 超过 10 分钟的标记允许重新认领。

    企业微信发送失败会先进入统一发送日志，后续由 wecom_service 重试；这里再把本次结果写回
    异常单 reminder_log，方便审计。
    """
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

            action_id = get_item_id(action)
            due_date = parse_date(action.get("due_date", ""))
            owner = action.get("owner", "")
            if not action_id or not due_date or not owner:
                continue

            for rule in ERROR_REMINDER_RULES:
                if not reminder_rule_matches(due_date, today, rule):
                    continue

                marker = f"{action_id}:{rule['key']}:{today_key}"
                if marker in error_data.get("reminder_log", {}):
                    continue

                # claim_id 用于确认发送结果仍属于当前认领者，防止超时重认领后旧任务覆盖新任务状态。
                claim_id = uuid.uuid4().hex

                def claim_reminder(current, marker=marker, rule=rule, claim_id=claim_id, action_id=action_id):
                    stored_action = find_preventive_action(current, action_id)
                    if not stored_action or stored_action.get("status") == "已关闭":
                        return "not_eligible", current
                    fresh_due_date = parse_date(stored_action.get("due_date", ""))
                    if (
                        not fresh_due_date
                        or not stored_action.get("owner", "")
                        or not reminder_rule_matches(fresh_due_date, today, rule)
                    ):
                        return "not_eligible", current

                    reminder_log = current.setdefault("reminder_log", {})
                    existing_marker = reminder_log.get(marker)
                    can_claim = marker not in reminder_log
                    if existing_marker and existing_marker.get("state") == "sending":
                        try:
                            sending_time = datetime.strptime(existing_marker.get("time", ""), "%Y-%m-%d %H:%M:%S")
                            can_claim = datetime.now() - sending_time > timedelta(minutes=10)
                        except ValueError:
                            can_claim = True
                    if not can_claim:
                        return "already_claimed", current
                    reminder_log[marker] = {
                        "rule": rule["label"],
                        "state": "sending",
                        "claim_id": claim_id,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    return "updated", current

                claim_result = await atomic_error_update(error_data["error_id"], claim_reminder)
                if not claim_result.changed or not claim_result.record:
                    continue

                # 认领成功后使用事务返回的最新记录发送，避免使用循环开始时已经过时的负责人或日期。
                fresh_error = claim_result.record
                fresh_action = find_preventive_action(fresh_error, action_id)
                if not fresh_action:
                    continue
                owner = fresh_action.get("owner", "")
                content = (
                    "生产异常纠正预防措施提醒\n"
                    f"异常单：{fresh_error.get('error_id')}\n"
                    f"产品：{fresh_error.get('basic_info', {}).get('product_name', '')}\n"
                    f"措施：{fresh_action.get('content', '')}\n"
                    f"负责人：{owner}\n"
                    f"预计完成日期：{fresh_action.get('due_date', '')}\n"
                    f"已通过延期：{get_extension_counts(fresh_action)[0]} 次\n"
                    f"提醒策略：{rule['label']}"
                )
                success, message = await send_wecom_text_message(
                    content,
                    await format_people_for_wecom(owner),
                    module="error_management",
                    business_key=f"{fresh_error.get('error_id')}:{get_item_id(fresh_action)}:{rule['key']}",
                    message_type="preventive_reminder",
                    link_url=get_error_management_url(fresh_error.get("error_id", "")),
                )
                if success:
                    sent_count += 1
                else:
                    fail_count += 1

                def add_reminder_log(
                    current,
                    marker=marker,
                    rule=rule,
                    success=success,
                    message=message,
                    claim_id=claim_id,
                ):
                    existing_marker = current.setdefault("reminder_log", {}).get(marker, {})
                    if existing_marker.get("claim_id") != claim_id:
                        return "claim_lost", current
                    current.setdefault("reminder_log", {})[marker] = {
                        "rule": rule["label"],
                        "state": "sent" if success else "failed_retrying",
                        "success": success,
                        "message": message,
                        "claim_id": claim_id,
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    return "updated", current

                await atomic_error_update(error_data["error_id"], add_reminder_log)

    if show_result:
        ui.notify(
            f"提醒检查完成：新发成功 {sent_count} 条，失败进入重试 {fail_count} 条；历史重试成功 {retry_success_count} 条，仍失败 {retry_fail_count} 条",
            type="info",
            position="bottom",
        )
    return sent_count, fail_count


def save_uploaded_evidence_file(error_id: str, action_id: str, original_filename: str, content: bytes) -> dict:
    """保存措施证据附件并返回可写入异常单的数据；关闭说明本身不要求必须上传文件。"""
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
async def error_management_page(error_id: str = ""):
    """构建异常管理页面；error_id 来自企业微信直达链接，可在登录后自动打开对应异常单。"""
    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    ui.add_head_html("""
        <style>
            .q-dialog__inner--minimized>div { max-width: 4000px; }
            html, body { overflow: hidden !important; }
            .pdf-border { border: 1px solid #cbd5e1; }
            .pdf-border-b { border-bottom: 1px solid #cbd5e1; }
            .pdf-border-r { border-right: 1px solid #cbd5e1; }
            
            /*
            ::-webkit-scrollbar { width: 3px; background-color: transparent; }
            ::-webkit-scrollbar-thumb { background-color: #cbd5e1; border-radius: 1px; }
            */
            .error-grid .error-grid-header-center .ag-header-cell-label { justify-content: center; }
            .error-grid .ag-row.row-pending { background-color: #fff1f2 !important; }
            .error-grid .ag-row.row-warning { background-color: #fff7ed !important; }
            .error-grid .ag-row.row-pending-close { background-color: #faf5ff !important; }
            .error-grid .ag-row.row-completed { background-color: #f0fdf4 !important; }
            .error-grid .ag-row:hover { filter: brightness(0.98); }
        </style>
    """)
    if not app.storage.user.get("current_user"):
        redirect_target = f"/error_management?error_id={error_id}" if error_id else "/error_management"
        ui.navigate.to(f"/login?redirect_to={quote(redirect_target, safe='')}")
        return

    current_user = app.storage.user.get("current_user", "未知用户")
    current_role = app.storage.user.get("current_role", "未知角色")
    current_display_path = get_cache_busted_path(
        app.storage.general.get("user_preferences", {}).get(current_user, {}).get("avatar", PRESET_AVATARS[0])
    )

    page_state = {"search_keyword": "", "filter_state": ERROR_FILTER_OPEN_STATE}

    # ui.dialog: NiceGUI框架提供的模态对话框组件
    dialog = ui.dialog().props("persistent")
    root_dialog = ui.dialog().props("maximized persistent")

    # admin 始终拥有整单编辑权；即使后续配置误删 admin，也不能影响管理员修正基础输入项。
    can_edit_all = is_error_admin(current_role) or is_error_editor(current_role)
    can_delete_record = is_error_admin(current_role)
    can_rename_record = is_error_admin(current_role)
    # 此变量只防止同一页面被连续点击触发多个手工检查；跨页面、跨用户的提醒去重由数据库认领机制负责。
    reminder_guard = {"running": False}

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
        """执行整单保存前的最低业务校验；更细的并发和权限校验仍由数据库更新入口负责。"""
        error_data["error_id"] = str(error_data.get("error_id", "")).strip()
        if not error_data["error_id"]:
            ui.notify("请填写异常单号", type="warning", position="bottom")
            return False
        if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", error_data["error_id"]):
            ui.notify("异常单号仅支持中文、英文、数字、短横线和下划线", type="warning", position="bottom")
            return False
        basic = error_data.get("basic_info", {})
        if not basic.get("product_name", "").strip():
            ui.notify("请填写产品型号", type="warning", position="bottom")
            return False
        if not any(item.get("content", "").strip() for item in error_data.get("descriptions", [])):
            ui.notify("请至少填写一条异常情况说明", type="warning", position="bottom")
            return False
        return True

    async def open_error_detail_dialog(error_id=None):
        """读取异常单快照并创建详情窗口。

        窗口中的 local_data 是独立深拷贝：整单编辑可在本地暂存到“保存异常单”时统一提交；
        延期、审批、关闭等动作则立即使用原子更新写库，成功后重新打开详情展示最新数据。
        """
        is_new = error_id is None
        stored_error_id = str(error_id or "")
        if is_new and not can_edit_all:
            return ui.notify("当前角色无异常单录入权限", type="warning", position="bottom")

        all_errors = db_storage.get_item(ERROR_DATA_KEY, {})
        if is_new:
            local_data = generate_initial_error_data(current_user, current_role)
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
            # 兼容字段是在打开历史记录时补齐的，避免早期数据缺少延期或关闭字段导致页面报错。
            ensure_item_id(item, "preventive")
            item.setdefault("status", "待执行")
            item.setdefault("evidence_files", [])
            item.setdefault("close_note", "")
            item.setdefault("close_requests", [])
            item.setdefault("closure_nature", "")
            item.setdefault("extension_requests", [])

        read_only = not can_edit_all
        owner_allowed_values = [*ERROR_EDITOR_ROLES, *ERROR_EXTENSION_APPROVER_ROLES, current_role]

        def bind_input(label, target, key, classes="w-full", readonly=None):
            field_readonly = read_only if readonly is None else readonly
            props = "outlined dense"
            if field_readonly:
                props += " readonly"
            field = ui.input(label, value=target.get(key, "")).props(props).classes(f"{classes} mb-3")
            if not field_readonly:
                field.on_value_change(lambda e, t=target, k=key: t.__setitem__(k, e.value))
            return field

        async def warn_unknown_wecom_names(label: str, value: str, allowed_values=None) -> None:
            unknown_names = await find_unknown_wecom_names(value, allowed_values=allowed_values)
            if unknown_names:
                ui.notify(
                    f"{label} 未在企业微信通讯录中找到：{'、'.join(unknown_names)}，请检查是否有错别字",
                    type="warning",
                    position="bottom",
                    multi_line=True,
                )

        def bind_people_input(label, target, key, classes="w-full", readonly=None, allowed_values=None):
            field_readonly = read_only if readonly is None else readonly
            field = bind_input(label, target, key, classes, readonly=readonly)
            if not field_readonly:

                async def handle_blur(event=None, label_text=label, data=target, data_key=key, allowed=allowed_values):
                    await warn_unknown_wecom_names(label_text, data.get(data_key, ""), allowed_values=allowed)

                field.on("blur", handle_blur)
            return field

        def bind_date(label, target, key, classes="w-full", readonly=None):
            """创建只能通过日历选择的日期输入框，并把选择结果同步到当前表单快照。"""
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
                apply_chinese_date_locale(ui.date(value=target.get(key, ""), mask="YYYY-MM-DD", on_change=set_date))

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

        def open_closure_nature_dialog(title: str, description: str, on_submit):
            """审批通过关闭申请前，由审批人补充便于统计的措施性质。"""
            nature_options = get_error_closure_nature_options(
                db_storage.get_item(ERROR_DATA_KEY, {}),
                db_storage.get_item(ERROR_CLOSURE_NATURE_CATALOG_KEY, {}),
            )
            state = {"nature": ""}

            async def submit_nature():
                nature = state["nature"].strip()
                if not nature:
                    return ui.notify("请填写或选择措施性质", type="warning", position="bottom")
                dialog.close()
                await on_submit(nature)

            dialog.clear()
            with dialog, ui.card().classes("w-1/3 max-w-lg p-5"):
                ui.label(title).classes("text-lg font-bold text-gray-800")
                if description:
                    ui.label(description).classes("text-sm text-gray-600")
                if nature_options:
                    ui.label(
                        "请优先选择已有性质；选项按历史使用次数排列，便于后续统一统计。确无合适项时再新增。"
                    ).classes("text-sm text-orange-700")

                    def select_nature(e):
                        state["nature"] = str(e.value or "").strip()

                    ui.select(
                        nature_options,
                        label="已有措施性质（优先选择）",
                        on_change=select_nature,
                        with_input=True,
                        clearable=True,
                    ).props("outlined dense options-dense").classes("w-full")
                    with ui.expansion("没有合适项？新增措施性质", icon="add").classes("w-full"):
                        nature_input = ui.input("新增措施性质").props("outlined dense clearable").classes("w-full")
                        nature_input.on_value_change(lambda e: state.__setitem__("nature", str(e.value or "")))
                else:
                    ui.label("暂无历史性质，请录入第一项；审批通过后会自动加入词库。").classes("text-sm text-gray-500")
                    nature_input = ui.input("措施性质").props("outlined dense clearable").classes("w-full")
                    nature_input.on_value_change(lambda e: state.__setitem__("nature", str(e.value or "")))
                with ui.row().classes("w-full justify-end gap-3 mt-3"):
                    ui.button("取消", on_click=dialog.close).props("outline color=grey")
                    ui.button("确认通过", icon="check", on_click=submit_nature).props("color=green")
            dialog.open()

        async def save_current_record():
            """提交整张编辑表单，并把重复单号或版本冲突转换为用户可理解的提示。"""
            if not can_edit_all:
                return ui.notify("当前角色无保存权限", type="warning", position="bottom")
            if not validate_error_record(local_data):
                return
            result = await save_error_record(
                local_data,
                current_user,
                current_role,
                is_new=is_new,
                original_error_id=stored_error_id,
            )
            if result.code == "forbidden":
                return ui.notify("只有 admin 可以修改异常单号", type="warning", position="bottom")
            if result.code == "already_exists":
                return ui.notify("异常单号已存在，请使用其它单号", type="warning", position="bottom")
            if result.code == "not_found":
                return ui.notify("原异常单已不存在，请关闭窗口后刷新", type="warning", position="bottom")
            if result.code == "revision_conflict":
                return ui.notify(
                    "保存已取消：异常单已被其他用户或后台任务更新，请关闭窗口后重新打开再修改",
                    type="warning",
                    position="bottom",
                    multi_line=True,
                )
            if not result.changed:
                return ui.notify("异常单保存失败，请刷新后重试", type="negative", position="bottom")
            ui.notify("异常单已保存", type="positive", position="bottom")
            root_dialog.close()
            refresh_list()

        def open_delete_confirmation():
            """打开高风险操作确认框；真正删除时仍会再次校验 admin 角色。"""
            if is_new or not can_delete_record:
                return ui.notify("当前角色无删除异常单权限", type="warning", position="bottom")

            target_error_id = stored_error_id

            async def confirm_delete():
                result = await delete_error_record(target_error_id, current_role)
                if result.code == "forbidden":
                    return ui.notify("当前角色无删除异常单权限", type="warning", position="bottom")
                if result.code == "not_found":
                    ui.notify("该异常单已被删除", type="warning", position="bottom")
                    dialog.close()
                    root_dialog.close()
                    refresh_list()
                    return
                if not result.changed:
                    return ui.notify("异常单删除失败，请刷新后重试", type="negative", position="bottom")

                ui.notify(f"异常单 {target_error_id} 已删除", type="positive", position="bottom")
                dialog.close()
                root_dialog.close()
                refresh_list()

            dialog.clear()
            with dialog, ui.card().classes("w-1/3 max-w-md p-5"):
                ui.label("确认删除异常单").classes("text-lg font-bold text-red-700")
                ui.label(f"异常单号：{target_error_id}").classes("font-mono font-bold text-gray-800")
                ui.label("删除后将无法从页面恢复，请确认该异常单确实需要删除。").classes("text-sm text-gray-600")
                with ui.row().classes("w-full justify-end gap-3 mt-3"):
                    ui.button("取消", on_click=dialog.close).props("outline color=grey")
                    ui.button("确认删除", icon="delete_forever", on_click=confirm_delete).props("color=negative")
            dialog.open()

        def render_standard_items(container, list_key, title, fields, prefix, empty_text):
            """渲染说明、分析和应急对策三类结构相近的可重复表单项。"""
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
                            elif field_type == "people":
                                bind_people_input(label, item, key)
                            else:
                                bind_input(label, item, key)
                if can_edit_all:

                    def add_item():
                        new_item = ensure_item_id({key: "" for key, _, _ in fields}, prefix)
                        items.append(new_item)
                        render_standard_items(container, list_key, title, fields, prefix, empty_text)

                    ui.button(f"添加{title}", icon="add", on_click=add_item).props("outline dense color=primary")

        async def open_extension_request_dialog(action: dict):
            """由措施负责人发起延期申请；真正提交时会再次读取数据库并核验负责人和日期。"""
            action_id = get_item_id(action)
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
                    "old_due_date": "",
                    "new_due_date": request_state["new_due_date"],
                    "reason": request_state["reason"].strip(),
                    "requester": current_user,
                    "requester_role": current_role,
                    "requested_at": now_str,
                }

                def add_extension_request(current):
                    # 此回调在数据库事务内执行。即使页面打开后负责人、状态或日期发生变化，也不会误提交。
                    stored_action = find_preventive_action(current, action_id)
                    if not stored_action:
                        return "action_not_found", current
                    if stored_action.get("status") == "已关闭":
                        return "action_closed", current
                    if not can_edit_all and not is_current_responsible(
                        stored_action.get("owner", ""), current_user, current_role
                    ):
                        return "permission_changed", current
                    if get_pending_extension_request(stored_action):
                        return "pending_exists", current
                    stored_due_date = parse_date(stored_action.get("due_date", ""))
                    if stored_due_date and new_date <= stored_due_date:
                        return "due_date_changed", current

                    extension_request["old_due_date"] = stored_action.get("due_date", "")
                    stored_action.setdefault("extension_requests", []).append(copy.deepcopy(extension_request))
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

                result = await atomic_error_update(stored_error_id, add_extension_request)
                if result.code == "pending_exists":
                    return ui.notify("该措施已有延期申请待审批，请刷新查看", type="warning", position="bottom")
                if result.code == "due_date_changed":
                    return ui.notify("预计完成日期已被更新，请刷新后重新申请", type="warning", position="bottom")
                if result.code == "permission_changed":
                    return ui.notify("该措施负责人已变更，当前用户不能再申请延期", type="warning", position="bottom")
                if result.code in {"action_not_found", "action_closed", "not_found"}:
                    return ui.notify("该措施已不存在或已关闭，请刷新查看", type="warning", position="bottom")
                if not result.changed or not result.record:
                    return ui.notify("延期申请提交失败，请刷新后重试", type="negative", position="bottom")

                fresh_action = find_preventive_action(result.record, action_id)
                fresh_request = find_extension_request(fresh_action or {}, extension_request["id"])
                if not fresh_action or not fresh_request:
                    return ui.notify(
                        "延期申请已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom"
                    )
                approved_extension_count, current_request_count = get_extension_counts(fresh_action)
                content = (
                    "生产异常纠正预防措施延期申请\n"
                    f"异常单：{result.record['error_id']}\n"
                    f"产品：{result.record.get('basic_info', {}).get('product_name', '')}\n"
                    f"措施：{fresh_action.get('content', '')}\n"
                    f"申请人：{current_user}\n"
                    f"本次为第 {current_request_count} 次延期申请\n"
                    f"此前已通过延期：{approved_extension_count} 次\n"
                    f"原预计日期：{fresh_request.get('old_due_date') or '-'}\n"
                    f"申请延期至：{fresh_request['new_due_date']}\n"
                    f"延期原因：{fresh_request['reason']}\n"
                    f"审批角色：{', '.join(ERROR_EXTENSION_APPROVER_ROLES)}"
                )
                await send_error_extension_wecom_message(
                    content,
                    error_id=result.record["error_id"],
                    business_key=f"{result.record['error_id']}:{get_item_id(fresh_action)}:{get_item_id(fresh_request)}",
                    message_type="extension_request",
                )
                ui.notify("延期申请已提交", type="positive", position="bottom")
                dialog.close()
                refresh_list()
                await open_error_detail_dialog(stored_error_id)

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
            """审批一条延期申请；通过时才修改措施预计完成日期，驳回只记录审批结果。"""
            if not is_error_extension_approver(current_role):
                return ui.notify("当前角色无延期审批权限", type="warning", position="bottom")

            action_id = get_item_id(action)
            request_id = get_item_id(request)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            action_text = "通过延期申请" if approved else "驳回延期申请"

            def update_extension_request(current):
                stored_action = find_preventive_action(current, action_id)
                if not stored_action:
                    return "action_not_found", current
                if stored_action.get("status") == "已关闭":
                    return "action_closed", current
                stored_request = find_extension_request(stored_action, request_id)
                if not stored_request:
                    return "request_not_found", current
                if stored_request.get("status") != "待审批":
                    return "already_processed", current

                if approved:
                    # 申请提交后若日期已被其它操作修改，原申请的基准已失效，不能继续直接通过。
                    current_due_date = parse_date(stored_action.get("due_date", ""))
                    requested_old_due_date = parse_date(stored_request.get("old_due_date", ""))
                    if current_due_date != requested_old_due_date:
                        return "due_date_changed", current

                stored_request["status"] = "已通过" if approved else "已驳回"
                stored_request["approver"] = current_user
                stored_request["approver_role"] = current_role
                stored_request["approved_at"] = now_str
                if approved:
                    stored_action["due_date"] = stored_request.get("new_due_date", stored_action.get("due_date", ""))
                current["updated_by"] = current_user
                current["updated_at"] = now_str
                current.setdefault("operation_log", []).append(
                    {"user": current_user, "role": current_role, "action": action_text, "time": now_str}
                )
                return "updated", current

            result = await atomic_error_update(stored_error_id, update_extension_request)
            if result.code == "already_processed":
                return ui.notify("该延期申请已被其他审批人处理，请刷新查看", type="warning", position="bottom")
            if result.code == "due_date_changed":
                return ui.notify("预计完成日期已发生变化，不能直接通过原延期申请", type="warning", position="bottom")
            if result.code in {"action_not_found", "action_closed", "request_not_found", "not_found"}:
                return ui.notify("该措施或延期申请已发生变化，请刷新查看", type="warning", position="bottom")
            if not result.changed or not result.record:
                return ui.notify("延期审批失败，请刷新后重试", type="negative", position="bottom")

            fresh_action = find_preventive_action(result.record, action_id)
            fresh_request = find_extension_request(fresh_action or {}, request_id)
            if not fresh_action or not fresh_request:
                return ui.notify("延期审批已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom")
            approved_extension_count, request_count = get_extension_counts(fresh_action)
            content = (
                "生产异常延期申请审批结果\n"
                f"异常单：{result.record['error_id']}\n"
                f"产品：{result.record.get('basic_info', {}).get('product_name', '')}\n"
                f"措施：{fresh_action.get('content', '')}\n"
                f"审批结果：{'通过' if approved else '驳回'}\n"
                f"累计延期申请：{request_count} 次\n"
                f"当前已通过延期：{approved_extension_count} 次\n"
                f"原预计日期：{fresh_request.get('old_due_date', '-')}\n"
                f"申请延期至：{fresh_request.get('new_due_date', '-')}\n"
                f"审批人：{current_user}"
            )
            schedule_background_task(
                # 审批数据已经成功落库，通知异步发送，避免企业微信接口延迟阻塞页面刷新。
                send_error_extension_wecom_message(
                    content,
                    error_id=result.record["error_id"],
                    business_key=(
                        f"{result.record['error_id']}:{get_item_id(fresh_action)}:{get_item_id(fresh_request)}:approval"
                    ),
                    message_type="extension_approval",
                    additional_people=(
                        fresh_request.get("requester", "") if ERROR_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL else ""
                    ),
                    # 品质经理和 QE 工程师只在延期通过时追加通知，驳回时不通知。
                    additional_targets=ERROR_EXTENSION_APPROVAL_NOTIFY_TARGETS if approved else None,
                ),
                "延期审批企业微信通知",
            )
            ui.notify("延期审批已处理", type="positive", position="bottom")
            refresh_list()
            await open_error_detail_dialog(stored_error_id)

        async def submit_close_request_from_dialog(action: dict):
            """由措施负责人发起关闭申请，等待审批角色通过或驳回。"""
            action_id = get_item_id(action)
            close_note = action.get("close_note", "").strip()
            result = await submit_error_preventive_close_request(
                stored_error_id,
                action_id,
                current_user,
                current_role,
                close_note,
            )
            if result.code == "missing_close_note":
                return ui.notify("请填写关闭说明", type="warning", position="bottom")
            if result.code == "pending_extension":
                return ui.notify("该措施存在待审批延期申请，请先完成审批", type="warning", position="bottom")
            if result.code == "pending_close":
                return ui.notify("该措施已有关闭申请待审批，请刷新查看", type="warning", position="bottom")
            if result.code == "permission_changed":
                return ui.notify("该措施负责人已变更，当前用户不能再申请关闭", type="warning", position="bottom")
            if result.code in {"already_closed", "action_not_found", "not_found"}:
                return ui.notify("该措施已被其他用户处理，请刷新查看", type="warning", position="bottom")
            if not result.changed or not result.record:
                return ui.notify("关闭申请提交失败，请刷新后重试", type="negative", position="bottom")

            fresh_action = find_preventive_action(result.record, action_id)
            fresh_request = get_pending_close_request(fresh_action or {})
            if fresh_action and fresh_request:
                content = (
                    "生产异常纠正预防措施关闭申请\n"
                    f"异常单：{result.record['error_id']}\n"
                    f"产品：{result.record.get('basic_info', {}).get('product_name', '')}\n"
                    f"措施：{fresh_action.get('content', '')}\n"
                    f"申请人：{current_user}\n"
                    f"关闭说明：{fresh_request.get('note', '-')}\n"
                    f"审批角色：{', '.join(ERROR_EXTENSION_APPROVER_ROLES)}"
                )
                schedule_background_task(
                    send_error_extension_wecom_message(
                        content,
                        error_id=result.record["error_id"],
                        business_key=f"{result.record['error_id']}:{action_id}:{fresh_request['id']}:close_request",
                        message_type="close_request",
                    ),
                    "纠正预防措施关闭申请企业微信通知",
                )

            ui.notify("关闭申请已提交", type="positive", position="bottom")
            refresh_list()
            await open_error_detail_dialog(stored_error_id)

        async def approve_close_request_from_dialog(
            action: dict,
            request: dict,
            approved: bool,
            closure_nature: str = "",
        ):
            """审批纠正预防措施关闭申请。"""
            action_id = get_item_id(action)
            request_id = str(request.get("id", ""))
            result = await approve_error_preventive_close_request(
                stored_error_id,
                action_id,
                request_id,
                approved,
                current_user,
                current_role,
                closure_nature,
            )
            if result.code == "forbidden":
                return ui.notify("当前角色无关闭审批权限", type="warning", position="bottom")
            if result.code == "missing_closure_nature":
                return ui.notify("请填写或选择措施性质", type="warning", position="bottom")
            if result.code == "already_processed":
                return ui.notify("该关闭申请已被其他审批人处理，请刷新查看", type="warning", position="bottom")
            if result.code in {"action_not_found", "request_not_found", "already_closed", "not_found"}:
                return ui.notify("该关闭申请已发生变化，请刷新查看", type="warning", position="bottom")
            if not result.changed or not result.record:
                return ui.notify("关闭审批失败，请刷新后重试", type="negative", position="bottom")

            fresh_action = find_preventive_action(result.record, action_id)
            fresh_request = find_close_request(fresh_action or {}, request_id)
            if not fresh_action or not fresh_request:
                return ui.notify("关闭审批已保存，但读取最新数据失败，请刷新查看", type="warning", position="bottom")

            content = (
                "生产异常纠正预防措施关闭审批结果\n"
                f"异常单：{result.record['error_id']}\n"
                f"产品：{result.record.get('basic_info', {}).get('product_name', '')}\n"
                f"措施：{fresh_action.get('content', '')}\n"
                f"审批结果：{'通过' if approved else '驳回'}\n"
                f"措施性质：{fresh_action.get('closure_nature', '-') or '-'}\n"
                f"申请人：{fresh_request.get('requester', '-')}\n"
                f"审批人：{current_user}"
            )
            schedule_background_task(
                send_error_extension_wecom_message(
                    content,
                    error_id=result.record["error_id"],
                    business_key=f"{result.record['error_id']}:{action_id}:{request_id}:close_approval",
                    message_type="close_approval",
                    additional_people=(
                        fresh_request.get("requester", "") if ERROR_EXTENSION_NOTIFY_REQUESTER_ON_APPROVAL else ""
                    ),
                    additional_targets=ERROR_EXTENSION_APPROVAL_NOTIFY_TARGETS if approved else None,
                ),
                "纠正预防措施关闭审批企业微信通知",
            )
            ui.notify("关闭审批已处理", type="positive", position="bottom")
            refresh_list()
            await open_error_detail_dialog(stored_error_id)

        def render_preventive_items(container):
            """渲染纠正预防措施及其延期、审批、关闭操作。

            页面只负责根据当前快照决定按钮是否显示；每个按钮对应的写入回调仍会在事务内重新校验，
            因而不能通过保留旧页面或并发点击绕过权限和状态限制。
            """
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
                    item.setdefault("close_requests", [])
                    item.setdefault("closure_nature", "")
                    item.setdefault("extension_requests", [])
                    approved_extension_count, extension_request_count = get_extension_counts(item)
                    approved_close_count, close_request_count = get_close_counts(item)
                    can_close = (
                        not is_new
                        and item.get("status") != "已关闭"
                        and (can_edit_all or is_current_responsible(item.get("owner", ""), current_user, current_role))
                    )
                    pending_extension = get_pending_extension_request(item)
                    pending_close = get_pending_close_request(item)
                    can_apply_extension = can_close and not pending_extension and not pending_close
                    can_apply_close = can_close and not pending_extension and not pending_close
                    with ui.element("div").classes("w-full border border-gray-200 rounded-md bg-gray-50 p-4"):
                        with ui.row().classes("w-full justify-between items-center mb-3"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(f"纠正预防措施 {index + 1}").classes("font-bold text-sm text-gray-700")
                                ui.badge(
                                    item.get("status", "待执行"),
                                    color="green" if item.get("status") == "已关闭" else "orange",
                                )
                                ui.badge(f"已延期 {approved_extension_count} 次", color="blue").props("outline")
                                if pending_close:
                                    ui.badge("关闭待审批", color="purple").props("outline")
                                elif approved_close_count:
                                    ui.badge(f"已通过关闭 {approved_close_count} 次", color="green").props("outline")
                            if can_edit_all:
                                ui.button(
                                    icon="delete",
                                    on_click=lambda _, i=index: (items.pop(i), render_preventive_items(container)),
                                ).props("flat round dense color=red")

                        bind_textarea("纠正预防措施", item, "content")
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_people_input(
                                "负责人",
                                item,
                                "owner",
                                "w-full md:w-1/3",
                                allowed_values=owner_allowed_values,
                            )
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

                        if pending_close:
                            with ui.element("div").classes(
                                "w-full border border-purple-200 bg-purple-50 rounded-md p-3 mb-3"
                            ):
                                ui.label(f"关闭申请待审批：{pending_close.get('requested_at', '-')}").classes(
                                    "text-sm font-bold text-purple-800"
                                )
                                ui.label(
                                    f"申请人：{pending_close.get('requester', '-')} ｜ "
                                    f"说明：{pending_close.get('note', '-')}"
                                ).classes("text-sm text-purple-700")
                                if is_error_extension_approver(current_role):

                                    async def reject_close(event=None, a=item, r=pending_close):
                                        await approve_close_request_from_dialog(a, r, False)

                                    async def approve_close(event=None, a=item, r=pending_close):

                                        async def submit_with_nature(nature: str, action=a, request=r):
                                            await approve_close_request_from_dialog(action, request, True, nature)

                                        open_closure_nature_dialog(
                                            "通过关闭申请",
                                            f"{local_data.get('error_id', '')} ｜ {a.get('content', '')[:40]}",
                                            submit_with_nature,
                                        )

                                    with ui.row().classes("justify-end gap-2 mt-2"):
                                        ui.button("驳回关闭", icon="close", on_click=reject_close).props(
                                            "outline color=negative dense"
                                        )
                                        ui.button("通过关闭", icon="check", on_click=approve_close).props(
                                            "color=green dense"
                                        )

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
                                bind_input("措施性质", item, "closure_nature", "w-full md:w-1/3", readonly=True)
                            bind_textarea("关闭说明", item, "close_note", readonly=True)
                        elif can_close and not pending_close:
                            ui.separator().classes("my-2")
                            bind_textarea("关闭说明", item, "close_note", readonly=False)
                            ui.label("可填写 ECN 编号、会议结论、沟通记录或其它执行结果。").classes(
                                "text-xs text-gray-500 -mt-2 mb-2"
                            )

                            async def close_preventive_action(event=None, action=item):
                                await submit_close_request_from_dialog(action)

                            with ui.row().classes("items-center justify-end gap-3 mt-2"):
                                ui.label(
                                    f"累计申请 {extension_request_count} 次，已延期 {approved_extension_count} 次"
                                ).classes("text-xs text-gray-500")
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
                                if can_apply_close:
                                    ui.button(
                                        "申请关闭该措施",
                                        icon="check_circle",
                                        on_click=close_preventive_action,
                                    ).props("color=green")

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
                                    "close_requests": [],
                                    "closure_nature": "",
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
                    detail_status = get_error_card_status(local_data)
                    detail_status_color = {
                        "已关闭": "green",
                        ERROR_FILTER_PENDING_CLOSE_STATE: "purple",
                        ERROR_FILTER_PENDING_EXTENSION_STATE: "orange",
                        "纠正预防执行中": "teal",
                        "应急处理中": "blue",
                        "原因分析中": "purple",
                    }.get(detail_status, "grey")
                    ui.badge(detail_status, color=detail_status_color)
                    ui.label(local_data["error_id"] or "新异常单").classes("font-mono font-bold text-lg text-gray-800")
                    ui.label(local_data["basic_info"].get("product_name") or "未命名异常单").classes(
                        "text-base font-bold text-gray-700"
                    )
                ui.button(icon="close", on_click=root_dialog.close).props("flat round dense")

            with ui.scroll_area().classes("w-full flex-grow"):
                with ui.column().classes("w-full max-w-6xl mx-auto p-4 gap-4"):
                    with section("基础信息"):
                        basic = local_data["basic_info"]
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input(
                                "异常单号",
                                local_data,
                                "error_id",
                                "w-full md:w-[32%]",
                                readonly=not (is_new or can_rename_record),
                            )
                            bind_input("产品型号", basic, "product_name", "w-full md:w-[32%]")
                            bind_input("料号", basic, "material_no", "w-full md:w-[32%]")
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
                            bind_input("订单号", basic, "order_no", "w-full md:w-[32%]")
                            bind_input("投产数量", basic, "production_qty", "w-full md:w-[32%]")
                            bind_date("发文日期", basic, "publish_date", "w-full md:w-[32%]")
                        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
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
                            [("content", "异常情况说明", "textarea"), ("speaker", "说明人", "people")],
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
                                ("analyst", "分析人", "people"),
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
                                ("output_person", "对策输出人", "people"),
                                ("output_date", "输出日期", "date"),
                            ],
                            "emergency",
                            "暂无应急对策",
                        )

                    with section("纠正预防措施"):
                        preventive_container = ui.column().classes("w-full gap-3")
                        render_preventive_items(preventive_container)

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
                    ui.button("删除异常单", icon="delete_forever", on_click=open_delete_confirmation).props(
                        "outline color=negative"
                    )
                ui.button("关闭窗口", on_click=root_dialog.close).props("outline color=grey")
                if can_edit_all:
                    ui.button("保存异常单", icon="save", on_click=save_current_record).props("color=primary")

        root_dialog.open()

    # ==========================================
    # 主页面 UI (头部与列表总览)
    # ==========================================
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("异常单管理系统").classes("text-white text-xl font-bold absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {current_user}")
                ui.separator()
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # 将滚动限制在 header 下方的内容区内，避免浏览器主滚动条覆盖到顶部导航栏
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-slate-50"):
        with ui.column().classes("w-full h-full p-4 gap-3"):
            with ui.row().classes("w-full justify-between items-center bg-white p-3 shadow-sm rounded-lg"):
                with ui.row().classes("gap-3 items-center"):
                    ui.input("搜索产品/料号/订单/负责人").props("dense outlined").bind_value(
                        page_state, "search_keyword"
                    ).classes("w-64")
                    ui.select(ERROR_FILTER_STATES, label="状态筛选").props("dense outlined").bind_value(
                        page_state, "filter_state"
                    ).classes("w-44")
                    ui.button("查询", icon="search", on_click=lambda: refresh_list()).props("outline color=primary")
                    ui.button("刷新", icon="refresh", on_click=lambda: refresh_list()).props("flat color=primary")
                with ui.row().classes("gap-2 items-center"):
                    ui.label("点击“详情”或双击行打开详情").classes("text-xs text-gray-500")
                    if is_error_rd_manager(current_role):
                        ui.button(
                            "检查提醒",
                            icon="notifications_active",
                            on_click=handle_manual_reminder_check,
                        ).props("outline color=orange")
                    if can_edit_all:
                        ui.button(
                            "录入异常单",
                            icon="add_box",
                            on_click=handle_new_error_record,
                        ).props("color=red-7")

            error_grid = ui.aggrid(
                {
                    "columnDefs": get_error_grid_columns(),
                    "rowData": [],
                    "defaultColDef": {
                        "sortable": True,
                        "resizable": True,
                        "cellStyle": {"textAlign": "center"},
                        "headerClass": "error-grid-header-center",
                        "filterParams": {"buttons": ["reset"], "debounceMs": 250},
                    },
                    "headerHeight": 42,
                    "rowHeight": 42,
                    "enableCellTextSelection": True,
                    "columnMenu": "new",
                    "suppressMenuHide": True,
                    "pagination": True,
                    "paginationPageSize": ERROR_GRID_PAGE_SIZE,
                    "paginationPageSizeSelector": [20, 30, 50, 100],
                    "animateRows": False,
                    "rowClassRules": {
                        "row-pending": "data.row_tone == 'pending'",
                        "row-warning": "data.row_tone == 'warning'",
                        "row-pending-close": "data.row_tone == 'pending_close'",
                        "row-completed": "data.row_tone == 'completed'",
                    },
                    "overlayNoRowsTemplate": "<span class='text-gray-500'>没有符合当前条件的异常单</span>",
                },
                auto_size_columns=False,
            ).classes("error-grid ag-theme-alpine w-full flex-grow min-h-0")

            async def open_error_grid_record(event: Any, *, require_action_column: bool = False) -> None:
                event_args = event.args if isinstance(event.args, dict) else {}
                if require_action_column and str(event_args.get("colId", "")) != "detail_action":
                    return
                row_data = event_args.get("data")
                target_error_id = str(row_data.get("record_id", "")).strip() if isinstance(row_data, dict) else ""
                if target_error_id:
                    await open_error_detail_dialog(target_error_id)

            async def open_error_grid_action(event: Any) -> None:
                await open_error_grid_record(event, require_action_column=True)

            error_grid.on("cellClicked", open_error_grid_action)
            error_grid.on("rowDoubleClicked", open_error_grid_record)

            def refresh_list():
                """从数据库缓存重新读取、筛选并更新生产异常总表。"""
                all_errors = db_storage.get_item(ERROR_DATA_KEY, {})
                keyword = str(page_state.get("search_keyword", "")).lower().strip()
                filter_state = str(page_state.get("filter_state", ERROR_FILTER_OPEN_STATE))
                raw_errors = all_errors.values() if isinstance(all_errors, dict) else []
                valid_errors = sorted(
                    (merge_with_error_template(error) for error in raw_errors if isinstance(error, dict)),
                    key=lambda item: get_error_card_sort_key(item, current_user, current_role),
                    reverse=True,
                )
                rows = []
                for error_data in valid_errors:
                    basic = error_data.get("basic_info", {})
                    owner_text = " ".join(
                        str(item.get("owner", ""))
                        for item in error_data.get("preventive_actions", [])
                        if isinstance(item, dict)
                    )
                    searchable = " ".join(
                        [
                            str(error_data.get("error_id", "")),
                            str(basic.get("product_name", "")),
                            str(basic.get("material_no", "")),
                            str(basic.get("order_no", "")),
                            owner_text,
                        ]
                    ).lower()
                    if keyword and keyword not in searchable:
                        continue
                    if not error_matches_filter(error_data, filter_state):
                        continue
                    rows.append(build_error_grid_row(error_data, current_user, current_role))
                error_grid.options["rowData"] = rows
                error_grid.update()

            def check_and_refresh_list():
                """检测后台或其他用户写入的版本时间戳，必要时自动刷新当前浏览器的总表。"""
                current_stamp = db_storage.get_item(ERROR_VERSION_KEY, 0.0)
                if page_state.get("version_stamp", 0.0) != 0.0 and current_stamp != page_state["version_stamp"]:
                    page_state["version_stamp"] = current_stamp
                    refresh_list()
                elif page_state.get("version_stamp", 0.0) == 0.0:
                    page_state["version_stamp"] = current_stamp

            refresh_list()
            ui.timer(5.0, check_and_refresh_list)

    if error_id:
        await open_error_detail_dialog(error_id)
