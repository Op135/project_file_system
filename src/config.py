# -*- encoding: utf-8 -*-
import os
from pathlib import Path

from nicegui import app

# 生产异常模块拥有独立的根目录 JSON 配置和校验加载器。
# 此处仅保留旧常量别名，兼容项目中可能仍从 src.config 导入这些名称的其它模块。
from . import error_management_config as _error_management_config

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

# ==========================================
# 工程变更 (ECN) 模块核心配置
# ==========================================
# ECN 数据结构字典 (Schema Config)
ECN_SCHEMA_CONFIG = {
    "material_categories": [
        "光源",
        "光源基板",
        "光学器件",
        "结构加工件",
        "标签包材",
        "紧固件",
        "外购标准件",
        "电子料",
        "PCB",
        "PCBA",
        "线材",
        "固件",
        "辅料",
    ],
    "material_actions": ["新增", "调量", "弃用", "返工使用", "弃用更换"],
    # 这里只留纯粹的“知会型”影响
    "impact_dimensions": [
        "光学部件",
        "内部结构",
        "结构外观",
        "线材",
        "标签包装",
        "硬件易识别",
        "硬件难识别",
        "硬件接口",
        "固件",
        "UI",
        "工艺",
        "工装治具",
        "成本",
        "生产效率",
        "风险等级",
    ],
    # 这里放所有需要强制出方案的“交付物”
    "document_types": [
        "光学件图纸",
        "结构件图纸",
        "成品/PCBA图档(3D/2D)",
        "线材图纸",
        "包材图纸",
        "原理图/Layout图/丝印图",
        "其它外购件图纸",
        "产品总BOM",
        "电子BOM",
        "装箱清单",
        "通讯协议/XML协议文档",
        "硬件使用说明书",
        "产品接线说明书",
        "固件使用说明书",
        "产品使用说明书",
        "产品技术规格书",
        "医疗器械产品风险管理",
        "SOP/作业指导书",
        "出厂检测报告",
        "工装治具清单",
        "其它",
    ],
    "reasons": ["需求更改", "设计改善", "工艺调整", "物料替换", "资料修正", "产品定标", "其他"],
}
# 1. 允许发起 ECN 变更的项目状态（严格模式）
ECN_ALLOWED_PROJECT_STATES = ["试产", "量产"]


# 1. ECN 状态机枚举增加
class ECNState:
    DRAFT = "草稿"
    ECR_REVIEWING = "ECR 审批中"
    ECN_SCHEMING = "ECN 方案编写与确认中"  # <--- 协同编辑阶段
    ECN_REVIEWING = "ECN 方案评审中"  # <--- 评审阶段
    ECN_EXECUTING = "ECN 等待各部执行确认"
    PENDING_FINAL_EXECUTE = "等待最终数据变更"
    CLOSED = "变更已完成"
    CANCEL = "变更已作废"
    REJECTED = "已被驳回"  # <--- 驳回态


# 2. 方案协同控制角色
# 有权限点击“发起 ECN 方案审批”的总控角色
ECN_SCHEME_INITIATOR_ROLES = ["研发经理", "admin"]
# 允许进入方案区编写方案的角色
ECN_SCHEME_WRITER_ROLES = ["研发", "工程", "质量"]

# 3. 审批流动态路由配置 (剥离了编写阶段，只保留审批)
ECN_WORKFLOW_ROUTES = {
    "ECR_PHASE": {"SALES_INITIATED": [["销售总监"], ["研发经理"]], "RD_INITIATED": [["研发经理"], ["销售总监"]]},
    # 纯方案评审阶段
    "ECN_SCHEME_REVIEW_PHASE": [["研发经理"], ["销售总监"], ["工程", "质量", "PMC"]],
    "ECN_EXECUTION_PHASE": [["工程", "生产", "PMC", "质量"], ["研发经理_EXECUTE"]],
}
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

# 总表识别到符合以下正则的内容将不显示
TABLE_IGNORE_REGULAR = ["^无$", "L[PB][0-9A-Z]{2}[A-Z]"]
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
