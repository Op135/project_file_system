"""概述文件按既有目标名称安全落盘，不改变历史路径和命名规则。

正式文件一经发布，不因业务失败、删除或撤销而回收。本模块只清理未发布的临时文件。
指纹仅用于校验内容，不用于生成文件名。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _save(source: BinaryIO, target: Path, expected_digest: str | None = None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".overview-upload-", suffix=".tmp", dir=target.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        digest = file_digest(temporary)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("文件在保存期间发生变化，请重新提交")
        try:
            # 同目录原子发布完整文件，不能用覆盖式复制处理并发上传。
            if os.name == "nt":
                # Windows 的 rename 不覆盖已存在目标，适用于共享盘；不依赖硬链接支持。
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or file_digest(target) != digest:
                raise ValueError("正式目录已有不同内容的同名文件，请选择已有文件或更换文件名")
        return target
    finally:
        temporary.unlink(missing_ok=True)


def copy_named_file(source: Path, target: Path, *, expected_digest: str | None = None) -> Path:
    if source.resolve() == target.resolve():
        digest = file_digest(source)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("文件在保存期间发生变化，请重新提交")
        return target
    with source.open("rb") as incoming:
        return _save(incoming, target, expected_digest)


def save_named_bytes(content: bytes, target: Path) -> Path:
    return _save(io.BytesIO(content), target)


def checked_filename(name: str) -> str:
    if (not name or name != name.strip() or name.endswith(".") or
        re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name in {".", ".."} or
        re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", name.split(".")[0])):
        raise ValueError("文件名无效，请只填写文件名，不要包含路径或特殊字符")
    return name


def make_file_selection(directory: Path, name: str, mode: str, digest: str) -> dict[str, str]:
    checked_filename(name)
    if not directory.is_dir():
        raise ValueError(f"正式目录不存在：{directory}")
    if mode not in {"new", "existing"} or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("文件选择或内容指纹无效")
    selection = {"directory": str(directory.resolve()), "name": name, "mode": mode, "sha256": digest}
    validate_file_selection(selection, directory)
    return selection


def validate_file_selection(selection: dict, directory: Path) -> Path:
    name = checked_filename(str(selection.get("name") or ""))
    if str(directory.resolve()) != selection.get("directory") or not directory.is_dir():
        raise ValueError("正式保存目录已变化，请重新选择文件")
    digest = str(selection.get("sha256") or "")
    if selection.get("mode") not in {"new", "existing"} or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("请先完成同名文件检查和选择")
    target = directory / name
    if target.exists():
        if not target.is_file() or file_digest(target) != digest:
            raise ValueError("正式目录同名文件内容已变化，请重新选择并送审，不能覆盖")
    elif selection["mode"] == "existing":
        raise ValueError("选择的服务器已有文件已不存在，请重新选择并送审")
    return target
