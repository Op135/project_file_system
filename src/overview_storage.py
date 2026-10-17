"""项目概述数据的业务存储入口。

概述写入在这里执行跨模块业务门禁；通用 ``db_storage`` 只提供事务能力，
不感知 ECN、审批状态或概述结构。
"""

from __future__ import annotations

import copy
import inspect
import json
from contextvars import ContextVar
from typing import Any, Callable

from . import db_storage
from .modules.ecn.overview_locks import find_changed_ecn_locked_overviews


_DELETE_VALUE = object()
_last_ecn_lock_message: ContextVar[str] = ContextVar("overview_last_ecn_lock_message", default="")


class OverviewWriteError(RuntimeError):
    """概述业务写入未提交；消息可直接反馈给操作用户。"""


def take_ecn_lock_message() -> str:
    """取出当前协程最近一次概述写入被 ECN 占用拦截的原因。"""
    message = _last_ecn_lock_message.get()
    _last_ecn_lock_message.set("")
    return message


def require_write_success(success: bool, default_message: str = "概述数据保存失败，请刷新后重试。") -> None:
    if not success:
        raise OverviewWriteError(take_ecn_lock_message() or default_message)


async def _load_latest_ecn_records(connection, storage) -> Any:
    async with connection.execute(
        f"SELECT value FROM {storage.TABLE_NAME} WHERE key = ?",
        ("ecn_management_data",),
    ) as cursor:
        row = await cursor.fetchone()
    return json.loads(row[0]) if row else {}


async def atomic_deep_update(
    path: list[str],
    update_function: Callable,
    *args,
    allow_ecn: bool = False,
    transaction_guard: Callable | None = None,
    storage=None,
    **kwargs,
) -> bool:
    """原子修改概述，并在同一事务中阻止改动被未完成 ECN 占用的条目。"""
    if not path or not str(path[0]).endswith("_over_data"):
        raise ValueError("概述存储路径必须以 '<项目>_over_data' 开始")
    storage = storage or db_storage
    top_key = str(path[0])
    deep_path = [str(value) for value in path[1:]]
    blocked = False
    _last_ecn_lock_message.set("")

    async def apply(current_top: Any, connection):
        nonlocal blocked
        if transaction_guard is not None:
            permitted = transaction_guard(current_top, connection)
            if inspect.isawaitable(permitted):
                permitted = await permitted
            if permitted is not True:
                blocked = True
                return storage.ATOMIC_NO_UPDATE
        before = copy.deepcopy(current_top)
        if not deep_path:
            new_top = update_function(copy.deepcopy(current_top), *args, **kwargs)
            if inspect.isawaitable(new_top):
                new_top = await new_top
            if new_top is storage.ATOMIC_NO_UPDATE:
                return storage.ATOMIC_NO_UPDATE
        else:
            new_top = copy.deepcopy(current_top) if isinstance(current_top, dict) else {}
            current_level = new_top
            for key in deep_path[:-1]:
                next_level = current_level.setdefault(key, {})
                if not isinstance(next_level, dict):
                    next_level = {}
                    current_level[key] = next_level
                current_level = next_level
            final_key = deep_path[-1]
            current_value = copy.deepcopy(current_level.get(final_key))
            new_value = update_function(current_value, *args, **kwargs)
            if inspect.isawaitable(new_value):
                new_value = await new_value
            if new_value is storage.ATOMIC_NO_UPDATE:
                return storage.ATOMIC_NO_UPDATE
            if new_value is _DELETE_VALUE:
                if final_key not in current_level:
                    return storage.ATOMIC_NO_UPDATE
                current_level.pop(final_key)
            else:
                current_level[final_key] = new_value

        if not allow_ecn:
            records = await _load_latest_ecn_records(connection, storage)
            conflicts = find_changed_ecn_locked_overviews(top_key, before, new_top, records)
            if conflicts:
                conflict = conflicts[0]
                _last_ecn_lock_message.set(
                    "概述已被ECN方案占用，普通通道不能修改："
                    f"{conflict['ecn_id']} / {conflict['project']} / {conflict['label']}"
                )
                blocked = True
                return storage.ATOMIC_NO_UPDATE
        return new_top

    success = await storage.atomic_deep_update_transaction([top_key], apply)
    return success and not blocked


async def checked_atomic_deep_update(path: list[str], update_function: Callable, *args, **kwargs) -> bool:
    """执行必须成功的概述原子写入；失败时中止调用链，避免继续记录成功副作用。"""
    success = await atomic_deep_update(path, update_function, *args, **kwargs)
    require_write_success(success)
    return True


async def set_item(
    key: str,
    value: Any,
    *,
    allow_ecn: bool = False,
    storage=None,
    raise_on_failure: bool = True,
) -> bool:
    success = await atomic_deep_update(
        [key],
        lambda _current: copy.deepcopy(value),
        allow_ecn=allow_ecn,
        storage=storage,
    )
    if raise_on_failure:
        require_write_success(success)
    return success


async def set_deep_item(
    path: list[str],
    value: Any,
    *,
    allow_ecn: bool = False,
    storage=None,
    raise_on_failure: bool = True,
) -> bool:
    success = await atomic_deep_update(
        path,
        lambda _current: copy.deepcopy(value),
        allow_ecn=allow_ecn,
        storage=storage,
    )
    if raise_on_failure:
        require_write_success(success)
    return success


async def del_deep_item(
    path: list[str],
    *,
    allow_ecn: bool = False,
    storage=None,
    raise_on_failure: bool = True,
) -> bool:
    deleted = False

    def remove(current: Any):
        nonlocal deleted
        if current is None:
            return (storage or db_storage).ATOMIC_NO_UPDATE
        deleted = True
        return _DELETE_VALUE

    success = await atomic_deep_update(
        path,
        remove,
        allow_ecn=allow_ecn,
        storage=storage,
    )
    result = success and deleted
    if raise_on_failure:
        require_write_success(result, "概述数据删除失败，请刷新后重试。")
    return result
