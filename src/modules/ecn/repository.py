"""ECN 存储入口：事务内分配编号、业务拒绝与成功结果区分、提交后通知刷新。"""

import copy
import time

from ... import (
    db_storage,
)
from ...ecn_management_config import (
    ECN_DATA_KEY,
    ECN_VERSION_KEY,
)
from .editing import (
    ECNConflict,
    ECNResult,
)
from .models import (
    generate_ecn_id,
)


async def atomic_ecn_deep_update(path, update_function, *args, **kwargs):
    changed = False

    def update(current):
        nonlocal changed
        result = update_function(current, *args, **kwargs)
        changed = result is not db_storage.ATOMIC_NO_UPDATE
        return result

    success = await db_storage.atomic_deep_update(path, update)
    if success and changed:
        await db_storage.set_item(ECN_VERSION_KEY, time.time())
    return success


async def del_ecn_deep_item(path):
    success = await db_storage.del_deep_item(path)
    if success:
        await db_storage.set_item(ECN_VERSION_KEY, time.time())
    return success


async def save_ecn_deep_item(path, data):
    if not await db_storage.set_deep_item(path, data):
        raise RuntimeError("关联资料保存失败")
    await db_storage.set_item(ECN_VERSION_KEY, time.time())


async def save_ecn_root_item(key, data):
    if not await db_storage.set_item(key, data):
        raise RuntimeError("ECN数据保存失败")
    await db_storage.set_item(ECN_VERSION_KEY, time.time())


async def mutate_record(ecn_id, operation, *, new_record=None, storage=None):
    """operation 接收最新单据与事务连接；新建时分配编号与插入是同一事务。

    不接受客户端指定的新编号。已有单据消失时拒绝更新，绝不从旧页面复活记录。
    """
    storage = storage or db_storage
    result = ECNResult(False)

    async def apply(current, connection):
        collection: dict = {}
        try:
            if new_record is not None:
                if current is not None and not isinstance(current, dict):
                    raise ECNConflict("ECN集合数据格式异常，无法创建单据。")
                collection = current or {}
                record = copy.deepcopy(new_record)
                record["ecn_id"] = generate_ecn_id(collection)
                record["basic_info"]["file_no"] = record["ecn_id"]
            else:
                if not isinstance(current, dict):
                    raise ECNConflict("单据已被删除，请关闭后刷新列表。")
                record = current
            updated = await operation(record, connection)
            result.record = copy.deepcopy(updated)
            if new_record is not None:
                collection[updated["ecn_id"]] = updated
                return collection
            return updated
        except ECNConflict as exc:
            result.message = str(exc)
            return storage.ATOMIC_NO_UPDATE

    path = [ECN_DATA_KEY] if new_record is not None else [ECN_DATA_KEY, ecn_id]
    success = await storage.atomic_deep_update_transaction(path, apply)
    result.ok = success and result.record is not None and not result.message
    if result.ok:
        await storage.set_item(ECN_VERSION_KEY, time.time())
    elif not result.message:
        result.message = "保存失败，本次修改未提交，请重试。"
    if not result.ok:
        result.record = None
    return result
