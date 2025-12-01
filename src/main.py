# -*- encoding: utf-8 -*-
import json
import logging
import os
import warnings
from logging.handlers import RotatingFileHandler

from nicegui import app, ui
from starlette.responses import Response

# 关键：导入所有页面。这将自动注册所有 @ui.page 路由
# 导入您已有的服务和新创建的模块
# 注意：现在使用相对导入或确保 src 是 Python 路径的一部分
# 导入新的配置和工具模块
from . import (
    db_storage,
    pages,  # 这将执行 src/pages/__init__.py
)
from .components import StorageBackupManager
from .config import BASE_DIR, IMG_DIR, PDF_PREVIEW_CACHE, ST
from .config_service import ConfigService
from .user_service import UserService

# 忽略所有来自 openpyxl 的 UserWarning
# 这样可以精确地屏蔽掉这个警告，而不影响其他库的警告
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# --- 最佳实践：使用 app.state 管理全局服务实例 ---
# 初始化服务并将实例附加到 app.state
# 将用户配置初始化到app.state.users_data
app.state.user_service = UserService()
app.state.users_data = app.state.user_service.load_users()
# 将config_service.json配置初始化到app.state.init_config_data
app.state.config_service = ConfigService()
app.state.init_config_data = app.state.config_service.load_config()
# 将overview_config.json配置初始化到服务器储存over_config_data
with open(f"{BASE_DIR}/overview_config.json", "r", encoding="utf-8") as f:
    # 使用 json.load() 读取文件内容并解析
    app.storage.general["over_config_data"] = json.load(f)


def setup_logging():
    # 1. 创建 Logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 2. 定义格式
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # 3. 处理器 A：控制台 (让开发者看到)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 4. 处理器 B：文件 (作为黑匣子存档)
    # 使用 RotatingFileHandler 可以防止日志文件无限膨胀
    # maxBytes=1MB, backupCount=5 表示：文件写满 1MB 后自动切割，最多保留 5 个旧文件
    log_dir = f"{BASE_DIR}/logs"
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(f"{log_dir}/app.log", maxBytes=1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


# 在 main.py 最开始调用
setup_logging()


# ==========================================
# ### 备份服务初始化函数
# ==========================================
def init_backup_service():
    """初始化备份管理器并启动定时任务"""
    # 1. 实例化管理器
    # storage_file: NiceGUI 默认生成的通用存储文件名为 'storage-general.json'
    # backup_dir: 建议将备份放在 BASE_DIR 下的 backups 文件夹中
    backup_dir = os.path.join(BASE_DIR, "backups")

    manager = StorageBackupManager(storage_file=f"{BASE_DIR}/.nicegui/storage-general.json", backup_dir=backup_dir)

    # 2. 启动每日定时任务 (例如：每日凌晨 18:30 进行备份)
    manager.start_daily_schedule(hour=18, minute=30)

    # 3. 将实例挂载到 app.state
    # 作用：防止实例被垃圾回收，且允许在其他页面通过 app.state.backup_manager 调用手动备份
    app.state.backup_manager = manager


# ==========================================
# 生命周期事件注册
# ==========================================
app.on_startup(db_storage.init_db)
# 注册备份初始化
app.on_startup(init_backup_service)

app.on_shutdown(db_storage.close_db)

# 存储服务器层级 概述数据 的变量初始化
# app.storage.general.setdefault("overview_data", {})
# 存储服务器层级 项目需求最高版本号 的变量初始化
app.storage.general.setdefault("project_req_max_ver", {})
# 存储服务器层级 项目简介 的变量初始化
app.storage.general.setdefault("project_summary", {})
# 存储服务器层级 项目简介与概述数据动态更新配置 的变量初始化
app.storage.general.setdefault("project_overview_config", {})
# 存储服务器层级 各项目各工程角色概述数据负责人 的变量初始化
app.storage.general.setdefault("overview_role", {})
# 存储服务器层级 各项目各工程角色概述数据负责人是否需要核对概述的记录信息
app.storage.general.setdefault("overview_charge_pending", {})
# 存储服务器层级 各项目负责销售 的变量初始化
app.storage.general.setdefault("project_sale", {})
# 储存服务器层级 等待审核的项目需求即待审版本
app.storage.general.setdefault("wait_review", {})
# 储存服务器层级 用于存储用户偏好信息
app.storage.general.setdefault("user_preferences", {})
# 储存服务器层级 用于存储用户偏好信息
app.storage.general.setdefault("custom_labels", {})
# 储存服务器层级 用于标记项目概述是否要因转产而刷新
app.storage.general.setdefault("conversion_refresh", {})


@ui.page("/view/svn_pdf")
async def get_svn_pdf_from_cache(id: str):  # <--- [修改] 接收 id 查询参数
    """
    一个专门的路由，用于从 *内存缓存* 中获取并返回 PDF 字节。
    """
    # !!! 关键修改：从 PDF_PREVIEW_CACHE 中读取 !!!
    #    使用 .pop() 来获取数据并立即将其从缓存中删除 (自清理)
    pdf_bytes = PDF_PREVIEW_CACHE.pop(id, None)

    if not pdf_bytes:
        return Response(content="PDF 数据未找到、已过期或会话已结束。", status_code=404)

    # (这个返回部分保持不变)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="document.pdf"'},
    )


# ======================
# 登录界面
# ======================
# 设置根路径重定向
@ui.page("/")
def root():
    ui.navigate.to("/login")  # 自动跳转至登录页


# ======================
# 运行程序
# ======================
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="项目文件管理系统",
        favicon=f"{IMG_DIR}/RFRF.png",
        # host='0.0.0.0' 允许来自局域网的任何IP访问
        host="0.0.0.0",
        # port=8080 是您选择的端口，可以自定义
        port=8080,
        storage_secret=ST,  # 添加存储密钥
        dark=False,
        # 在生产环境中，必须禁用热重载功能，以获得更好的性能和稳定性
        # False 不自动重载，True自动重载
        reload=True,
        # 添加排除项：忽略以 .json 结尾的文件，忽略 backups 文件夹，忽略数据库文件
        uvicorn_reload_excludes=".nicegui/*",
    )
