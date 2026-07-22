"""基于 SQLite 和进程内缓存的异步 JSON 存储模块。

本模块提供两种存储模型：

1. ``general_storage``：一个顶层键对应一段完整 JSON，兼容项目原有的深层路径读写接口；
2. ``json_entity_storage``：按 ``namespace + entity_id`` 拆分记录，避免修改单条业务记录时
   重写整个大型 JSON 字典。

并发与一致性约定：

* 普通读取优先访问进程内缓存，因此速度快，但多进程场景需要主动调用刷新接口；
* 所有 SQLite 操作共用一个异步连接，并通过 ``_db_transaction_lock`` 串行访问；
* 同一业务键还会先获取资源锁，确保同一进程内针对相同数据的操作保持顺序；
* 读取后修改再写回的操作使用 ``BEGIN IMMEDIATE``，防止多个进程互相覆盖；
* 数据库提交成功后才更新缓存；提交失败会回滚，提交期间取消则等待确定结果并同步缓存后再传播。

调用方应在应用启动时调用 :func:`init_db`，关闭时调用 :func:`close_db`。除明确传入
``return_ref=True`` 外，读取接口都会返回深拷贝，避免外部代码绕过持久化流程直接修改缓存。
"""

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

# ---------------------------------------------------------------------------
# 数据库路径、表名及原子更新哨兵
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
DB_PATH = f"{BASE_DIR}/db/nicegui_storage.db"  # 数据库文件名
TABLE_NAME = "general_storage"  # 模拟 general storage
JSON_ENTITY_TABLE_NAME = "json_entity_storage"  # 按业务实体逐行保存JSON，避免整块集合重写
JSON_ENTITY_NAMESPACE_TABLE_NAME = "json_entity_namespaces"  # 记录已初始化命名空间，允许合法空集合
ATOMIC_NO_UPDATE = object()  # update_function 返回此值时，原子事务不写入任何数据
ATOMIC_DELETE = object()  # update_function 返回此值时，原子删除当前实体

# ---------------------------------------------------------------------------
# 进程内缓存
# ---------------------------------------------------------------------------
# ``_data_cache`` 保存传统顶层键；``_entity_cache`` 按命名空间保存独立实体。
# 缓存不是跨进程共享的。其它服务进程完成写入后，本进程需要通过 get_fresh_item 或
# refresh_json_entities 主动读取 SQLite，才能看到对方提交的最新数据。
_data_cache: Dict[str, Any] = {}
_entity_cache: Dict[str, Dict[str, Any]] = {}
# 单独记录命名空间是否已初始化，才能区分“合法空集合”和“从未迁移”。
_entity_namespaces: set[str] = set()
_db: Optional[aiosqlite.Connection] = None

# ---------------------------------------------------------------------------
# 生命周期、数据库事务锁与业务资源锁
# ---------------------------------------------------------------------------
# 事件未设置表示初始化或关闭切换尚未完成；事件设置后，调用方仍需检查 _db 是否为 None，
# 因为初始化失败和正常关闭也会唤醒等待者，使其能够立即返回明确的失败结果。
_init_done = asyncio.Event()
# 防止 init_db 与 close_db 并发执行，也让重复初始化保持幂等。
_lifecycle_lock = asyncio.Lock()

# aiosqlite 的单个共享连接不能交叉执行多个事务，因此所有数据库访问最终都要进入此锁。
_db_transaction_lock = asyncio.Lock()
# 资源锁按顶层键或实体ID细分，让同一资源有序排队，并允许不同资源提前完成锁外预处理。
_resource_locks: Dict[str, asyncio.Lock] = {}
# 统计持锁者与等待者数量；数量归零后删除锁，避免业务ID不断增加造成常驻内存增长。
_resource_lock_users: Dict[str, int] = {}
# 等待超过阈值时记录告警；同一资源在指定间隔内最多记录一次。
LOCK_WAIT_WARNING_SECONDS = 0.05
LOCK_WAIT_WARNING_INTERVAL_SECONDS = 5.0
_last_lock_wait_warnings: Dict[str, float] = {}


def _get_resource_lock(resource_key: str) -> asyncio.Lock:
    """返回业务资源对应的稳定进程内锁。

    在资源仍有持有者或等待者时，相同 ``resource_key`` 一定得到同一把锁。锁的引用计数和
    自动回收由 :func:`_timed_lock` 在 ``managed_resource=True`` 时负责。
    """
    # 先复用池中已有锁，保证相同业务资源不会出现两把并行生效的锁。
    lock = _resource_locks.get(resource_key)
    if lock is None:
        # 首次访问该资源时创建锁，并立即登记到池中供后续协程复用。
        lock = asyncio.Lock()
        _resource_locks[resource_key] = lock
    return lock


@asynccontextmanager
async def _timed_lock(
    lock: asyncio.Lock,
    lock_kind: str,
    resource_key: str,
    *,
    managed_resource: bool = False,
):
    """获取锁、统计等待时间，并确保退出上下文时释放锁。

    Args:
        lock: 要获取的 ``asyncio.Lock``。
        lock_kind: 写入日志的锁类型，例如“业务资源锁”或“数据库事务锁”。
        resource_key: 用于定位热点业务数据的标识。
        managed_resource: 是否对该资源锁做引用计数。为 ``True`` 时，即使等待任务被取消，
            也会正确减少计数；最后一个使用者退出后会删除锁和对应告警状态。
    """
    if managed_resource:
        # 在真正等待锁之前登记使用者，保证排队中的任务也能阻止该锁被提前回收。
        _resource_lock_users[resource_key] = _resource_lock_users.get(resource_key, 0) + 1
    # 标记本协程是否已经成功取得锁，避免等待阶段被取消后误调用 release。
    acquired = False
    try:
        # 使用高精度计时器记录从开始等待到实际获得锁的耗时。
        started = time.perf_counter()
        await lock.acquire()
        acquired = True
        waited = time.perf_counter() - started
        # 锁类型与业务资源共同组成告警去重键，避免不同锁的统计互相覆盖。
        warning_key = f"{lock_kind}:{resource_key}"
        now = time.monotonic()
        last_warning = _last_lock_wait_warnings.get(warning_key, 0.0)
        # 只有等待超阈值且距离上次告警足够久时才输出日志，防止热点资源刷屏。
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
        # 把锁保护区的执行权交给 async with 内部的业务代码。
        yield
    finally:
        # 无论业务成功、异常还是取消，只要已经持锁就必须释放。
        if acquired:
            lock.release()
        if managed_resource:
            # 当前协程退出后减少活跃使用者数量；等待者此前已经计入，所以不会误删仍在使用的锁。
            remaining_users = _resource_lock_users.get(resource_key, 1) - 1
            if remaining_users > 0:
                _resource_lock_users[resource_key] = remaining_users
            else:
                # 最后一个使用者退出后同时移除锁对象和告警节流状态，控制常驻内存规模。
                _resource_lock_users.pop(resource_key, None)
                if _resource_locks.get(resource_key) is lock:
                    _resource_locks.pop(resource_key, None)
                _last_lock_wait_warnings.pop(f"业务资源锁:{resource_key}", None)
                _last_lock_wait_warnings.pop(f"数据库事务锁:{resource_key}", None)


def _require_db() -> aiosqlite.Connection:
    """返回当前活动连接；没有连接时抛出明确异常。

    调用方必须已经持有 ``_db_transaction_lock``。这样即使任务在等待锁期间遇到数据库关闭，
    也会在真正执行 SQL 前重新检查连接，而不是继续使用已经关闭的对象。
    """
    # 连接为空既可能表示尚未初始化，也可能表示关闭流程已经完成。
    if _db is None:
        raise RuntimeError("数据库未初始化或已经关闭")
    return _db


async def _rollback_safely(db: aiosqlite.Connection, operation: str) -> None:
    """尽力回滚失败或被取消的事务，同时保留原始异常。

    aiosqlite 会把 SQL 放入后台工作线程的队列。任务在等待 ``BEGIN IMMEDIATE`` 时被取消，
    并不代表已经入队的 ``BEGIN`` 也被取消；因此不能根据当前 ``in_transaction`` 状态提前跳过
    回滚，而要始终把 ``rollback`` 排在它后面，作为队列屏障清理可能稍后开启的事务。

    ``asyncio.shield`` 用于避免外层任务的一次取消同时中断回滚。回滚自身失败只记录日志，
    不覆盖最初导致事务失败的异常；SQLite 在没有活动事务时执行回滚也是安全的。
    """
    try:
        # 无条件入队回滚，确保它一定排在尚未完成、但已经交给 aiosqlite 工作线程的 SQL 后面。
        await asyncio.shield(db.rollback())
    except Exception:
        # 回滚失败不能替换最初的业务异常，因此这里只记录完整堆栈。
        logger.exception("回滚数据库事务失败：%s", operation)


async def _commit_cancellation_safely(
    db: aiosqlite.Connection,
) -> Optional[asyncio.CancelledError]:
    """等待提交得到确定结果，并暂存提交期间收到的任务取消。

    aiosqlite 在后台线程执行真正的 ``commit``。等待提交的协程被取消时，后台提交仍可能已经
    成功；如果此时直接进入回滚分支，已提交的数据无法撤销，而提交后的缓存发布也会被跳过。

    本函数用独立任务和 ``shield`` 保护提交，并在发生一次或多次取消后继续等待提交完成。提交
    成功时返回首次捕获的 ``CancelledError``，调用方必须先同步更新缓存，再重新抛出该异常；
    没有取消时返回 ``None``。如果提交本身失败，则直接抛出提交异常；若同时发生过取消，则以
    取消异常为主，并把提交异常保留为异常原因。
    """
    # 独立任务承载 aiosqlite 提交，使外层任务取消不会把提交任务一并取消。
    commit_task = asyncio.create_task(db.commit())
    pending_cancellation: Optional[asyncio.CancelledError] = None

    # 重复使用 shield，确保外层收到多次 cancel() 时仍会等到后台提交得出确定结果。
    while not commit_task.done():
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError as cancellation:
            # 提交任务自身被取消时没有可发布的成功结果，应直接保留其取消语义。
            if commit_task.cancelled():
                raise
            # 只保存首次取消，后续取消不会中断对同一提交结果的等待。
            if pending_cancellation is None:
                pending_cancellation = cancellation
        except Exception:
            # 提交异常将在下面通过 task.result() 统一重新抛出，并正确关联可能同时发生的取消。
            break

    try:
        # 显式取得后台任务结果；成功返回 None，失败则重新抛出原始提交异常。
        commit_task.result()
    except Exception as commit_error:
        if pending_cancellation is not None:
            # 调用方已经要求取消时仍优先传播取消，同时保留提交失败作为诊断原因。
            raise pending_cancellation from commit_error
        raise

    return pending_cancellation


async def init_db() -> None:
    """初始化数据库连接、表结构和进程内缓存。

    执行顺序：

    1. 在生命周期锁内建立新的 aiosqlite 连接；
    2. 启用 WAL 和忙等待，并创建缺失的表、索引；
    3. 把数据库内容加载到局部缓存；
    4. 全部成功后一次性发布连接和缓存，再唤醒等待中的业务操作。

    重复调用是幂等的：已有可用连接时直接返回。初始化失败会关闭临时连接、清空缓存并
    重新抛出异常；等待者会被唤醒，并通过 ``_db is None`` 判断初始化失败。
    """
    global _db, _data_cache, _entity_cache, _entity_namespaces
    # 生命周期锁确保两个初始化、或初始化与关闭流程不会同时修改全局状态。
    async with _lifecycle_lock:
        # 已经存在完整可用的连接时直接返回，使重复注册的启动钩子保持幂等。
        if _db is not None and _init_done.is_set():
            return

        # 初始化期间让数据库操作停在 wait()，并先丢弃上一次生命周期可能留下的缓存。
        _init_done.clear()
        _data_cache = {}
        _entity_cache = {}
        _entity_namespaces = set()
        # 初始化阶段只操作局部连接，避免业务协程看到“连接已创建但缓存尚未加载”的半成品状态。
        new_db: Optional[aiosqlite.Connection] = None
        try:
            # 新连接暂存在局部变量中，所有准备工作成功前不会发布到全局 _db。
            new_db = await aiosqlite.connect(DB_PATH)
            # 启用 WAL 模式 (Write-Ahead Logging) 提高并发性能
            await new_db.execute("PRAGMA journal_mode=WAL;")
            # 多进程同时争用写事务时等待最多 30 秒，避免短暂竞争直接导致原子更新失败
            await new_db.execute("PRAGMA busy_timeout=30000;")

            # 创建一个简单的 key-value 表
            # 我们将 key 存为 TEXT，将 value 序列化为 JSON 字符串后存为 TEXT
            await new_db.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            # 独立实体表用 namespace 与 entity_id 组成复合主键，保证同一命名空间内ID唯一。
            await new_db.execute(f"""
                CREATE TABLE IF NOT EXISTS {JSON_ENTITY_TABLE_NAME} (
                    namespace TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (namespace, entity_id)
                )
            """)
            # namespace 索引用于加速整组实体刷新和迁移检查。
            await new_db.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{JSON_ENTITY_TABLE_NAME}_namespace "
                f"ON {JSON_ENTITY_TABLE_NAME} (namespace)"
            )
            # 命名空间表单独记录“已经初始化的空集合”，不能用实体行数代替。
            await new_db.execute(f"""
                CREATE TABLE IF NOT EXISTS {JSON_ENTITY_NAMESPACE_TABLE_NAME} (
                    namespace TEXT PRIMARY KEY
                )
            """)
            # 表结构全部准备完成后统一提交，随后再读取缓存快照。
            await new_db.commit()

            # 三份局部缓存必须全部加载完成后才能替换全局缓存。
            loaded_data: Dict[str, Any] = {}
            loaded_entities: Dict[str, Dict[str, Any]] = {}
            loaded_namespaces: set[str] = set()

            # 先加载到局部变量，全部成功后再一次性发布缓存。
            logger.info(f"从数据库{DB_PATH}加载所有现有数据到内存缓存...")
            # 逐行读取传统顶层键，避免一次 fetchall 额外复制整个结果集。
            async with new_db.execute(f"SELECT key, value FROM {TABLE_NAME}") as cursor:
                async for row in cursor:
                    key, value_json = row
                    try:
                        # 缓存保存反序列化对象，后续读取无需重复解析 JSON。
                        loaded_data[key] = json.loads(value_json)
                    except (json.JSONDecodeError, TypeError):
                        logger.error(f"警告: 不能从键'{key}'中解码json数据", exc_info=True)
                        # 跳过损坏行，避免用 None 冒充合法的 JSON null；其它数据仍可正常启动。
                        continue

            # 先加载命名空间标记，确保没有实体的合法空命名空间也会出现在缓存中。
            async with new_db.execute(
                f"SELECT namespace FROM {JSON_ENTITY_NAMESPACE_TABLE_NAME}"
            ) as cursor:
                async for (namespace,) in cursor:
                    loaded_namespaces.add(namespace)
                    loaded_entities.setdefault(namespace, {})
            # 再加载每条实体的 JSON 内容，并挂到对应命名空间字典下。
            async with new_db.execute(
                f"SELECT namespace, entity_id, value FROM {JSON_ENTITY_TABLE_NAME}"
            ) as cursor:
                async for namespace, entity_id, value_json in cursor:
                    try:
                        loaded_entities.setdefault(namespace, {})[entity_id] = json.loads(value_json)
                    except (json.JSONDecodeError, TypeError):
                        # 损坏实体只跳过当前行，其它可用实体仍正常进入缓存。
                        logger.error(
                            "不能解码实体存储数据：namespace=%s, entity_id=%s",
                            namespace,
                            entity_id,
                            exc_info=True,
                        )

            # 从这一刻起连接和缓存作为一个完整快照对业务代码可见。
            _db = new_db
            _data_cache = loaded_data
            _entity_cache = loaded_entities
            _entity_namespaces = loaded_namespaces
            logger.info(f"装载{len(_data_cache)}条数据到缓存中.")
            # 最后设置事件，所有等待中的写操作此时才能观察到完整连接和缓存。
            _init_done.set()
            logger.info("数据库初始化完成，缓存已就绪。")
        except BaseException:
            # 初始化任一步骤失败时，先处理局部连接，防止泄漏线程和文件句柄。
            if new_db is not None:
                await _rollback_safely(new_db, "数据库初始化")
                try:
                    await new_db.close()
                except Exception:
                    logger.exception("初始化失败后关闭数据库连接失败")
            # 清空全局状态，确保调用方不会误用部分加载的数据。
            _db = None
            _data_cache = {}
            _entity_cache = {}
            _entity_namespaces = set()
            # 唤醒等待者，使其通过 _db is None 得到明确失败，而不是永久等待。
            _init_done.set()
            raise


async def close_db():
    """安全关闭共享连接并清空本进程缓存。

    关闭开始时先清除 ``_init_done``，阻止新的数据库操作进入；随后等待当前事务释放共享锁，
    回滚可能遗留的事务并关闭连接。最终无论关闭是否抛出异常，都会重置全局状态并唤醒等待者。
    函数可重复调用。
    """
    global _db, _data_cache, _entity_cache, _entity_namespaces
    # 与初始化共用生命周期锁，避免连接建立到一半时被另一个协程关闭。
    async with _lifecycle_lock:
        # 暂停后续数据库操作；已经进入事务锁队列的操作会在取得锁后重新检查 _db。
        _init_done.clear()
        # 保存局部引用，确保 finally 把全局 _db 置空后仍能完成当前连接的关闭流程。
        connection = _db
        try:
            # 等待当前数据库操作完成，确保关闭动作不会和 SQL 请求交叉使用同一连接。
            async with _timed_lock(_db_transaction_lock, "数据库事务锁", "database-close"):
                if connection is not None:
                    # 防御性回滚可能遗留的事务，再关闭 aiosqlite 后台线程和 SQLite 句柄。
                    await _rollback_safely(connection, "关闭数据库")
                    await connection.close()
        finally:
            # 即使 close() 自身失败，也必须解除全局引用并清空不可再信任的缓存。
            _db = None
            _data_cache = {}
            _entity_cache = {}
            _entity_namespaces = set()
            # 关闭是一个已完成状态；后续调用会立即发现 _db 为 None。
            _init_done.set()
        if connection is not None:
            logger.info("数据库连接关闭.")


# ---------------------------------------------------------------------------
# 传统顶层键：内部事务写入函数
# ---------------------------------------------------------------------------
async def _internal_set(
    key: str,
    value: Any,
    value_json: Optional[str] = None,
):
    """在已持有数据库事务锁的前提下写入一个传统顶层键。

    ``value_json`` 允许调用方在锁外完成耗时序列化。函数仍会开启显式写事务，只有提交成功后
    才更新缓存；提交失败会回滚。任务在提交期间被取消时，会先等待提交结果并同步缓存，再继续
    传播取消，避免数据库与缓存状态分叉。
    """
    # 进入内部函数时数据库锁已经持有，此处再次取得当前连接可识别关闭竞争。
    db = _require_db()
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
        # 显式申请写锁，使本次写入与其它进程的读取-修改-写回操作保持一致顺序。
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)", (key, value_json))
        # 只有 commit 返回成功后，下面的缓存才允许反映新值。
        pending_cancellation = await _commit_cancellation_safely(db)

        # 4. 更新内存缓存 (使用深拷贝)
        #    通过从刚序列化的字符串中 "loads"，我们确保缓存中的
        #    对象是一个全新的、无引用的副本，与数据库100%一致。
        _data_cache[key] = json.loads(value_json)
        # 好处 2（深拷贝）： json.loads(value_json) 创建了一个 100% 干净的、与数据库内容完全一致的深拷贝副本，从而解决了您担心的浅引用问题。
        # 数据库与缓存已经同步后，才继续传播提交期间暂存的任务取消。
        if pending_cancellation is not None:
            raise pending_cancellation
    except asyncio.CancelledError:
        # asyncio 的取消不属于普通业务失败，但离开前仍必须释放 SQLite 写事务。
        await _rollback_safely(db, f"写入顶层键 {key}")
        raise
    except Exception:
        # SQL、序列化或提交异常统一回滚，避免失败数据被下一次 commit 意外提交。
        await _rollback_safely(db, f"写入顶层键 {key}")
        # 在实际的锁持有函数中处理这个异常
        logger.error(f"错误：内部写入失败：'{key}'", exc_info=True)
        raise  # 抛出异常，让外层函数知道失败了


async def _internal_remove(key: str):
    """在已持有数据库事务锁的前提下删除一个传统顶层键。

    数据库删除成功提交后才清理缓存，因此调用方不会看到“缓存已删除、数据库仍保留”的状态。
    """
    # 使用持锁后的最新连接，避免关闭流程把全局连接置空后仍继续执行。
    db = _require_db()
    try:
        # 3. 从数据库删除
        # 即使键不存在也开启完整事务，使返回值只表达数据库操作是否成功。
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(f"DELETE FROM {TABLE_NAME} WHERE key = ?", (key,))
        pending_cancellation = await _commit_cancellation_safely(db)

        # 4. 从缓存删除
        # 数据库提交完成后再移除缓存，保持持久化数据优先的更新顺序。
        if key in _data_cache:
            del _data_cache[key]
        # 删除结果已经同步到缓存后，再恢复调用方原本请求的取消语义。
        if pending_cancellation is not None:
            raise pending_cancellation
    except asyncio.CancelledError:
        # 取消任务时回滚后继续传播 CancelledError，让上层正确结束任务。
        await _rollback_safely(db, f"删除顶层键 {key}")
        raise
    except Exception:
        # 普通失败由外层 remove_item 转换为 False。
        await _rollback_safely(db, f"删除顶层键 {key}")
        logger.error(f"错误：内部删除失败：'{key}'", exc_info=True)
        raise  # 抛出异常


# ---------------------------------------------------------------------------
# 进程内缓存读取与跨进程主动刷新
# ---------------------------------------------------------------------------
def get_item(key: str, default: Any = None, return_ref: bool = False) -> Any:
    """
    从内存缓存中获取数据。

    :param key: 键名
    :param default: 默认值
    :param return_ref: 如果为 True，则返回原始内存引用（极快，但绝对禁止修改返回的对象！）；
                       如果为 False（默认），返回深拷贝（安全，防篡改）。
    :return: 查找到的值或默认值

    注意：该函数只读取本进程缓存，不会等待初始化，也不会主动查询 SQLite。需要跨进程最新值时
    应使用 :func:`get_fresh_item`。
    """
    # 独立哨兵只表示“键不存在”，不能用 default 或 None 判断，否则会混淆合法的 JSON null。
    missing = object()
    val = _data_cache.get(key, missing)
    if val is missing:
        return default
    # return_ref 是显式的只读性能通道，调用方修改该对象会直接污染缓存。
    if return_ref:
        return val  # 极速模式：直接返回引用

    return copy.deepcopy(val)  # 安全模式：返回深拷贝


def get_json_entities(namespace: str, return_ref: bool = False) -> Dict[str, Any]:
    """从缓存读取一个命名空间下的全部独立 JSON 实体。

    Args:
        namespace: 业务命名空间。
        return_ref: 是否直接返回缓存引用。默认返回完整深拷贝；直接引用只适合严格只读场景。

    Returns:
        以实体ID为键的字典；命名空间不存在时返回空字典。
    """
    # 先取得命名空间内部字典，再按参数决定直接返回还是复制整组实体。
    entities = _entity_cache.get(namespace, {})
    return entities if return_ref else copy.deepcopy(entities)


def is_json_entity_namespace_initialized(namespace: str) -> bool:
    """判断实体命名空间是否已完成建库或旧数据迁移。

    该判断独立于实体数量，因此已经初始化但当前没有记录的命名空间仍然返回 ``True``。
    """
    return namespace in _entity_namespaces


def get_json_entity(namespace: str, entity_id: str, default: Any = None) -> Any:
    """从缓存读取一个独立 JSON 实体并返回深拷贝。

    找不到实体时返回 ``default``；实体存在且值为 JSON ``null`` 时返回 ``None``。该接口不会
    访问 SQLite；跨进程同步应先调用 :func:`refresh_json_entities`。
    """
    # 独立哨兵区分缺失实体和合法的 JSON null，避免调用方提供的 default 覆盖已存储的 None。
    missing = object()
    entity = _entity_cache.get(namespace, {}).get(entity_id, missing)
    if entity is missing:
        return default
    return copy.deepcopy(entity)


async def get_fresh_item(key: str, default: Any = None) -> Any:
    """直接从 SQLite 读取顶层键并刷新本进程缓存。

    主要用于多进程版本戳检查。数据库中不存在该键时会同时清理本进程的旧缓存并返回
    ``default``；读取或反序列化失败时记录日志并返回 ``default``。
    """
    # 等待当前生命周期切换完成，再通过 _db 判断连接是否真正可用。
    await _init_done.wait()
    if _db is None:
        return default
    # 顶层键构成资源锁标识，使同一键的刷新不会穿插到其写事务中间。
    resource_key = f"general:{key}"
    # 先取得业务资源锁，再进入共享连接锁；全模块始终遵守这一顺序以避免死锁。
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            # Optional 初值便于在连接关闭竞争或异常时保持清晰的类型状态。
            db: Optional[aiosqlite.Connection] = None
            try:
                # 等待数据库锁期间可能发生关闭，因此必须在锁内重新取得连接。
                db = _require_db()
                # 直接查询 SQLite，而不是读取可能过期的 _data_cache。
                async with db.execute(
                    f"SELECT value FROM {TABLE_NAME} WHERE key = ?",
                    (key,),
                ) as cursor:
                    row = await cursor.fetchone()
                # 数据库已经没有该键时，同步删除本进程中可能残留的旧缓存。
                if row is None:
                    _data_cache.pop(key, None)
                    return default
                # 使用数据库结果覆盖缓存，并给调用方返回独立副本。
                value = json.loads(row[0])
                _data_cache[key] = value
                return copy.deepcopy(value)
            except Exception:
                # 刷新失败不破坏原缓存，调用方通过 default 得到可预测结果。
                logger.exception("读取最新顶层键失败：%s", key)
                return default


async def refresh_json_entities(namespace: str) -> int:
    """从 SQLite 重新加载一个实体命名空间及其初始化标记。

    刷新成功后会整体替换该命名空间的缓存，使本进程看到其它服务实例已经提交的新增、修改和
    删除。返回刷新后的实体数量；失败时保留原缓存并返回 ``0``，具体原因写入日志。
    """
    # 等待初始化或关闭切换结束，避免使用处于半初始化状态的连接。
    await _init_done.wait()
    if _db is None:
        return 0
    # 整个命名空间共用一把资源锁，防止批量刷新与本进程批量新增同时改写同一缓存字典。
    resource_key = f"entity-namespace:{namespace}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                # 在数据库锁内重新确认连接仍然有效。
                db = _require_db()
                # 先在局部字典中构建完整快照；中途失败时不会污染现有缓存。
                refreshed: Dict[str, Any] = {}
                async with db.execute(
                    f"SELECT entity_id, value FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ?",
                    (namespace,),
                ) as cursor:
                    async for entity_id, value_json in cursor:
                        refreshed[entity_id] = json.loads(value_json)
                # 命名空间标记与实体行分开查询，用于识别合法空集合。
                async with db.execute(
                    f"SELECT 1 FROM {JSON_ENTITY_NAMESPACE_TABLE_NAME} WHERE namespace = ?",
                    (namespace,),
                ) as cursor:
                    namespace_row = await cursor.fetchone()
                # 所有行解析成功后一次性替换旧命名空间缓存。
                _entity_cache[namespace] = refreshed
                # 同步更新进程内初始化标记，使跨进程创建或删除标记能够被本进程看到。
                if namespace_row is None:
                    _entity_namespaces.discard(namespace)
                else:
                    _entity_namespaces.add(namespace)
                return len(refreshed)
            except Exception:
                # refreshed 尚未发布，因此异常时原缓存仍保持完整。
                logger.exception("刷新实体命名空间失败：%s", namespace)
                return 0


# ---------------------------------------------------------------------------
# 独立 JSON 实体：迁移、单实体原子更新与批量新增
# ---------------------------------------------------------------------------
async def migrate_json_dict_to_entities(
    namespace: str,
    legacy_key: str,
) -> int:
    """把旧版字典型顶层键一次性迁移到独立实体表。

    ``json_entity_namespaces`` 中的记录是迁移完成的权威标记，即使命名空间当前为空，也不会再次
    从旧键恢复数据。为兼容早期版本，如果发现实体已经存在但缺少标记，会保留现有实体并补迁移
    旧字典中缺失的实体，全部完成后才写入标记。旧顶层键会保留，作为人工回滚备份。

    Args:
        namespace: 新实体表使用的业务命名空间。
        legacy_key: ``general_storage`` 中旧字典的顶层键。

    Returns:
        本次实际迁移的实体数量。已经迁移、旧数据为空或迁移失败时返回 ``0``；失败原因写入日志。
    """
    # 迁移必须在基础表和初始缓存准备完成后执行。
    await _init_done.wait()
    if _db is None:
        logger.error("实体数据迁移失败：数据库尚未初始化")
        return 0

    # 命名空间级资源锁避免同一进程重复迁移或与批量新增互相穿插。
    resource_key = f"entity-namespace:{namespace}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            # 连接只有在成功取得共享锁后才确定，异常处理据此判断是否需要回滚。
            db: Optional[aiosqlite.Connection] = None
            try:
                db = _require_db()
                # BEGIN IMMEDIATE 抢占写事务，保证多个服务实例中最多一个执行实际迁移。
                await db.execute("BEGIN IMMEDIATE")
                # 首先读取权威迁移标记，而不是依赖可能为零的实体行数。
                async with db.execute(
                    f"SELECT 1 FROM {JSON_ENTITY_NAMESPACE_TABLE_NAME} WHERE namespace = ?",
                    (namespace,),
                ) as cursor:
                    namespace_row = await cursor.fetchone()

                # 同时读取现有实体，用于刷新缓存及兼容旧版本“有实体但无标记”的状态。
                existing_entities: Dict[str, Any] = {}
                async with db.execute(
                    f"SELECT entity_id, value FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ?",
                    (namespace,),
                ) as cursor:
                    async for entity_id, value_json in cursor:
                        existing_entities[entity_id] = json.loads(value_json)

                # 必须先检查标记再检查实体数量：用户可能已经合法删除全部实体，此时行数也是 0，
                # 但绝不能再次从保留的 legacy_key 中恢复已经删除的数据。
                if namespace_row is not None:
                    # 此分支不修改数据库，用 rollback 结束 BEGIN IMMEDIATE 并尽快释放跨进程写锁。
                    await db.rollback()
                    # 用事务内读到的最新实体修正本进程缓存。
                    _entity_cache[namespace] = existing_entities
                    _entity_namespaces.add(namespace)
                    return 0

                # 尚无完成标记时读取传统表中的旧版整块 JSON，用于首次迁移或补齐部分迁移。
                async with db.execute(
                    f"SELECT value FROM {TABLE_NAME} WHERE key = ?",
                    (legacy_key,),
                ) as cursor:
                    legacy_row = await cursor.fetchone()
                # 旧键不存在等价于待迁移的空字典，仍会在后面建立命名空间标记。
                legacy_data = json.loads(legacy_row[0]) if legacy_row else {}
                # 旧数据结构错误时不能擅自覆盖，回滚并保留原始数据供人工处理。
                if not isinstance(legacy_data, dict):
                    await db.rollback()
                    logger.error("实体数据迁移失败：旧键 %s 不是字典", legacy_key)
                    return 0

                # 先完整校验旧字典；任意非法实体都会中止整批迁移，避免部分数据被永久跳过。
                invalid_entity_ids = [
                    repr(entity_id)
                    for entity_id, value in legacy_data.items()
                    if not isinstance(entity_id, str) or not isinstance(value, dict)
                ]
                if invalid_entity_ids:
                    # 当前事务尚未插入实体，但仍需结束 BEGIN IMMEDIATE 并释放跨进程写锁。
                    await db.rollback()
                    logger.error(
                        "实体数据迁移失败：旧键 %s 包含 %s 条非法实体（ID必须为字符串且值必须为字典）：%s",
                        legacy_key,
                        len(invalid_entity_ids),
                        ", ".join(invalid_entity_ids[:10]),
                    )
                    return 0

                # 兼容已经写入部分实体但尚无完成标记的旧版本：现有实体作为较新的权威值保留，
                # 只补充旧字典中缺失的ID，避免覆盖迁移后可能已经发生的业务修改。
                migrated: Dict[str, Any] = copy.deepcopy(existing_entities)
                migrated_count = 0
                for entity_id, value in legacy_data.items():
                    # 已经存在的实体不重复插入；缺失实体必须在写完成标记前补齐。
                    if entity_id in existing_entities:
                        continue
                    # 先序列化验证实体，再把同一 JSON 文本写入数据库。
                    value_json = json.dumps(value)
                    await db.execute(
                        f"INSERT INTO {JSON_ENTITY_TABLE_NAME} (namespace, entity_id, value) VALUES (?, ?, ?)",
                        (namespace, entity_id, value_json),
                    )
                    migrated[entity_id] = json.loads(value_json)
                    migrated_count += 1
                # 无论迁移数量是否为零，都要写入标记，阻止以后再次读取旧备份。
                await db.execute(
                    f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                    (namespace,),
                )
                # 实体和命名空间标记必须在同一个事务中共同提交。
                pending_cancellation = await _commit_cancellation_safely(db)
                # 提交成功后再发布缓存和内存标记。
                _entity_cache[namespace] = migrated
                _entity_namespaces.add(namespace)
                # 缓存已反映确定提交结果后，恢复提交期间暂存的任务取消。
                if pending_cancellation is not None:
                    raise pending_cancellation
                if migrated_count:
                    logger.info(
                        "已把旧键 %s 的 %s 条记录迁移到实体命名空间 %s；旧键保留为只读回滚备份",
                        legacy_key,
                        migrated_count,
                        namespace,
                    )
                return migrated_count
            except asyncio.CancelledError:
                # 取消发生在 BEGIN 之后时先回滚，再保留异步取消语义。
                if db is not None:
                    await _rollback_safely(db, f"迁移实体命名空间 {namespace}")
                raise
            except Exception:
                # 任何解析、插入或提交异常都不能留下半迁移事务。
                if db is not None:
                    await _rollback_safely(db, f"迁移实体命名空间 {namespace}")
                logger.exception("实体数据迁移失败：namespace=%s, legacy_key=%s", namespace, legacy_key)
                return 0


async def atomic_json_entity_update(
    namespace: str,
    entity_id: str,
    update_function: Callable,
    *args,
    **kwargs,
) -> bool:
    """在单个写事务内读取、校验并更新或删除一条 JSON 实体。

    ``update_function`` 接收数据库中的最新值副本，并返回以下三类结果：

    * 普通 JSON 可序列化值：新增或替换当前实体；
    * :data:`ATOMIC_NO_UPDATE`：业务校验正常结束，但主动放弃写入；
    * :data:`ATOMIC_DELETE`：删除当前实体，同时持久化命名空间标记。

    Args:
        namespace: 实体所属业务命名空间。
        entity_id: 实体唯一标识。
        update_function: 同步更新函数；不应执行耗时阻塞操作或返回协程。
        *args: 传给更新函数的额外位置参数。
        **kwargs: 传给更新函数的额外关键字参数。

    Returns:
        事务正常完成（包括主动不更新）返回 ``True``；数据库、业务回调或序列化异常返回 ``False``。
        如果当前任务被取消，会先确定提交结果：未提交则回滚，已经提交则同步缓存；随后继续抛出
        ``CancelledError``。
    """
    # 等待数据库生命周期切换完成；初始化失败或关闭后直接返回业务失败。
    await _init_done.wait()
    if _db is None:
        logger.error("实体更新失败：数据库尚未初始化")
        return False

    # 单实体锁只串行化相同 namespace/entity_id，避免无关实体在业务层互相阻塞。
    resource_key = f"entity:{namespace}:{entity_id}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            # 连接变量预先设为 None，异常分支可以安全判断事务是否已经开始。
            db: Optional[aiosqlite.Connection] = None
            try:
                db = _require_db()
                # 写事务在读取之前开始，确保读取到的值直到提交前不会被其它进程改写。
                await db.execute("BEGIN IMMEDIATE")
                # 从 SQLite 获取跨进程最新实体，不依赖当前进程可能过期的缓存。
                async with db.execute(
                    f"SELECT value FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ? AND entity_id = ?",
                    (namespace, entity_id),
                ) as cursor:
                    row = await cursor.fetchone()
                # 不存在的实体用 None 传给业务回调，让回调同时支持新增逻辑。
                current_value = json.loads(row[0]) if row else None
                # 深拷贝隔离数据库快照，防止回调在最终决定“不更新”前污染缓存对象。
                new_value = update_function(copy.deepcopy(current_value), *args, **kwargs)
                # 主动不更新是正常业务结果；结束事务后仍用数据库值校正本进程缓存。
                if new_value is ATOMIC_NO_UPDATE:
                    # 本分支只执行过读取，rollback 用于结束 BEGIN IMMEDIATE 并释放写锁。
                    await db.rollback()
                    # 必须依据查询行是否存在来更新缓存；实体行可以合法存储 JSON null。
                    if row is None:
                        _entity_cache.setdefault(namespace, {}).pop(entity_id, None)
                    else:
                        # 数据库中有实体时，用事务读取值覆盖当前进程旧缓存。
                        _entity_cache.setdefault(namespace, {})[entity_id] = copy.deepcopy(current_value)
                    return True
                # 删除哨兵进入删除分支，普通返回值则在后面执行新增或替换。
                if new_value is ATOMIC_DELETE:
                    # DELETE 对不存在行也是合法操作，最终仍返回事务成功。
                    await db.execute(
                        f"DELETE FROM {JSON_ENTITY_TABLE_NAME} WHERE namespace = ? AND entity_id = ?",
                        (namespace, entity_id),
                    )
                    # 即使删除的是最后一条或原本不存在的实体，也要保留命名空间标记，避免重启后
                    # 旧版备份被迁移逻辑重新导入。
                    await db.execute(
                        f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                        (namespace,),
                    )
                    # 删除和命名空间标记一起提交，避免数据库只完成其中一项。
                    pending_cancellation = await _commit_cancellation_safely(db)
                    # 数据库提交成功后再清理当前进程缓存。
                    _entity_cache.setdefault(namespace, {}).pop(entity_id, None)
                    _entity_namespaces.add(namespace)
                    # 删除结果发布到缓存后，再继续传播提交期间收到的取消。
                    if pending_cancellation is not None:
                        raise pending_cancellation
                    return True

                # 普通结果必须先验证为合法 JSON，验证失败会进入统一回滚分支。
                value_json = json.dumps(new_value)
                # INSERT OR REPLACE 同时覆盖实体首次创建和已有实体更新。
                await db.execute(
                    f"INSERT OR REPLACE INTO {JSON_ENTITY_TABLE_NAME} (namespace, entity_id, value) VALUES (?, ?, ?)",
                    (namespace, entity_id, value_json),
                )
                # 新增或更新实体时确保命名空间标记存在。
                await db.execute(
                    f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                    (namespace,),
                )
                # 两条 SQL 共同提交后，数据库才成为新的真实来源。
                pending_cancellation = await _commit_cancellation_safely(db)
                # 使用刚提交 JSON 的反序列化副本更新缓存，避免持有回调返回对象的外部引用。
                _entity_cache.setdefault(namespace, {})[entity_id] = json.loads(value_json)
                _entity_namespaces.add(namespace)
                # 写入结果发布到缓存后，再继续传播提交期间收到的取消。
                if pending_cancellation is not None:
                    raise pending_cancellation
                return True
            except asyncio.CancelledError:
                # 任务取消不能跳过事务清理，否则共享连接会一直停留在事务中。
                if db is not None:
                    await _rollback_safely(db, f"更新实体 {namespace}/{entity_id}")
                raise
            except Exception:
                # 回调、JSON 或 SQLite 异常统一回滚并转换为 False。
                if db is not None:
                    await _rollback_safely(db, f"更新实体 {namespace}/{entity_id}")
                logger.exception(
                    "独立实体原子更新失败：namespace=%s, entity_id=%s",
                    namespace,
                    entity_id,
                )
                return False


async def insert_json_entities(namespace: str, entities: Dict[str, Any]) -> bool:
    """在一个事务内批量新增独立 JSON 实体。

    该接口只执行新增，不覆盖已有主键；任意主键冲突都会使整批数据回滚。空字典是合法输入，
    此时不会插入实体，但仍会持久化命名空间标记，从而表示“已经初始化的空集合”。所有数据会在
    获取数据库锁前完成 JSON 序列化，序列化失败时返回 ``False``，不会启动事务。

    Args:
        namespace: 实体所属业务命名空间。
        entities: ``entity_id -> JSON可序列化值`` 的映射。

    Returns:
        整批数据和命名空间标记提交成功时返回 ``True``；否则回滚并返回 ``False``。任务取消会在
        提交结果确定且数据库、缓存完成同步后继续向外传播。
    """
    # 等待初始化完成，避免空批次在数据库尚未就绪时错误地报告成功。
    await _init_done.wait()
    if _db is None:
        logger.error("实体批量新增失败：数据库尚未初始化")
        return False

    try:
        # 在任何锁外一次性序列化，既提前验证数据，也缩短共享连接锁的占用时间。
        serialized = {
            entity_id: json.dumps(value)
            for entity_id, value in entities.items()
        }
    except Exception:
        logger.exception("独立实体批量新增序列化失败：namespace=%s", namespace)
        return False

    # 批量新增会影响整个命名空间，因此使用命名空间级资源锁而不是单实体锁。
    resource_key = f"entity-namespace:{namespace}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            db: Optional[aiosqlite.Connection] = None
            try:
                db = _require_db()
                # 一个事务包裹整批 INSERT，任意一行主键冲突都会整体失败。
                await db.execute("BEGIN IMMEDIATE")
                # 空映射跳过 executemany，但仍继续写入命名空间标记。
                if serialized:
                    await db.executemany(
                        f"INSERT INTO {JSON_ENTITY_TABLE_NAME} (namespace, entity_id, value) VALUES (?, ?, ?)",
                        [
                            (namespace, entity_id, value_json)
                            for entity_id, value_json in serialized.items()
                        ],
                    )
                # 标记与实体在同一个事务提交，确保空命名空间也可持久化识别。
                await db.execute(
                    f"INSERT OR IGNORE INTO {JSON_ENTITY_NAMESPACE_TABLE_NAME} (namespace) VALUES (?)",
                    (namespace,),
                )
                pending_cancellation = await _commit_cancellation_safely(db)
                # 取得或创建命名空间缓存，随后逐项发布已提交数据的独立副本。
                namespace_cache = _entity_cache.setdefault(namespace, {})
                for entity_id, value_json in serialized.items():
                    namespace_cache[entity_id] = json.loads(value_json)
                # 内存标记与数据库命名空间表保持一致。
                _entity_namespaces.add(namespace)
                # 整批提交结果发布到缓存后，再继续传播提交期间收到的取消。
                if pending_cancellation is not None:
                    raise pending_cancellation
                return True
            except asyncio.CancelledError:
                # 批量事务取消时必须全部回滚，不能留下部分新增记录。
                if db is not None:
                    await _rollback_safely(db, f"批量新增实体 {namespace}")
                raise
            except Exception:
                # 主键冲突、提交失败等普通异常回滚后以 False 告知调用方。
                if db is not None:
                    await _rollback_safely(db, f"批量新增实体 {namespace}")
                logger.exception("独立实体批量新增失败：namespace=%s", namespace)
                return False


# ---------------------------------------------------------------------------
# 传统顶层键：设置、删除及任意深度路径操作
# ---------------------------------------------------------------------------
async def set_item(key: str, value: Any) -> bool:
    """设置或替换一个传统顶层键。

    JSON 序列化在线程中、业务锁外执行，减少大型对象序列化对事件循环和其它数据库事务的影响。
    随后依次获取同键资源锁和共享数据库锁，提交成功后使用反序列化结果更新缓存，以切断缓存与
    调用方原对象之间的引用关系。

    Args:
        key: ``general_storage`` 中的顶层键。
        value: JSON 可序列化的值。

    Raises:
        TypeError: ``value`` 包含 JSON 不支持的类型。
        ValueError: ``value`` 包含循环引用或其它无法编码的结构。
        Exception: SQLite 写入或提交失败。失败事务会在异常抛出前回滚。

    Returns:
        数据库与缓存均更新成功时返回 ``True``；数据库未初始化时返回 ``False``。
    """
    # 1. 等待初始化完成
    await _init_done.wait()
    if _db is None:
        logger.info("错误: 数据库未初始化.")
        return False

    # 序列化不占用共享数据库连接，让其它模块可在此期间完成自己的事务。
    try:
        # 把可能耗时的 JSON 编码交给工作线程，避免长时间独占 asyncio 事件循环。
        value_json = await asyncio.to_thread(json.dumps, value)
    except Exception:
        logger.error("错误：序列化写入值失败：'%s'", key, exc_info=True)
        raise

    # 相同顶层键共用资源锁；不同键可以并行完成锁外序列化并在数据库锁前分别排队。
    resource_key = f"general:{key}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        # SQLite 连接仍是共享资源，真正执行事务前必须再取得全局数据库锁。
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            # 内部函数负责显式事务、回滚和提交后缓存更新。
            await _internal_set(key, value, value_json)
            return True


async def remove_item(key: str) -> bool:
    """删除一个传统顶层键，并在提交成功后同步清理缓存。

    Args:
        key: 要删除的顶层键。键不存在也视为数据库删除操作成功。

    Returns:
        事务成功提交返回 ``True``；数据库未初始化或删除失败返回 ``False``。任务取消不会被转换
        为 ``False``，而是在提交结果确定且数据库、缓存完成同步后继续向外传播。
    """

    # 1. 等待初始化完成
    await _init_done.wait()
    if _db is None:
        logger.info("错误: 数据库未初始化.")
        return False

    # 删除与同一键的设置、刷新和原子更新共享资源锁，保证进程内操作顺序。
    resource_key = f"general:{key}"
    async with _timed_lock(
        _get_resource_lock(resource_key),
        "业务资源锁",
        resource_key,
        managed_resource=True,
    ):
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", resource_key):
            try:
                # 内部删除只有在 SQLite 提交后才清理缓存。
                await _internal_remove(key)
                return True
            except Exception:
                # 详细异常已经由内部函数记录，公开接口只返回稳定的布尔结果。
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

    # 路径的第一个元素定位缓存中的整块 JSON，其余元素用于逐层进入内部字典。
    top_key = path[0]
    deep_path = path[1:]

    # 直接在只读缓存引用上定位目标，避免先深拷贝整个顶层对象。
    missing = object()
    current_level_data = get_item(top_key, missing, return_ref=True)
    if current_level_data is missing:
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
        # 只移动当前引用，不在遍历过程中复制无关分支；最终值为 None 代表命中了 JSON null。
        current_level_data = current_level_data[key]

    if return_ref:
        return current_level_data

    # 默认只深拷贝最终命中的分支，而不是整个顶层大型 JSON。
    return copy.deepcopy(current_level_data)


async def set_deep_item(path: List[str], value: Any) -> bool:
    """
    异步设置一个任意深度的值，并在原子事务中持久化到数据库。
    如果路径上的字典不存在，会自动创建；如果中间节点不是字典，会用新字典替换后继续创建。
    深层修改最终仍会重写该顶层键对应的完整 JSON。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    :param value: 要设置的新值
    :return: 数据库与缓存均更新成功返回 True；数据库未初始化或原子写入失败返回 False。
    """
    if not path:
        raise ValueError("路径列表 'path' 不能为空")

    top_key = path[0]
    deep_path = path[1:]
    # 只有顶层键时直接复用 set_item，避免进入不必要的读取-修改-写回流程。
    if not deep_path:
        return await set_item(top_key, value)

    # 回调接收事务内最新顶层值，并在它的副本上构造或覆盖目标路径。
    def apply_set(current_top: Any) -> Dict[str, Any]:
        # 非字典顶层值无法承载深层路径，因此从新的空字典开始。
        top_level_data = current_top if isinstance(current_top, dict) else {}
        current_level_data = top_level_data
        # 只遍历到目标键的父节点；缺少的中间节点由 setdefault 自动创建。
        for key in deep_path[:-1]:
            next_level = current_level_data.setdefault(key, {})
            # 已有中间值不是字典时，用空字典替换，以便继续向下写入。
            if not isinstance(next_level, dict):
                next_level = {}
                current_level_data[key] = next_level
            current_level_data = next_level
        # 最后一个路径元素是真正需要设置的字段。
        current_level_data[deep_path[-1]] = value
        return top_level_data

    # 顶层原子更新负责从数据库读取最新值、执行回调并整块提交。
    return await atomic_deep_update([top_key], apply_set)


async def del_deep_item(path: List[str]) -> bool:
    """
    异步删除一个任意深度的值（原子“读取-修改-写回”），并持久化到数据库。
    如果路径不存在，函数会返回False，不会报错。
    路径存在且删除成功，函数会返回True。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    """
    if not path:
        logger.info("路径列表 'path' 不能为空")
        return False

    top_key = path[0]
    deep_path = path[1:]
    # 单元素路径表示删除整个顶层键，直接复用 remove_item。
    if not deep_path:
        return await remove_item(top_key)

    # 闭包变量区分“事务正常但目标不存在”和“确实删除成功”。
    deleted = False

    def apply_delete(current_top: Any) -> Any:
        nonlocal deleted
        # 顶层不是字典时路径必然不存在，使用哨兵结束而不重写数据。
        if not isinstance(current_top, dict):
            return ATOMIC_NO_UPDATE
        current_level_data = current_top
        # 逐层查找目标父字典；任一中间节点缺失或类型错误都表示无需更新。
        for key in deep_path[:-1]:
            next_level = current_level_data.get(key)
            if not isinstance(next_level, dict):
                return ATOMIC_NO_UPDATE
            current_level_data = next_level
        # 只在最终键实际存在时执行删除。
        final_key = deep_path[-1]
        if final_key not in current_level_data:
            return ATOMIC_NO_UPDATE
        del current_level_data[final_key]
        # 标记真实删除，供外层组合数据库执行结果。
        deleted = True
        return current_top

    # 数据库异常和路径不存在最终都会返回 False，但原因分别由 success 与 deleted 表达。
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
        managed_resource=True,
    ), _timed_lock(
        _db_transaction_lock,
        "数据库事务锁",
        resource_key,
    ):
        # try 会捕获事务期间的所有异常，保证失败时能够回滚并返回 False。
        # 预先设置为空可安全处理“取得数据库锁后、连接已被关闭”的竞争情况。
        db: Optional[aiosqlite.Connection] = None
        try:
            # 必须在数据库锁内重新检查连接，不能依赖进入等待队列前的 _db 状态。
            db = _require_db()
            # BEGIN IMMEDIATE 会立即申请 SQLite 写锁。
            # 它负责阻止其它服务进程在本事务结束前写入，从而提供跨进程并发保护。
            await db.execute("BEGIN IMMEDIATE")

            # 从数据库表读取 top_key 对应的最新 JSON 字符串。
            # 这里故意不读取 _data_cache，因为其它进程写入后，本进程缓存可能还是旧数据。
            async with db.execute(f"SELECT value FROM {TABLE_NAME} WHERE key = ?", (top_key,)) as cursor:
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
                    await db.rollback()

                    # 根据查询行判断键是否存在；顶层键本身可以合法存储 JSON null。
                    if row is None:
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
                await db.execute(
                    f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)",
                    (top_key, value_json),
                )

                # 提交事务，使刚才的写入正式生效并释放 SQLite 写锁。
                pending_cancellation = await _commit_cancellation_safely(db)

                # 数据库提交成功后，再同步更新本进程内存缓存。
                # 使用 json.loads 创建独立对象，避免调用方继续持有并修改缓存中的对象。
                _data_cache[top_key] = json.loads(value_json)

                # 缓存已经发布确定提交结果后，再继续传播提交期间收到的取消。
                if pending_cancellation is not None:
                    raise pending_cancellation

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
                        await db.rollback()

                        # 根据查询行判断键是否存在；不能把合法的 JSON null 当成缺失键。
                        if row is None:
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
            await db.execute(
                f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)",
                (top_key, value_json),
            )

            # 提交事务，使修改正式生效，并允许其它进程开始下一次写操作。
            pending_cancellation = await _commit_cancellation_safely(db)

            # 数据库提交成功后，用相同内容刷新当前进程的内存缓存。
            _data_cache[top_key] = json.loads(value_json)

            # 缓存已经发布确定提交结果后，再继续传播提交期间收到的取消。
            if pending_cancellation is not None:
                raise pending_cancellation

            # 数据库与缓存均已更新成功。
            return True

        except asyncio.CancelledError:
            # 取消属于控制流而不是业务失败：回滚后必须继续抛出，不能转换成 False。
            if db is not None:
                await _rollback_safely(db, f"深度原子更新 {path}")
            raise

        # 捕获路径处理、业务更新函数、JSON 转换或 SQLite 操作中发生的任何异常。
        except Exception:
            if db is not None:
                await _rollback_safely(db, f"深度原子更新 {path}")

            # 记录完整异常堆栈和更新路径，方便定位是哪一次深层更新失败。
            logger.error(f"错误: 深度原子更新失败 '{path}'", exc_info=True)

            # 向调用方明确表示本次更新因异常失败。
            return False


# ---------------------------------------------------------------------------
# SQLite 在线备份与过期文件轮转
# ---------------------------------------------------------------------------
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
        # backup_dir 默认相对项目根目录；Path 也允许调用方显式传入绝对路径。
        target_dir = BASE_DIR / backup_dir
        # 首次备份时递归创建目录，已存在则不报错。
        target_dir.mkdir(parents=True, exist_ok=True)

        # 秒级时间戳使日常定时备份文件按名称自然排序。
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_filename = f"storage_backup_{timestamp}.db"
        backup_path = target_dir / backup_filename

        logger.info(f"开始数据库备份: {backup_path}")

        # 3. 执行热备份
        # backup API 自己处理文件锁；这里再保护共享连接，避免与本进程事务交叉。
        async with _timed_lock(_db_transaction_lock, "数据库事务锁", "database-backup"):
            # 等待数据库锁期间可能发生关闭，因此在锁内再次验证源连接。
            db = _require_db()
            # 创建一个新的连接指向备份文件
            async with aiosqlite.connect(backup_path) as dest_db:
                # 使用 aiosqlite 的 backup 方法将当前 _db 复制到 dest_db
                # pages=0 表示一步完成，也可以设置为正整数来分块备份以减少阻塞
                await db.backup(dest_db)

        logger.info(f"数据库备份成功: {backup_path}")

        # 4. 执行备份轮转 (清理旧文件)
        # retention_days 为 0 或负数时明确禁用自动删除。
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
        # 修改时间早于该时间点的备份会被视为过期。
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=retention_days)

        # 遍历目录下所有 .db 文件
        for file_path in backup_dir.glob("storage_backup_*.db"):
            # 获取文件修改时间
            mtime = datetime.datetime.fromtimestamp(file_path.stat().st_mtime)

            # 只删除本模块命名规则匹配且确实超过保留期的文件。
            if mtime < cutoff_date:
                try:
                    file_path.unlink()  # 删除文件
                    logger.info(f"已清理过期备份: {file_path.name}")
                except Exception as e:
                    logger.warning(f"清理文件失败 {file_path.name}: {e}")

    except Exception:
        logger.error("执行备份轮转清理时出错", exc_info=True)
