"""ECN 附件的短时预览地址；每次读取都重新检查单据权限。"""

from __future__ import annotations

import secrets
import time
import copy
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from nicegui import app

from ... import db_storage
from ...ecn_access import get_active_ecn_actor_role
from ...ecn_management_config import ECN_DATA_KEY
from .attachments import resolve_attachment, resolve_staged_attachment, visible_attachments
from .editing import ECNConflict


IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"})
PREVIEW_LIFETIME_SECONDS = 15 * 60
_preview_tokens: dict[str, tuple[float, str, str, str, str, str, str, str]] = {}
_pending_preview_tokens: dict[str, tuple[float, dict, str, str]] = {}
_overview_preview_tokens: dict[str, tuple[float, str, str, str, str, str]] = {}


def attachment_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    return "download"


def issue_attachment_preview_url(
    ecn_id: str, scope: str, item_id: str, task_key: str, file_id: str, user: str, role: str,
) -> str:
    now = time.monotonic()
    for expired in [key for key, value in _preview_tokens.items() if value[0] <= now]:
        _preview_tokens.pop(expired, None)
    token = secrets.token_urlsafe(32)
    _preview_tokens[token] = (
        now + PREVIEW_LIFETIME_SECONDS, ecn_id, scope, item_id, task_key, file_id, user, role,
    )
    return f"/ecn_attachment_preview/{token}"


def issue_staged_attachment_preview_url(attachment: dict, user: str, role: str) -> str:
    if attachment.get("uploaded_by") != user or not resolve_staged_attachment(attachment).is_file():
        raise ECNConflict("暂存附件已失效")
    now = time.monotonic()
    for expired in [key for key, value in _pending_preview_tokens.items() if value[0] <= now]:
        _pending_preview_tokens.pop(expired, None)
    token = secrets.token_urlsafe(32)
    _pending_preview_tokens[token] = (now + PREVIEW_LIFETIME_SECONDS, copy.deepcopy(attachment), user, role)
    return f"/ecn_pending_attachment_preview/{token}"


def issue_overview_scheme_preview_url(
    ecn_id: str,
    item_id: str,
    file_id: str,
    user: str,
    role: str,
) -> str:
    """签发系统内资料方案归档的短时查看地址。"""
    now = time.monotonic()
    for expired in [key for key, value in _overview_preview_tokens.items() if value[0] <= now]:
        _overview_preview_tokens.pop(expired, None)
    token = secrets.token_urlsafe(32)
    _overview_preview_tokens[token] = (
        now + PREVIEW_LIFETIME_SECONDS,
        ecn_id,
        item_id,
        file_id,
        user,
        role,
    )
    return f"/ecn_overview_scheme_preview/{token}"


def _inline_response(path: Path, name: str) -> FileResponse:
    kind = attachment_kind(name)
    if kind not in {"image", "pdf"}:
        raise HTTPException(status_code=404, detail="此类型附件不支持预览")
    media_type = "application/pdf" if kind == "pdf" else {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".avif": "image/avif",
    }[Path(name).suffix.lower()]
    return FileResponse(
        path, media_type=media_type, filename=name, content_disposition_type="inline",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/ecn_attachment_preview/{token}")
def serve_ecn_attachment_preview(token: str) -> FileResponse:
    request_info = _preview_tokens.get(token)
    if request_info is None or request_info[0] <= time.monotonic():
        _preview_tokens.pop(token, None)
        raise HTTPException(status_code=404, detail="附件预览链接已失效")
    _, ecn_id, scope, item_id, task_key, file_id, user, role = request_info
    service = getattr(app.state, "user_service", None)
    files = visible_attachments(ecn_id, scope, item_id, task_key, user, role, service)
    attachment = next((entry for entry in files if str(entry.get("id")) == file_id), None)
    if attachment is None:
        raise HTTPException(status_code=404, detail="附件不存在或无权查看")
    name = str(attachment.get("name") or "附件")
    try:
        path = resolve_attachment(attachment)
    except ECNConflict as exc:
        raise HTTPException(status_code=404, detail="附件路径无效") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="附件文件不存在")
    return _inline_response(path, name)


@app.get("/ecn_pending_attachment_preview/{token}")
def serve_ecn_pending_attachment_preview(token: str) -> FileResponse:
    request_info = _pending_preview_tokens.get(token)
    if request_info is None or request_info[0] <= time.monotonic():
        _pending_preview_tokens.pop(token, None)
        raise HTTPException(status_code=404, detail="暂存附件预览链接已失效")
    _, attachment, user, role = request_info
    service = getattr(app.state, "user_service", None)
    if attachment.get("uploaded_by") != user or get_active_ecn_actor_role(
        user, role, user_service=service,
    ) is None:
        raise HTTPException(status_code=404, detail="暂存附件不存在或无权查看")
    try:
        path = resolve_staged_attachment(attachment)
    except ECNConflict as exc:
        raise HTTPException(status_code=404, detail="暂存附件路径无效") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="暂存附件不存在")
    return _inline_response(path, str(attachment.get("name") or "附件"))


@app.get("/ecn_overview_scheme_preview/{token}")
def serve_ecn_overview_scheme_preview(token: str) -> FileResponse:
    """每次读取都复核方案作者、账号状态和最新单据中的文件引用。"""
    request_info = _overview_preview_tokens.get(token)
    if request_info is None or request_info[0] <= time.monotonic():
        _overview_preview_tokens.pop(token, None)
        raise HTTPException(status_code=404, detail="方案文件查看地址已失效")
    _, ecn_id, item_id, file_id, user, role = request_info
    service = getattr(app.state, "user_service", None)
    if get_active_ecn_actor_role(user, role, user_service=service) is None:
        raise HTTPException(status_code=404, detail="当前账号不可用")
    record = db_storage.get_deep_item([ECN_DATA_KEY, ecn_id])
    items = record.get("change_items", []) if isinstance(record, dict) else []
    item = next(
        (
            entry
            for entry in items
            if isinstance(entry, dict) and str(entry.get("item_id") or "") == item_id
        ),
        None,
    ) if isinstance(items, list) else None
    upload = item.get("new_data", {}).get("overview_upload") if isinstance(item, dict) else None
    if (
        not isinstance(item, dict)
        or item.get("author") != user
        or not isinstance(upload, dict)
        or str(upload.get("id") or "") != file_id
    ):
        raise HTTPException(status_code=404, detail="方案文件不存在或无权查看")
    try:
        path = resolve_attachment(upload)
    except ECNConflict as exc:
        raise HTTPException(status_code=404, detail="方案文件路径无效") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="方案文件不存在")
    name = str(upload.get("name") or "附件")
    if attachment_kind(name) in {"image", "pdf"}:
        return _inline_response(path, name)
    return FileResponse(
        path,
        filename=name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )
