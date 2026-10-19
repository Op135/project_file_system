"""系统内资料：UUID 隔离送审，按已确认的原文件名执行，绝不覆盖正式文件。"""

from __future__ import annotations

import copy
import mimetypes
from pathlib import Path

from nicegui import app

from ...overview_file_storage import copy_named_file, file_digest, validate_file_selection
from .attachments import publish_pending, resolve_attachment
from .editing import ECNConflict


def prepare_overview_upload(item: dict, original: dict | None, ecn_id: str, user: str) -> tuple[dict, Path | None]:
    updated = copy.deepcopy(item)
    needs_file = item.get("type") == "overview_update" and item.get("config_processing_type") in {"file", "image", "video"} and any(
        isinstance(state, dict) and state.get("action") in {"add", "update"}
        for state in item.get("project_states", {}).values()
    )
    if not needs_file:
        return updated, None
    new_data = updated.setdefault("new_data", {})
    attachment = new_data.get("overview_upload")
    selection = new_data.get("file_selection")
    if not isinstance(attachment, dict) or not isinstance(selection, dict):
        raise ECNConflict("请先上传文件并完成正式目录同名检查。")
    config = app.storage.general.get("over_config_data_flat", {}).get(str(item.get("label") or ""), {})
    root = str(config.get("upload_path") or "")
    if not root:
        raise ECNConflict("概述参数未配置正式上传目录。")
    target = validate_file_selection(selection, Path(root))
    if Path(str(attachment.get("name") or "")).suffix.lower() != target.suffix.lower():
        raise ECNConflict("不能改变送审文件的扩展名。")
    installed: Path | None = None
    if attachment.get("pending_path"):
        if attachment.get("uploaded_by") != user:
            raise ECNConflict("系统内资料上传人不匹配。")
        source = target if selection["mode"] == "existing" else None
        attachment, installed = publish_pending(
            {**attachment, "name": selection["name"]}, ecn_id, user, f"overview_{item['item_id']}",
            overview_source=source, expected_digest=selection["sha256"],
        )
        new_data["overview_upload"] = attachment
    else:
        original_data = (original or {}).get("new_data", {})
        if attachment != original_data.get("overview_upload") or selection != original_data.get("file_selection"):
            raise ECNConflict("送审文件选择已变化，请重新上传并确认。")
    path = resolve_attachment(attachment)
    if not path.is_file() or file_digest(path) != selection["sha256"]:
        raise ECNConflict("送审文件内容已变化，请重新上传。")
    new_data.update(
        content=selection["name"], local_file_path=str(path),
        url_path=f"/files/ecn-review/{attachment['id']}",
        file_type=str(mimetypes.guess_type(selection["name"])[0] or attachment.get("file_type") or "application/octet-stream"),
    )
    return updated, installed


def publish_overview_upload(item: dict, project: str, ecn_id: str, config: dict) -> tuple[dict, Path | None]:
    """只执行已确认的文件名、来源和内容；目录或文件发生变化时拒绝执行。"""
    new_data = item.get("new_data", {})
    attachment = new_data.get("overview_upload")
    selection = new_data.get("file_selection")
    if not isinstance(attachment, dict) or not isinstance(selection, dict):
        raise ECNConflict("方案未保存完整的文件选择，请重新编辑方案。")
    root = str(config.get("upload_path") or "")
    if not root:
        raise ECNConflict("概述参数未配置正式上传目录。")
    target = validate_file_selection(selection, Path(root))
    source = resolve_attachment(attachment)
    if not source.is_file() or file_digest(source) != selection["sha256"]:
        raise ECNConflict("送审文件已变化，不能执行。")
    existed = target.exists()
    if selection["mode"] == "new":
        copy_named_file(source, target, expected_digest=selection["sha256"])
    # 已有文件模式只引用，绝不从归档恢复被删除的正式文件。
    return {
        "local_file_path": str(target), "url_path": f"/files/{selection['name']}",
        "content": selection["name"],
    }, None if existed else target


def verify_published_overview_upload(item: dict, chip: dict, config: dict) -> None:
    """恢复检查点只校验正式文件，绝不重新发布或恢复被删除文件。"""
    selection = item.get("new_data", {}).get("file_selection")
    if not isinstance(selection, dict):
        raise ECNConflict("送审文件选择缺失，不能恢复执行")
    target = validate_file_selection({**selection, "mode": "existing"}, Path(str(config.get("upload_path") or "")))
    if Path(str(chip.get("local_file_path") or "")).resolve() != target.resolve():
        raise ECNConflict("已执行概述的正式文件路径与方案不一致")
