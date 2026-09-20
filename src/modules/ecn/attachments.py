"""ECN 附件的磁盘归档、元数据读写与实时权限校验。"""

from __future__ import annotations

import asyncio
import copy
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ... import db_storage
from ...ecn_access import (
    can_confirm_ecn_material_spec,
    can_create_ecn_request,
    can_edit_ecn_scheme,
    can_execute_ecn_assistant_stage,
    can_view_ecn_validation_report,
    can_view_ecn,
    can_view_ecn_scheme_non_image_file,
    get_active_ecn_actor_role,
)
from ...ecn_management_config import (
    ECN_ATTACHMENT_CONFIG,
    ECN_DATA_KEY,
    ECN_LEVEL_COMPLEX,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECNState,
    get_ecn_material_execution_specs,
    get_ecn_level_code,
)
from .editing import ECNConflict, ECNResult
from .repository import mutate_record


def attachment_root() -> Path:
    configured = Path(str(ECN_ATTACHMENT_CONFIG["storage_path"]))
    root = configured if configured.is_absolute() else Path(__file__).resolve().parents[3] / configured
    return root.resolve()


def _segment(value: object) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", str(value or "").strip(), flags=re.UNICODE)
    return text.strip("._")[:80] or "unknown"


def _safe_path(relative: str) -> Path:
    root = attachment_root()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ECNConflict("附件路径无效")
    return target


def _read_attachments(record: dict, scope: str, item_id: str, task_key: str) -> list[dict]:
    if scope == "ecr":
        basic = record.get("basic_info", {})
        value = basic.get("attachments", []) if isinstance(basic, dict) else []
    elif scope == "scheme":
        items = record.get("change_items", [])
        item = next(
            (entry for entry in items if isinstance(entry, dict) and str(entry.get("item_id")) == item_id),
            None,
        ) if isinstance(items, list) else None
        value = item.get("attachments", []) if isinstance(item, dict) else []
    elif scope == "validation":
        items = record.get("change_items", [])
        item = next(
            (
                entry
                for entry in items
                if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
            ),
            None,
        ) if isinstance(items, list) else None
        report = item.get("validation_report", {}) if isinstance(item, dict) else {}
        value = report.get("attachments", []) if isinstance(report, dict) else []
    else:
        execution = record.get("execution_info", {})
        registry = execution.get("attachments", {}) if isinstance(execution, dict) else {}
        value = registry.get(_execution_key(scope, item_id, task_key), []) if isinstance(registry, dict) else []
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def _execution_key(scope: str, item_id: str, task_key: str) -> str:
    return f"{scope}|{item_id}|{task_key}"


def _attachment_list(record: dict, scope: str, item_id: str, task_key: str) -> list[dict]:
    if scope == "ecr":
        return record.setdefault("basic_info", {}).setdefault("attachments", [])
    if scope == "scheme":
        item = next(
            (entry for entry in record.get("change_items", []) if str(entry.get("item_id")) == item_id),
            None,
        )
        if not isinstance(item, dict):
            raise ECNConflict("方案不存在，请刷新页面")
        return item.setdefault("attachments", [])
    if scope == "validation":
        item = next(
            (
                entry
                for entry in record.get("change_items", [])
                if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
            ),
            None,
        )
        if not isinstance(item, dict):
            raise ECNConflict("方案不存在，请刷新页面")
        report = item.setdefault("validation_report", {})
        if not isinstance(report, dict):
            raise ECNConflict("验证报告数据异常")
        return report.setdefault("attachments", [])
    execution = record.setdefault("execution_info", {})
    return execution.setdefault("attachments", {}).setdefault(_execution_key(scope, item_id, task_key), [])


def _validate_access(record: dict, scope: str, item_id: str, task_key: str, user: str, role: str, service: Any) -> None:
    workflow = record.get("workflow", {})
    state = workflow.get("current_state") if isinstance(workflow, dict) else None
    if scope == "ecr":
        basic = record.get("basic_info", {})
        if state not in {ECNState.DRAFT, ECNState.REJECTED} or not isinstance(basic, dict) or basic.get("applicant") != user:
            raise ECNConflict("当前不能上传 ECR 附件")
        if not can_create_ecn_request(role, user, user_service=service):
            raise ECNConflict("当前用户没有维护 ECR 的权限")
        return
    if scope == "scheme":
        item = next(
            (entry for entry in record.get("change_items", []) if isinstance(entry, dict) and str(entry.get("item_id")) == item_id),
            None,
        )
        if state != ECNState.ECN_SCHEMING or not isinstance(item, dict) or item.get("author") != user:
            raise ECNConflict("当前不能上传此方案的附件")
        if item.get("scheme_category") != "ordinary_document" or not can_edit_ecn_scheme(role, user, user_service=service):
            raise ECNConflict("当前用户没有维护此方案的权限")
        participants = workflow.get("scheme_participants", {}) if isinstance(workflow, dict) else {}
        if isinstance(participants, dict) and participants.get(user) == ECN_PARTICIPANT_STATUS_CONFIRMED:
            raise ECNConflict("已确认方案不能直接补传附件，请先重新开启编辑")
        return
    if scope == "validation":
        items = record.get("change_items", [])
        item = next(
            (
                entry
                for entry in items
                if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
            ),
            None,
        ) if isinstance(items, list) else None
        report = item.get("validation_report", {}) if isinstance(item, dict) else {}
        if (
            state != ECNState.ECN_SCHEMING
            or get_ecn_level_code(record) != ECN_LEVEL_COMPLEX
            or not isinstance(item, dict)
            or item.get("author") != user
            or not isinstance(report, dict)
            or report.get("required") is not True
        ):
            raise ECNConflict("当前不能上传此方案的验证报告")
        if not can_edit_ecn_scheme(role, user, user_service=service):
            raise ECNConflict("当前用户没有提交验证报告的权限")
        return
    if state != ECNState.ECN_EXECUTING:
        raise ECNConflict("当前不在 ECN 执行阶段")
    execution = record.get("execution_info", {})
    if not isinstance(execution, dict):
        raise ECNConflict("执行数据异常")
    if scope in {"ordinary", "erp"}:
        entries = execution.get("ordinary_confirmations", {}) if scope == "ordinary" else execution.get("erp_confirmation", {})
        confirmation = entries.get(item_id) if scope == "ordinary" and isinstance(entries, dict) else entries
        if not isinstance(confirmation, dict):
            raise ECNConflict("执行事项不存在")
        assignee = str(confirmation.get("assignee") or "")
        if (assignee and assignee != user) or (
            not assignee and not can_execute_ecn_assistant_stage(role, user, user_service=service)
        ):
            raise ECNConflict("当前用户不是该执行事项的负责人")
        return
    if scope == "overview":
        results = execution.get("overview_results", {})
        if not isinstance(results, dict) or item_id not in results:
            raise ECNConflict("系统内资料执行项不存在")
        if not can_execute_ecn_assistant_stage(role, user, user_service=service):
            raise ECNConflict("当前用户没有执行助理权限")
        return
    if scope == "material":
        items = record.get("change_items", [])
        item = next((entry for entry in items if isinstance(entry, dict) and str(entry.get("item_id")) == item_id), None)
        material = execution.get("material_confirmations", {})
        entry = material.get(item_id) if isinstance(material, dict) else None
        if not isinstance(item, dict) or not isinstance(entry, dict):
            raise ECNConflict("物料执行项不存在")
        spec = next((candidate for candidate in get_ecn_material_execution_specs(item, entry) if str(candidate.get("key")) == task_key), None)
        if not isinstance(spec, dict) or not can_confirm_ecn_material_spec(spec, role, user, user_service=service):
            raise ECNConflict("当前用户不是该物料节点的负责人")
        return
    raise ECNConflict("附件业务位置无效")


async def stage_upload(uploaded_file: Any, username: str) -> dict:
    name = Path(str(uploaded_file.name or "附件")).name
    max_size = int(ECN_ATTACHMENT_CONFIG["max_file_size_mb"]) * 1024 * 1024
    if uploaded_file.size() > max_size:
        raise ECNConflict(f"附件不能超过 {ECN_ATTACHMENT_CONFIG['max_file_size_mb']} MB")
    file_id = uuid.uuid4().hex
    pending_relative = f"_pending/{file_id}"
    target = _safe_path(pending_relative)
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
    try:
        await uploaded_file.save(target)
        size = target.stat().st_size
        if size > max_size:
            raise ECNConflict(f"附件不能超过 {ECN_ATTACHMENT_CONFIG['max_file_size_mb']} MB")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return {
        "id": file_id,
        "name": name,
        "size": size,
        "uploaded_by": username,
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pending_path": pending_relative,
    }


def publish_pending(attachment: dict, ecn_id: str, username: str, scope: str) -> tuple[dict, Path]:
    pending = _safe_path(str(attachment.get("pending_path") or ""))
    file_id = str(attachment.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", file_id) or pending.name != file_id or not pending.is_file() or pending.parent != _safe_path("_pending"):
        raise ECNConflict("暂存附件已失效，请重新上传")
    extension = re.sub(r"[^A-Za-z0-9.]", "", Path(str(attachment["name"])).suffix)[:16]
    relative = "/".join(
        (_segment(username), _segment(ecn_id), _segment(scope), f"{attachment['id']}{extension}")
    )
    target = _safe_path(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ECNConflict("附件归档编号已存在，请重新上传")
    try:
        shutil.copyfile(pending, target)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    published = {key: value for key, value in attachment.items() if key != "pending_path"}
    published["relative_path"] = relative
    return published, target


def cleanup_staged(attachments: list[dict]) -> None:
    for attachment in attachments:
        pending = str(attachment.get("pending_path") or "")
        if pending.startswith("_pending/"):
            _safe_path(pending).unlink(missing_ok=True)


def resolve_staged_attachment(attachment: dict) -> Path:
    file_id = str(attachment.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", file_id) or attachment.get("pending_path") != f"_pending/{file_id}":
        raise ECNConflict("暂存附件路径无效")
    return _safe_path(f"_pending/{file_id}")


def resolve_attachment(attachment: dict) -> Path:
    relative = str(attachment.get("relative_path") or "")
    if not relative or relative.startswith("_pending/"):
        raise ECNConflict("附件尚未保存")
    return _safe_path(relative)


def validate_scheme_attachments(record: dict, item: dict, user: str) -> None:
    """方案提交不能借客户端字段添加、删改其他附件元数据。"""
    submitted = item.get("attachments", [])
    if item.get("scheme_category") != "ordinary_document":
        if submitted:
            raise ECNConflict("该类方案不支持附件")
        return
    if not isinstance(submitted, list):
        raise ECNConflict("方案附件数据异常")
    item_id = str(item.get("item_id") or "")
    existing = _read_attachments(record, "scheme", item_id, "")
    if submitted[: len(existing)] != existing:
        raise ECNConflict("方案附件已变化，请重新打开方案")
    ecn_id = str(record.get("ecn_id") or "")
    for attachment in submitted[len(existing):]:
        if not isinstance(attachment, dict) or attachment.get("uploaded_by") != user:
            raise ECNConflict("方案附件上传人无效")
        file_id = str(attachment.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", file_id):
            raise ECNConflict("方案附件编号无效")
        expected_prefix = f"{_segment(user)}/{_segment(ecn_id)}/{_segment('scheme_' + item_id)}/"
        relative = str(attachment.get("relative_path") or "")
        if not relative.startswith(expected_prefix) or not Path(relative).name.startswith(file_id):
            raise ECNConflict("方案附件归档位置无效")
        if not resolve_attachment(attachment).is_file():
            raise ECNConflict("方案附件文件不存在，请重新上传")


def visible_attachments(ecn_id: str, scope: str, item_id: str, task_key: str, user: str, role: str, service: Any) -> list[dict]:
    record = db_storage.get_deep_item([ECN_DATA_KEY, ecn_id])
    if not isinstance(record, dict) or not can_view_ecn(role, user, user_service=service):
        return []
    if scope == "scheme":
        item = next((entry for entry in record.get("change_items", []) if isinstance(entry, dict) and str(entry.get("item_id")) == item_id), None)
        if not isinstance(item, dict) or (
            item.get("author") != user
            and not can_view_ecn_scheme_non_image_file(item, role, user, user_service=service)
        ):
            return []
    if scope == "validation":
        item = next(
            (
                entry
                for entry in record.get("change_items", [])
                if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
            ),
            None,
        )
        if not isinstance(item, dict) or (
            item.get("author") != user
            and not can_view_ecn_validation_report(role, user, user_service=service)
        ):
            return []
    return copy.deepcopy(_read_attachments(record, scope, item_id, task_key))


async def attach_existing(
    ecn_id: str, scope: str, item_id: str, task_key: str, staged: dict, user: str, role: str, service: Any,
) -> ECNResult:
    published, target = await asyncio.to_thread(
        publish_pending, staged, ecn_id, user, f"{scope}_{item_id}_{task_key}"
    )

    async def operation(record: dict, connection: Any) -> dict:
        del connection
        active_role = get_active_ecn_actor_role(user, role, user_service=service)
        if active_role is None:
            raise ECNConflict("当前账号已停用")
        if not can_view_ecn(active_role, user, user_service=service):
            raise ECNConflict("当前用户没有查看 ECN 的权限")
        _validate_access(record, scope, item_id, task_key, user, active_role, service)
        _attachment_list(record, scope, item_id, task_key).append(copy.deepcopy(published))
        if scope == "scheme":
            participants = record["workflow"].setdefault("scheme_participants", {})
            if participants.get(user) != ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION:
                participants[user] = ECN_PARTICIPANT_STATUS_EDITING
        elif scope == "validation":
            item = next(
                entry
                for entry in record.get("change_items", [])
                if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
            )
            report = item.setdefault("validation_report", {})
            report["status"] = "pending_review"
            for key in ("reviewed_by", "reviewed_at", "review_note"):
                report.pop(key, None)
        return record

    try:
        result = await mutate_record(ecn_id, operation)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if not result.ok:
        target.unlink(missing_ok=True)
    else:
        cleanup_staged([staged])
    return result


async def remove_existing(
    ecn_id: str, scope: str, item_id: str, task_key: str, file_id: str,
    user: str, role: str, service: Any,
) -> ECNResult:
    removed: dict | None = None

    async def operation(record: dict, connection: Any) -> dict:
        nonlocal removed
        del connection
        active_role = get_active_ecn_actor_role(user, role, user_service=service)
        if active_role is None or not can_view_ecn(active_role, user, user_service=service):
            raise ECNConflict("当前账号无权维护 ECN 附件")
        _validate_access(record, scope, item_id, task_key, user, active_role, service)
        files = _attachment_list(record, scope, item_id, task_key)
        current = next((entry for entry in files if str(entry.get("id")) == file_id), None)
        if not isinstance(current, dict) or current.get("uploaded_by") != user:
            raise ECNConflict("附件不存在或不属于当前上传人")
        removed = copy.deepcopy(current)
        files.remove(current)
        if scope == "scheme":
            participants = record["workflow"].setdefault("scheme_participants", {})
            if participants.get(user) != ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION:
                participants[user] = ECN_PARTICIPANT_STATUS_EDITING
        elif scope == "validation":
            item = next(
                entry
                for entry in record.get("change_items", [])
                if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
            )
            report = item.setdefault("validation_report", {})
            report["status"] = "pending_review" if files else "pending_upload"
            for key in ("reviewed_by", "reviewed_at", "review_note"):
                report.pop(key, None)
        return record

    result = await mutate_record(ecn_id, operation)
    if result.ok and removed is not None:
        try:
            await asyncio.to_thread(resolve_attachment(removed).unlink, missing_ok=True)
        except OSError:
            # 已提交的元数据不能再恢复；遗留文件可由后续离线清理处理。
            pass
    return result
