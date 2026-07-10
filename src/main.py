# -*- encoding: utf-8 -*-
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
from .config import BASE_DIR, IMG_DIR, PDF_PREVIEW_CACHE, ST, WECOM_CONTACT_CACHE_TTL_SECONDS
from .config_service import ConfigService
from .error_management_config import (
    ERROR_BACKGROUND_REMINDER_ENABLED,
    ERROR_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS,
    ERROR_BACKGROUND_REMINDER_INTERVAL_SECONDS,
    ERROR_REMINDER_CHECK_WINDOW,
)
from .issue_workflow_utils import is_time_in_window
from .sample_issue_config import (
    SAMPLE_BACKGROUND_REMINDER_ENABLED,
    SAMPLE_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS,
    SAMPLE_BACKGROUND_REMINDER_INTERVAL_SECONDS,
    SAMPLE_REMINDER_CHECK_WINDOW,
)
from .user_service import UserService
from .utils import (  # 导入上面定义的函数
    handle_connect,
    handle_disconnect,
    updata_overview_config,
)
from .wecom_service import refresh_wecom_contacts_if_stale, retry_failed_wecom_messages

# 注册这两个钩子，实现监控用户连线与下线
app.on_connect(handle_connect)
app.on_disconnect(handle_disconnect)

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


# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


def setup_logging():
    # 1. 创建 Logger
    # 获取根记录器
    logger = logging.getLogger()
    # 设置全局最低门槛：只有 INFO 及以上才会处理
    logger.setLevel(logging.INFO)
    # openpyxl 解析 Excel 时如果表格有样式兼容问题，会疯狂报 Warning，屏蔽掉
    logging.getLogger("openpyxl").setLevel(logging.ERROR)
    # httpx (NiceGUI 内部使用) 的请求日志太多，屏蔽掉
    logging.getLogger("httpx").setLevel(logging.ERROR)
    # 还可以屏蔽 watchfiles (如果你不想看文件变动的监控日志)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

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

    logging.info("日志系统初始化完成 (Console + File)")


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

    manager = StorageBackupManager(json_storage_file=f"{BASE_DIR}/.nicegui/storage-general.json", backup_dir=backup_dir)

    # 2. 启动每日定时任务 (例如：每日凌晨 18:30 进行备份)
    manager.start_daily_schedule(hour=18, minute=30)

    # 3. 将实例挂载到 app.state
    # 作用：防止实例被垃圾回收，且允许在其他页面通过 app.state.backup_manager 调用手动备份
    app.state.backup_manager = manager
    # 4. 【关键修改】将数据库关闭注册移到这里！
    # 因为代码是从上往下执行的，先执行了 manager 的初始化（注册了备份），
    # 再执行这一行（注册关闭数据库）。
    # 这样关机列表就是：[1.备份, 2.关库]。
    app.on_shutdown(db_storage.close_db)


def init_pending_history_task():
    """初始化每日待办数据快照任务 (APScheduler + 工作日精准触发)"""
    import copy
    from datetime import datetime

    from apscheduler.schedulers.background import BackgroundScheduler
    from chinese_calendar import is_workday

    # 局部导入避免循环引用。假设 statistics.py 位于 pages 文件夹下
    try:
        from .pages.statistics import record_daily_stats
    except ImportError as e:
        logger.error(f"无法导入 record_daily_stats，请检查模块路径: {e}")
        record_daily_stats = None

    def daily_1800_job():
        """每天 18:00 准时执行的任务实体"""
        now = datetime.now()

        # 1. 节假日拦截器
        if not is_workday(now):
            logger.info(f"今日 ({now.strftime('%Y-%m-%d')}) 为法定休息日/周末，跳过待办统计。")
            return

        logger.info("工作日 18:00 触发：开始执行待办状态快照记录...")

        try:
            today_str = now.strftime("%Y-%m-%d")
            # 获取存储结构
            history = app.storage.general.setdefault("overview_pending_history", {})
            current_pending = app.storage.general.get("overview_charge_pending", {})
            # 2. 更新内存快照 (服务于前端 7日待办趋势图)
            history[today_str] = copy.deepcopy(current_pending)

            # 清理超过 14 天的内存快照，防止 JSON 文件体积无限膨胀
            sorted_dates = sorted(history.keys())
            if len(sorted_dates) > 14:
                for d in sorted_dates[:-14]:
                    history.pop(d, None)

            # 3. 触发 Excel 长期持久化 (服务于前端 30日多维图表)
            if record_daily_stats:
                project_summary = app.storage.general.get("project_summary", {})
                record_daily_stats(project_summary, current_pending)

        except Exception as e:
            logger.error(f"每日待办项快照记录出错: {e}")

    # 配置后台调度器
    scheduler = BackgroundScheduler()
    # 设定每天 18:00 触发
    scheduler.add_job(daily_1800_job, "cron", hour=18, minute=0)
    scheduler.start()
    logger.info("数据统计定时器已挂载 (触发规则: 工作日 18:00)")


def init_wecom_retry_task():
    """初始化企业微信失败消息全局重试任务。"""

    async def retry_wecom_messages():
        success_count, fail_count = await retry_failed_wecom_messages()
        if success_count or fail_count:
            logger.info("企业微信失败消息重试完成：成功 %s 条，仍失败 %s 条", success_count, fail_count)

    app.timer(300, retry_wecom_messages, once=True)
    app.timer(3600, retry_wecom_messages)
    logger.info("企业微信失败消息重试任务已挂载。")


def init_wecom_contacts_task():
    """初始化企业微信通讯录缓存刷新任务。"""

    async def refresh_wecom_contacts():
        success, message = await refresh_wecom_contacts_if_stale()
        if success:
            logger.info(message)
        else:
            logger.warning(message)

    app.timer(20, refresh_wecom_contacts, once=True)
    app.timer(max(WECOM_CONTACT_CACHE_TTL_SECONDS, 3600), refresh_wecom_contacts)
    logger.info("企业微信通讯录缓存刷新任务已挂载。")


def init_error_reminder_task():
    """初始化生产异常纠正预防措施后台提醒检查任务。

    该任务挂载在服务进程上，不依赖用户打开异常管理页面。首次执行延迟、循环间隔和总开关均来自
    根目录 ``error_management_config.json``。每次检查内部仍会使用数据库认领机制防止重复提醒，
    所以手工检查与后台检查同时发生也不会重复发送同一条通知。
    """
    if not ERROR_BACKGROUND_REMINDER_ENABLED:
        logger.info("生产异常后台提醒检查任务已通过配置禁用。")
        return

    async def check_error_reminders():
        if not is_time_in_window(ERROR_REMINDER_CHECK_WINDOW):
            logger.info(
                "生产异常提醒检查已跳过：当前时间不在配置窗口 %s-%s。",
                ERROR_REMINDER_CHECK_WINDOW["start"],
                ERROR_REMINDER_CHECK_WINDOW["end"],
            )
            return

        # 延迟导入页面模块，避免应用启动阶段因页面与 main 互相导入形成循环依赖。
        from .pages.error_management import check_and_send_error_reminders

        sent_count, fail_count = await check_and_send_error_reminders(show_result=False)
        if sent_count or fail_count:
            logger.info("生产异常提醒检查完成：新发成功 %s 条，失败进入重试 %s 条", sent_count, fail_count)

    app.timer(ERROR_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS, check_error_reminders, once=True)
    app.timer(ERROR_BACKGROUND_REMINDER_INTERVAL_SECONDS, check_error_reminders)
    logger.info(
        "生产异常后台提醒检查任务已挂载（首次 %s 秒，循环 %s 秒，窗口 %s-%s）。",
        ERROR_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS,
        ERROR_BACKGROUND_REMINDER_INTERVAL_SECONDS,
        ERROR_REMINDER_CHECK_WINDOW["start"],
        ERROR_REMINDER_CHECK_WINDOW["end"],
    )


def init_sample_issue_reminder_task():
    """初始化样品问题纠正预防措施后台提醒检查任务。"""
    if not SAMPLE_BACKGROUND_REMINDER_ENABLED:
        logger.info("样品问题后台提醒检查任务已通过配置禁用。")
        return

    async def check_sample_issue_reminders():
        if not is_time_in_window(SAMPLE_REMINDER_CHECK_WINDOW):
            logger.info(
                "样品问题提醒检查已跳过：当前时间不在配置窗口 %s-%s。",
                SAMPLE_REMINDER_CHECK_WINDOW["start"],
                SAMPLE_REMINDER_CHECK_WINDOW["end"],
            )
            return

        from .pages.sample_issue_collection import check_and_send_sample_issue_reminders

        sent_count, fail_count = await check_and_send_sample_issue_reminders(show_result=False)
        if sent_count or fail_count:
            logger.info("样品问题提醒检查完成：新发成功 %s 条，失败进入重试 %s 条", sent_count, fail_count)

    app.timer(SAMPLE_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS, check_sample_issue_reminders, once=True)
    app.timer(SAMPLE_BACKGROUND_REMINDER_INTERVAL_SECONDS, check_sample_issue_reminders)
    logger.info(
        "样品问题后台提醒检查任务已挂载（首次 %s 秒，循环 %s 秒，窗口 %s-%s）。",
        SAMPLE_BACKGROUND_REMINDER_INITIAL_DELAY_SECONDS,
        SAMPLE_BACKGROUND_REMINDER_INTERVAL_SECONDS,
        SAMPLE_REMINDER_CHECK_WINDOW["start"],
        SAMPLE_REMINDER_CHECK_WINDOW["end"],
    )


# ==========================================
# 🌟 核心重构：统一的异步启动序列
# ==========================================
async def master_startup():
    """
    主控启动序列：严格保证执行的先后顺序
    """
    logger.info("系统启动序列开始执行...")

    # 第一顺位：建立底层基础设施（必须加 await 等待完成）
    await db_storage.init_db()

    # 第二顺位：执行依赖数据库的全局数据加载与配置更新
    # 此时可以100%确定数据库已经就绪
    updata_overview_config()
    # 第三顺位：满足需求，在系统启动时仅执行一次业务字典更新
    # update_overview_charge_pending_dic("all") 上面updata_overview_config()里已经包含

    # 第三顺位：启动非核心周边服务
    init_backup_service()

    # 第四顺位：启动每日历史记录统一定时任务
    init_pending_history_task()

    # 第五顺位：启动企业微信通讯录缓存刷新任务
    init_wecom_contacts_task()

    # 第六顺位：启动企业微信失败消息统一重试任务
    init_wecom_retry_task()

    # 第七顺位：启动生产异常后台提醒检查任务
    init_error_reminder_task()

    # 第八顺位：启动样品问题后台提醒检查任务
    init_sample_issue_reminder_task()

    logger.info("系统启动序列全部执行完毕。")


# ==========================================
# 生命周期事件注册
# ==========================================
# ✅ 注册唯一的启动统管函数
app.on_startup(master_startup)
# 为了能在系统关闭时顺利执行备份，需将这里外部注册关闭数据库移到init_backup_service函数内部最后
# app.on_shutdown(db_storage.close_db)

# 存储服务器层级 概述数据 的变量初始化
# app.storage.general.setdefault("overview_data", {})
# 存储服务器层级 项目需求最高版本号 的变量初始化
app.storage.general.setdefault("project_req_max_ver", {})
# 存储服务器层级 项目简介 的变量初始化
app.storage.general.setdefault("project_summary", {})
# 存储服务器层级 项目简介与概述数据动态更新配置 的变量初始化
app.storage.general.setdefault("project_table_update_config", {})
# 存储服务器层级 各项目各工程角色概述数据负责人 的变量初始化
app.storage.general.setdefault("overview_role", {})
# 存储服务器层级 各项目各工程角色概述数据负责人是否需要核对概述的记录信息
app.storage.general.setdefault("overview_charge_pending", {})
# 存储服务器层级 各项目负责销售 的变量初始化
app.storage.general.setdefault("project_sale", {})
# 储存服务器层级 等待审核的项目需求即待审版本
app.storage.general.setdefault("wait_review", {})
# 储存服务器层级 记录暂存的项目需求项目与版本
app.storage.general.setdefault("temp_req", {})
# 储存服务器层级 用于存储用户偏好信息
app.storage.general.setdefault("user_preferences", {})
# 储存服务器层级 用于存储项目定制信息
app.storage.general.setdefault("custom_labels", {})
# 储存服务器层级 用于标记项目概述是否要因转产而刷新
# app.storage.general.setdefault("conversion_refresh", {})
# 储存服务器层级 用于记录已经存在的临时项目号
app.storage.general.setdefault("temp_project_name", [])
# 储存服务器层级 用于记录各项目的项目工程师负责人
app.storage.general.setdefault("project_engineer", {})
# 储存服务器层级 用于记录各项目的项目工程师负责人
app.storage.general.setdefault("over_change_broadcast", {})
# 储存服务器层级 用于扁平化记录概述项配置信息
app.storage.general.setdefault("over_config_data_flat", {})
# 储存服务器层级 用于扁平化需求配置里的定制标签信息{"node_id": {"option_id":"option_label"}}
app.storage.general.setdefault("config_service_custom_labels", {})
# 用于全局更新标记，通知所有监听此项目的客户端，暂时不用
app.storage.general.setdefault("overview_last_update", {})
# 储存服务器层级 用于记录概述数据的历史快照，键为日期字符串，值为当日概述数据的快照
app.storage.general.setdefault("overview_pending_history", {})
# 已完成概述填写的项目列表
app.storage.general.setdefault("overview_completed", [])
# 仅缺需填的项目列表
app.storage.general.setdefault("overview_only_need", [])
# 用于记录概述数据的版本核对状态，结构示例：{"project_name": {"version": True/False}}
# True 表示已核对，False 表示未核对或需要重新核对（例如因为转产导致版本变更）
app.storage.general.setdefault("overview_active_state_checked_versions", {})


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


# 【测试代码】人为制造崩溃
# logger.info("准备测试崩溃备份...")
# raise RuntimeError("启动阶段的致命错误测试！")
# ======================
# 运行程序
# ======================
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="研发项目文件管理系统",
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
        # reload=False,
        # 🚀 核心新增配置：增加断线重连宽容期（默认是 3.0 秒）
        # 设置为 300.0 秒（5分钟），允许前端 WebSocket 断开长达5分钟。
        reconnect_timeout=300.0,
        # 【关键修改 1】让父进程闭嘴
        # 将 Uvicorn 自身的日志级别设为 warning，
        # 这样它就不会打印 "changes detected" 这种 INFO 级别的废话了
        uvicorn_logging_level="warning",
        # 添加排除项：忽略以 .json 结尾的文件，忽略 backups 文件夹，忽略数据库文件
        uvicorn_reload_excludes="logs,backups,.nicegui,*.json,*.db,*.log,*.txt",
    )
