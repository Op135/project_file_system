import asyncio
import copy
import datetime
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import aiosqlite

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)
# 文件夹路径设定
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
DB_PATH = f"{BASE_DIR}/db/nicegui_storage.db"  # 数据库文件名
TABLE_NAME = "general_storage"  # 模拟 general storage
JSON_ENTITY_TABLE_NAME = "json_entity_storage"  # 按业务实体逐行保存JSON，避免整块集合重写
JSON_ENTITY_NAMESPACE_TABLE_NAME = "json_entity_namespaces"  # 记录已初始化命名空间，允许合法空集合
ATOMIC_NO_UPDATE = object()  # update_function 返回此值时，原子事务不写入任何数据
ATOMIC_DELETE = object()  # update_function 返回此值时，原子删除当前实体
# 内存缓存 (The "Instant Retrieval" part)
# 这就是实现“即时获取”的关键。数据从内存中读取。
_data_cache: Dict[str, Any] = {}
_entity_cache: Dict[str, Dict[str, Any]] = {}
_entity_namespaces: set[str] = set()
_db: Optional[aiosqlite.Connection] = None

# 1. 初始化事件：防止在缓存加载完成前执行 RMW 操作
_init_done = asyncio.Event()  # 相当于创建一个红灯告示牌，所有操作都要排队等候

# SQLite共享连接的事务锁只保护实际数据库事务；业务资源另按顶层键或实体ID分别排队。
_db_transaction_lock = asyncio.Lock()
_resource_locks: Dict[str, asyncio.Lock] = {}
LOCK_WAIT_WARNING_SECONDS = 0.05
LOCK_WAIT_WARNING_INTERVAL_SECONDS = 5.0
_last_lock_wait_warnings: Dict[str, float] = {}


def _get_resource_lock(resource_key: str) -> asyncio.Lock:
    """为业务资源返回稳定的进程内锁，不改变上层调用接口。"""
    lock = _resource_locks.get(resource_key)
    if lock is None:
        lock = asyncio.Lock()
        _resource_locks[resource_key] = lock
    return lock


@asynccontextmanager
async def _timed_lock(lock: asyncio.Lock, lock_kind: str, resource_key: str):
    """获取锁并记录异常等待，便于定位热点模块和大JSON写入。"""
    started = time.perf_counter()
    await lock.acquire()
    waited = time.perf_counter() - started
    warning_key = f"{lock_kind}:{resource_key}"
    now = time.monotonic()
    last_warning = _last_lock_wait_warnings.get(warning_key, 0.0)
    if (
        waited >= LOCK_WAIT_WARNING_SECONDS
        and now - last_warning >= LOCK_WAIT_WARNING_INTERVAL_SECONDS
    ):
        _last_lock_wait_warnings[warning_key] = now
        logger.warning(
            "%s等待 %.1f ms：%s",
            lock_kind,
            waited * 1000,
            resource_key,
        )
    try:
        yield
    finally:
        lock.release()


async def init_db() -> None:
    """
    在服务器启动时调用的初始化函数。
    1. 连接数据库。
    2. 创建表（如果不存在）。
    3. 将数据库中的所有数据加载到内存缓存 _data_cache 中。
    """
    global _db, _data_cache, _entity_cache, _entity_namespaces
    _db = await aiosqlite.connect(DB_PATH)
    # 启用 WAL 模式 (Write-Ahead Logging) 提高并发性能
    await _db.execute("PRAGMA journal_mode=WAL;")
    # 多进程同时争用写事务时等待最多 30 秒，避免短暂竞争直接导致原子更新失败
    await _db.execute("PRAGMA busy_timeout=30000;")

    # 创建一个简单的 key-value 表
    # 我们将 key 存为 TEXT，将 value 序列化为 JSON 字符串后存为 TEXT
    await _db.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    await _db.execute(f"""
        CREATE TABLE IF NOT EXISTS {JSON_ENTITY_TABLE_NAME} (
            namespace TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (namespace, entity_id)
        )
    """)
    await _db.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{JSON_ENTITY_TABLE_NAME}_namespace "
        f"ON {JSON_ENTITY_TABLE_NAME} (namespace)"
    )
    await _db.execute(f"""
        CREATE TABLE IF NOT EXISTS {JSON_ENTITY_NAMESPACE_TABLE_NAME} (
            namespace TEXT PRIMARY KEY
        )
    """)
    await _db.commit()

    # 从数据库加载所有现有数据到内存缓存
    logger.info(f"从数据库{DB_PATH}加载所有现有数据到内存缓存...")
    async with _db.execute(f"SELECT key, value FROM {TABLE_NAME}") as cursor:
        async for row in cursor:
            key, value_json = row
            try:
                _data_cache[key] = json.loads(value_json)
            except json.JSONDecodeError:
                logger.error(f"警告: 不能从键'{key}'中解码json数据", exc_info=True)
                _data_cache[key] = None  # 或 other default

    _entity_cache = {}
    _entity_namespaces = set()
    async with _db.execute(
        f"SELECT namespace FROM {JSON_ENTITY_NAMESPACE_TABLE_NAME}"
    ) as cursor:
        async for (namespace,) in cursor:
            _entity_namespaces.add(namespace)
            _entity_cache.setdefault(namespace, {})
    async with _db.execute(
        f"SELECT namespace, entity_id, value FROM {JSON_ENTITY_TABLE_NAME}"
    ) as cursor:
        async for namespace, entity_id, value_json in cursor:
            try:
                _entity_cache.setdefault(namespace, {})[entity_id] = json.loads(value_json)
            except json.JSONDecodeError:
                logger.error(
                    "不能解码实体存储数据：namespace=%s, entity_id=%s",
                    namespace,
                    entity_id,
                    exc_info=True,
                )

    logger.info(f"装载{len(_data_cache)}条数据到缓存中.")
    # 通知所有等待者，初始化已完成
    _init_done.set()
    logger.info("数据库初始化完成，缓存已就绪。")


async def close_db():
    """
    在服务器关闭时调用，确保数据库连接被安全关闭。
    """
    if _db:
        await _db.close()
        logger.info("数据库连接关闭.")


async def _internal_set(
    key: str,
    value: Any,
    value_json: Optional[str] = None,
):
    """
    内部函数：假设数据库事务锁已被持有。
    只执行数据库和缓存的写入操作。
    """
    if _db is None:
        logger.info("错误: 数据库未初始化.")
        return
    try:
        # 将 Python 对象序列化为 JSON 字符串
        # 这是数据持久化和深拷贝的“唯一真实来源”
        if value_json is None:
            value_json = json.dumps(value)
        # 好处 1（验证）： 如果 value 包含 datetime 等非法类型，json.dumps(value) 会立即失败。数据库和缓存都不会被修改。
        # 数据类型受限： 任何 JSON 不认识的类型（set, tuple, datetime, bytes, 自定义类）都会导致失败或数据失真。
        # 数据失真（有损）： 字典的 int 键会变成 str 键。tuple 会变成 list。这对某些应用是致命的。

        # 3. 持久化到数据库 (优先)
        # 使用 INSERT OR REPLACE (UPSERT) 来插入或更新
        await _db.execute(f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)", (key, value_json))
        await _db.commit()

        # 4. 更新内存缓存 (使用深拷贝)
        #    通过从刚序列化的字符串中 "loads"，我们确保缓存中的
        #    对象是一个全新的、无引用的副本，与数据库100%一致。
        _data_cache[key] = json.loads(value_json)
        # 好处 2（深拷贝）： json.loads(value_json) 创建了一个 100% 干净的、与数据库内容完全一致的深拷贝副本，从而解决了您担心的浅引用问题。
    except Exception:
        # 在实际的锁持有函数中处理这个异常
        logger.error(f"错误：内部写入失败：'{key}'", exc_info=True)
        raise  # 抛出异常，让外层函数知道失败了


async def _internal_remove(key: str):
    """
    内部函数：假设数据库事务锁已被持有。
    只执行数据库和缓存的删除操作。
    """
    if _db is None:
        logger.info("错误: 数据库未初始化.")
        return
    try:
        # 3. 从数据库删除
        await _db.execute(f"DELETE FROM {TABLE_NAME} WHERE key = ?", (key,))
        await _db.commit()

        # 4. 从缓存删除
        if key in _data_cache:
            del _data_cache[key]
    except Exception:
        logger.error(f"错误：内部删除失败：'{key}'", exc_info=True)
        raise  # 抛出异常


def get_item(key: str, default: Any = None, return_ref: bool = False) -> Any:
    """
    从内存缓存中获取数据。

    :param key: 键名
    :param default: 默认值
    :param return_ref: 如果为 True，则返回原始内存引用（极快，但绝对禁止修改返回的对象！）；
                       如果为 False（默认），返回深拷贝（安全，防篡改）。
    :return: 查找到的值或默认值
    """
    val = _data_cache.get(key, default)
    if val is default or val is None:
        return default
    if return_ref:
        return val  # 极速模式：直接返回引用

    return copy.deepcopy(val)  # 安全模式：返回深拷贝


def get_json_entities(namespace: str, return_ref: bool = False) -> Dict[str, Any]:
    """读取一个命名空间下的全部独立JSON实体。"""
    entities = _entity_cache.get(namespace, {})
    return entities if return_ref else copy.deepcopy(entities)


def is_json_entity_namespace_initialized(namespace: str) -> bool:
    """判断实体命名空间是否已完成建库或旧数据迁移。"""
    return namespace in _entity_namespaces


def get_json_entity(namespace: str, entity_id: str, default: Any = None) -> Any:
    """读取一个独立JSON实体并返回深拷贝。"""
    entity = _entity_cache.get(namespace, {}).get(entity_id, default)
    if entity is default or entity is None:
        return default
    return copy.deepcopy(entity)


async def get_fresh_item(key: str, default: Any = None) -> Any:
    """直接从SQLite读取顶层键并刷新本进程缓存，供多进程版本检查使用。"""
    await _init_done.wait()
    if _db is None:
        return default
    resource_key = f"general:{key}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                async with _db.execute(
                    f"SELECT value FROM {TABLE_NAME} WHERE key = ?",
                    (key,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    _data_cache.pop(key, None)
                    return default
                value = json.loads(row[0])
                _data_cache[key] = value
                return copy.deepcopy(value)
            except Exception:
                logger.exception("读取最新顶层键失败：%s", key)
                return default


async def refresh_json_entities(namespace: str) -> int:
    """从SQLite重新加载一个实体命名空间，确保多服务实例间能够看到最新记录。"""
    await _init_done.wait()
    if _db is None:
        return 0
    resource_key = f"entity-namespace:{namespace}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                refreshed: Dict[str, Any] = {}
                async with _db.execute(
                    f"SELECT entity_id, value FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ?",
                    (namespace,),
                ) as cursor:
                    async for entity_id, value_json in cursor:
                        refreshed[entity_id] = json.loads(value_json)
                _entity_cache[namespace] = refreshed
                return len(refreshed)
            except Exception:
                logger.exception("刷新实体命名空间失败：%s", namespace)
                return 0


async def migrate_json_dict_to_entities(
    namespace: str,
    legacy_key: str,
) -> int:
    """首次启动时把旧的字典型顶层键原子复制到独立实体表。"""
    await _init_done.wait()
    if _db is None:
        logger.error("实体数据迁移失败：数据库尚未初始化")
        return 0

    resource_key = f"entity-namespace:{namespace}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                await _db.execute("BEGIN IMMEDIATE")
                async with _db.execute(
                    f"SELECT COUNT(*) FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ?",
                    (namespace,),
                ) as cursor:
                    existing_row = await cursor.fetchone()
                if existing_row and int(existing_row[0]) > 0:
                    await _db.execute(
                        f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                        (namespace,),
                    )
                    await _db.commit()
                    _entity_namespaces.add(namespace)
                    return 0

                async with _db.execute(
                    f"SELECT value FROM {TABLE_NAME} WHERE key = ?",
                    (legacy_key,),
                ) as cursor:
                    legacy_row = await cursor.fetchone()
                legacy_data = json.loads(legacy_row[0]) if legacy_row else {}
                if not isinstance(legacy_data, dict):
                    await _db.rollback()
                    logger.error("实体数据迁移失败：旧键 %s 不是字典", legacy_key)
                    return 0

                migrated: Dict[str, Any] = {}
                for entity_id, value in legacy_data.items():
                    if not isinstance(entity_id, str) or not isinstance(value, dict):
                        continue
                    value_json = json.dumps(value)
                    await _db.execute(
                        f"INSERT INTO {JSON_ENTITY_TABLE_NAME} (namespace, entity_id, value) VALUES (?, ?, ?)",
                        (namespace, entity_id, value_json),
                    )
                    migrated[entity_id] = json.loads(value_json)
                await _db.execute(
                    f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                    (namespace,),
                )
                await _db.commit()
                _entity_cache[namespace] = migrated
                _entity_namespaces.add(namespace)
                if migrated:
                    logger.info(
                        "已把旧键 %s 的 %s 条记录迁移到实体命名空间 %s；旧键保留为只读回滚备份",
                        legacy_key,
                        len(migrated),
                        namespace,
                    )
                return len(migrated)
            except Exception:
                if _db.in_transaction:
                    await _db.rollback()
                logger.exception("实体数据迁移失败：namespace=%s, legacy_key=%s", namespace, legacy_key)
                return 0


async def atomic_json_entity_update(
    namespace: str,
    entity_id: str,
    update_function: Callable,
    *args,
    **kwargs,
) -> bool:
    """在单个事务内读取、校验并更新或删除一条JSON实体。"""
    await _init_done.wait()
    if _db is None:
        logger.error("实体更新失败：数据库尚未初始化")
        return False

    resource_key = f"entity:{namespace}:{entity_id}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                await _db.execute("BEGIN IMMEDIATE")
                async with _db.execute(
                    f"SELECT value FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ? AND entity_id = ?",
                    (namespace, entity_id),
                ) as cursor:
                    row = await cursor.fetchone()
                current_value = json.loads(row[0]) if row else None
                new_value = update_function(copy.deepcopy(current_value), *args, **kwargs)
                if new_value is ATOMIC_NO_UPDATE:
                    await _db.rollback()
                    if current_value is None:
                        _entity_cache.setdefault(namespace, {}).pop(entity_id, None)
                    else:
                        _entity_cache.setdefault(namespace, {})[entity_id] = copy.deepcopy(current_value)
                    return True
                if new_value is ATOMIC_DELETE:
                    await _db.execute(
                        f"DELETE FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ? AND entity_id = ?",
                        (namespace, entity_id),
                    )
                    await _db.commit()
                    _entity_cache.setdefault(namespace, {}).pop(entity_id, None)
                    _entity_namespaces.add(namespace)
                    return True

                value_json = json.dumps(new_value)
                await _db.execute(
                    f"INSERT OR REPLACE INTO {JSON_ENTITY_TABLE_NAME} (namespace, entity_id, value) VALUES (?, ?, ?)",
                    (namespace, entity_id, value_json),
                )
                await _db.execute(
                    f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                    (namespace,),
                )
                await _db.commit()
                _entity_cache.setdefault(namespace, {})[entity_id] = json.loads(value_json)
                _entity_namespaces.add(namespace)
                return True
            except Exception:
                if _db.in_transaction:
                    await _db.rollback()
                logger.exception(
                    "独立实体原子更新失败：namespace=%s, entity_id=%s",
                    namespace,
                    entity_id,
                )
                return False


async def insert_json_entities(namespace: str, entities: Dict[str, Any]) -> bool:
    """在一个事务内批量新增独立JSON实体，任意主键冲突都会整体回滚。"""
    if not entities:
        return True
    await _init_done.wait()
    if _db is None:
        logger.error("实体批量新增失败：数据库尚未初始化")
        return False

    serialized = {
        entity_id: json.dumps(value)
        for entity_id, value in entities.items()
    }
    resource_key = f"entity-namespace:{namespace}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                await _db.execute("BEGIN IMMEDIATE")
                await _db.executemany(
                    f"INSERT INTO {JSON_ENTITY_TABLE_NAME} (namespace, entity_id, value) VALUES (?, ?, ?)",
                    [
                        (namespace, entity_id, value_json)
                        for entity_id, value_json in serialized.items()
                    ],
                )
                await _db.execute(
                    f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                    (namespace,),
                )
                await _db.commit()
                namespace_cache = _entity_cache.setdefault(namespace, {})
                for entity_id, value_json in serialized.items():
                    namespace_cache[entity_id] = json.loads(value_json)
                _entity_namespaces.add(namespace)
                return True
            except Exception:
                if _db.in_transaction:
                    await _db.rollback()
                logger.exception("独立实体批量新增失败：namespace=%s", namespace)
                return False


async def set_item(key: str, value: Any) -> None:
    """
    深拷贝，设置一个值。

    (包装在资源锁、数据库事务锁和 _init_done.wait() 中)
    设置一个顶层值。
    1. 等待初始化完成。
    2. 获取写入锁。
    3. 异步地将数据持久化到 SQLite 数据库。
    4. 更新内存缓存（使用反序列化后的深拷贝）。
    """
    # 1. 等待初始化完成
    await _init_done.wait()
    if _db is None:
        logger.info("错误: 数据库未初始化.")
        return

    # 序列化不占用共享数据库连接，让其它模块可在此期间完成自己的事务。
    try:
        value_json = await asyncio.to_thread(json.dumps, value)
    except Exception:
        logger.error("错误：序列化写入值失败：'%s'", key, exc_info=True)
        raise

    resource_key = f"general:{key}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            await _internal_set(key, value, value_json)


async def remove_item(key: str) -> bool:
    """
    (包装在资源锁、数据库事务锁和 _init_done.wait() 中)
    删除一个值。
    1. 从内存缓存中删除。
    2. 异步地从 SQLite 数据库中删除。
    删除成功返回True，失败返回False
    """

    # 1. 等待初始化完成
    await _init_done.wait()
    if _db is None:
        logger.info("错误: 数据库未初始化.")
        return False

    resource_key = f"general:{key}"
    async with _timed_lock(_get_resource_lock(resource_key), "业务资源锁", resource_key):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                await _internal_remove(key)
                return True
            except Exception:
                return False


def get_deep_item(path: List[str], default: Any = None, return_ref: bool = False) -> Any:
    """
    从 db_storage 中“即时获取”一个任意深度的值（深拷贝）。
    (这个函数是只读的，不需要等待或加锁)

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    :param default: 如果找不到，返回的默认值
    :param return_ref: 如果为 True，则返回原始内存引用（极快，但绝对禁止修改返回的对象！）；
                       如果为 False（默认），返回深拷贝（安全，防篡改）。
    :return: 查找到的值或默认值
    """
    if not path:
        return default

    # 路径的第一个元素是顶层键
    top_key = path[0]
    deep_path = path[1:]

    # 直接在只读缓存引用上定位目标，避免先深拷贝整个顶层对象。
    missing = object()
    current_level_data = get_item(top_key, missing, return_ref=True)
    if current_level_data is missing or current_level_data is None:
        return default

    if not deep_path:
        # 如果路径只有一个元素，就是返回顶层对象
        return current_level_data if return_ref else copy.deepcopy(current_level_data)

    # 2. 逐层深入查找
    for key in deep_path:
        if not isinstance(current_level_data, dict):
            # 路径尚未走完，但数据已不是字典，无法继续深入
            return default

        if key not in current_level_data:
            return default
        current_level_data = current_level_data[key]
        if current_level_data is None:
            # 提前在路径中遇到 None
            return default

    if return_ref:
        return current_level_data

    return copy.deepcopy(current_level_data)


async def set_deep_item(path: List[str], value: Any) -> None:
    """
    异步设置一个任意深度的值，并在原子事务中持久化到数据库。
    如果路径上的字典不存在，会自动创建它们。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    :param value: 要设置的新值
    """
    if not path:
        raise ValueError("路径列表 'path' 不能为空")

    top_key = path[0]
    deep_path = path[1:]
    if not deep_path:
        await set_item(top_key, value)
        return

    def apply_set(current_top: Any) -> Dict[str, Any]:
        top_level_data = current_top if isinstance(current_top, dict) else {}
        current_level_data = top_level_data
        for key in deep_path[:-1]:
            next_level = current_level_data.setdefault(key, {})
            if not isinstance(next_level, dict):
                next_level = {}
                current_level_data[key] = next_level
            current_level_data = next_level
        current_level_data[deep_path[-1]] = value
        return top_level_data

    await atomic_deep_update([top_key], apply_set)


async def del_deep_item(path: List[str]) -> bool:
    """
    异步删除一个任意深度的值（原子 RMW），并持久化到数据库。
    如果路径不存在，函数会返回False，不会报错。
    路径存在且删除成功，函数会返回True。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    """
    if not path:
        logger.info("路径列表 'path' 不能为空")
        return False

    top_key = path[0]
    deep_path = path[1:]
    if not deep_path:
        return await remove_item(top_key)

    deleted = False

    def apply_delete(current_top: Any) -> Any:
        nonlocal deleted
        if not isinstance(current_top, dict):
            return ATOMIC_NO_UPDATE
        current_level_data = current_top
        for key in deep_path[:-1]:
            next_level = current_level_data.get(key)
            if not isinstance(next_level, dict):
                return ATOMIC_NO_UPDATE
            current_level_data = next_level
        final_key = deep_path[-1]
        if final_key not in current_level_data:
            return ATOMIC_NO_UPDATE
        del current_level_data[final_key]
        deleted = True
        return current_top

    success = await atomic_deep_update([top_key], apply_delete)
    return success and deleted


async def atomic_deep_update(path: List[str], update_function: Callable, *args, **kwargs) -> bool:
    """
    在任意深层位置执行一次不可被其它写操作插入的“读取 -> 修改 -> 写回”操作。

    通俗地说，它会先“锁门”，读取数据库中最新的数据，在副本上完成修改并写回，最后再“开门”。
    在整个过程中，其它协程或其它服务进程不能插入一次写操作，因此不会发生并发覆盖。

    示例：
        path = ["error_management_data", "ERR-001", "status"]
        表示修改 error_management_data 字典中，ERR-001 异常单下面的 status 字段。

    :param path: 从顶层键开始的访问路径，至少需要包含一个键。
    :param update_function: 接收目标位置当前值并返回新值的函数；返回 ATOMIC_NO_UPDATE 表示主动放弃写入。
    :param args: 原样传给 update_function 的额外位置参数。
    :param kwargs: 原样传给 update_function 的额外关键字参数。
    :return: True 表示事务正常完成，包括主动放弃写入；False 表示数据库操作发生异常。
    """
    # path 决定要修改数据库中的哪个位置；空列表无法定位任何数据，所以直接拒绝。
    if not path:
        # 使用 ValueError 明确告诉调用方：问题来自传入参数，而不是数据库故障。
        raise ValueError("路径列表 'path' 不能为空")

    # 数据库初始化完成后，_init_done 才会被设置；这里等待可以避免在连接尚未建立时执行更新。
    await _init_done.wait()

    # _db 保存当前模块使用的 SQLite 异步连接；正常初始化后它不应为 None。
    if _db is None:
        # 记录错误原因，方便从终端或日志判断是启动顺序问题。
        logger.info("错误: 数据库未初始化.")
        # 没有数据库连接，无法执行更新，以 False 告知调用方失败。
        return False

    # path 的第一个元素对应数据库表中的 key，例如 "error_management_data"。
    top_key = path[0]

    # 剩余元素是顶层对象内部的访问路径；如果为空，说明调用方想直接修改整个顶层值。
    deep_path = path[1:]

    # 同一顶层键先按业务资源排队；真正使用共享连接时再进入全局数据库事务锁。
    resource_key = f"general:{top_key}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
    ), _timed_lock(
        _db_transaction_lock,
        "数据库事务锁",
        resource_key,
    ):
        # try 会捕获事务期间的所有异常，保证失败时能够回滚并返回 False。
        try:
            # BEGIN IMMEDIATE 会立即申请 SQLite 写锁。
            # 它负责阻止其它服务进程在本事务结束前写入，从而提供跨进程并发保护。
            await _db.execute("BEGIN IMMEDIATE")

            # 从数据库表读取 top_key 对应的最新 JSON 字符串。
            # 这里故意不读取 _data_cache，因为其它进程写入后，本进程缓存可能还是旧数据。
            async with _db.execute(f"SELECT value FROM {TABLE_NAME} WHERE key = ?", (top_key,)) as cursor:
                # 顶层键是唯一键，所以最多只会取到一行；不存在时 row 为 None。
                row = await cursor.fetchone()

            # 数据存在时，把 JSON 字符串还原为 Python 对象；不存在时使用 None 表示“尚未创建”。
            current_top_value = json.loads(row[0]) if row else None

            # deep_path 为空表示 path 只有一层，例如 ["error_management_data"]，
            # 此时 update_function 修改的是整个顶层值，而不是其中某个子字段。
            if not deep_path:
                # 把当前值深拷贝后交给业务函数，防止业务函数直接修改原对象造成意外副作用。
                data_to_process = copy.deepcopy(current_top_value)

                # 执行业务方提供的修改函数，并把附加参数一起传进去。
                # new_data 就是业务函数希望保存的新顶层值。
                new_data = update_function(data_to_process, *args, **kwargs)

                # ATOMIC_NO_UPDATE 是特殊哨兵值，表示业务检查已正常完成，但决定不修改数据库。
                if new_data is ATOMIC_NO_UPDATE:
                    # 因为不需要写入，所以回滚刚才开启的事务并释放 SQLite 写锁。
                    await _db.rollback()

                    # 数据库中原本不存在这个键时，缓存中也应删除它，避免缓存保留不存在的旧数据。
                    if current_top_value is None:
                        # pop 的默认值 None 可保证缓存中没有该键时也不会报错。
                        _data_cache.pop(top_key, None)
                    else:
                        # 数据库中原本有值时，用事务读取到的最新值刷新本进程缓存。
                        _data_cache[top_key] = copy.deepcopy(current_top_value)

                    # 主动放弃写入是一次正常业务结果，不属于数据库失败，所以返回 True。
                    return True

                # SQLite 表中保存的是 JSON 文本，因此先把业务函数返回的新对象序列化为字符串。
                value_json = json.dumps(new_data)

                # INSERT OR REPLACE 表示：top_key 不存在就新增，已经存在就替换为新 JSON。
                await _db.execute(
                    f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)",
                    (top_key, value_json),
                )

                # 提交事务，使刚才的写入正式生效并释放 SQLite 写锁。
                await _db.commit()

                # 数据库提交成功后，再同步更新本进程内存缓存。
                # 使用 json.loads 创建独立对象，避免调用方继续持有并修改缓存中的对象。
                _data_cache[top_key] = json.loads(value_json)

                # 整个顶层值已经成功写入数据库和缓存。
                return True

            # 下面处理多层路径，例如 ["error_management_data", "ERR-001", "status"]。
            # 顶层数据存在时对它做深拷贝；不存在时从空字典开始创建路径。
            top_level_data_copy = copy.deepcopy(current_top_value) if current_top_value is not None else {}

            # 深层路径只能在字典中逐层查找；如果原顶层值不是字典，就用空字典重新开始。
            if not isinstance(top_level_data_copy, dict):
                # 这会允许后续路径继续创建，但也意味着原来的非字典顶层值会在成功提交后被替换。
                top_level_data_copy = {}

            # current_level_data 是路径遍历指针，初始指向整个顶层字典副本。
            current_level_data = top_level_data_copy

            # enumerate 同时提供当前位置序号 i 和当前路径键 key，便于判断是否已到最后一层。
            for i, key in enumerate(deep_path):
                # 最后一层就是调用方真正想读取和修改的位置。
                if i == len(deep_path) - 1:
                    # 从当前字典读取目标键的旧值；目标键不存在时得到 None。
                    current_deep_value = current_level_data.get(key, None)

                    # 再做一次深拷贝后交给业务函数，确保业务函数只能操作独立副本。
                    value_to_process = copy.deepcopy(current_deep_value)

                    # 执行业务更新函数；返回值将作为目标键的新值。
                    new_deep_value = update_function(value_to_process, *args, **kwargs)

                    # 业务函数可以通过返回 ATOMIC_NO_UPDATE 主动取消本次写入。
                    if new_deep_value is ATOMIC_NO_UPDATE:
                        # 取消事务并释放 SQLite 写锁，数据库内容保持不变。
                        await _db.rollback()

                        # 如果数据库中原本没有顶层键，就清除本进程缓存中可能存在的旧键。
                        if current_top_value is None:
                            # 使用带默认值的 pop，保证键不存在时不会抛出 KeyError。
                            _data_cache.pop(top_key, None)
                        else:
                            # 如果顶层键存在，用事务读取的最新数据库值修正当前进程缓存。
                            _data_cache[top_key] = copy.deepcopy(current_top_value)

                        # 主动取消仍然表示函数正常执行完成，因此返回 True。
                        return True

                    # 把业务函数返回的新值放回顶层对象副本中的目标位置。
                    current_level_data[key] = new_deep_value

                else:
                    # 尚未到目标位置；setdefault 会读取下一层字典，不存在时自动创建空字典。
                    next_level = current_level_data.setdefault(key, {})

                    # 如果路径中间某层不是字典，就无法继续向下访问。
                    if not isinstance(next_level, dict):
                        # 用空字典替换阻塞路径的旧值，让后续路径能够继续创建。
                        next_level = {}

                        # 把新建的空字典放回当前层，否则修改不会进入最终写回对象。
                        current_level_data[key] = next_level

                    # 将遍历指针移动到下一层，下一次循环会继续从这里查找。
                    current_level_data = next_level

            # 深层目标修改完成后，把包含该修改的整个顶层字典序列化为 JSON。
            value_json = json.dumps(top_level_data_copy)

            # 将完整顶层对象写回数据库。
            # 虽然只改了一个深层字段，但表结构是一行保存一个完整顶层 JSON，所以必须整行替换。
            await _db.execute(
                f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)",
                (top_key, value_json),
            )

            # 提交事务，使修改正式生效，并允许其它进程开始下一次写操作。
            await _db.commit()

            # 数据库提交成功后，用相同内容刷新当前进程的内存缓存。
            _data_cache[top_key] = json.loads(value_json)

            # 数据库与缓存均已更新成功。
            return True

        # 捕获路径处理、业务更新函数、JSON 转换或 SQLite 操作中发生的任何异常。
        except Exception:
            # 只有连接仍处于事务中时才需要回滚，避免在无事务状态下重复回滚。
            if _db.in_transaction:
                # 回滚会撤销本次尚未提交的数据库修改，并释放 SQLite 写锁。
                await _db.rollback()

            # 记录完整异常堆栈和更新路径，方便定位是哪一次深层更新失败。
            logger.error(f"错误: 深度原子更新失败 '{path}'", exc_info=True)

            # 向调用方明确表示本次更新因异常失败。
            return False


async def backup_db(backup_dir: str = "backups", retention_days: int = 30) -> str:
    """
    执行数据库热备份 (Hot Backup)。

    优势：
    1. 安全：使用 SQLite 原生 backup API，完美兼容 WAL 模式。
    2. 在线：不需要停止服务或关闭数据库连接。
    3. 维护：包含自动清理旧备份的逻辑。

    :param backup_dir: 备份文件夹名称 (相对于 BASE_DIR)
    :param retention_days: 保留多少天内的备份，超过的会自动删除。设为 0 不删除。
    :return: 成功生成的备份文件绝对路径
    """
    # 1. 检查初始化状态
    await _init_done.wait()
    if _db is None:
        logger.error("备份失败: 数据库未初始化")
        return ""

    try:
        # 2. 准备备份路径
        # 结构: /项目根目录/backups/
        target_dir = BASE_DIR / backup_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"storage_backup_{timestamp}.db"
        backup_path = target_dir / backup_filename

        logger.info(f"开始数据库备份: {backup_path}")

        # 3. 执行热备份
        # backup API 自己处理文件锁；这里再保护共享连接，避免与本进程事务交叉。
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", "database-backup"):
            # 创建一个新的连接指向备份文件
            async with aiosqlite.connect(backup_path) as dest_db:
                # 使用 aiosqlite 的 backup 方法将当前 _db 复制到 dest_db
                # pages=0 表示一步完成，也可以设置为正整数来分块备份以减少阻塞
                await _db.backup(dest_db)

        logger.info(f"数据库备份成功: {backup_path}")

        # 4. 执行备份轮转 (清理旧文件)
        if retention_days > 0:
            await _rotate_backups(target_dir, retention_days)

        return str(backup_path)

    except Exception:
        logger.error("数据库备份过程中发生严重错误", exc_info=True)
        return ""


async def _rotate_backups(backup_dir: Path, retention_days: int):
    """
    内部辅助函数：清理超过指定天数的旧备份文件。
    """
    try:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=retention_days)

        # 遍历目录下所有 .db 文件
        for file_path in backup_dir.glob("storage_backup_*.db"):
            # 获取文件修改时间
            mtime = datetime.datetime.fromtimestamp(file_path.stat().st_mtime)

            if mtime < cutoff_date:
                try:
                    file_path.unlink()  # 删除文件
                    logger.info(f"已清理过期备份: {file_path.name}")
                except Exception as e:
                    logger.warning(f"清理文件失败 {file_path.name}: {e}")

    except Exception:
        logger.error("执行备份轮转清理时出错", exc_info=True)
