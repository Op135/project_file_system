# -*- encoding: utf-8 -*-
import os
from pathlib import Path

from nicegui import app, events, ui

# 从环境变量中读取密钥。如果找不到，则使用一个仅供本地开发的默认值。
# 在生产环境中，必须设置环境变量，否则会使用不安全的默认值。
ST = os.environ.get("STORAGE_SECRET", "this_is_not_a_secret_for_development_only")

# 概述可上传文件类型，除了图片，图片都可以
OVER_UPLOADS_FILE_TYPE = {".pdf", ".xlsx", ".docx", ".pptx", ".txt", ".csv", ".hex", ".s19", ".bin", ".zip"}
# 需求可上传文件类型，除了图片，图片都可以
REQ_UPLOADS_FILE_TYPE = {".pdf", ".xlsx", ".docx", ".pptx", ".txt", ".csv", ".zip"}
# 文件夹路径设定
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
IMG_DIR = f"{BASE_DIR}/img"
UPLOADS_DIR = f"{BASE_DIR}/uploads"
SUBMIT_FILES_DIR = Path(f"{BASE_DIR}/files")
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
# 定义头像处理后的最大尺寸
AVATAR_MAX_SIZE = (190, 190)
# ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf"}
# MAX_FILE_SIZE = 20 * 1024 * 1024
app.add_static_files(UPLOAD_URL_DIR, UPLOADS_DIR)
app.add_static_files(IMG_URL_DIR, IMG_DIR)
