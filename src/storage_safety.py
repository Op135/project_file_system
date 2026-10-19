"""NiceGUI 通用 JSON 存储的并发与备份安全保护。

NiceGUI 的文件型 ``app.storage.general`` 会在初始化和每次变更时直接重写目标文件。
本模块在应用导入 NiceGUI 前串行化初始化，并在导入后把后续写入替换为“临时文件写入、
刷盘、原子替换”。这样热重载父子进程不会读到正在截断的半个 JSON 文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

logger = logging.getLogger(__name__)

GENERAL_STORAGE_RELATIVE_PATH: Final = Path(".nicegui") / "storage-general.json"
BACKUP_DIRECTORY_NAME: Final = "backups"
BACKUP_GLOB: Final = "storage-general_*.json"

# 这些键仍由 app.storage.general 持久化，SQLite 备份不能替代它们。
PROTECTED_GENERAL_KEYS: Final[tuple[str, ...]] = (
    "overview_pending_history",
    "overview_charge_pending",
    "project_summary",
    "overview_role",
    "wait_review",
    "custom_labels",
    "project_engineer",
    "project_sale",
    "project_req_max_ver",
    "temp_req",
    "user_preferences",
    "overview_only_need",
    "overview_active_state_checked_versions",
)

_REFERENCE_MIN_PROTECTED_BYTES: Final = 50_000
_MAX_AGGREGATE_SHRINK_RATIO: Final = 0.20
_MIN_COLLAPSED_KEYS: Final = 3
_PER_KEY_MEANINGFUL_BYTES: Final = 500
_PER_KEY_COLLAPSE_RATIO: Final = 0.05
_installed_storage_ids: set[int] = set()


class GeneralStorageSafetyError(RuntimeError):
    """通用 JSON 存储不满足安全要求。"""


@dataclass(frozen=True)
class GeneralStorageSnapshot:
    """一次完整且可解析的通用 JSON 文件快照。"""

    path: Path
    raw: bytes
    data: dict[str, object]
    sha256: str
    protected_bytes: int
    protected_key_bytes: dict[str, int]


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _decode_general_storage_snapshot(path: Path, raw: bytes) -> GeneralStorageSnapshot:
    """把文件字节解码为完成指标计算的通用存储快照。"""

    if not raw:
        raise GeneralStorageSafetyError(f"NiceGUI 通用存储为空文件：{path}")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GeneralStorageSafetyError(f"NiceGUI 通用存储不是完整 JSON：{path}") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise GeneralStorageSafetyError(f"NiceGUI 通用存储根节点必须是字符串键对象：{path}")

    data: dict[str, object] = dict(decoded)
    key_sizes = {key: _json_size(data[key]) if key in data else 0 for key in PROTECTED_GENERAL_KEYS}
    return GeneralStorageSnapshot(
        path=path,
        raw=raw,
        data=data,
        sha256=hashlib.sha256(raw).hexdigest(),
        protected_bytes=sum(key_sizes.values()),
        protected_key_bytes=key_sizes,
    )


def read_general_storage_snapshot(path: Path) -> GeneralStorageSnapshot:
    """读取并严格验证一个通用 JSON 存储文件。"""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GeneralStorageSafetyError(f"无法读取 NiceGUI 通用存储：{path}") from exc
    return _decode_general_storage_snapshot(path, raw)


def find_latest_general_storage_backup(backup_dir: Path) -> Path | None:
    """返回最新的通用 JSON 备份；同时间时以文件名稳定排序。"""

    candidates = [path for path in backup_dir.glob(BACKUP_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, path.name))


def assert_no_suspicious_storage_shrink(
    current: GeneralStorageSnapshot,
    reference: GeneralStorageSnapshot | None,
) -> None:
    """拒绝相对可信备份发生多键同步坍缩的当前文件。"""

    if reference is None or reference.protected_bytes < _REFERENCE_MIN_PROTECTED_BYTES:
        return
    aggregate_ratio = current.protected_bytes / reference.protected_bytes
    collapsed_keys = [
        key
        for key in PROTECTED_GENERAL_KEYS
        if reference.protected_key_bytes[key] >= _PER_KEY_MEANINGFUL_BYTES
        and current.protected_key_bytes[key]
        < reference.protected_key_bytes[key] * _PER_KEY_COLLAPSE_RATIO
    ]
    if aggregate_ratio < _MAX_AGGREGATE_SHRINK_RATIO and len(collapsed_keys) >= _MIN_COLLAPSED_KEYS:
        collapsed_text = "、".join(collapsed_keys)
        raise GeneralStorageSafetyError(
            "NiceGUI 通用存储相对最近备份发生异常缩水，已拒绝继续运行："
            f"当前受保护数据 {current.protected_bytes} 字节，"
            f"参考备份 {reference.protected_bytes} 字节；坍缩键：{collapsed_text}。"
            f"当前文件：{current.path}；参考备份：{reference.path}"
        )


def validate_general_storage_against_latest_backup(
    storage_path: Path,
    backup_dir: Path,
) -> GeneralStorageSnapshot | None:
    """校验当前文件，并用最近备份识别明显的整库空壳覆盖。"""

    latest_backup = find_latest_general_storage_backup(backup_dir)
    if not storage_path.exists():
        if latest_backup is None:
            return None
        raise GeneralStorageSafetyError(
            f"当前 NiceGUI 通用存储不存在，但发现历史备份：{latest_backup}。请先人工恢复。"
        )
    current = read_general_storage_snapshot(storage_path)
    reference = read_general_storage_snapshot(latest_backup) if latest_backup is not None else None
    assert_no_suspicious_storage_shrink(current, reference)
    return current


def _lock_file(file_obj: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX)


def _unlock_file(file_obj: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        file_obj.seek(0)
        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


@contextmanager
def storage_file_lock(storage_path: Path) -> Iterator[None]:
    """跨进程串行化同一通用存储文件的初始化或替换。"""

    lock_path = storage_path.with_suffix(f"{storage_path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        _lock_file(lock_file)
        try:
            yield
        finally:
            _unlock_file(lock_file)


@contextmanager
def nicegui_storage_startup_guard(project_root: Path) -> Iterator[None]:
    """在 NiceGUI 导入期间加锁，并在其读取前阻止已知空壳文件启动。"""

    storage_path = project_root / GENERAL_STORAGE_RELATIVE_PATH
    backup_dir = project_root / BACKUP_DIRECTORY_NAME
    with storage_file_lock(storage_path):
        validate_general_storage_against_latest_backup(storage_path, backup_dir)
        yield


def atomic_write_bytes(path: Path, content: bytes, *, content_already_validated: bool = False) -> None:
    """同目录写入临时文件，刷盘后原子替换目标文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as temp_file:
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        # 常规调用在替换前再次解析；运行时存储已经在持锁区验证，可避免重复解析大文件。
        if not content_already_validated:
            read_general_storage_snapshot(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install_safe_nicegui_general_storage(general_storage: object) -> bool:
    """把 NiceGUI 文件存储的变更回调替换为原子、拒绝陈旧覆盖的实现。"""

    from nicegui import background_tasks, core
    from nicegui.persistence.file_persistent_dict import FilePersistentDict

    if not isinstance(general_storage, FilePersistentDict):
        logger.info("NiceGUI 通用存储不是本地文件后端，无需安装文件原子写保护。")
        return False
    storage_id = id(general_storage)
    if storage_id in _installed_storage_ids:
        return True

    storage_path = general_storage.filepath
    initial = read_general_storage_snapshot(storage_path) if storage_path.exists() else None
    expected_sha256: list[str | None] = [initial.sha256 if initial is not None else None]
    reference_cache: list[tuple[Path, int, int, GeneralStorageSnapshot] | None] = [None]

    def get_reference_snapshot() -> GeneralStorageSnapshot | None:
        latest_backup = find_latest_general_storage_backup(
            storage_path.parent.parent / BACKUP_DIRECTORY_NAME
        )
        if latest_backup is None:
            reference_cache[0] = None
            return None
        stat = latest_backup.stat()
        cached = reference_cache[0]
        if (
            cached is not None
            and cached[0] == latest_backup
            and cached[1] == stat.st_mtime_ns
            and cached[2] == stat.st_size
        ):
            return cached[3]
        snapshot = read_general_storage_snapshot(latest_backup)
        reference_cache[0] = (latest_backup, stat.st_mtime_ns, stat.st_size, snapshot)
        return snapshot

    def persist_snapshot(content: bytes) -> None:
        with storage_file_lock(storage_path):
            disk_raw = storage_path.read_bytes() if storage_path.exists() else None
            disk_sha256 = hashlib.sha256(disk_raw).hexdigest() if disk_raw is not None else None
            if disk_sha256 != expected_sha256[0]:
                candidate_sha256 = hashlib.sha256(content).hexdigest()
                if disk_sha256 == candidate_sha256:
                    expected_sha256[0] = disk_sha256
                    return
                raise GeneralStorageSafetyError(
                    "检测到另一个进程已更新 NiceGUI 通用存储，已拒绝用本进程的陈旧快照覆盖："
                    f"{storage_path}"
                )
            candidate = _decode_general_storage_snapshot(storage_path, content)
            reference = get_reference_snapshot()
            if reference is None and disk_raw is not None:
                reference = _decode_general_storage_snapshot(storage_path, disk_raw)
            assert_no_suspicious_storage_shrink(candidate, reference)
            atomic_write_bytes(storage_path, content, content_already_validated=True)
            expected_sha256[0] = hashlib.sha256(content).hexdigest()

    def safe_backup() -> None:
        async def async_backup() -> None:
            content = json.dumps(
                general_storage,
                ensure_ascii=False,
                indent=general_storage.indent,
            ).encode(general_storage.encoding or "utf-8")
            await asyncio.to_thread(persist_snapshot, content)

        if core.loop and core.loop.is_running():
            background_tasks.create_lazy(
                background_tasks.await_on_shutdown(async_backup)(),
                name=f"safe-{storage_path.resolve()}",
            )
        else:
            content = json.dumps(
                general_storage,
                ensure_ascii=False,
                indent=general_storage.indent,
            ).encode(general_storage.encoding or "utf-8")
            persist_snapshot(content)

    # FilePersistentDict 构造时保存的是旧 backup 的绑定方法，改类方法不能替换已有回调。
    general_storage._change_handlers = [safe_backup]
    # NiceGUI 原实现会在 clear 后直接 unlink 正式文件，绕过变更回调和原子替换。
    def reject_clear() -> None:
        raise GeneralStorageSafetyError(
            "禁止直接清空 NiceGUI 通用存储；请使用经过业务校验的逐项维护或受控恢复流程。"
        )

    setattr(general_storage, "clear", reject_clear)
    _installed_storage_ids.add(storage_id)
    logger.info("NiceGUI 通用 JSON 存储原子写与陈旧覆盖保护已启用：%s", storage_path)
    return True


def validated_storage_bytes(storage_path: Path, backup_dir: Path) -> bytes:
    """返回通过解析和异常缩水检查的当前存储字节，供备份原样落盘。"""

    with storage_file_lock(storage_path):
        snapshot = validate_general_storage_against_latest_backup(storage_path, backup_dir)
        if snapshot is None:
            raise GeneralStorageSafetyError(f"NiceGUI 通用存储不存在：{storage_path}")
        return snapshot.raw


