# -*- encoding: utf-8 -*-
import os
from pathlib import Path

from nicegui import app

# 生产异常模块拥有独立的根目录 JSON 配置和校验加载器。
# 此处仅保留旧常量别名，兼容项目中可能仍从 src.config 导入这些名称的其它模块。
from . import error_management_config as _error_management_config
from .ecn_management_config import (
    ECN_ALLOWED_PROJECT_STATES,
    ECN_SCHEMA_CONFIG,
    ECN_SCHEME_INITIATOR_ROLES,
    ECN_SCHEME_WRITER_ROLES,
    ECN_WORKFLOW_ROUTES,
    ECNState,
)

# 从环境变量中读取密钥。如果找不到，则使用一个仅供本地开发的默认值。
# 在生产环境中，必须设置环境变量，否则会使用不安全的默认值。
ST = os.environ.get("STORAGE_SECRET", "this_is_not_a_secret_for_development_only")
# 通过环境变量获取企业微信基础配置
WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "your_corp_id")
WECOM_AGENT_ID = os.environ.get("WECOM_AGENT_ID", "1000008")
WECOM_CORP_SECRET = os.environ.get("WECOM_CORP_SECRET", "your_corp_secret")
# 设置企业微信默认消息接收人，可以是用户ID、部门ID或标签ID，格式如下：
WECOM_DEFAULT_TOUSER = "YueYeXiaoSheng"
# 企业微信接口地址
WECOM_API_BASE = "https://qyapi.weixin.qq.com"
# 企业微信通讯录同步配置。建议生产环境配置为通讯录同步 Secret 或具备通讯录权限的应用 Secret。
WECOM_CONTACTS_SECRET = os.environ.get("WECOM_CONTACTS_SECRET", WECOM_CORP_SECRET)
WECOM_CONTACT_ROOT_DEPARTMENT_ID = int(os.environ.get("WECOM_CONTACT_ROOT_DEPARTMENT_ID", "1"))
WECOM_CONTACT_CACHE_TTL_SECONDS = int(os.environ.get("WECOM_CONTACT_CACHE_TTL_SECONDS", "86400"))
# 企业微信发送日志保留天数和失败重试次数
WECOM_LOG_RETENTION_DAYS = int(os.environ.get("WECOM_LOG_RETENTION_DAYS", "90"))
WECOM_MAX_RETRY_COUNT = int(os.environ.get("WECOM_MAX_RETRY_COUNT", "3"))
# 新代码应直接从 src.error_management_config 导入；以下别名可在确认无旧调用后逐步移除。
SYSTEM_PUBLIC_BASE_URL = _error_management_config.ERROR_PUBLIC_BASE_URL
ERROR_EXTENSION_APPROVER_ROLES = _error_management_config.ERROR_EXTENSION_APPROVER_ROLES
ERROR_EXTENSION_NOTIFY_TARGETS = _error_management_config.ERROR_EXTENSION_NOTIFY_TARGETS
SVN_USERNAME = "temp_t1"

SVN_PASSWORD = "123456"

# ECN 业务配置已迁移到根目录 ecn_management_config.json；上方导入保留兼容导出。
# 如果某个分组没在这里配置，代码里默认它使用 InteractiveButton。
OVERVIEW_UI_RENDER_REGISTRY = {
    "光源": "OverviewTableGroup",
    "产品图纸": "OverviewTableGroup",
    "PCB图纸": "OverviewTableGroup",
    "驱动板资料": "OverviewTableGroup",
    "控制板资料": "OverviewTableGroup",
    "光源基板资料": "OverviewTableGroup",
    "其它类型PCB资料": "OverviewTableGroup",
    "软件文档": "OverviewTableGroup",
    "UI文档": "OverviewTableGroup",
}
# 概述可上传文件类型，除了图片，图片都可以
OVER_UPLOADS_FILE_TYPE = {
    ".pdf",
    ".xlsx",
    ".docx",
    ".pptx",
    ".txt",
    ".csv",
    ".xml",
    ".hex",
    ".s19",
    ".bin",
    ".zip",
    ".rar",
    ".mp4",
    ".webm",
    ".mov",
    ".step",
    ".stp",
    ".x_t",
}
# 需求可上传文件类型，除了图片，图片都可以
REQ_UPLOADS_FILE_TYPE = {
    ".pdf",
    ".xlsx",
    ".docx",
    ".pptx",
    ".txt",
    ".csv",
    ".xml",
    ".zip",
    ".rar",
    ".mp4",
    ".webm",
    ".mov",
    ".step",
    ".stp",
    ".x_t",
}
# 文件夹路径设定
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
IMG_DIR = f"{BASE_DIR}/img"
REQ_REMOVE_DIR = f"{BASE_DIR}/req/remove"
UPLOADS_DIR = f"{BASE_DIR}/uploads"
SUBMIT_FILES_DIR = f"{BASE_DIR}/files"
REQ_DIR = f"{BASE_DIR}/req"
OVER_DIR = f"{BASE_DIR}/over"
AVATAR_DIR = f"{UPLOADS_DIR}/avatars"

os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(SUBMIT_FILES_DIR, exist_ok=True)
os.makedirs(REQ_DIR, exist_ok=True)
os.makedirs(OVER_DIR, exist_ok=True)
os.makedirs(AVATAR_DIR, exist_ok=True)

# URL路径设定
UPLOAD_URL_DIR = "/uploads"
FILES_URL_DIR = "/files"
AVATAR_URL_DIR = "/uploads/avatars"
IMG_URL_DIR = "/img"

# 创建一个全局的、内存中的缓存
# 用它来临时存储 PDF 字节，完全绕过 JSON 序列化
PDF_PREVIEW_CACHE = {}

# 总表识别到符合以下正则的内容将不显示，"^无$"之前不显示，现在显示
TABLE_IGNORE_REGULAR = [
    "L[PB][0-9A-Z]{2}[A-Z]",
]
# 概述填写内容识别到符合以下正则的内容将等同于填写“无”
NONE_REGULAR = ["^无$"]
# 项目状态可选项
PROJECT_STATE_LIST = ["作废", "待定", "研发", "转产", "试产", "量产"]
# 项目信息表按角色限制显示的状态规则：命中角色关键词时，仅显示这些状态的项目。
PROJECT_TABLE_STATE_FILTER_ROLE_KEYWORDS = ["生产", "质量", "采购", "客服", "IE工程"]
PROJECT_TABLE_STATE_FILTER_ALLOWED_STATES = ["试产", "量产"]
# 默认头像路径
PRESET_AVATARS = [
    f"{IMG_URL_DIR}/avatars/avatar1.png",
    f"{IMG_URL_DIR}/avatars/avatar2.png",
    f"{IMG_URL_DIR}/avatars/avatar3.png",
    f"{IMG_URL_DIR}/avatars/avatar4.png",
    f"{IMG_URL_DIR}/avatars/avatar5.png",
    f"{IMG_URL_DIR}/avatars/avatar6.png",
    f"{IMG_URL_DIR}/avatars/avatar7.png",
    f"{IMG_URL_DIR}/avatars/avatar8.png",
    f"{IMG_URL_DIR}/avatars/avatar9.png",
    f"{IMG_URL_DIR}/avatars/avatar10.png",
    f"{IMG_URL_DIR}/avatars/avatar11.png",
    f"{IMG_URL_DIR}/avatars/avatar12.png",
    f"{IMG_URL_DIR}/avatars/avatar13.png",
    f"{IMG_URL_DIR}/avatars/avatar14.png",
    f"{IMG_URL_DIR}/avatars/avatar15.png",
]
# 配置临时项目号长度
TEMP_PROJECT_NUM_LENGTH = 4
# 定义头像处理后的最大尺寸
AVATAR_MAX_SIZE = (190, 190)
# ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf"}
# MAX_FILE_SIZE = 20 * 1024 * 1024
app.add_static_files(UPLOAD_URL_DIR, UPLOADS_DIR)
app.add_static_files(IMG_URL_DIR, IMG_DIR)
