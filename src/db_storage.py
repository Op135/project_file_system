import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

# 文件夹路径设定
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
DB_PATH = f"{BASE_DIR}/db/nicegui_storage.db"  # 数据库文件名
TABLE_NAME = "general_storage"  # 模拟 general storage
# 内存缓存 (The "Instant Retrieval" part)
# 这就是实现“即时获取”的关键。数据从内存中读取。
_data_cache: Dict[str, Any] = {}
_db: Optional[aiosqlite.Connection] = None


async def init_db():
    """
    在服务器启动时调用的初始化函数。
    1. 连接数据库。
    2. 创建表（如果不存在）。
    3. 将数据库中的所有数据加载到内存缓存 _data_cache 中。
    """
    global _db, _data_cache
    _db = await aiosqlite.connect(DB_PATH)
    # 启用 WAL 模式 (Write-Ahead Logging) 提高并发性能
    await _db.execute("PRAGMA journal_mode=WAL;")

    # 创建一个简单的 key-value 表
    # 我们将 key 存为 TEXT，将 value 序列化为 JSON 字符串后存为 TEXT
    await _db.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    await _db.commit()

    # 从数据库加载所有现有数据到内存缓存
    print(f"从数据库{DB_PATH}加载所有现有数据到内存缓存...")
    async with _db.execute(f"SELECT key, value FROM {TABLE_NAME}") as cursor:
        async for row in cursor:
            key, value_json = row
            try:
                _data_cache[key] = json.loads(value_json)
            except json.JSONDecodeError:
                print(f"警告: 不能从键'{key}'中解码json数据")
                _data_cache[key] = None  # 或 other default

    print(f"装载{len(_data_cache)}条数据到缓存中.")


async def close_db():
    """
    在服务器关闭时调用，确保数据库连接被安全关闭。
    """
    if _db:
        await _db.close()
        print("数据库连接关闭.")


def get_item(key: str, default: Any = None) -> Any:
    """
    从内存缓存中“即时获取”数据（浅引用）。
    这是一个同步函数，因为它只访问内存，速度极快。
    """
    return _data_cache.get(key, default)


async def set_item(key: str, value: Any):
    """
    深拷贝，设置一个值。
    1. 异步地将数据持久化到 SQLite 数据库。
    2. 更新内存缓存（使用反序列化后的深拷贝，打破引用）。
    """

    if _db is None:
        print("错误: 数据库未初始化.")
        return

    try:
        # 1. 将 Python 对象序列化为 JSON 字符串
        #    这是数据持久化和深拷贝的“唯一真实来源”
        value_json = json.dumps(value)
        # 好处 1（验证）： 如果 value 包含 datetime 等非法类型，json.dumps(value) 会立即失败。数据库和缓存都不会被修改。
        # 数据类型受限： 任何 JSON 不认识的类型（set, tuple, datetime, bytes, 自定义类）都会导致失败或数据失真。
        # 数据失真（有损）： 字典的 int 键会变成 str 键。tuple 会变成 list。这对某些应用是致命的。

        # 2. 持久化到数据库 (优先)
        # 使用 INSERT OR REPLACE (UPSERT) 来插入或更新
        await _db.execute(f"INSERT OR REPLACE INTO {TABLE_NAME} (key, value) VALUES (?, ?)", (key, value_json))
        await _db.commit()

        # 3. 更新内存缓存 (使用深拷贝)
        #    通过从刚序列化的字符串中 "loads"，我们确保缓存中的
        #    对象是一个全新的、无引用的副本，与数据库100%一致。
        _data_cache[key] = json.loads(value_json)
        # 好处 2（深拷贝）： json.loads(value_json) 创建了一个 100% 干净的、与数据库内容完全一致的深拷贝副本，从而解决了您担心的浅引用问题。

    except Exception as e:
        print(f"错误：数据项写入失败：'{key}': {e}")


async def remove_item(key: str) -> bool:
    """
    删除一个值。
    1. 从内存缓存中删除。
    2. 异步地从 SQLite 数据库中删除。
    删除成功返回True，失败返回False
    """
    # 1. 从缓存删除
    if key in _data_cache:
        del _data_cache[key]

    # 2. 从数据库删除
    if _db is None:
        print("错误: 数据库未初始化.")
        return False

    try:
        await _db.execute(f"DELETE FROM {TABLE_NAME} WHERE key = ?", (key,))
        await _db.commit()
        return True
    except Exception as e:
        print(f"错误：删除数据项失败：'{key}': {e}")
        return False


def get_deep_item(path: List[str], default: Any = None) -> Any:
    """
    从 db_storage 中“即时获取”一个任意深度的值（浅引用）。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    :param default: 如果找不到，返回的默认值
    :return: 查找到的值或默认值
    """
    if not path:
        return default

    # 路径的第一个元素是顶层键
    top_key = path[0]
    deep_path = path[1:]

    # 1. 从缓存中同步获取顶层对象
    current_level_data = get_item(top_key)

    if not deep_path:
        # 如果路径只有一个元素，就是返回顶层对象
        return current_level_data if current_level_data is not None else default

    # 2. 逐层深入查找
    for key in deep_path:
        if not isinstance(current_level_data, dict):
            # 路径尚未走完，但数据已不是字典，无法继续深入
            return default

        current_level_data = current_level_data.get(key)

        if current_level_data is None:
            # 提前在路径中遇到 None
            return default

    return current_level_data if current_level_data is not None else default


async def set_deep_item(path: List[str], value: Any):
    """
    异步设置一个任意深度的值（借用set_item实现深拷贝），并持久化到数据库。
    如果路径上的字典不存在，会自动创建它们。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    :param value: 要设置的新值
    """
    if not path:
        raise ValueError("路径列表 'path' 不能为空")

    # 路径的第一个元素是顶层键
    top_key = path[0]
    deep_path = path[1:]

    # 如果路径只有一个元素，等同于调用 set_item
    if not deep_path:
        await set_item(top_key, value)
        return

    # --- 执行 "Read-Modify-Write" ---

    # 1. READ (读取)
    original_data = get_item(top_key, {})
    if not isinstance(original_data, dict):
        # 如果原始数据不是字典（例如是个字符串），但我们要深度设置
        # 我们用新字典覆盖它。
        original_data = {}

    # 2. COPY (!!! V3 核心修复 !!!)
    #    在副本上操作，绝不污染缓存
    top_level_data_copy = copy.deepcopy(original_data)

    # 3. MODIFY: 逐层深入，使用 setdefault 确保路径存在
    current_level_data = top_level_data_copy
    for i, key in enumerate(deep_path):
        if i == len(deep_path) - 1:
            # 到达路径末尾，设置值
            current_level_data[key] = value
        else:
            # 还未到末尾，确保下一层是字典
            # setdefault: 如果 key 存在，返回它的值；如果不存在，
            #             创建新字典 {}，存入 key，并返回这个新字典。
            next_level = current_level_data.setdefault(key, {})

            # 健壮性处理：如果路径上的某个值存在但不是字典（例如是个字符串）
            # 我们将强制用空字典覆盖它，以允许路径继续深入。
            if not isinstance(next_level, dict):
                next_level = {}
                current_level_data[key] = next_level

            current_level_data = next_level

    # 4. WRITE: 异步将修改后的 *整个顶层对象* 写回数据库
    await set_item(top_key, top_level_data_copy)


async def del_deep_item(path: List[str]) -> bool:
    """
    异步删除一个任意深度的值，并持久化到数据库。
    如果路径不存在，函数会返回False，不会报错。
    路径存在且删除成功，函数会返回True。

    :param path: 键的路径列表，第一个必须是第一层键， 例如 ['overview_data', 'project_A', 'chip_1']
    """
    if not path:
        print("路径列表 'path' 不能为空")
        return False

    top_key = path[0]
    deep_path = path[1:]

    # 如果路径只有一个元素，等同于调用 remove_item
    if not deep_path:
        re_bool = await remove_item(top_key)
        return re_bool

    # --- 执行 "Read-Modify-Write" ---

    # 1. READ
    original_data = get_item(top_key)
    if not isinstance(original_data, dict):
        return False  # 不是字典，无法删除子项

    # 2. COPY (!!! V3 核心修复 !!!)
    top_level_data_copy = copy.deepcopy(original_data)

    # 3. MODIFY (在副本上)
    current_level_data = top_level_data_copy
    try:
        for i, key in enumerate(deep_path):
            if not isinstance(current_level_data, dict):
                # 路径中断，无法继续
                return False

            if i == len(deep_path) - 1:
                # 到达路径末尾，尝试删除
                del current_level_data[key]

                # 3. WRITE: 仅在删除成功时，才写回数据库
                await set_item(top_key, top_level_data_copy)
                return True
            else:
                # 继续深入
                current_level_data = current_level_data.get(key)
                if current_level_data is None:
                    # 路径不存在，无需操作
                    return False
        # 添加这一行来满足类型检查器。
        # 根据你的逻辑，这一行代码永远不会被执行到，
        # 但它保证了函数在所有代码路径上都有一个 bool 返回值。
        return False
    except KeyError:
        return False  # 要删除的键不存在
