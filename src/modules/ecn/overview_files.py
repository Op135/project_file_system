"""系统内资料上传的隔离归档与正式执行发布；不覆盖共享目录中的同名文件。"""

from __future__ import annotations

import copy
import hashlib
import mimetypes
import re
import shutil
import uuid
from pathlib import Path

from .attachments import publish_pending, resolve_attachment
from .editing import ECNConflict


def prepare_overview_upload(item: dict, original: dict | None, ecn_id: str, user: str) -> tuple[dict, Path | None]:
    """仅归档本次上传，正式概述目录在获批执行前保持不变。"""
    updated = copy.deepcopy(item)
    if item.get("type") != "overview_update":
        return updated, None
    needs_file = item.get("config_processing_type") in {"file", "image", "video"} and any(
        isinstance(state, dict) and state.get("action") in {"add", "update"}
        for state in item.get("project_states", {}).values()
    )
    if not needs_file:
        return updated, None
    new_data = updated.setdefault("new_data", {})
    attachment = new_data.get("overview_upload")
    if not isinstance(attachment, dict):
        raise ECNConflict("请先上传本方案的新文件。")
    installed: Path | None = None
    if attachment.get("pending_path"):
        if attachment.get("uploaded_by") != user:
            raise ECNConflict("系统内资料上传人不匹配。")
        attachment, installed = publish_pending(attachment, ecn_id, user, f"overview_{item['item_id']}")
        new_data["overview_upload"] = attachment
    else:
        original_upload = (original or {}).get("new_data", {}).get("overview_upload")
        if attachment != original_upload:
            raise ECNConflict("系统内资料文件已变化，请重新上传。")
    path = resolve_attachment(attachment)
    if not path.is_file():
        raise ECNConflict("方案上传文件已不存在，请重新上传。")
    new_data.update(
        content=str(attachment["name"]), local_file_path=str(path),
        url_path=f"/files/ecn-review/{attachment['id']}",
        file_type=str(attachment.get("file_type") or mimetypes.guess_type(str(attachment["name"]))[0] or "application/octet-stream"),
    )
    return updated, installed


def publish_overview_upload(item: dict, project: str, ecn_id: str, config: dict) -> tuple[dict, Path | None]:
    """执行时独占创建正式文件；幂等重试仅接受字节相同的已有文件。"""
    attachment = item.get("new_data", {}).get("overview_upload")
    if not isinstance(attachment, dict):
        raise ECNConflict("方案未保存有效的上传文件。")
    source = resolve_attachment(attachment)
    if not source.is_file():
        raise ECNConflict("方案上传文件已不存在，不能执行。")
    root = str(config.get("upload_path") or "").strip()
    if not root:
        raise ECNConflict("概述参数未配置正式上传目录。")
    file_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ecn-file:{ecn_id}:{item['item_id']}:{project}:{attachment['id']}").hex
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(str(attachment["name"])).suffix)[:16]
    target = Path(root) / f"{file_id}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with source.open("rb") as incoming:
            with target.open("xb") as output:
                created = True
                shutil.copyfileobj(incoming, output)
    except FileExistsError:
        with source.open("rb") as incoming, target.open("rb") as existing:
            if hashlib.file_digest(incoming, "sha256").digest() != hashlib.file_digest(existing, "sha256").digest():
                raise ECNConflict("正式文件已存在且内容不一致，已拒绝覆盖。")
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    return {
        "local_file_path": str(target), "url_path": f"/files/ecn-overview/{file_id}{suffix}",
    }, target if created else None
