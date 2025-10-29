# -*- encoding: utf-8 -*-
import os
from pathlib import Path

# 从环境变量中读取密钥。如果找不到，则使用一个仅供本地开发的默认值。
# 在生产环境中，必须设置环境变量，否则会使用不安全的默认值。
ST = os.environ.get("STORAGE_SECRET", "this_is_not_a_secret_for_development_only")

# 文件夹路径设定
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
IMG_DIR = f"{BASE_DIR}/img"
UPLOADS_DIR = f"{BASE_DIR}/uploads"
SUBMIT_FILES_DIR = Path(f"{BASE_DIR}/files")
REQ_DIR = f"{BASE_DIR}/req"
OVER_DIR = f"{BASE_DIR}/over"
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(SUBMIT_FILES_DIR, exist_ok=True)
os.makedirs(REQ_DIR, exist_ok=True)
os.makedirs(OVER_DIR, exist_ok=True)
# URL路径设定
UPLOAD_URL_DIR = "/uploads"
FILES_URL_DIR = "/files"


# ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf"}
# MAX_FILE_SIZE = 20 * 1024 * 1024
