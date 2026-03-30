# -*- encoding: utf-8 -*-
import logging
import os
from pathlib import Path

from nicegui import app, events, ui

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)
# 从环境变量中读取密钥。如果找不到，则使用一个仅供本地开发的默认值。
# 在生产环境中，必须设置环境变量，否则会使用不安全的默认值。
ST = os.environ.get("STORAGE_SECRET", "this_is_not_a_secret_for_development_only")

SVN_USERNAME = "temp_t1"
SVN_PASSWORD = "123456"
# ==========================================
# 工程变更 (ECN) 模块核心配置
# ==========================================

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
