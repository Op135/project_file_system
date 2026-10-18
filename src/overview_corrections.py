# -*- encoding: utf-8 -*-
"""单项概述原记录纠错的申请、审批执行与历史归档服务。"""

import copy
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from nicegui import app

from . import db_storage
from . import overview_storage
from .overview_file_storage import copy_named_file
from .config import OVER_UPLOADS_FILE_TYPE
from .overview_batch_operations import validate_overview_content
from .overview_change_workflow_config import OVERVIEW_CHANGE_WORKFLOW_CONFIG
from .project_overview_access import (
    can_edit_overview_item,
    can_review_overview_correction,
)

OVERVIEW_CORRECTION_REQUESTS_KEY = "overview_correction_requests"
OVERVIEW_CORRECTION_ARCHIVES_KEY = "overview_correction_archives"
OVERVIEW_CORRECTION_STAGING_DIR = Path(__file__).resolve().parents[1] / ".overview_correction_staging"

CORRECTION_CONFIG = OVERVIEW_CHANGE_WORKFLOW_CONFIG["single_correction"]
CORRECTION_PREVENT_SELF_APPROVAL = CORRECTION_CONFIG["prevent_self_approval"]
CORRECTION_APPROVAL_ROLE_TARGETS = CORRECTION_CONFIG["approval_role_targets"]

TEST_FIELD_DEFINITIONS = (
    ("test_nature", "测试性质", "test_nature_options"),
    ("state", "条件/状态", "state_options"),
    ("node", "节点/位置", "node_options"),
    ("instrument", "工具/仪器/治具", "instrument_options"),
)


def configured_test_field_definitions(config: object) -> tuple[tuple[str, str, str], ...]:
    """返回配置中实际启用、会在录入界面展示的测试参数字段。"""
    if not isinstance(config, dict):
        return ()
    return tuple(field for field in TEST_FIELD_DEFINITIONS if config.get(field[2]))


def configured_test_display_values(
    chip: object,
    config: object,
) -> tuple[tuple[str, str], ...]:
    """读取测试概述已配置字段的展示值；选择数据实际嵌套在 test_select_data 中。"""
    if not isinstance(chip, dict):
        return ()
    test_data_value = chip.get("test_select_data")
    test_data = test_data_value if isinstance(test_data_value, dict) else {}
    values: list[tuple[str, str]] = []
    for prefix, title, _ in configured_test_field_definitions(config):
        selected = str(test_data.get(f"{prefix}_select") or "").strip()
        other_text = str(test_data.get(f"{prefix}_other_text") or "").strip()
        value = " / ".join(part for part in (selected, other_text) if part) or "未填写"
        values.append((title, value))
    return tuple(values)

# 激活状态及其显示、操作历史会被需求审批或工程师操作独立更新，不属于原记录纠错的业务对象。
# 纠错审批的并发保护只忽略这些字段；正文、测试参数、文件信息等其它变化仍会触发冲突。
CORRECTION_CONCURRENCY_IGNORED_FIELDS = {
    "enabled",
    "select_activ_dic",
    "icon",
    "bg_color",
    "timestamp",
}


def correction_now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_correction_reviewer_roles(applicant_role: str) -> list[str]:
    return sorted(
        reviewer_role
        for reviewer_role, target_roles in CORRECTION_APPROVAL_ROLE_TARGETS.items()
        if applicant_role in target_roles
    )


def can_review_correction_request(request: dict, reviewer: str, reviewer_role: str) -> bool:
    service = getattr(app.state, "user_service", None)
    if service is not None:
        return can_review_overview_correction(request, reviewer_role, reviewer, user_service=service)
    configured_roles = get_correction_reviewer_roles(str(request.get("submitter_role") or ""))
    return bool(
        reviewer
        and (not CORRECTION_PREVENT_SELF_APPROVAL or reviewer != request.get("submitter"))
        and reviewer_role in configured_roles
        and reviewer_role in request.get("reviewer_roles", [])
    )


def get_correction_pending_count(requests: dict, current_user: str, current_role: str) -> int:
    count = 0
    for request in requests.values():
        status = request.get("status")
        if status == "pending" and can_review_correction_request(request, current_user, current_role):
            count += 1
        elif status in {"rejected", "failed"} and request.get("submitter") == current_user:
            count += 1
    return count


def chip_snapshot_fingerprint(chip: dict) -> str:
    serialized = json.dumps(chip or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def chip_correction_guard_fingerprint(chip: dict) -> str:
    """生成纠错执行并发指纹，排除与纠错正文无关的激活状态字段。"""
    guarded_snapshot = {
        key: value
        for key, value in (chip or {}).items()
        if key not in CORRECTION_CONCURRENCY_IGNORED_FIELDS
    }
    return chip_snapshot_fingerprint(guarded_snapshot)


def build_test_field_changes(before_data: dict, after_data: dict, config: dict) -> list[dict]:
    """列出配置中实际启用的测试字段；未变化字段也明确标记。"""
    changes = []
    for prefix, title, _ in configured_test_field_definitions(config):
        select_key = f"{prefix}_select"
        other_key = f"{prefix}_other_text"
        before_select = before_data.get(select_key)
        after_select = after_data.get(select_key)
        before_other = str(before_data.get(other_key) or "")
        after_other = str(after_data.get(other_key) or "")
        changes.append(
            {
                "key": prefix,
                "title": title,
                "before_select": before_select,
                "after_select": after_select,
                "before_other": before_other,
                "after_other": after_other,
                "changed": before_select != after_select or before_other != after_other,
            }
        )
    return changes


def build_correction_changes(before: dict, after: Optional[dict], config: dict, action: str) -> list[dict]:
    if action == "delete":
        return [{"key": "record", "title": "记录", "before": before.get("content", ""), "after": "已删除", "changed": True}]
    after = after or {}
    changes = [
        {
            "key": "content",
            "title": "概述内容",
            "before": before.get("content", ""),
            "after": after.get("content", ""),
            "changed": before.get("content", "") != after.get("content", ""),
        }
    ]
    if before.get("type") == "test":
        changes.extend(
            build_test_field_changes(
                before.get("test_select_data", {}) or {},
                after.get("test_select_data", {}) or {},
                config,
            )
        )
    return changes


def find_active_correction_for_chip(requests: dict, project: str, label: str, chip_id: str) -> tuple[str, dict] | None:
    for request_id, request in requests.items():
        if (
            request.get("project") == project
            and request.get("label") == label
            and request.get("chip_id") == chip_id
            and request.get("status") in {"pending", "processing", "rejected", "failed"}
        ):
            return str(request_id), request
    return None


async def create_correction_request(request: dict) -> tuple[bool, str]:
    request_id = str(request.get("id") or uuid.uuid4())
    record = copy.deepcopy(request)
    record["id"] = request_id

    def insert(records):
        records = records or {}
        duplicate = find_active_correction_for_chip(
            records,
            str(record.get("project") or ""),
            str(record.get("label") or ""),
            str(record.get("chip_id") or ""),
        )
        if request_id in records or duplicate:
            return db_storage.ATOMIC_NO_UPDATE
        records[request_id] = record
        return records

    inserted = {"value": False}

    def guarded_insert(records):
        result = insert(records)
        if result is not db_storage.ATOMIC_NO_UPDATE:
            inserted["value"] = True
        return result

    success = await db_storage.atomic_deep_update([OVERVIEW_CORRECTION_REQUESTS_KEY], guarded_insert)
    return bool(success and inserted["value"]), request_id


async def update_correction_request(request_id: str, changes: dict) -> bool:
    updated = {"value": False}

    def update(request):
        if not request:
            return db_storage.ATOMIC_NO_UPDATE
        request.update(copy.deepcopy(changes))
        request["updated_at"] = correction_now()
        updated["value"] = True
        return request

    success = await db_storage.atomic_deep_update([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id], update)
    return bool(success and updated["value"])


def get_correction_archives_for_chip(project: str, label: str, chip_id: str) -> list[dict]:
    archives = db_storage.get_item(OVERVIEW_CORRECTION_ARCHIVES_KEY, {}) or {}
    records = [
        record
        for record in archives.values()
        if record.get("project") == project
        and record.get("label") == label
        and record.get("chip_id") == chip_id
        and record.get("status") == "approved"
    ]
    return sorted(records, key=lambda record: str(record.get("reviewed_at") or ""), reverse=True)


def get_project_correction_archives(project: str) -> list[dict]:
    archives = db_storage.get_item(OVERVIEW_CORRECTION_ARCHIVES_KEY, {}) or {}
    records = [record for record in archives.values() if record.get("project") == project]
    return sorted(records, key=lambda record: str(record.get("updated_at") or ""), reverse=True)


def validate_test_correction(test_data: dict, config: dict) -> tuple[bool, str]:
    for prefix, title, _ in configured_test_field_definitions(config):
        selected = test_data.get(f"{prefix}_select")
        if not selected:
            return False, f"{title}必须选择"
        if selected == "其它" and not str(test_data.get(f"{prefix}_other_text") or "").strip():
            return False, f"{title}选择“其它”时必须填写特殊要求"
    return True, ""


def validate_staged_path(path_value: str) -> tuple[bool, Optional[Path]]:
    if not str(path_value or "").strip():
        return True, None
    path = Path(path_value).resolve()
    if not path.is_relative_to(OVERVIEW_CORRECTION_STAGING_DIR.resolve()):
        return False, None
    return path.is_file(), path


def cleanup_correction_staged_files(paths: Iterable[str]) -> None:
    for path_value in paths:
        valid, path = validate_staged_path(str(path_value or ""))
        if not valid or path is None:
            continue
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def correction_media_path(snapshot: dict, config: dict) -> Path:
    """实际文件路径优先于展示文件名，兼容 ECN 独立归档与普通上传。"""
    local_path = str(snapshot.get("local_file_path") or "").strip()
    return Path(local_path) if local_path else Path(str(config.get("upload_path") or "")) / Path(
        str(snapshot.get("content") or "")
    ).name


async def _validate_saved_media_request(request: dict) -> None:
    """保留已删除或已变更申请的旧窗口防护。"""
    request_id = str(request.get("id") or "")
    if request_id:
        records = await db_storage.get_fresh_item(OVERVIEW_CORRECTION_REQUESTS_KEY, {})
        current = records.get(request_id) if isinstance(records, dict) else None
        if not isinstance(current, dict) or any(
            current.get(key) != request.get(key) for key in ("project", "label", "chip_id", "action", "payload")
        ):
            raise ValueError("纠错申请已被修改或删除，请刷新后重试。")


async def publish_correction_media(request: dict, source: Path, directory: Path, filename: str, digest: str) -> Path:
    """保留原文件名；已存在的同名文件只接受相同内容，正式文件不回收。"""
    await _validate_saved_media_request(request)
    return copy_named_file(source, directory / Path(filename).name, expected_digest=digest)


def build_media_file_audit(before: dict, config: dict, staged_path_value: str) -> tuple[bool, str, dict]:
    """记录送审时的新旧文件指纹，确保审批执行的正是申请人提交的文件。"""
    upload_path_value = str(config.get("upload_path") or "").strip()
    if not upload_path_value:
        return False, "该概述项没有配置正式上传目录", {}
    upload_path = Path(upload_path_value)
    if not upload_path.is_dir():
        return False, f"正式上传目录不存在：{upload_path}", {}

    original_path = correction_media_path(before, config)
    staged_ok, staged_path = validate_staged_path(staged_path_value)
    if staged_path_value and (not staged_ok or staged_path is None):
        return False, "纠错暂存文件不存在或路径无效", {}
    return (
        True,
        "",
        {
            "original_file_sha256": file_sha256(original_path) if original_path.is_file() else "",
            "staged_file_sha256": file_sha256(staged_path) if staged_path is not None else "",
        },
    )


def _prepare_media_target(payload: dict, config: dict) -> tuple[bool, str, Optional[Path], Optional[Path]]:
    after = payload.get("after_snapshot") or {}
    filename = Path(str(after.get("content") or "")).name
    if not filename:
        return False, "文件名不能为空", None, None
    upload_path_value = str(config.get("upload_path") or "").strip()
    if not upload_path_value:
        return False, "该概述项没有配置正式上传目录", None, None
    upload_path = Path(upload_path_value)
    if not upload_path.is_dir():
        return False, f"正式上传目录不存在：{upload_path}", None, None
    staged_value = str(payload.get("staged_file_path") or "")
    staged_ok, staged_path = validate_staged_path(staged_value)
    target_path = upload_path / filename
    if staged_value and not staged_ok:
        return False, "纠错暂存文件不存在或路径无效", None, None
    if staged_path is None:
        if not target_path.is_file():
            return False, f"正式目录中不存在文件：{filename}", None, None
        return True, "", None, target_path
    if target_path.exists() and file_sha256(target_path) != file_sha256(staged_path):
        return False, f"正式目录已有不同内容的同名文件：{filename}，请更换文件名", None, None
    return True, "", staged_path, target_path


async def execute_correction_request(request: dict) -> dict:
    """审批通过后执行纠错；不修改 creator、timestamp、req_ver、notes 或激活状态。"""
    from .utils import validate_search_path, validate_svn_url

    project = str(request.get("project") or "")
    label = str(request.get("label") or "")
    chip_id = str(request.get("chip_id") or "")
    action = str(request.get("action") or "correct")
    payload = copy.deepcopy(request.get("payload") or {})
    before = copy.deepcopy(payload.get("before_snapshot") or {})
    after = copy.deepcopy(payload.get("after_snapshot") or {})
    config_snapshot = copy.deepcopy(payload.get("config") or {})
    live_config = copy.deepcopy(app.storage.general.get("over_config_data_flat", {}).get(label, {}))
    config = copy.deepcopy(config_snapshot)
    config.update(live_config)

    submitter_role = str(request.get("submitter_role") or "")
    if not can_edit_overview_item(config, submitter_role, str(request.get("submitter") or "")):
        return {"ok": False, "message": "申请人已不再具有该概述项的编辑权限"}
    if not before or chip_snapshot_fingerprint(before) != str(payload.get("before_fingerprint") or ""):
        return {"ok": False, "message": "申请中的原记录快照不完整"}

    chip_type = str(before.get("type") or config.get("processing_type") or "text")
    staged_path: Optional[Path] = None
    target_path: Optional[Path] = None
    file_change: Optional[dict] = None
    if action == "correct":
        content = str(after.get("content") or "").strip()
        if not validate_overview_content(content, config):
            return {"ok": False, "message": "纠错后的内容为空或不符合填写格式"}
        after["content"] = content
        if chip_type == "test":
            valid, message = validate_test_correction(after.get("test_select_data", {}) or {}, config)
            if not valid:
                return {"ok": False, "message": message}
        elif chip_type == "search":
            valid, url_path, file_type, _, message = await validate_search_path(content, config, [project])
            if not valid:
                return {"ok": False, "message": message}
            after.update({"url_path": url_path, "file_type": file_type, "icon": "saved_search"})
        elif chip_type == "svn":
            valid, url_path, file_type, message = await validate_svn_url(content, config, [project])
            if not valid:
                return {"ok": False, "message": message}
            project_state = next(
                (
                    summary.get("state")
                    for key, summary in app.storage.general.get("project_summary", {}).items()
                    if str(summary.get("sub_project") or key) == project
                ),
                None,
            )
            after.update(
                {
                    "url_path": url_path,
                    "file_type": file_type,
                    "warehouse": config.get("state_path", {}).get(project_state),
                    "icon": "saved_search",
                }
            )
        elif chip_type in {"file", "image", "video"}:
            media_ok, media_message, staged_path, target_path = _prepare_media_target(payload, config)
            if not media_ok:
                return {"ok": False, "message": media_message}
            original_path = correction_media_path(before, config)
            original_hash = file_sha256(original_path) if original_path.is_file() else ""
            expected_original_hash = str(payload.get("original_file_sha256") or "")
            if expected_original_hash and original_hash != expected_original_hash:
                return {"ok": False, "message": "原文件已在审批期间发生变化，请驳回后重新申请"}
            staged_hash = file_sha256(staged_path) if staged_path is not None else ""
            expected_staged_hash = str(payload.get("staged_file_sha256") or "")
            if expected_staged_hash and staged_hash != expected_staged_hash:
                return {"ok": False, "message": "纠错暂存文件已发生变化，请驳回后重新申请"}
            extension = Path(str(after.get("content") or "")).suffix.lower()
            uploaded_file_type = str(payload.get("uploaded_file_type") or after.get("file_type") or "")
            if chip_type == "file" and extension not in OVER_UPLOADS_FILE_TYPE:
                return {"ok": False, "message": f"{extension or '无扩展名'}不是允许的文件类型"}
            if chip_type == "image" and "image" not in uploaded_file_type:
                return {"ok": False, "message": "纠错文件不是有效图片类型"}
            if chip_type == "video" and "video" not in uploaded_file_type:
                return {"ok": False, "message": "纠错文件不是有效视频类型"}
            after["url_path"] = str(payload.get("target_url_path") or after.get("url_path") or "")
            if target_path is not None:
                after["local_file_path"] = str(target_path)
            if payload.get("uploaded_file_type"):
                after["file_type"] = payload["uploaded_file_type"]
            new_file_source = staged_path or target_path
            file_change = {
                "before_name": before.get("content", ""),
                "after_name": after.get("content", ""),
                "before_url": before.get("url_path", ""),
                "after_url": after.get("url_path", ""),
                "before_sha256": original_hash,
                "after_sha256": file_sha256(new_file_source) if new_file_source and new_file_source.is_file() else "",
            }

    delete_targets = payload.get("delete_targets") or [
        {"label": label, "chip_id": chip_id, "snapshot": before}
    ]
    correction_applied = False
    actual_after: Optional[dict] = None
    changed_labels: set[str] = set()

    def apply_correction(overview_data):
        nonlocal correction_applied, actual_after
        overview_data = overview_data or {}
        current = overview_data.get(label, {}).get(chip_id)
        if not current or chip_correction_guard_fingerprint(current) != chip_correction_guard_fingerprint(before):
            return db_storage.ATOMIC_NO_UPDATE
        if action == "delete":
            for target in delete_targets:
                target_label = str(target.get("label") or "")
                target_id = str(target.get("chip_id") or "")
                expected = target.get("snapshot") or {}
                live_target = overview_data.get(target_label, {}).get(target_id)
                if not live_target or chip_correction_guard_fingerprint(
                    live_target
                ) != chip_correction_guard_fingerprint(expected):
                    return db_storage.ATOMIC_NO_UPDATE
            for target in delete_targets:
                target_label = str(target.get("label") or "")
                target_id = str(target.get("chip_id") or "")
                overview_data.get(target_label, {}).pop(target_id, None)
                changed_labels.add(target_label)
        else:
            corrected = copy.deepcopy(current)
            corrected["content"] = after["content"]
            for metadata_key in ("url_path", "file_type", "warehouse", "test_select_data"):
                if metadata_key in after:
                    corrected[metadata_key] = copy.deepcopy(after[metadata_key])
            if chip_type in {"file", "image", "video"}:
                corrected["local_file_path"] = after["local_file_path"]
            overview_data.setdefault(label, {})[chip_id] = corrected
            actual_after = copy.deepcopy(corrected)
            changed_labels.add(label)
        correction_applied = True
        return overview_data

    if action == "correct" and chip_type in {"file", "image", "video"} and target_path is not None:
        try:
            source = staged_path or target_path
            digest = str((file_change or {}).get("after_sha256") or "")
            target_path = await publish_correction_media(request, source, target_path.parent, after["content"], digest)
            after["local_file_path"] = str(target_path)
        except Exception as exc:
            return {"ok": False, "message": f"纠错文件保存失败：{exc}"}

    success = await overview_storage.atomic_deep_update([f"{project}_over_data"], apply_correction)
    if not success or not correction_applied:
        # 正式文件可能被其他概述引用，业务失败只放弃本次引用。
        return {"ok": False, "message": "原概述已在审批期间发生变化，请驳回后重新申请"}
    if staged_path is not None:
        cleanup_correction_staged_files([str(staged_path)])

    from .components import OverviewVersionManager

    for changed_label in changed_labels:
        OverviewVersionManager.bump(project, changed_label)
    if action != "correct":
        actual_after = None
    return {
        "ok": True,
        "message": "原记录纠错已执行" if action == "correct" else "错误记录已删除",
        "before_snapshot": before,
        "after_snapshot": actual_after,
        "changes": build_correction_changes(before, actual_after, config, action),
        "deleted_snapshots": [copy.deepcopy(target.get("snapshot") or {}) for target in delete_targets]
        if action == "delete"
        else [],
        "file_change": file_change,
    }


async def archive_correction_request(request_id: str, request: dict, status: str, extra: Optional[dict] = None) -> bool:
    archive = copy.deepcopy(request)
    archive.update(copy.deepcopy(extra or {}))
    archive["status"] = status
    archive["updated_at"] = correction_now()
    result = archive.get("result") or {}
    actual_after = result.get("after_snapshot")
    if status == "approved" and isinstance(actual_after, dict) and actual_after.get("type") in {"file", "image", "video"}:
        # 归档预览使用实际正式文件，不再依赖已清理的暂存文件。
        archive.setdefault("payload", {})["after_snapshot"] = copy.deepcopy(actual_after)
        archive["payload"]["staged_file_path"] = ""

    def append(archives):
        archives = archives or {}
        archives[request_id] = archive
        return archives

    saved = await db_storage.atomic_deep_update([OVERVIEW_CORRECTION_ARCHIVES_KEY], append)
    if not saved:
        return False
    return await db_storage.del_deep_item([OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
