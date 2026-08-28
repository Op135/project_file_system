# -*- encoding: utf-8 -*-
"""设计知识库页面。

该模块用于沉淀设计规范、错误案例和优秀案例。第一版聚焦受控分类、受控标签、
基础录入与检索，后续可以继续扩展审核流、附件和与异常单/样品问题的联动。
"""

import copy
import hashlib
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.parse import quote_from_bytes, unquote

from nicegui import app, ui

from .. import db_storage
from ..access_control import can
from ..approval_workflow import is_assigned_approver, resolve_approval_workflow
from ..components import ButtonUploader, FileThumbnail, get_upload_local_path
from ..config import IMG_DIR, PRESET_AVATARS, REQ_UPLOADS_FILE_TYPE, UPLOAD_URL_DIR, UPLOADS_DIR
from ..design_knowledge_config import (
    APPLICABLE_PHASES,
    CONTENT_TYPE_COPY,
    CONTENT_TYPES,
    DEFAULT_TAG_CATALOG,
    DESIGN_ATTACHMENT_DIR_NAME,
    DESIGN_ATTACHMENT_PARENTS_H,
    DESIGN_DOMAINS,
    DESIGN_KNOWLEDGE_EDITOR_ROLE_KEYWORDS,
    DESIGN_KNOWLEDGE_TAG_MANAGER_ROLE_KEYWORDS,
    ERROR_SEVERITY_LEVELS,
    PRACTICE_VALUE_LEVELS,
    PROJECT_CATEGORIES,
    RULE_LEVELS,
    can_review_design_knowledge_submission as can_review_legacy_submission,
    is_design_knowledge_review_approver_role as is_review_approver_role,
    resolve_design_knowledge_review_route as get_review_route,
)
from ..permission_catalog import (
    DESIGN_KNOWLEDGE_CREATE_PERMISSION,
    DESIGN_KNOWLEDGE_DELETE_PERMISSION,
    DESIGN_KNOWLEDGE_EDIT_PERMISSION,
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_MANAGE_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_VIEW_PERMISSION,
)
from ..utils import (
    get_cache_busted_path,
    handle_key,
    logout,
    setup_global_activity_tracking,
    sync_current_user_role,
)

logger = logging.getLogger(__name__)

DESIGN_KNOWLEDGE_DATA_KEY = "design_knowledge_data"
DESIGN_KNOWLEDGE_VERSION_KEY = "design_knowledge_version_stamp"
DESIGN_TAG_CATALOG_KEY = "design_knowledge_tag_catalog"
DESIGN_TAG_REQUESTS_KEY = "design_knowledge_tag_requests"
DESIGN_ATTACHMENT_ACCEPT = ",".join(["image/*", *sorted(REQ_UPLOADS_FILE_TYPE)])
DESIGN_KNOWLEDGE_MODULE = "design_knowledge"
DESIGN_KNOWLEDGE_REVIEW_EVENT = "knowledge_review"
DESIGN_TAG_REVIEW_EVENT = "tag_review"
DESIGN_KNOWLEDGE_LEGACY_VIEW_ROLE_KEYWORDS = ["质量", "销售", "工程", "研发", "boss", "admin"]

RECORD_STATUS_DRAFT = "草稿"
RECORD_STATUS_REVIEW = "待审核"
RECORD_STATUS_RETURNED = "已退回"
RECORD_STATUS_PUBLISHED = "已发布"
RECORD_STATUS_INACTIVE = "不再适用"
RECORD_STATUSES = [
    RECORD_STATUS_DRAFT,
    RECORD_STATUS_REVIEW,
    RECORD_STATUS_RETURNED,
    RECORD_STATUS_PUBLISHED,
    RECORD_STATUS_INACTIVE,
]
LEGACY_STATUS_MAP = {"已归档": RECORD_STATUS_INACTIVE}
FILTER_ALL = "全部"


def normalize_text(value: Any) -> str:
    """把用户输入整理为单行文本。"""
    return re.sub(r"\s+", " ", str(value or "").strip())


def unique_texts(values: Any) -> list[str]:
    """返回去重后的非空文本列表，并保持原始顺序。"""
    result = []
    seen = set()
    if not isinstance(values, list):
        return result
    for value in values:
        text = normalize_text(value)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def option_text(value: Any, default: str = "") -> str:
    """把选择器返回值整理成确定字符串，避免 None 进入业务函数。"""
    text = normalize_text(value)
    return text or default


def option_text_in(value: Any, options: list[str], default: str = "") -> str:
    """从受控选项中取值；选择器为空或异常时使用默认值。"""
    fallback = default or (options[0] if options else "")
    text = option_text(value, fallback)
    return text if text in options else fallback


def quote_url_component(value: str) -> str:
    """将字符串按 UTF-8 编码为可安全用于 URL 路径的片段。"""
    return quote_from_bytes(value.encode("utf-8"), safe="")


def get_attachment_label_number(file_info: dict) -> int:
    try:
        return int(str(file_info.get("file_lab", "0")))
    except (TypeError, ValueError):
        return 0


def get_active_attachments(record_data: dict) -> list[dict]:
    files = record_data.get("attachments", [])
    if not isinstance(files, list):
        return []
    active_files = [
        copy.deepcopy(file_info)
        for file_info in files
        if isinstance(file_info, dict) and not file_info.get("file_del_bool")
    ]
    return sorted(active_files, key=get_attachment_label_number)


def get_active_attachment_hashes_from_thumbnail_state(thumbnail_dic: dict) -> set[str]:
    active_hashes = set()
    if not isinstance(thumbnail_dic, dict):
        return active_hashes
    for entry in thumbnail_dic.values():
        if not isinstance(entry, dict):
            continue
        file_info = entry.get("file_information", {})
        if not isinstance(file_info, dict) or file_info.get("file_del_bool"):
            continue
        file_name_hash = str(file_info.get("file_name_hash", "")).strip()
        if file_name_hash:
            active_hashes.add(file_name_hash)
            active_hashes.add(unquote(file_name_hash))
    return active_hashes


def sanitize_upload_path_segment(value: str, default: str) -> str:
    safe_value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value or "").strip())
    safe_value = safe_value.strip(" .")
    return safe_value or default


def get_design_attachment_thumbnail_key(file_lab: str) -> str:
    return f"design_knowledge:{sanitize_upload_path_segment(file_lab, '0')}"


def get_design_attachment_file_hash_name(
    content_type: str,
    uploader_name: str,
    original_filename: str,
    content: bytes,
) -> tuple[str, str, str]:
    safe_name = sanitize_upload_path_segment(os.path.basename(original_filename), "attachment")
    file_name, file_suffix = os.path.splitext(safe_name)
    file_name = file_name or "attachment"
    file_suffix = file_suffix.lstrip(".").lower()
    file_hash = hashlib.md5(content).hexdigest()
    safe_type = sanitize_upload_path_segment(content_type, "unknown_type")
    safe_uploader = sanitize_upload_path_segment(uploader_name, "unknown")
    return file_name, file_suffix, f"design_knowledge_{safe_type}_{safe_uploader}_{file_name}.{file_hash}.{file_suffix}"


def get_design_attachment_storage_paths(content_type: str, uploader_name: str, file_name_hash: str) -> tuple[str, str]:
    type_folder = sanitize_upload_path_segment(content_type, "unknown_type")
    user_folder = sanitize_upload_path_segment(uploader_name, "unknown")
    target_dir = os.path.join(UPLOADS_DIR, DESIGN_ATTACHMENT_DIR_NAME, type_folder, user_folder)
    target_path = os.path.join(target_dir, file_name_hash)
    url_path = "/".join(
        [
            UPLOAD_URL_DIR.rstrip("/"),
            DESIGN_ATTACHMENT_DIR_NAME,
            quote_url_component(type_folder),
            quote_url_component(user_folder),
            quote_url_component(file_name_hash),
        ]
    )
    return target_path, url_path


def get_design_knowledge_template() -> dict:
    """返回一条知识记录的完整模板。"""
    return {
        "knowledge_id": "",
        "_revision": 0,
        "title": "",
        "content_type": CONTENT_TYPES[0],
        "domain": DESIGN_DOMAINS[0],
        "project_category": PROJECT_CATEGORIES[0],
        "applicable_phases": [],
        "tags": [],
        "rule_level": RULE_LEVELS[0],
        "severity_level": ERROR_SEVERITY_LEVELS[2],
        "practice_value": PRACTICE_VALUE_LEVELS[1],
        "summary": "",
        "scene": "",
        "analysis": "",
        "suggestion": "",
        "reference_project": "",
        "reference_projects": [],
        "attachments": [],
        "extra_keywords": "",
        "status": RECORD_STATUS_DRAFT,
        "review_route_key": "",
        "review_route_label": "",
        "approver_roles": [],
        "workflow_assignment": {},
        "created_by": "",
        "created_role": "",
        "created_at": "",
        "updated_by": "",
        "updated_at": "",
        "operation_log": [],
    }


def merge_with_knowledge_template(db_data: Any) -> dict:
    """用模板补齐历史数据，避免后续扩字段时旧记录渲染失败。"""
    merged = copy.deepcopy(get_design_knowledge_template())
    if not isinstance(db_data, dict):
        return merged

    for key, value in db_data.items():
        if key in {
            "applicable_phases",
            "tags",
            "reference_projects",
            "attachments",
            "approver_roles",
            "operation_log",
        }:
            merged[key] = copy.deepcopy(value) if isinstance(value, list) else []
        elif key == "workflow_assignment":
            merged[key] = copy.deepcopy(value) if isinstance(value, dict) else {}
        elif key in merged:
            merged[key] = copy.deepcopy(value)

    merged["content_type"] = merged["content_type"] if merged["content_type"] in CONTENT_TYPES else CONTENT_TYPES[0]
    merged["domain"] = merged["domain"] if merged["domain"] in DESIGN_DOMAINS else DESIGN_DOMAINS[0]
    merged["project_category"] = (
        merged["project_category"] if merged["project_category"] in PROJECT_CATEGORIES else PROJECT_CATEGORIES[0]
    )
    merged["applicable_phases"] = [p for p in unique_texts(merged["applicable_phases"]) if p in APPLICABLE_PHASES]
    merged["tags"] = unique_texts(merged["tags"])
    if not merged["reference_projects"] and merged.get("reference_project"):
        merged["reference_projects"] = unique_texts(
            re.split(r"[、,，;；\s]+", str(merged.get("reference_project", "")))
        )
    merged["reference_projects"] = unique_texts(merged["reference_projects"])
    merged["reference_project"] = "、".join(merged["reference_projects"])
    merged["attachments"] = [
        copy.deepcopy(file_info)
        for file_info in merged["attachments"]
        if isinstance(file_info, dict) and not file_info.get("file_del_bool")
    ]
    merged["status"] = LEGACY_STATUS_MAP.get(merged["status"], merged["status"])
    merged["status"] = merged["status"] if merged["status"] in RECORD_STATUSES else RECORD_STATUS_DRAFT
    return merged


def get_knowledge_id_prefix(reference_time: Optional[datetime] = None) -> str:
    target_time = reference_time or datetime.now()
    return f"DK{target_time.strftime('%Y%m%d')}"


def get_next_knowledge_id(all_records: Any, reference_time: Optional[datetime] = None) -> str:
    """按当天已有编号生成下一个 DKyyyyMMddNNN 编号。"""
    prefix = get_knowledge_id_prefix(reference_time)
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{3}})$")
    max_sequence = 0

    if isinstance(all_records, dict):
        for key, record in all_records.items():
            candidates = [key]
            if isinstance(record, dict):
                candidates.append(record.get("knowledge_id", ""))
            for candidate in candidates:
                match = pattern.fullmatch(str(candidate or ""))
                if match:
                    max_sequence = max(max_sequence, int(match.group(1)))

    return f"{prefix}{max_sequence + 1:03d}"


def get_tag_catalog() -> dict[str, list[str]]:
    """返回当前受控标签库，缺失领域会用默认标签补齐。"""
    stored_catalog = db_storage.get_item(DESIGN_TAG_CATALOG_KEY, {})
    if not isinstance(stored_catalog, dict):
        stored_catalog = {}

    catalog: dict[str, list[str]] = {}
    for domain in DESIGN_DOMAINS:
        merged_tags = []
        for tag in DEFAULT_TAG_CATALOG.get(domain, []):
            if tag not in merged_tags:
                merged_tags.append(tag)
        for tag in unique_texts(stored_catalog.get(domain, [])):
            if tag not in merged_tags:
                merged_tags.append(tag)
        catalog[domain] = merged_tags
    return catalog


def get_domain_tags(domain: str) -> list[str]:
    return get_tag_catalog().get(domain, [])


def get_content_type_copy(content_type: str) -> dict[str, str]:
    """返回随内容类型变化的表单文案。"""
    return CONTENT_TYPE_COPY.get(content_type, CONTENT_TYPE_COPY.get(CONTENT_TYPES[0], {}))


def build_project_model_hierarchy(project_summary: Any) -> dict[str, dict[str, dict[str, str]]]:
    """把 storage-general 中的项目型号整理成大系列/小系列/具体型号三级结构。"""
    hierarchy: dict[str, dict[str, dict[str, str]]] = {}
    if not isinstance(project_summary, dict):
        return hierarchy

    for project_name in sorted(project_summary.keys()):
        if not project_name:
            continue
        parts = str(project_name).split("-")
        level_1 = parts[0] if parts else "其它"
        level_2 = parts[1] if len(parts) > 1 else "其它"
        display_name = "-".join(parts[2:]) if len(parts) > 2 else "基础版"
        hierarchy.setdefault(level_1, {}).setdefault(level_2, {})[project_name] = display_name

    return hierarchy


def get_record_level(record: dict) -> str:
    content_type = record.get("content_type", "")
    if content_type == "设计规范":
        return record.get("rule_level", "")
    if content_type == "错误案例":
        return record.get("severity_level", "")
    if content_type == "优秀案例":
        return record.get("practice_value", "")
    return ""


def get_level_color(level: str) -> str:
    return {
        "规定": "red",
        "推荐": "blue",
        "提示": "grey",
        "致命": "red",
        "严重": "orange",
        "中等": "amber",
        "轻度": "blue-grey",
        "强推荐": "green",
        "可参考": "blue",
        "特定场景适用": "purple",
    }.get(level, "grey")


def get_type_color(content_type: str) -> str:
    return {
        "设计规范": "indigo",
        "错误案例": "red",
        "优秀案例": "green",
    }.get(content_type, "grey")


def get_status_color(status: str) -> str:
    return {
        RECORD_STATUS_PUBLISHED: "green",
        RECORD_STATUS_DRAFT: "orange",
        RECORD_STATUS_REVIEW: "purple",
        RECORD_STATUS_RETURNED: "red",
        RECORD_STATUS_INACTIVE: "grey",
    }.get(status, "grey")


def get_level_options_for_content_type(content_type: str) -> list[str]:
    if content_type == "设计规范":
        return RULE_LEVELS
    if content_type == "错误案例":
        return ERROR_SEVERITY_LEVELS
    if content_type == "优秀案例":
        return PRACTICE_VALUE_LEVELS
    return [*RULE_LEVELS, *ERROR_SEVERITY_LEVELS, *PRACTICE_VALUE_LEVELS]


def _design_role_matches(role: object, keywords: list[str]) -> bool:
    """只供旧 Excel 模式兼容原角色关键词。"""
    role_text = str(role or "").strip().casefold()
    return any(
        str(keyword).strip().casefold() in role_text
        for keyword in keywords
        if str(keyword).strip()
    )


def _has_design_permission(
    username: str,
    role: object,
    permission_code: str,
    legacy_keywords: list[str],
) -> bool:
    """数据库模式只认稳定权限；旧 Excel 模式继续兼容原角色规则。"""
    role_text = str(role or "").strip()
    user_service = getattr(app.state, "user_service", None)
    if user_service is None or not str(username or "").strip():
        return _design_role_matches(role_text, legacy_keywords)
    matched_roles = [role_text] if _design_role_matches(role_text, legacy_keywords) else []
    return can(
        user_service,
        str(username).strip(),
        permission_code,
        legacy_role=role_text,
        legacy_allowed_roles=matched_roles,
    )


def can_view_design_knowledge(role: object, username: str = "") -> bool:
    return _has_design_permission(
        username,
        role,
        DESIGN_KNOWLEDGE_VIEW_PERMISSION,
        DESIGN_KNOWLEDGE_LEGACY_VIEW_ROLE_KEYWORDS,
    )


def can_create_design_knowledge(role: object, username: str = "") -> bool:
    return _has_design_permission(
        username,
        role,
        DESIGN_KNOWLEDGE_CREATE_PERMISSION,
        DESIGN_KNOWLEDGE_EDITOR_ROLE_KEYWORDS,
    )


def is_knowledge_editor(current_user: str, current_role: str) -> bool:
    return _has_design_permission(
        current_user,
        current_role,
        DESIGN_KNOWLEDGE_EDIT_PERMISSION,
        DESIGN_KNOWLEDGE_EDITOR_ROLE_KEYWORDS,
    )


def is_tag_manager(current_user: str, current_role: str) -> bool:
    return _has_design_permission(
        current_user,
        current_role,
        DESIGN_KNOWLEDGE_TAG_MANAGE_PERMISSION,
        DESIGN_KNOWLEDGE_TAG_MANAGER_ROLE_KEYWORDS,
    )


def can_review_design_knowledge(current_user: str, current_role: str) -> bool:
    legacy_roles = [
        str(current_role)
        if is_review_approver_role(current_role)
        else ""
    ]
    return _has_design_permission(
        current_user,
        current_role,
        DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
        legacy_roles,
    )


def can_review_design_tag(current_user: str, current_role: str) -> bool:
    legacy_roles = [
        str(current_role)
        if is_review_approver_role(current_role)
        else ""
    ]
    return _has_design_permission(
        current_user,
        current_role,
        DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
        legacy_roles,
    )


def is_design_knowledge_admin(current_user: str, current_role: str) -> bool:
    """判断用户是否拥有永久删除设计知识的高风险权限。"""
    return _has_design_permission(
        current_user,
        current_role,
        DESIGN_KNOWLEDGE_DELETE_PERMISSION,
        ["admin"],
    )


def _submission_event_and_permission(submission_type: str) -> tuple[str, str]:
    if submission_type == "tag":
        return DESIGN_TAG_REVIEW_EVENT, DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION
    return DESIGN_KNOWLEDGE_REVIEW_EVENT, DESIGN_KNOWLEDGE_REVIEW_PERMISSION


def _restore_pending_assignment(submission: dict) -> list[str]:
    """待办表意外缺失时，使用单据内固化的审批人快照恢复。"""
    assignment = submission.get("workflow_assignment", {})
    user_service = getattr(app.state, "user_service", None)
    if (
        not isinstance(assignment, dict)
        or not assignment.get("task_key")
        or user_service is None
        or getattr(user_service, "storage_mode", "legacy_excel") != "database"
    ):
        return []
    entity_id = str(submission.get("request_id") or submission.get("knowledge_id") or "")
    pending = user_service.list_pending_assignment_usernames(
        module=DESIGN_KNOWLEDGE_MODULE,
        entity_id=entity_id,
        task_key=str(assignment["task_key"]),
    )
    if pending or submission.get("status") not in {RECORD_STATUS_REVIEW, "待审核"}:
        return pending
    try:
        return user_service.replace_work_assignments(
            module=DESIGN_KNOWLEDGE_MODULE,
            entity_id=entity_id,
            task_key=str(assignment["task_key"]),
            assignee_usernames=assignment.get("assignee_usernames", []),
            source_policy_code=(
                f"{assignment.get('workflow_code', '')}@"
                f"{assignment.get('version_number', '')}"
            ),
        )
    except Exception:
        logger.error("恢复设计知识审批待办失败", exc_info=True)
        return []


def can_review_submission(
    submission: Any,
    current_user: str,
    current_role: str,
    *,
    submission_type: str = "knowledge",
) -> bool:
    """数据库模式校验稳定权限和本单待办；旧记录继续兼容固化角色快照。"""
    if not isinstance(submission, dict):
        return False
    permission_code = _submission_event_and_permission(submission_type)[1]
    legacy_allowed = (
        can_review_legacy_submission(submission, current_user, current_role)
    )
    legacy_roles = [str(current_role)] if legacy_allowed else []
    if not _has_design_permission(
        current_user,
        current_role,
        permission_code,
        legacy_roles,
    ):
        return False

    user_service = getattr(app.state, "user_service", None)
    if user_service is None or getattr(user_service, "storage_mode", "legacy_excel") != "database":
        return legacy_allowed
    assignment = submission.get("workflow_assignment", {})
    if not isinstance(assignment, dict) or not assignment.get("task_key"):
        # 只有迁移前已经处于待审核状态的记录才允许按原审批角色快照收尾。
        return legacy_allowed
    _restore_pending_assignment(submission)
    entity_id = str(submission.get("request_id") or submission.get("knowledge_id") or "")
    return is_assigned_approver(
        user_service,
        module=DESIGN_KNOWLEDGE_MODULE,
        entity_id=entity_id,
        task_key=str(assignment["task_key"]),
        username=current_user,
    ) and str(assignment.get("required_permission_code", "")) == permission_code


def can_edit_record(record: dict, current_user: str, current_role: str) -> bool:
    if record.get("status") == RECORD_STATUS_REVIEW and can_review_submission(
        record,
        current_user,
        current_role,
    ):
        return True
    return (
        is_knowledge_editor(current_user, current_role)
        and record.get("created_by") == current_user
        and record.get("status") != RECORD_STATUS_INACTIVE
    )


def can_manage_record_status(record: dict, current_user: str, current_role: str) -> bool:
    """待审核记录必须是本单审批人；已发布记录按审核管理权限维护。"""
    if record.get("status") == RECORD_STATUS_REVIEW:
        return can_review_submission(record, current_user, current_role)
    return can_review_design_knowledge(current_user, current_role)


def get_design_knowledge_dashboard_pending_count(
    all_records: Any,
    current_user: str,
    current_role: str,
    tag_requests: Any = None,
) -> int:
    """返回本人知识审核、标签审核和退回修改待办数量。"""
    current_user = str(current_user or "")
    pending_count = 0
    if isinstance(all_records, dict):
        for record_data in all_records.values():
            if not isinstance(record_data, dict):
                continue
            record = merge_with_knowledge_template(record_data)
            if record.get("status") == RECORD_STATUS_REVIEW and can_review_submission(
                record,
                current_user,
                current_role,
            ):
                pending_count += 1
            elif (
                record.get("created_by") == current_user
                and record.get("status") == RECORD_STATUS_RETURNED
                and is_knowledge_editor(current_user, current_role)
            ):
                pending_count += 1
    if isinstance(tag_requests, dict):
        pending_count += sum(
            1
            for request in tag_requests.values()
            if isinstance(request, dict)
            and request.get("status") == "待审核"
            and can_review_submission(
                request,
                current_user,
                current_role,
                submission_type="tag",
            )
        )
    return pending_count


def _resolve_design_workflow(event: str, requester_username: str) -> Optional[dict]:
    """数据库身份模式必须命中已发布流程；旧 Excel 模式返回空值。"""
    user_service = getattr(app.state, "user_service", None)
    if user_service is None or getattr(user_service, "storage_mode", "legacy_excel") != "database":
        return None
    return resolve_approval_workflow(
        user_service,
        module=DESIGN_KNOWLEDGE_MODULE,
        event=event,
        requester_username=requester_username,
    )


def _workflow_error_message(result: dict, subject: str) -> str:
    status = str(result.get("status", "error"))
    details = str(result.get("message") or "审批流程解析失败")
    hints = {
        "missing_membership": "请先在用户管理中配置申请人的主部门和主岗位",
        "no_match": "请在系统管理中发布能匹配该申请人的审批流程",
        "ambiguous": "请调整重复命中流程的条件或优先级",
        "no_approver": "请检查审批岗位、在职人员及审批权限",
        "invalid_policy": "请修正流程使用的审批权限",
    }
    hint = hints.get(status, "请检查系统管理中的审批流程配置")
    return f"{subject}无法提交：{details}；{hint}"


def _build_workflow_assignment(workflow_result: dict, task_key: str) -> dict:
    workflow = workflow_result["workflow"]
    version = workflow_result["version"]
    return {
        "workflow_id": workflow["workflow_id"],
        "workflow_code": workflow["code"],
        "workflow_name": workflow["name"],
        "version_id": version["version_id"],
        "version_number": version["version_number"],
        "task_key": task_key,
        "required_permission_code": version["required_permission_code"],
        "approval_mode": version.get("approval_mode", "any"),
        "assignee_usernames": [item["username"] for item in workflow_result["approvers"]],
        "assignee_names": [
            item.get("display_name") or item["username"]
            for item in workflow_result["approvers"]
        ],
    }


def _workflow_approver_text(submission: dict) -> str:
    assignment = submission.get("workflow_assignment", {})
    if isinstance(assignment, dict) and assignment.get("assignee_names"):
        return "、".join(str(value) for value in assignment["assignee_names"] if str(value))
    return "、".join(submission.get("approver_roles", [])) or "未配置"


def _persist_workflow_assignments(entity_id: str, assignment: dict) -> None:
    """创建具体审批待办；失败时保留单据快照供页面自动恢复。"""
    user_service = getattr(app.state, "user_service", None)
    if user_service is None or getattr(user_service, "storage_mode", "legacy_excel") != "database":
        return
    try:
        user_service.replace_work_assignments(
            module=DESIGN_KNOWLEDGE_MODULE,
            entity_id=entity_id,
            task_key=str(assignment["task_key"]),
            assignee_usernames=assignment.get("assignee_usernames", []),
            source_policy_code=(
                f"{assignment.get('workflow_code', '')}@"
                f"{assignment.get('version_number', '')}"
            ),
        )
    except Exception:
        logger.error("创建设计知识审批待办失败，等待页面自愈", exc_info=True)


def _complete_workflow_assignment(submission: dict, current_user: str) -> None:
    assignment = submission.get("workflow_assignment", {})
    user_service = getattr(app.state, "user_service", None)
    if (
        not isinstance(assignment, dict)
        or not assignment.get("task_key")
        or user_service is None
        or getattr(user_service, "storage_mode", "legacy_excel") != "database"
    ):
        return
    entity_id = str(submission.get("request_id") or submission.get("knowledge_id") or "")
    try:
        user_service.complete_work_assignment(
            module=DESIGN_KNOWLEDGE_MODULE,
            entity_id=entity_id,
            task_key=str(assignment["task_key"]),
            username=current_user,
            approval_mode=str(assignment.get("approval_mode", "any")),
        )
    except Exception:
        logger.error("完成设计知识审批待办失败", exc_info=True)


async def save_knowledge_record(
    record_data: dict, current_user: str, current_role: str
) -> tuple[bool, str, Optional[dict]]:
    """原子保存知识记录，返回保存结果和最新记录。"""
    incoming = merge_with_knowledge_template(record_data)
    knowledge_id = normalize_text(incoming.get("knowledge_id"))
    all_records_snapshot = db_storage.get_item(DESIGN_KNOWLEDGE_DATA_KEY, {})
    existing_snapshot = (
        merge_with_knowledge_template(all_records_snapshot.get(knowledge_id, {}))
        if knowledge_id and isinstance(all_records_snapshot, dict) and knowledge_id in all_records_snapshot
        else None
    )
    if existing_snapshot is None:
        if not can_create_design_knowledge(current_role, current_user):
            return False, "当前用户没有录入设计知识的权限", None
    elif not can_edit_record(existing_snapshot, current_user, current_role):
        return False, "当前用户没有维护这条设计知识的权限", None

    target_status = incoming.get("status")
    previous_status = existing_snapshot.get("status") if existing_snapshot else None
    allowed_save_transitions = {
        None: {RECORD_STATUS_DRAFT, RECORD_STATUS_REVIEW},
        RECORD_STATUS_DRAFT: {RECORD_STATUS_DRAFT, RECORD_STATUS_REVIEW},
        RECORD_STATUS_RETURNED: {RECORD_STATUS_DRAFT, RECORD_STATUS_REVIEW},
        RECORD_STATUS_REVIEW: {RECORD_STATUS_REVIEW},
        RECORD_STATUS_PUBLISHED: {RECORD_STATUS_REVIEW, RECORD_STATUS_PUBLISHED},
        RECORD_STATUS_INACTIVE: {RECORD_STATUS_INACTIVE},
    }
    if target_status not in allowed_save_transitions.get(previous_status, set()):
        return False, "当前状态不能通过编辑表单直接切换到目标状态", None
    if target_status == RECORD_STATUS_PUBLISHED:
        if (
            existing_snapshot is None
            or existing_snapshot.get("status") not in {RECORD_STATUS_REVIEW, RECORD_STATUS_PUBLISHED, RECORD_STATUS_INACTIVE}
            or not can_manage_record_status(existing_snapshot, current_user, current_role)
        ):
            return False, "当前用户不能直接发布这条设计知识", None
    elif target_status == RECORD_STATUS_INACTIVE:
        if (
            existing_snapshot is None
            or existing_snapshot.get("status") not in {RECORD_STATUS_PUBLISHED, RECORD_STATUS_INACTIVE}
            or not can_review_design_knowledge(current_user, current_role)
        ):
            return False, "当前用户不能调整这条设计知识的适用状态", None

    workflow_result = None
    workflow_assignment = None
    starts_new_review = target_status == RECORD_STATUS_REVIEW and (
        existing_snapshot is None or existing_snapshot.get("status") != RECORD_STATUS_REVIEW
    )
    if starts_new_review:
        workflow_result = _resolve_design_workflow(DESIGN_KNOWLEDGE_REVIEW_EVENT, current_user)
        if workflow_result is not None:
            if workflow_result.get("status") != "matched":
                return False, _workflow_error_message(workflow_result, "设计知识"), None
            workflow_assignment = _build_workflow_assignment(
                workflow_result,
                f"knowledge_review:{uuid.uuid4().hex[:12]}",
            )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result: dict[str, Any] = {"code": "", "record": None}

    def update_all_records(all_records: Any) -> Any:
        records = all_records if isinstance(all_records, dict) else {}
        record = merge_with_knowledge_template(incoming)
        knowledge_id = normalize_text(record.get("knowledge_id"))
        is_new = not knowledge_id or knowledge_id not in records
        existing: Optional[dict] = None

        if is_new:
            knowledge_id = get_next_knowledge_id(records)
            record["knowledge_id"] = knowledge_id
            record["_revision"] = 1
            record["created_by"] = current_user
            record["created_role"] = current_role
            record["created_at"] = now_str
            action = "创建知识"
        else:
            existing = merge_with_knowledge_template(records.get(knowledge_id, {}))
            if existing.get("_revision") != record.get("_revision"):
                result["code"] = "conflict"
                return db_storage.ATOMIC_NO_UPDATE
            record["created_by"] = existing.get("created_by", current_user)
            record["created_role"] = existing.get("created_role", current_role)
            record["created_at"] = existing.get("created_at", now_str)
            record["_revision"] = int(existing.get("_revision", 0)) + 1
            action = "更新知识"

        record["title"] = normalize_text(record["title"])
        record["reference_project"] = normalize_text(record["reference_project"])
        record["extra_keywords"] = normalize_text(record["extra_keywords"])
        record["applicable_phases"] = [p for p in unique_texts(record["applicable_phases"]) if p in APPLICABLE_PHASES]
        record_domain = option_text_in(record.get("domain"), DESIGN_DOMAINS, DESIGN_DOMAINS[0])
        record["domain"] = record_domain
        record["tags"] = [tag for tag in unique_texts(record["tags"]) if tag in get_domain_tags(record_domain)]
        record["reference_projects"] = unique_texts(record.get("reference_projects", []))
        record["reference_project"] = "、".join(record["reference_projects"])
        record["attachments"] = get_active_attachments(record)
        record["status"] = LEGACY_STATUS_MAP.get(record["status"], record["status"])
        record["status"] = record["status"] if record["status"] in RECORD_STATUSES else RECORD_STATUS_DRAFT
        if record["status"] == RECORD_STATUS_REVIEW:
            if workflow_assignment is not None and workflow_result is not None:
                record["review_route_key"] = workflow_result["workflow"]["code"]
                record["review_route_label"] = workflow_result["workflow"]["name"]
                record["approver_roles"] = copy.deepcopy(workflow_assignment["assignee_names"])
                record["workflow_assignment"] = copy.deepcopy(workflow_assignment)
            elif not record.get("workflow_assignment"):
                # 旧 Excel 模式仍按原 JSON 路由固化审核角色。
                route = get_review_route(record.get("created_role", current_role))
                record["review_route_key"] = route["key"]
                record["review_route_label"] = route["label"]
                record["approver_roles"] = copy.deepcopy(route["approver_roles"])
        record["updated_by"] = current_user
        record["updated_at"] = now_str
        record["operation_log"].append({"user": current_user, "role": current_role, "action": action, "time": now_str})

        records[knowledge_id] = record
        result["code"] = "saved"
        result["record"] = copy.deepcopy(record)
        return records

    db_success = await db_storage.atomic_deep_update([DESIGN_KNOWLEDGE_DATA_KEY], update_all_records)
    if not db_success:
        return False, "数据库写入失败", None
    if result["code"] == "conflict":
        return False, "这条知识已被其他人更新，请刷新后再编辑", None
    if result["code"] != "saved":
        return False, "未保存任何修改", None

    await db_storage.set_item(DESIGN_KNOWLEDGE_VERSION_KEY, time.time())
    saved_record = result["record"]
    if saved_record and workflow_assignment is not None:
        _persist_workflow_assignments(saved_record["knowledge_id"], workflow_assignment)
    if saved_record and saved_record.get("status") == RECORD_STATUS_REVIEW:
        approver_text = _workflow_approver_text(saved_record)
        self_review_hint = "；当前账号也会收到待审批提示" if can_review_submission(
            saved_record,
            current_user,
            current_role,
        ) else ""
        return True, f"已提交至 {approver_text} 审核{self_review_hint}", saved_record
    return True, "保存成功", saved_record


async def delete_knowledge_record(knowledge_id: str, current_user: str, current_role: str) -> tuple[bool, str]:
    """由 admin 原子删除单张设计知识卡。"""
    if not is_design_knowledge_admin(current_user, current_role):
        return False, "forbidden"

    outcome = {"changed": False, "code": "db_error"}

    def remove_record(all_records: Any) -> Any:
        records = all_records if isinstance(all_records, dict) else {}
        if knowledge_id not in records:
            outcome["code"] = "not_found"
            return db_storage.ATOMIC_NO_UPDATE

        del records[knowledge_id]
        outcome["changed"] = True
        outcome["code"] = "deleted"
        return records

    success = await db_storage.atomic_deep_update([DESIGN_KNOWLEDGE_DATA_KEY], remove_record)
    if success and outcome["changed"]:
        await db_storage.set_item(DESIGN_KNOWLEDGE_VERSION_KEY, time.time())
    return bool(success and outcome["changed"]), outcome["code"] if success else "db_error"


async def save_tag_catalog(catalog: dict[str, list[str]]) -> None:
    """保存受控标签库。"""
    normalized = {domain: unique_texts(catalog.get(domain, [])) for domain in DESIGN_DOMAINS}
    await db_storage.set_item(DESIGN_TAG_CATALOG_KEY, normalized)
    await db_storage.set_item(DESIGN_KNOWLEDGE_VERSION_KEY, time.time())


def get_next_tag_request_id(all_requests: Any) -> str:
    prefix = f"DKTAG{datetime.now().strftime('%Y%m%d')}"
    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{3}})$")
    max_sequence = 0
    if isinstance(all_requests, dict):
        for key in all_requests:
            match = pattern.fullmatch(str(key or ""))
            if match:
                max_sequence = max(max_sequence, int(match.group(1)))
    return f"{prefix}{max_sequence + 1:03d}"


async def submit_tag_request(
    domain: str, tag_name: str, reason: str, current_user: str, current_role: str
) -> tuple[bool, str]:
    if not (
        is_knowledge_editor(current_user, current_role)
        or can_create_design_knowledge(current_role, current_user)
    ):
        return False, "当前用户没有维护设计知识和申请新标签的权限"
    tag_name = normalize_text(tag_name)
    reason = normalize_text(reason)
    if domain not in DESIGN_DOMAINS:
        return False, "请选择专业领域"
    if not tag_name:
        return False, "请输入标签名称"
    if tag_name in get_domain_tags(domain):
        return False, "该标签已在受控标签库中"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    workflow_result = _resolve_design_workflow(DESIGN_TAG_REVIEW_EVENT, current_user)
    workflow_assignment = None
    if workflow_result is not None:
        if workflow_result.get("status") != "matched":
            return False, _workflow_error_message(workflow_result, "新标签申请")
        workflow_assignment = _build_workflow_assignment(
            workflow_result,
            f"tag_review:{uuid.uuid4().hex[:12]}",
        )
    review_route = get_review_route(current_role) if workflow_result is None else {
        "key": workflow_result["workflow"]["code"],
        "label": workflow_result["workflow"]["name"],
        "approver_roles": copy.deepcopy(workflow_assignment["assignee_names"]),
    }
    saved_request: dict[str, Any] = {}

    def update_requests(all_requests: Any) -> Any:
        nonlocal saved_request
        requests = all_requests if isinstance(all_requests, dict) else {}
        request_id = get_next_tag_request_id(requests)
        request = {
            "request_id": request_id,
            "domain": domain,
            "tag_name": tag_name,
            "reason": reason,
            "status": "待审核",
            "created_by": current_user,
            "created_role": current_role,
            "created_at": now_str,
            "review_route_key": review_route["key"],
            "review_route_label": review_route["label"],
            "approver_roles": copy.deepcopy(review_route["approver_roles"]),
            "handled_by": "",
            "handled_at": "",
        }
        if workflow_assignment is not None:
            request["workflow_assignment"] = copy.deepcopy(workflow_assignment)
        requests[request_id] = request
        saved_request = copy.deepcopy(request)
        return requests

    success = await db_storage.atomic_deep_update([DESIGN_TAG_REQUESTS_KEY], update_requests)
    if success and workflow_assignment is not None and saved_request:
        _persist_workflow_assignments(saved_request["request_id"], workflow_assignment)
    approver_text = _workflow_approver_text(saved_request) if saved_request else (
        "、".join(review_route["approver_roles"]) or "未配置"
    )
    self_review_hint = ""
    if success:
        if can_review_submission(
            saved_request,
            current_user,
            current_role,
            submission_type="tag",
        ):
            self_review_hint = "；当前账号也会收到待审批提示"
    return success, f"标签申请已提交至 {approver_text}{self_review_hint}" if success else "标签申请提交失败"


async def update_tag_request_status(
    request_id: str, status: str, current_user: str, current_role: str
) -> tuple[bool, str]:
    """审批或驳回标签申请。"""
    if status not in {"已通过", "已驳回"}:
        return False, "标签申请目标状态无效"
    request_data = {}
    outcome = {"code": "not_found"}

    def update_requests(all_requests: Any) -> Any:
        nonlocal request_data
        requests = all_requests if isinstance(all_requests, dict) else {}
        current = requests.get(request_id)
        if not isinstance(current, dict) or current.get("status") != "待审核":
            return db_storage.ATOMIC_NO_UPDATE
        if not can_review_submission(
            current,
            current_user,
            current_role,
            submission_type="tag",
        ):
            outcome["code"] = "forbidden"
            return db_storage.ATOMIC_NO_UPDATE
        current = copy.deepcopy(current)
        current["status"] = status
        current["handled_by"] = current_user
        current["handled_role"] = current_role
        current["handled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        requests[request_id] = current
        request_data = current
        outcome["code"] = "updated"
        return requests

    success = await db_storage.atomic_deep_update([DESIGN_TAG_REQUESTS_KEY], update_requests)
    if outcome["code"] == "forbidden":
        return False, "当前用户不是该标签申请指定的审核人"
    if not success or not request_data:
        return False, "标签申请状态更新失败或已被处理"

    _complete_workflow_assignment(request_data, current_user)

    if status == "已通过":
        catalog = get_tag_catalog()
        domain = request_data.get("domain", "")
        tag_name = request_data.get("tag_name", "")
        if domain in DESIGN_DOMAINS and tag_name and tag_name not in catalog[domain]:
            catalog[domain].append(tag_name)
            await save_tag_catalog(catalog)

    return True, "标签申请已处理"


@ui.page("/design_knowledge")
def design_knowledge_page():
    setup_global_activity_tracking()
    app.storage.client.setdefault("key_state", {})
    ui.keyboard(on_key=handle_key, ignore=[])
    ui.add_head_html("""
        <style>
            html, body {
                overflow: hidden !important;
                margin: 0;
                padding: 0;
                height: 100vh;
                background-color: #f8fafc;
            }
            .knowledge-clamp-2 {
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
                overflow: hidden;
            }
        </style>
    """)

    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")
        return

    current_user = app.storage.user.get("current_user", "匿名用户")
    current_role = sync_current_user_role()
    if not can_view_design_knowledge(current_role, current_user):
        ui.navigate.to("/main")
        return
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])
    current_display_path = get_cache_busted_path(current_avatar_path)
    can_add_record = can_create_design_knowledge(current_role, current_user)
    can_edit_own_record = is_knowledge_editor(current_user, current_role)
    can_manage_tags = is_tag_manager(current_user, current_role)
    can_review_knowledge_records = can_review_design_knowledge(current_user, current_role)
    can_review_tag_requests = can_review_design_tag(current_user, current_role)
    can_access_tag_manager = can_manage_tags or can_review_tag_requests
    can_view_workflow_records = (
        can_add_record
        or can_edit_own_record
        or can_review_knowledge_records
        or can_review_tag_requests
    )
    can_delete_record = is_design_knowledge_admin(current_user, current_role)

    page_state = {
        "search_keyword": "",
        "content_type": FILTER_ALL,
        "domain": FILTER_ALL,
        "project_category": FILTER_ALL,
        "phase": FILTER_ALL,
        "tag": FILTER_ALL,
        "level": FILTER_ALL,
        "status": FILTER_ALL if can_view_workflow_records else RECORD_STATUS_PUBLISHED,
        "version_stamp": 0.0,
    }

    edit_dialog = ui.dialog().props("persistent")
    detail_dialog = ui.dialog()
    delete_dialog = ui.dialog().props("persistent")
    tag_request_dialog = ui.dialog().props("persistent")
    tag_manager_dialog = ui.dialog().props("persistent")

    def get_available_filter_tags() -> list[str]:
        catalog = get_tag_catalog()
        tags = []
        selected_domain = option_text_in(page_state.get("domain"), [FILTER_ALL, *DESIGN_DOMAINS], FILTER_ALL)
        domains = DESIGN_DOMAINS if selected_domain == FILTER_ALL else [selected_domain]
        for domain in domains:
            for tag in catalog.get(domain, []):
                if tag not in tags:
                    tags.append(tag)
        return tags

    def record_matches_filters(record: dict) -> bool:
        if not can_view_workflow_records and record.get("status") != RECORD_STATUS_PUBLISHED:
            return False
        if record.get("status") == RECORD_STATUS_DRAFT and record.get("created_by") != current_user:
            return False
        if record.get("status") != RECORD_STATUS_PUBLISHED and record.get("created_by") != current_user:
            can_see_assigned_review = (
                record.get("status") == RECORD_STATUS_REVIEW
                and can_review_submission(record, current_user, current_role)
            )
            can_see_inactive = (
                record.get("status") == RECORD_STATUS_INACTIVE
                and can_review_knowledge_records
            )
            if not can_see_assigned_review and not can_see_inactive:
                return False
        if page_state["content_type"] != FILTER_ALL and record.get("content_type") != page_state["content_type"]:
            return False
        if page_state["domain"] != FILTER_ALL and record.get("domain") != page_state["domain"]:
            return False
        if (
            page_state["project_category"] != FILTER_ALL
            and record.get("project_category") != page_state["project_category"]
        ):
            return False
        if page_state["phase"] != FILTER_ALL and page_state["phase"] not in record.get("applicable_phases", []):
            return False
        if page_state["tag"] != FILTER_ALL and page_state["tag"] not in record.get("tags", []):
            return False
        if page_state["level"] != FILTER_ALL and get_record_level(record) != page_state["level"]:
            return False
        if page_state["status"] != FILTER_ALL and record.get("status") != page_state["status"]:
            return False

        keyword = page_state["search_keyword"].lower().strip()
        if keyword:
            searchable = " ".join(
                [
                    record.get("knowledge_id", ""),
                    record.get("title", ""),
                    record.get("summary", ""),
                    record.get("scene", ""),
                    record.get("analysis", ""),
                    record.get("suggestion", ""),
                    record.get("reference_project", ""),
                    record.get("extra_keywords", ""),
                    record.get("content_type", ""),
                    record.get("domain", ""),
                    record.get("project_category", ""),
                    get_record_level(record),
                    *record.get("applicable_phases", []),
                    *record.get("tags", []),
                    *[
                        str(file_info.get("file_name_suffix", ""))
                        for file_info in record.get("attachments", [])
                        if isinstance(file_info, dict)
                    ],
                ]
            ).lower()
            if keyword not in searchable:
                return False

        return True

    def get_filtered_records() -> list[dict]:
        all_records = db_storage.get_item(DESIGN_KNOWLEDGE_DATA_KEY, {})
        if not isinstance(all_records, dict):
            return []
        records = [merge_with_knowledge_template(record) for record in all_records.values() if isinstance(record, dict)]
        records = [record for record in records if record_matches_filters(record)]
        return sorted(records, key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)

    def render_badges(record: dict) -> None:
        ui.badge(record.get("content_type", ""), color=get_type_color(record.get("content_type", ""))).props("outline")
        level = get_record_level(record)
        if level:
            ui.badge(level, color=get_level_color(level)).props("outline")
        ui.badge(record.get("domain", ""), color="blue-grey").props("outline")
        ui.badge(record.get("status", ""), color=get_status_color(record.get("status", ""))).props("outline")

    async def change_record_status(knowledge_id: str, target_status: str) -> None:
        if target_status not in RECORD_STATUSES:
            ui.notify("目标状态无效", type="warning", position="bottom")
            return

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = {"changed": False, "code": "not_found", "record": None, "previous_status": ""}

        def update_all_records(all_records: Any) -> Any:
            records = all_records if isinstance(all_records, dict) else {}
            current = records.get(knowledge_id)
            if not isinstance(current, dict):
                return db_storage.ATOMIC_NO_UPDATE
            record_data = merge_with_knowledge_template(current)
            allowed_targets = {
                RECORD_STATUS_REVIEW: {RECORD_STATUS_PUBLISHED, RECORD_STATUS_RETURNED},
                RECORD_STATUS_PUBLISHED: {RECORD_STATUS_INACTIVE},
                RECORD_STATUS_INACTIVE: {RECORD_STATUS_PUBLISHED},
            }
            if target_status not in allowed_targets.get(record_data.get("status"), set()):
                result["code"] = "invalid_transition"
                return db_storage.ATOMIC_NO_UPDATE
            if not can_manage_record_status(record_data, current_user, current_role):
                result["code"] = "forbidden"
                return db_storage.ATOMIC_NO_UPDATE
            result["previous_status"] = record_data.get("status", "")
            record_data["status"] = target_status
            record_data["_revision"] = int(record_data.get("_revision", 0)) + 1
            record_data["updated_by"] = current_user
            record_data["updated_at"] = now_str
            record_data["operation_log"].append(
                {"user": current_user, "role": current_role, "action": f"状态调整为{target_status}", "time": now_str}
            )
            records[knowledge_id] = record_data
            result["changed"] = True
            result["code"] = "updated"
            result["record"] = copy.deepcopy(record_data)
            return records

        success = await db_storage.atomic_deep_update([DESIGN_KNOWLEDGE_DATA_KEY], update_all_records)
        if result["code"] == "forbidden":
            ui.notify("当前用户没有该知识的审核或状态管理权限", type="warning", position="bottom")
            return
        if result["code"] == "invalid_transition":
            ui.notify("知识状态已经变化，请刷新后重试", type="warning", position="bottom")
            return
        if not success or not result["changed"]:
            ui.notify("状态调整失败，请刷新后重试", type="warning", position="bottom")
            return
        await db_storage.set_item(DESIGN_KNOWLEDGE_VERSION_KEY, time.time())
        if result["previous_status"] == RECORD_STATUS_REVIEW and result["record"]:
            _complete_workflow_assignment(result["record"], current_user)
        ui.notify("状态已更新", type="positive", position="bottom")
        detail_dialog.close()
        refresh_list()

    def open_delete_confirmation(record: dict) -> None:
        """打开删除确认框；真正删除时仍会再次校验 admin 权限。"""
        if not can_delete_record:
            ui.notify("当前账号无删除知识权限", type="warning", position="bottom")
            return

        target_knowledge_id = record.get("knowledge_id", "")
        target_title = record.get("title") or "未命名知识"
        if not target_knowledge_id:
            ui.notify("知识编号无效，无法删除", type="warning", position="bottom")
            return

        async def confirm_delete() -> None:
            changed, code = await delete_knowledge_record(target_knowledge_id, current_user, current_role)
            if code == "forbidden":
                ui.notify("当前账号无删除知识权限", type="warning", position="bottom")
                return
            if code == "not_found":
                ui.notify("该知识已被删除", type="warning", position="bottom")
                delete_dialog.close()
                detail_dialog.close()
                refresh_list()
                return
            if not changed:
                ui.notify("知识删除失败，请刷新后重试", type="negative", position="bottom")
                return

            ui.notify(f"知识 {target_knowledge_id} 已删除", type="positive", position="bottom")
            delete_dialog.close()
            detail_dialog.close()
            refresh_list()

        delete_dialog.clear()
        with delete_dialog, ui.card().classes("w-1/3 max-w-md p-5"):
            ui.label("确认删除知识").classes("text-lg font-bold text-red-700")
            ui.label(f"知识编号：{target_knowledge_id}").classes("font-mono font-bold text-gray-800")
            ui.label(target_title).classes("text-sm font-bold text-gray-700")
            ui.label("删除后将无法从页面恢复，请确认该知识确实需要删除。").classes("text-sm text-gray-600")
            with ui.row().classes("w-full justify-end gap-3 mt-3"):
                ui.button("取消", on_click=delete_dialog.close).props("outline color=grey")
                ui.button("确认删除", icon="delete_forever", on_click=confirm_delete).props("color=negative")
        delete_dialog.open()

    def open_detail_dialog(knowledge_id: str) -> None:
        all_records = db_storage.get_item(DESIGN_KNOWLEDGE_DATA_KEY, {})
        record = (
            merge_with_knowledge_template(all_records.get(knowledge_id, {})) if isinstance(all_records, dict) else None
        )
        if not record or not record.get("knowledge_id"):
            ui.notify("未找到该知识记录", type="warning", position="bottom")
            return

        detail_dialog.clear()
        with detail_dialog, ui.card().classes("w-[900px] max-w-[95vw] max-h-[88vh] p-0 overflow-hidden"):
            with ui.row().classes("w-full justify-between items-center bg-slate-50 border-b px-5 py-3"):
                with ui.column().classes("gap-1 min-w-0"):
                    ui.label(record.get("title") or "未命名知识").classes("text-xl font-bold text-gray-800")
                    with ui.row().classes("gap-2 flex-wrap"):
                        render_badges(record)
                        ui.badge(record.get("knowledge_id", ""), color="grey").props("outline")
                ui.button(icon="close", on_click=detail_dialog.close).props("flat round dense")

            with ui.element("div").classes("w-full overflow-y-auto p-5"):

                def detail_field(label: str, value: str, *, emphasize: bool = False) -> None:
                    with ui.row().classes("w-full items-start gap-2"):
                        ui.label(f"{label}：").classes("w-20 shrink-0 text-sm font-bold text-gray-600")
                        ui.label(value or "-").classes(
                            "text-sm text-gray-800 whitespace-pre-wrap font-bold"
                            if emphasize
                            else "text-sm text-gray-700 whitespace-pre-wrap"
                        )

                level_label = get_record_level(record)
                with ui.column().classes("w-full gap-3 mb-4 border border-gray-200 rounded-md p-4 bg-gray-50"):
                    ui.label("基础信息").classes("text-sm font-bold text-gray-800")
                    with ui.grid().classes("w-full grid-cols-1 md:grid-cols-2 gap-x-8 gap-y-2"):
                        detail_field("内容类型", record.get("content_type", ""), emphasize=True)
                        detail_field("专业领域", record.get("domain", ""), emphasize=True)
                        detail_field("等级", level_label, emphasize=True)
                        detail_field("状态", record.get("status", ""), emphasize=True)
                        if record.get("status") == RECORD_STATUS_REVIEW:
                            detail_field("审批人", _workflow_approver_text(record))
                        detail_field("适用对象", record.get("project_category", ""))
                        detail_field("适用环节", "、".join(record.get("applicable_phases", [])))
                        detail_field("受控标签", "、".join(record.get("tags", [])))
                        detail_field("关联项目", record.get("reference_project", ""))
                        detail_field("创建人", record.get("created_by", ""))
                        detail_field("更新时间", record.get("updated_at", ""))

                copy_text = get_content_type_copy(
                    option_text_in(record.get("content_type"), CONTENT_TYPES, CONTENT_TYPES[0])
                )
                sections = [
                    (copy_text["summary_label"], record.get("summary", "")),
                    (copy_text["scene_label"], record.get("scene", "")),
                    (copy_text["analysis_label"], record.get("analysis", "")),
                    (copy_text["suggestion_label"], record.get("suggestion", "")),
                ]
                for title, content in sections:
                    with ui.column().classes("w-full gap-1 mb-4"):
                        ui.label(title).classes("text-sm font-bold text-gray-700")
                        ui.label(content or "暂无内容").classes(
                            "w-full whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 border border-gray-100 rounded-md p-3"
                        )

                active_attachments = get_active_attachments(record)
                if active_attachments:
                    with ui.column().classes("w-full gap-2 mb-4"):
                        ui.label("附件").classes("text-sm font-bold text-gray-700")
                        with ui.row().classes("w-full flex-wrap items-start gap-2"):
                            for file_info in active_attachments:
                                file_url = file_info.get("file_url", "")
                                if file_url:
                                    file_path = get_upload_local_path(file_url)
                                    if os.path.exists(file_path):
                                        app.add_static_file(local_file=file_path, url_path=file_url)
                                FileThumbnail(
                                    file_url=file_url,
                                    file_type=file_info.get("file_type", "application/octet-stream"),
                                    file_name_suffix=file_info.get(
                                        "file_name_suffix",
                                        file_info.get("file_name", "附件"),
                                    ),
                                    file_lab=file_info.get("file_lab", ""),
                                    display_lab=file_info.get("file_lab", ""),
                                    parents_h=int(file_info.get("parents_h", DESIGN_ATTACHMENT_PARENTS_H)),
                                    delet_lab=False,
                                )

                if record.get("extra_keywords"):
                    ui.label(f"补充关键词：{record.get('extra_keywords')}").classes("text-xs text-gray-500")

            with ui.row().classes("w-full justify-end gap-2 border-t px-5 py-3 bg-white"):
                can_review_current_record = can_review_submission(record, current_user, current_role)
                can_manage_current_status = can_manage_record_status(record, current_user, current_role)
                if can_edit_record(record, current_user, current_role):

                    def edit_current_record(_=None, record_data=record) -> None:
                        detail_dialog.close()
                        open_edit_dialog(record_data)

                    ui.button("编辑", icon="edit", on_click=edit_current_record).props("color=primary")
                if can_review_current_record and record.get("status") == RECORD_STATUS_REVIEW:

                    async def approve_current_record(_=None, k=record["knowledge_id"]):
                        await change_record_status(k, RECORD_STATUS_PUBLISHED)

                    async def return_current_record(_=None, k=record["knowledge_id"]):
                        await change_record_status(k, RECORD_STATUS_RETURNED)

                    ui.button("审核通过", icon="check_circle", on_click=approve_current_record).props("color=green")
                    ui.button("退回修改", icon="reply", on_click=return_current_record).props("outline color=orange")
                elif can_manage_current_status and record.get("status") == RECORD_STATUS_PUBLISHED:

                    async def deactivate_current_record(_=None, k=record["knowledge_id"]):
                        await change_record_status(k, RECORD_STATUS_INACTIVE)

                    ui.button(
                        "标记不再适用",
                        icon="archive",
                        on_click=deactivate_current_record,
                    ).props("outline color=grey")
                elif can_manage_current_status and record.get("status") == RECORD_STATUS_INACTIVE:

                    async def restore_current_record(_=None, k=record["knowledge_id"]):
                        await change_record_status(k, RECORD_STATUS_PUBLISHED)

                    ui.button(
                        "恢复发布",
                        icon="unarchive",
                        on_click=restore_current_record,
                    ).props("outline color=primary")
                if can_delete_record:
                    ui.button(
                        "删除知识",
                        icon="delete_forever",
                        on_click=lambda _=None, r=record: open_delete_confirmation(r),
                    ).props("outline color=negative")
                ui.button("关闭", on_click=detail_dialog.close).props("outline")

        detail_dialog.open()

    def open_tag_request_dialog(default_domain: str = DESIGN_DOMAINS[0]) -> None:
        if not (can_edit_own_record or can_add_record):
            ui.notify("当前账号无申请新标签权限", type="warning", position="bottom")
            return
        form_data = {
            "domain": default_domain if default_domain in DESIGN_DOMAINS else DESIGN_DOMAINS[0],
            "tag_name": "",
            "reason": "",
        }
        tag_request_dialog.clear()
        with tag_request_dialog, ui.card().classes("w-[480px] max-w-[95vw]"):
            ui.label("申请新标签").classes("text-lg font-bold text-gray-800")
            ui.select(DESIGN_DOMAINS, label="专业领域", value=form_data["domain"]).bind_value(
                form_data, "domain"
            ).props("outlined dense").classes("w-full")
            ui.input("标签名称", value=form_data["tag_name"]).bind_value(form_data, "tag_name").props(
                "outlined dense"
            ).classes("w-full")
            ui.textarea("申请理由", value=form_data["reason"]).bind_value(form_data, "reason").props(
                "outlined rows=3"
            ).classes("w-full")

            async def handle_submit_request():
                success, message = await submit_tag_request(
                    form_data["domain"],
                    form_data["tag_name"],
                    form_data["reason"],
                    current_user,
                    current_role,
                )
                ui.notify(message, type="positive" if success else "warning", position="bottom")
                if success:
                    tag_request_dialog.close()

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("提交申请", icon="send", on_click=handle_submit_request).props("color=primary")
                ui.button("取消", on_click=tag_request_dialog.close).props("outline")

        tag_request_dialog.open()

    def open_tag_manager_dialog() -> None:
        if not can_access_tag_manager:
            ui.notify("当前账号无标签管理或标签审批权限", type="warning", position="bottom")
            return
        manager_state = {"domain": DESIGN_DOMAINS[0], "new_tag": ""}
        tag_manager_dialog.clear()
        with tag_manager_dialog, ui.card().classes("w-[820px] max-w-[95vw] max-h-[88vh] p-0 overflow-hidden"):
            with ui.row().classes("w-full justify-between items-center bg-slate-50 border-b px-5 py-3"):
                ui.label("标签管理").classes("text-lg font-bold text-gray-800")
                ui.button(icon="close", on_click=tag_manager_dialog.close).props("flat round dense")

            with ui.element("div").classes("w-full overflow-y-auto p-5"):
                with ui.row().classes("w-full items-end gap-3 mb-4"):
                    domain_select = (
                        ui.select(DESIGN_DOMAINS, label="专业领域", value=manager_state["domain"])
                        .props("outlined dense")
                        .classes("w-40")
                    )
                    if can_manage_tags:
                        new_tag_input = (
                            ui.input("新增正式标签", value=manager_state["new_tag"])
                            .props("outlined dense")
                            .classes("w-56")
                        )

                        async def handle_add_tag():
                            if not can_manage_tags:
                                ui.notify("当前账号无直接维护标签库权限", type="warning", position="bottom")
                                return
                            tag_name = normalize_text(new_tag_input.value)
                            domain = option_text_in(domain_select.value, DESIGN_DOMAINS, DESIGN_DOMAINS[0])
                            if not tag_name:
                                ui.notify("请输入标签名称", type="warning", position="bottom")
                                return
                            catalog = get_tag_catalog()
                            if tag_name in catalog.get(domain, []):
                                ui.notify("该标签已存在", type="info", position="bottom")
                                return
                            catalog[domain].append(tag_name)
                            await save_tag_catalog(catalog)
                            new_tag_input.value = ""
                            ui.notify("标签已加入受控标签库", type="positive", position="bottom")
                            render_tag_manager()

                        ui.button("加入标签库", icon="add", on_click=handle_add_tag).props("color=primary")

                tag_list_container = ui.column().classes("w-full gap-2")
                request_container = ui.column().classes("w-full gap-2 mt-5")

                def render_tag_manager() -> None:
                    tag_list_container.clear()
                    request_container.clear()
                    domain = option_text_in(domain_select.value, DESIGN_DOMAINS, DESIGN_DOMAINS[0])
                    with tag_list_container:
                        ui.label(f"{domain}标签").classes("text-sm font-bold text-gray-700")
                        with ui.row().classes("gap-2 flex-wrap"):
                            for tag in get_domain_tags(domain):
                                ui.chip(tag, icon="sell", color="blue").props("dense outline")

                    requests = db_storage.get_item(DESIGN_TAG_REQUESTS_KEY, {})
                    pending_requests = (
                        [
                            request
                            for request in requests.values()
                            if (
                                isinstance(request, dict)
                                and request.get("status") == "待审核"
                                and can_review_submission(
                                    request,
                                    current_user,
                                    current_role,
                                    submission_type="tag",
                                )
                            )
                        ]
                        if isinstance(requests, dict)
                        else []
                    )
                    pending_requests = sorted(pending_requests, key=lambda item: item.get("created_at", ""))
                    with request_container:
                        ui.separator()
                        ui.label("分配给我的待审核标签申请").classes("text-sm font-bold text-gray-700")
                        if not pending_requests:
                            ui.label("暂无待审核标签申请").classes("text-sm text-gray-500")
                            return
                        for request in pending_requests:
                            with ui.row().classes(
                                "w-full items-center justify-between gap-3 border border-gray-100 rounded-md p-3"
                            ):
                                with ui.column().classes("gap-1 min-w-0"):
                                    with ui.row().classes("gap-2 items-center"):
                                        ui.badge(request.get("domain", ""), color="blue-grey").props("outline")
                                        ui.label(request.get("tag_name", "")).classes("font-bold text-gray-800")
                                    ui.label(
                                        f"申请人：{request.get('created_by', '-')}, 理由：{request.get('reason', '-') or '-'}"
                                    ).classes("text-xs text-gray-500")

                                request_id = option_text(request.get("request_id"))

                                async def approve(_=None, req_id=request_id):
                                    success, message = await update_tag_request_status(
                                        req_id, "已通过", current_user, current_role
                                    )
                                    ui.notify(message, type="positive" if success else "warning", position="bottom")
                                    render_tag_manager()

                                async def reject(_=None, req_id=request_id):
                                    success, message = await update_tag_request_status(
                                        req_id, "已驳回", current_user, current_role
                                    )
                                    ui.notify(message, type="positive" if success else "warning", position="bottom")
                                    render_tag_manager()

                                with ui.row().classes("gap-2 shrink-0"):
                                    ui.button("通过", icon="check", on_click=approve).props("dense color=green")
                                    ui.button("驳回", icon="close", on_click=reject).props("dense outline color=grey")

                domain_select.on_value_change(lambda _=None: render_tag_manager())
                render_tag_manager()

        tag_manager_dialog.open()

    def open_edit_dialog(record: Optional[dict] = None) -> None:
        source_record = merge_with_knowledge_template(record) if record else get_design_knowledge_template()
        if record and not can_edit_record(source_record, current_user, current_role):
            ui.notify("当前账号无权编辑这条设计知识", type="warning", position="bottom")
            return
        if not record and not can_add_record:
            ui.notify("当前账号无录入设计知识权限", type="warning", position="bottom")
            return
        if not record:
            source_record["created_by"] = current_user
            source_record["created_role"] = current_role
        form_data = copy.deepcopy(source_record)
        project_hierarchy = build_project_model_hierarchy(app.storage.general.get("project_summary", {}))

        edit_dialog.clear()
        with edit_dialog, ui.card().classes("w-[980px] max-w-[96vw] max-h-[90vh] p-0 overflow-hidden"):
            with ui.row().classes("w-full justify-between items-center bg-slate-50 border-b px-5 py-3"):
                ui.label("新增设计知识" if not record else "编辑设计知识").classes("text-lg font-bold text-gray-800")
                ui.button(icon="close", on_click=edit_dialog.close).props("flat round dense")

            with ui.element("div").classes("w-full overflow-y-auto p-5"):
                with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 gap-3 mb-3"):
                    content_type_select = (
                        ui.select(CONTENT_TYPES, label="内容类型", value=form_data["content_type"])
                        .props("outlined dense")
                        .classes("w-full")
                    )
                    domain_select = (
                        ui.select(DESIGN_DOMAINS, label="专业领域", value=form_data["domain"])
                        .props("outlined dense")
                        .classes("w-full")
                    )
                    ui.select(PROJECT_CATEGORIES, label="适用对象", value=form_data["project_category"]).bind_value(
                        form_data, "project_category"
                    ).props("outlined dense").classes("w-full")

                with ui.grid().classes("w-full grid-cols-1 md:grid-cols-3 gap-3 mb-3"):
                    level_container = ui.column().classes("w-full")
                    phase_select = (
                        ui.select(
                            APPLICABLE_PHASES,
                            label="适用环节",
                            value=form_data["applicable_phases"],
                            multiple=True,
                        )
                        .props("outlined dense use-chips")
                        .classes("w-full")
                    )
                    with ui.column().classes("w-full gap-1"):
                        ui.label("当前状态").classes("text-xs text-gray-500")
                        ui.badge(form_data["status"], color=get_status_color(form_data["status"])).props("outline")

                with ui.row().classes("w-full items-end gap-3 mb-2"):
                    initial_domain = option_text_in(form_data.get("domain"), DESIGN_DOMAINS, DESIGN_DOMAINS[0])
                    tag_select = (
                        ui.select(
                            get_domain_tags(initial_domain),
                            label="受控标签",
                            value=form_data["tags"],
                            multiple=True,
                        )
                        .props("outlined dense use-chips")
                        .classes("flex-grow")
                    )
                    ui.button(
                        "申请新标签",
                        icon="new_label",
                        on_click=lambda _=None: open_tag_request_dialog(
                            option_text_in(domain_select.value, DESIGN_DOMAINS, DESIGN_DOMAINS[0])
                        ),
                    ).props("outline color=primary")

                body_field_container = ui.column().classes("w-full gap-0")

                def render_body_fields() -> None:
                    body_field_container.clear()
                    selected_content_type = option_text_in(content_type_select.value, CONTENT_TYPES, CONTENT_TYPES[0])
                    copy_text = get_content_type_copy(selected_content_type)
                    with body_field_container:
                        ui.input(
                            "标题",
                            value=form_data["title"],
                            placeholder=copy_text["title_hint"],
                        ).bind_value(form_data, "title").props("outlined dense autofocus").classes("w-full mb-3")
                        # ui.label(copy_text["title_hint"]).classes("text-xs text-gray-500 mb-3")
                        ui.textarea(
                            copy_text["summary_label"],
                            value=form_data["summary"],
                            placeholder=copy_text["summary_placeholder"],
                        ).bind_value(form_data, "summary").props("outlined rows=2").classes("w-full mb-3")
                        ui.textarea(
                            copy_text["scene_label"],
                            value=form_data["scene"],
                            placeholder=copy_text["scene_placeholder"],
                        ).bind_value(form_data, "scene").props("outlined rows=3").classes("w-full mb-3")
                        ui.textarea(
                            copy_text["analysis_label"],
                            value=form_data["analysis"],
                            placeholder=copy_text["analysis_placeholder"],
                        ).bind_value(form_data, "analysis").props("outlined rows=3").classes("w-full mb-3")
                        ui.textarea(
                            copy_text["suggestion_label"],
                            value=form_data["suggestion"],
                            placeholder=copy_text["suggestion_placeholder"],
                        ).bind_value(form_data, "suggestion").props("outlined rows=4").classes("w-full mb-3")

                def initialize_attachment_state() -> None:
                    normalized_files = []
                    used_labels = set()
                    max_label = 0
                    for file_info in get_active_attachments(form_data):
                        file_lab = str(file_info.get("file_lab", "")).strip()
                        if not file_lab or file_lab in used_labels:
                            max_label += 1
                            while str(max_label) in used_labels:
                                max_label += 1
                            file_lab = str(max_label)
                        else:
                            try:
                                max_label = max(max_label, int(file_lab))
                            except (TypeError, ValueError):
                                pass
                        used_labels.add(file_lab)
                        file_info["file_lab"] = file_lab
                        file_info["thumbnail_key"] = get_design_attachment_thumbnail_key(file_lab)
                        normalized_files.append(file_info)

                    form_data["attachments"] = sorted(normalized_files, key=get_attachment_label_number)
                    app.storage.client["file_thumbnail_dic"] = {}
                    app.storage.client["files"] = [
                        file_info.get("file_name_hash", "")
                        for file_info in form_data["attachments"]
                        if file_info.get("file_name_hash")
                    ]
                    app.storage.client["deleted_files"] = []
                    app.storage.client["file_counter"] = max_label
                    app.storage.client["design_attachment_counter"] = max_label
                    app.storage.client["ref_question_dic"] = {}
                    app.storage.client.setdefault("page_elements", {})

                def sync_attachments_from_thumbnail_state() -> list[dict]:
                    thumbnail_dic = app.storage.client.get("file_thumbnail_dic", {})
                    deleted_files = set(app.storage.client.get("deleted_files", []))
                    attachments = []
                    for entry in thumbnail_dic.values():
                        if not isinstance(entry, dict):
                            continue
                        file_info = copy.deepcopy(entry.get("file_information", {}))
                        if not file_info or file_info.get("file_del_bool"):
                            continue
                        if file_info.get("file_name_hash") in deleted_files:
                            continue
                        file_info.pop("thumbnail_key", None)
                        attachments.append(file_info)
                    form_data["attachments"] = sorted(attachments, key=get_attachment_label_number)
                    return form_data["attachments"]

                def create_attachment_thumbnail(file_info: dict, deletable: bool) -> FileThumbnail:
                    display_lab = str(file_info.get("file_lab", ""))
                    thumbnail_key = file_info.get("thumbnail_key") or get_design_attachment_thumbnail_key(display_lab)
                    file_info["thumbnail_key"] = thumbnail_key
                    file_url = file_info.get("file_url", "")
                    if file_url:
                        file_path = get_upload_local_path(file_url)
                        if os.path.exists(file_path):
                            app.add_static_file(local_file=file_path, url_path=file_url)
                    thumbnail = FileThumbnail(
                        file_url=file_url,
                        file_type=file_info.get("file_type", "application/octet-stream"),
                        file_name_suffix=file_info.get("file_name_suffix", file_info.get("file_name", "附件")),
                        file_lab=thumbnail_key,
                        display_lab=display_lab,
                        parents_h=int(file_info.get("parents_h", DESIGN_ATTACHMENT_PARENTS_H)),
                        delet_lab=deletable,
                    )
                    app.storage.client["file_thumbnail_dic"][thumbnail.file_index] = {
                        "file_obj": thumbnail,
                        "file_information": copy.deepcopy(file_info),
                    }
                    return thumbnail

                async def handle_attachment_file_upload(e, parents_h: int):
                    try:
                        file_type = e.file.content_type or "application/octet-stream"
                        content = await e.file.read()
                        selected_content_type = option_text_in(
                            content_type_select.value, CONTENT_TYPES, CONTENT_TYPES[0]
                        )
                        file_name, file_suffix, file_name_hash = get_design_attachment_file_hash_name(
                            selected_content_type,
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

                        target_path, url_path = get_design_attachment_storage_paths(
                            selected_content_type,
                            current_user,
                            file_name_hash,
                        )
                        os.makedirs(os.path.dirname(target_path), exist_ok=True)
                        if not os.path.isfile(target_path):
                            with open(target_path, "wb") as uploaded_file:
                                uploaded_file.write(content)

                        app.add_static_file(local_file=target_path, url_path=url_path)
                        active_attachment_hashes = get_active_attachment_hashes_from_thumbnail_state(
                            app.storage.client.get("file_thumbnail_dic", {})
                        )
                        if (
                            file_name_hash in active_attachment_hashes
                            or quote_url_component(file_name_hash) in active_attachment_hashes
                        ):
                            return ui.notify(f"文件已存在：{e.file.name}", type="warning", position="bottom")

                        tracked_files = app.storage.client.setdefault("files", [])
                        if file_name_hash not in tracked_files:
                            tracked_files.append(file_name_hash)
                        next_file_lab = int(app.storage.client.get("design_attachment_counter", 0)) + 1
                        app.storage.client["design_attachment_counter"] = next_file_lab
                        app.storage.client["file_counter"] = max(
                            int(app.storage.client.get("file_counter", 0)),
                            next_file_lab,
                        )
                        file_lab = str(next_file_lab)
                        deleted_files = app.storage.client.setdefault("deleted_files", [])
                        for deleted_file in {file_name_hash, quote_url_component(file_name_hash)}:
                            while deleted_file in deleted_files:
                                deleted_files.remove(deleted_file)

                        file_info = {
                            "thumbnail_key": get_design_attachment_thumbnail_key(file_lab),
                            "file_del_bool": False,
                            "file_name": file_name,
                            "file_url": url_path,
                            "file_name_hash": file_name_hash,
                            "file_name_suffix": e.file.name,
                            "file_type": file_type,
                            "file_lab": file_lab,
                            "parents_h": parents_h,
                            "content_type": selected_content_type,
                            "uploaded_by": current_user,
                            "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        attachment_row = app.storage.client["page_elements"].get("design_knowledge_attachment_row")
                        if attachment_row is None:
                            return ui.notify("附件区域尚未初始化，请关闭窗口后重试", type="warning", position="bottom")
                        with attachment_row:
                            create_attachment_thumbnail(file_info, deletable=True)
                        sync_attachments_from_thumbnail_state()
                        ui.notify("附件已添加，请保存知识记录", type="positive", position="bottom")
                    except Exception as exc:
                        logger.exception("设计知识附件上传失败")
                        ui.notify(
                            f"上传文件 '{e.file.name}' 失败：{exc}", type="negative", position="bottom", multi_line=True
                        )

                def render_attachment_section() -> None:
                    active_files = get_active_attachments(form_data)
                    with ui.column().classes("w-full gap-2 border border-gray-100 rounded-md p-3 mb-3 bg-gray-50"):
                        ui.label("附件").classes("text-sm font-bold text-gray-700")
                        ui.label("可上传图片、PDF、Office 等资料作为补充说明。").classes("text-xs text-gray-500")
                        with ui.row().classes("w-full flex-wrap items-start gap-2") as attachment_row:
                            app.storage.client["page_elements"]["design_knowledge_attachment_row"] = attachment_row
                            ButtonUploader(
                                on_upload=handle_attachment_file_upload,
                                label="上传附件",
                                input_any_suffix=DESIGN_ATTACHMENT_ACCEPT,
                                classes_str=f"h-{DESIGN_ATTACHMENT_PARENTS_H}",
                                props_str="outline color=primary dense",
                                parents_h=DESIGN_ATTACHMENT_PARENTS_H,
                            )
                            for file_info in active_files:
                                create_attachment_thumbnail(file_info, deletable=True)

                initialize_attachment_state()
                render_body_fields()
                render_attachment_section()

                with ui.column().classes("w-full gap-2 border border-gray-100 rounded-md p-3 mb-3 bg-gray-50"):
                    ui.label("关联项目").classes("text-sm font-bold text-gray-700")
                    ui.label("不关联具体项目时可以留空。").classes("text-xs text-gray-500")
                    reference_chip_container = ui.row().classes("w-full gap-2 flex-wrap")

                    def render_reference_project_chips() -> None:
                        reference_chip_container.clear()
                        form_data["reference_projects"] = unique_texts(form_data.get("reference_projects", []))
                        with reference_chip_container:
                            if not form_data["reference_projects"]:
                                ui.label("尚未关联项目").classes("text-xs text-gray-400")
                            for project_name in form_data["reference_projects"]:
                                with ui.chip(color="primary", text_color="white").classes("gap-1 items-center"):
                                    ui.label(project_name)
                                    ui.icon("close", size="16px").classes("cursor-pointer").on(
                                        "click",
                                        lambda _, p=project_name: (
                                            form_data["reference_projects"].remove(p)
                                            if p in form_data["reference_projects"]
                                            else None,
                                            render_reference_project_chips(),
                                        ),
                                    )

                    if project_hierarchy:
                        project_select_state = {"l1": "", "l2": "", "project": ""}

                        def handle_project_l1_change(e) -> None:
                            selected_l1 = option_text(e.value)
                            project_select_state["l1"] = selected_l1
                            project_select_state["l2"] = ""
                            project_select_state["project"] = ""
                            level_2_options = list(project_hierarchy.get(selected_l1, {}).keys()) if selected_l1 else []
                            project_l2_select.set_options(level_2_options)
                            project_l2_select.set_value(None)
                            project_model_select.set_options({})
                            project_model_select.set_value(None)

                        def handle_project_l2_change(e) -> None:
                            selected_l1 = project_select_state["l1"]
                            selected_l2 = option_text(e.value)
                            project_select_state["l2"] = selected_l2
                            project_select_state["project"] = ""
                            model_options = (
                                project_hierarchy.get(selected_l1, {}).get(selected_l2, {})
                                if selected_l1 and selected_l2
                                else {}
                            )
                            project_model_select.set_options(model_options)
                            project_model_select.set_value(None)

                        def add_reference_project() -> None:
                            project_name = project_select_state.get("project")
                            if not project_name:
                                ui.notify("请先选择具体型号后再添加", type="warning", position="bottom")
                                return
                            if project_name in form_data["reference_projects"]:
                                ui.notify("该项目已在关联列表中", type="info", position="bottom")
                                return
                            form_data["reference_projects"].append(project_name)
                            render_reference_project_chips()

                        with ui.row().classes("w-full items-center gap-2"):
                            ui.select(
                                list(project_hierarchy.keys()),
                                label="大系列",
                                on_change=handle_project_l1_change,
                            ).props("outlined dense").classes("flex-grow")
                            project_l2_select = (
                                ui.select(
                                    [],
                                    label="小系列",
                                    on_change=handle_project_l2_change,
                                )
                                .props("outlined dense")
                                .classes("flex-grow")
                            )
                            project_model_select = (
                                ui.select(
                                    {},
                                    label="具体型号",
                                    on_change=lambda e: project_select_state.update(project=option_text(e.value)),
                                )
                                .props("outlined dense")
                                .classes("flex-grow")
                            )
                            ui.button("添加", icon="add", on_click=add_reference_project).props("outline color=primary")
                    else:
                        ui.label("当前系统尚未加载项目型号，暂时无法关联具体项目。").classes("text-xs text-orange-600")

                    render_reference_project_chips()

                with ui.grid().classes("w-full grid-cols-1 md:grid-cols-2 gap-3"):
                    ui.input(
                        "补充关键词",
                        value=form_data["extra_keywords"],
                        placeholder="可填写未进入正式标签库的临时关键词，用空格分隔",
                    ).bind_value(form_data, "extra_keywords").props("outlined dense").classes("w-full")

                def render_level_field() -> None:
                    level_container.clear()
                    with level_container:
                        content_type = option_text_in(content_type_select.value, CONTENT_TYPES, CONTENT_TYPES[0])
                        if content_type == "设计规范":
                            ui.select(RULE_LEVELS, label="规范等级", value=form_data["rule_level"]).bind_value(
                                form_data, "rule_level"
                            ).props("outlined dense").classes("w-full")
                        elif content_type == "错误案例":
                            ui.select(
                                ERROR_SEVERITY_LEVELS, label="严重等级", value=form_data["severity_level"]
                            ).bind_value(form_data, "severity_level").props("outlined dense").classes("w-full")
                        else:
                            ui.select(
                                PRACTICE_VALUE_LEVELS, label="推荐价值", value=form_data["practice_value"]
                            ).bind_value(form_data, "practice_value").props("outlined dense").classes("w-full")

                def handle_content_type_change() -> None:
                    form_data["content_type"] = option_text_in(
                        content_type_select.value, CONTENT_TYPES, CONTENT_TYPES[0]
                    )
                    render_level_field()
                    render_body_fields()

                def handle_domain_change() -> None:
                    selected_domain = option_text_in(domain_select.value, DESIGN_DOMAINS, DESIGN_DOMAINS[0])
                    form_data["domain"] = selected_domain
                    valid_tags = get_domain_tags(selected_domain)
                    form_data["tags"] = [tag for tag in unique_texts(tag_select.value or []) if tag in valid_tags]
                    tag_select.set_options(valid_tags)
                    tag_select.value = form_data["tags"]

                content_type_select.on_value_change(lambda _=None: handle_content_type_change())
                domain_select.on_value_change(lambda _=None: handle_domain_change())
                phase_select.bind_value(form_data, "applicable_phases")
                tag_select.bind_value(form_data, "tags")
                render_level_field()

            async def handle_save(target_status: Optional[str] = None) -> None:
                form_data["content_type"] = option_text_in(content_type_select.value, CONTENT_TYPES, CONTENT_TYPES[0])
                form_data["domain"] = option_text_in(domain_select.value, DESIGN_DOMAINS, DESIGN_DOMAINS[0])
                form_data["applicable_phases"] = unique_texts(phase_select.value or [])
                form_data["tags"] = unique_texts(tag_select.value or [])
                form_data["reference_projects"] = unique_texts(form_data.get("reference_projects", []))
                form_data["reference_project"] = "、".join(form_data["reference_projects"])
                sync_attachments_from_thumbnail_state()
                if target_status:
                    form_data["status"] = target_status
                if not normalize_text(form_data.get("title")):
                    ui.notify("请填写标题", type="warning", position="bottom")
                    return
                if form_data["content_type"] not in CONTENT_TYPES:
                    ui.notify("请选择内容类型", type="warning", position="bottom")
                    return
                if form_data["domain"] not in DESIGN_DOMAINS:
                    ui.notify("请选择专业领域", type="warning", position="bottom")
                    return
                if not form_data["tags"]:
                    ui.notify("请至少选择一个受控标签", type="warning", position="bottom")
                    return
                if not normalize_text(form_data.get("summary")):
                    ui.notify("请填写摘要，便于列表检索和快速浏览", type="warning", position="bottom")
                    return

                success, message, saved_record = await save_knowledge_record(form_data, current_user, current_role)
                ui.notify(message, type="positive" if success else "warning", position="bottom")
                if success:
                    edit_dialog.close()
                    refresh_list()
                    if saved_record:
                        open_detail_dialog(saved_record["knowledge_id"])

            async def save_as_draft() -> None:
                await handle_save(RECORD_STATUS_DRAFT)

            async def submit_for_review() -> None:
                await handle_save(RECORD_STATUS_REVIEW)

            async def save_published_record() -> None:
                await handle_save(RECORD_STATUS_PUBLISHED)

            async def save_review_record() -> None:
                await handle_save(RECORD_STATUS_REVIEW)

            async def save_inactive_record() -> None:
                await handle_save(RECORD_STATUS_INACTIVE)

            with ui.row().classes("w-full justify-end gap-2 border-t px-5 py-3 bg-white"):
                if form_data["status"] in {RECORD_STATUS_DRAFT, RECORD_STATUS_RETURNED}:
                    ui.button("保存草稿", icon="save", on_click=save_as_draft).props("outline color=primary")
                    ui.button("提交审核", icon="approval", on_click=submit_for_review).props("color=primary")
                elif form_data["status"] == RECORD_STATUS_REVIEW:
                    ui.button("保存修改", icon="save", on_click=save_review_record).props("color=primary")
                elif form_data["status"] == RECORD_STATUS_PUBLISHED:
                    if can_review_submission(form_data, current_user, current_role):
                        ui.button("保存修改", icon="save", on_click=save_published_record).props("color=primary")
                    else:
                        ui.button("提交审核", icon="approval", on_click=submit_for_review).props("color=primary")
                else:
                    ui.button("保存修改", icon="save", on_click=save_inactive_record).props("color=grey")
                ui.button("取消", on_click=edit_dialog.close).props("outline")

        edit_dialog.open()

    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("设计知识库").classes("text-white text-xl font-bold absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {current_user}")
                ui.separator()
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 overflow-hidden bg-gray-50"):
        with ui.column().classes("w-full h-full p-4 gap-4"):
            with ui.column().classes("w-full bg-white p-4 shadow-sm rounded-md gap-3"):
                with ui.row().classes("w-full justify-between items-center gap-3"):
                    with ui.row().classes("gap-3 items-center flex-wrap"):
                        ui.input("搜索标题/摘要/项目/标签", placeholder="输入关键词").props(
                            "dense outlined clearable"
                        ).bind_value(page_state, "search_keyword").classes("w-72")
                        ui.button("查询", icon="search", on_click=lambda _=None: refresh_list()).props(
                            "outline color=primary"
                        )
                    with ui.row().classes("gap-2 items-center"):
                        if can_access_tag_manager:
                            ui.button("标签管理", icon="local_offer", on_click=open_tag_manager_dialog).props(
                                "outline color=primary"
                            )
                        if can_add_record:
                            ui.button("录入知识", icon="add_box", on_click=lambda _=None: open_edit_dialog()).props(
                                "color=primary"
                            )

                with ui.row().classes("w-full gap-3 items-center flex-wrap"):
                    content_type_filter = (
                        ui.select(
                            [FILTER_ALL, *CONTENT_TYPES],
                            label="内容类型",
                            value=page_state["content_type"],
                        )
                        .props("dense outlined")
                        .bind_value(page_state, "content_type")
                        .classes("w-36")
                    )
                    domain_filter = (
                        ui.select([FILTER_ALL, *DESIGN_DOMAINS], label="专业领域", value=page_state["domain"])
                        .props("dense outlined")
                        .bind_value(page_state, "domain")
                        .classes("w-36")
                    )
                    ui.select(
                        [FILTER_ALL, *PROJECT_CATEGORIES], label="适用对象", value=page_state["project_category"]
                    ).props("dense outlined").bind_value(page_state, "project_category").classes("w-44")
                    ui.select([FILTER_ALL, *APPLICABLE_PHASES], label="适用环节", value=page_state["phase"]).props(
                        "dense outlined"
                    ).bind_value(page_state, "phase").classes("w-40")
                    tag_filter = (
                        ui.select([FILTER_ALL, *get_available_filter_tags()], label="标签", value=page_state["tag"])
                        .props("dense outlined")
                        .bind_value(page_state, "tag")
                        .classes("w-40")
                    )
                    level_filter = (
                        ui.select(
                            [
                                FILTER_ALL,
                                *get_level_options_for_content_type(
                                    option_text_in(
                                        page_state.get("content_type"), [FILTER_ALL, *CONTENT_TYPES], FILTER_ALL
                                    )
                                ),
                            ],
                            label="等级",
                            value=page_state["level"],
                        )
                        .props("dense outlined")
                        .bind_value(page_state, "level")
                        .classes("w-36")
                    )
                    if can_view_workflow_records:
                        ui.select([FILTER_ALL, *RECORD_STATUSES], label="状态", value=page_state["status"]).props(
                            "dense outlined"
                        ).bind_value(page_state, "status").classes("w-36")

                    def handle_domain_filter_change() -> None:
                        page_state["domain"] = option_text_in(
                            domain_filter.value, [FILTER_ALL, *DESIGN_DOMAINS], FILTER_ALL
                        )
                        valid_options = [FILTER_ALL, *get_available_filter_tags()]
                        if page_state["tag"] not in valid_options:
                            page_state["tag"] = FILTER_ALL
                        tag_filter.set_options(valid_options)
                        tag_filter.value = page_state["tag"]
                        refresh_list()

                    def handle_content_type_filter_change() -> None:
                        selected_content_type = option_text_in(
                            content_type_filter.value,
                            [FILTER_ALL, *CONTENT_TYPES],
                            FILTER_ALL,
                        )
                        page_state["content_type"] = selected_content_type
                        valid_level_options = [FILTER_ALL, *get_level_options_for_content_type(selected_content_type)]
                        if page_state["level"] not in valid_level_options:
                            page_state["level"] = FILTER_ALL
                        level_filter.set_options(valid_level_options)
                        level_filter.value = page_state["level"]
                        refresh_list()

                    content_type_filter.on_value_change(lambda _=None: handle_content_type_filter_change())
                    domain_filter.on_value_change(lambda _=None: handle_domain_filter_change())

            with ui.element("div").classes("w-full flex-grow overflow-y-auto overflow-x-hidden p-1"):
                list_container = ui.column().classes("w-full gap-3")

                def refresh_list() -> None:
                    list_container.clear()
                    records = get_filtered_records()

                    with list_container:
                        with ui.row().classes("w-full justify-between items-center px-1"):
                            ui.label(f"共 {len(records)} 条知识").classes("text-sm text-gray-500")
                            if page_state["status"] != RECORD_STATUS_PUBLISHED and not can_view_workflow_records:
                                ui.label("当前仅显示已发布内容").classes("text-xs text-gray-400")

                        if not records:
                            ui.label("没有符合筛选条件的设计知识").classes("text-gray-500 m-auto mt-10")
                            return

                        for record in records:
                            level = get_record_level(record)
                            with ui.element("div").classes(
                                "w-full bg-white border border-gray-200 border-l-4 rounded-md p-4 shadow-sm "
                                "hover:bg-sky-50 cursor-pointer transition-colors"
                            ) as card:
                                card.style(f"border-left-color: {get_level_border_color(level)}")
                                card.on("click", lambda _, k_id=record["knowledge_id"]: open_detail_dialog(k_id))
                                with ui.row().classes("w-full justify-between items-start gap-4"):
                                    with ui.column().classes("gap-2 min-w-0 flex-grow"):
                                        with ui.row().classes("items-center gap-2 flex-wrap"):
                                            ui.label(record.get("title") or "未命名知识").classes(
                                                "text-lg font-bold text-gray-800"
                                            )
                                            render_badges(record)
                                        ui.label(record.get("summary", "") or "暂无摘要").classes(
                                            "text-sm text-gray-600 knowledge-clamp-2"
                                        )
                                        with ui.row().classes("gap-2 flex-wrap"):
                                            for tag in record.get("tags", [])[:6]:
                                                ui.chip(tag, icon="sell", color="blue").props("dense outline size=sm")
                                            for phase in record.get("applicable_phases", [])[:4]:
                                                ui.chip(phase, icon="flag", color="teal").props("dense outline size=sm")
                                    with ui.column().classes("items-end gap-1 shrink-0 text-xs text-gray-500"):
                                        ui.label(record.get("knowledge_id", ""))
                                        ui.label(f"适用对象：{record.get('project_category') or '-'}")
                                        ui.label(f"更新：{record.get('updated_at') or '-'}")
                                        ui.label(f"作者：{record.get('created_by') or '-'}")

                def check_and_refresh_list() -> None:
                    current_stamp = db_storage.get_item(DESIGN_KNOWLEDGE_VERSION_KEY, 0.0)
                    if page_state.get("version_stamp", 0.0) != 0.0 and current_stamp != page_state["version_stamp"]:
                        page_state["version_stamp"] = current_stamp
                        refresh_list()
                    elif page_state.get("version_stamp", 0.0) == 0.0:
                        page_state["version_stamp"] = current_stamp

                refresh_list()
                ui.timer(5.0, check_and_refresh_list)


def get_level_border_color(level: str) -> str:
    return {
        "规定": "#ef4444",
        "推荐": "#3b82f6",
        "提示": "#64748b",
        "致命": "#dc2626",
        "严重": "#f97316",
        "中等": "#f59e0b",
        "轻度": "#64748b",
        "强推荐": "#22c55e",
        "可参考": "#3b82f6",
        "特定场景适用": "#a855f7",
    }.get(level, "#64748b")
