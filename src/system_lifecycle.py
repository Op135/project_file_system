"""管理员触发的系统重启与关闭协调。

危险操作必须先完成一份可验证的数据库备份，再交给 NiceGUI 的优雅关闭流程。
重启请求通过一个短生命周期标记从热重载子进程传递给父进程；父进程只有在服务器
已经退出、全部 shutdown hook 执行完毕后才会重启当前命令。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from nicegui import app

from .config import BASE_DIR

logger = logging.getLogger(__name__)

RESTART_REQUEST_PATH = BASE_DIR / ".nicegui" / "system-restart-request.json"
RESTART_REQUEST_MAX_AGE_SECONDS = 120


class SystemAction(str, Enum):
    RESTART = "restart"
    SHUTDOWN = "shutdown"


class BackupManager(Protocol):
    async def run_safe_backup(self, trigger_type: str, *, strict: bool = False) -> str: ...


@dataclass(frozen=True)
class SystemActionReceipt:
    action: SystemAction
    backup_path: str


_action_lock = asyncio.Lock()
_pending_action: SystemAction | None = None
_shutdown_tasks: set[asyncio.Task[None]] = set()


def action_label(action: SystemAction) -> str:
    return "重启" if action is SystemAction.RESTART else "关闭"


def clear_stale_restart_request() -> None:
    """在主进程开始服务前清除上次异常遗留的标记，避免意外重启循环。"""
    try:
        RESTART_REQUEST_PATH.unlink(missing_ok=True)
    except OSError:
        logger.exception("无法清理旧的系统重启标记")


def _write_restart_request(actor: str) -> None:
    RESTART_REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "parent_pid": os.getppid(),
    }
    temporary_path = RESTART_REQUEST_PATH.with_name(
        f"{RESTART_REQUEST_PATH.name}.{os.getpid()}.tmp"
    )
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary_path.replace(RESTART_REQUEST_PATH)


def consume_restart_request() -> bool:
    """读取并删除一次性重启标记；过期或损坏标记一律拒绝执行。"""
    if not RESTART_REQUEST_PATH.exists():
        return False
    try:
        payload = json.loads(RESTART_REQUEST_PATH.read_text(encoding="utf-8"))
        requested_at = datetime.fromisoformat(str(payload["requested_at"]))
        age_seconds = (datetime.now(timezone.utc) - requested_at).total_seconds()
        return 0 <= age_seconds <= RESTART_REQUEST_MAX_AGE_SECONDS
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        logger.exception("系统重启标记无效，已拒绝自动重启")
        return False
    finally:
        try:
            RESTART_REQUEST_PATH.unlink(missing_ok=True)
        except OSError:
            logger.exception("无法删除已消费的系统重启标记")


async def request_system_action(
    action: SystemAction,
    *,
    actor: str,
    backup_manager: BackupManager,
    delay_seconds: float = 1.5,
    shutdown_callback: Callable[[], None] = app.shutdown,
) -> SystemActionReceipt:
    """严格备份成功后安排优雅关闭；同一进程只接受首个操作请求。"""
    global _pending_action

    async with _action_lock:
        if _pending_action is not None:
            raise RuntimeError(f"系统已在执行{action_label(_pending_action)}，请勿重复操作")
        _pending_action = action

        trigger_type = "SYSTEM_RESTART_PRECHECK" if action is SystemAction.RESTART else "SYSTEM_SHUTDOWN_PRECHECK"
        try:
            backup_path = await backup_manager.run_safe_backup(trigger_type, strict=True)
            if not backup_path:
                raise RuntimeError("未生成有效的数据库备份文件")
        except BaseException:
            _pending_action = None
            raise

        logger.warning(
            "管理员 %s 已确认%s整个系统；安全备份：%s",
            actor,
            action_label(action),
            backup_path,
        )

        async def shutdown_later() -> None:
            await asyncio.sleep(max(0.0, delay_seconds))
            if action is SystemAction.RESTART:
                try:
                    _write_restart_request(actor)
                except OSError:
                    logger.exception("写入重启标记失败，已取消系统重启")
                    return
            logger.warning("开始%s整个系统，等待在途请求和数据库事务安全结束", action_label(action))
            shutdown_callback()

        task = asyncio.create_task(shutdown_later())
        _shutdown_tasks.add(task)
        task.add_done_callback(_shutdown_tasks.discard)
        return SystemActionReceipt(action=action, backup_path=backup_path)


def reset_action_state_for_tests() -> None:
    """仅供隔离单元测试中的模块级状态。"""
    global _pending_action
    _pending_action = None
    for task in tuple(_shutdown_tasks):
        task.cancel()
    _shutdown_tasks.clear()

