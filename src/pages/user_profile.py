# -*- encoding: utf-8 -*-
import io
import logging
import uuid  # 用于生成唯一文件名
from pathlib import Path

from nicegui import app, ui
from nicegui.events import GenericEventArguments, KeyEventArguments, MouseEventArguments, UploadEventArguments
from PIL import Image  # 导入 Pillow

from ..config import (  # 用于获取 IMG_DIR
    AVATAR_DIR,
    AVATAR_MAX_SIZE,
    AVATAR_URL_DIR,
    IMG_DIR,
    PRESET_AVATARS,
)
from ..utils import get_cache_busted_path, logout

# 步骤 1: 从我们重构的 login.py 中导入可重用的函数
from .login import create_password_dialog

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/profile")
def user_profile_page():
    # 1. 验证用户是否登录
    if not (current_user := app.storage.user.get("current_user")):
        ui.navigate.to("/login")
        return

    def pick_file(uploader):
        # 在上传新文件前，先清空upload列表，否则后续删除文件后，不能在重新插入
        uploader.reset()
        # 触发隐藏的上传组件
        uploader.run_method("pickFiles")  # 触发浏览器的文件选择对话框

    # 3. 定义头像更新函数
    def set_avatar(avatar_path: str):
        # setdefault 确保字典键存在
        app.storage.general["user_preferences"].setdefault(current_user, {})
        # 更新全局存储
        app.storage.general["user_preferences"][current_user]["avatar"] = avatar_path
        # 更新页面上显示的两个头像
        # 步骤 3: 在 *更新显示* 时，应用缓存清除
        display_path = get_cache_busted_path(avatar_path)
        current_avatar_display.set_source(display_path)
        header_avatar_display.set_source(display_path)

    # --- 新增：处理上传的函数 ---
    async def handle_upload(e: UploadEventArguments):
        try:
            file_content = await e.file.read()
            if not file_content:
                ui.notify(
                    "未选择文件",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return
            img = Image.open(io.BytesIO(file_content))

            # 2. 核心处理：调整图片大小
            #    .thumbnail() 会在保持宽高比的前提下，将图片缩小到指定尺寸内
            img.thumbnail(AVATAR_MAX_SIZE)

            # 3. 生成唯一文件名 (使用 UUID 防止重名覆盖)
            #    我们统一保存为 PNG 格式，以支持透明度
            unique_filename = f"{current_user}_{uuid.uuid4().hex}.png"
            save_path = f"{AVATAR_DIR}/{unique_filename}"

            # 5. 保存处理后的图片
            #    如果图片是 'P' (调色板) 或 'RGBA' 模式，直接保存为 PNG
            if img.mode in ("P", "RGBA"):
                img.save(save_path, "PNG")
            else:
                #    如果是 'RGB' (如 JPG)，先转换为 'RGBA' 再保存，以防万一
                img.convert("RGBA").save(save_path, "PNG")

            # 6. 获取 Web 访问路径
            web_path = f"{AVATAR_URL_DIR}/{unique_filename}"

            # 7. 调用现有的 set_avatar 函数
            set_avatar(web_path)

            ui.notify(
                "自定义头像设置成功！",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        except Exception as ex:
            logger.error("头像上传处理失败", exc_info=True)  # 在服务器端打印错误详情
            ui.notify(
                f"上传文件 '{e.file.name}' 失败: {str(ex)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 2. 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

    # --- 页面 UI 布局 ---
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("用户信息管理").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            header_avatar_display = ui.image(current_display_path)
            with ui.menu().props("auto-close flex-nowrap"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    with ui.column().classes("w-full max-w-md mx-auto p-4 gap-4"):
        # --- 头像选择 ---
        ui.label("当前头像").classes("text-xl font-semibold")

        # ui.image 会自动处理本地文件路径的伺服
        current_avatar_display = ui.image(current_display_path).classes(
            "w-16 h-16 rounded-full self-center ring-4 ring-blue-500"
        )

        ui.separator()

        ui.label("选择新头像").classes("text-xl font-semibold")
        # --- 上传组件 ---
        uploader = (
            ui.upload(
                label="上传自定义头像",
                on_upload=handle_upload,
                auto_upload=True,  # 选择文件后立即上传
            )
            .props('max-file-size=5242880 accept="image/*"')
            .classes("w-full")
        )
        uploader.set_visibility(False)
        with ui.row().classes("items-center"):
            ui.button("上传自定义头像", on_click=lambda: pick_file(uploader))
            ui.label("或选择以下预设头像：").classes("text-gray-500")
        with ui.row().classes("gap-2 flex-wrap justify-center"):
            for avatar_path in PRESET_AVATARS:
                # 步骤 6: 在循环预设头像时应用缓存清除
                display_path = get_cache_busted_path(avatar_path)
                ui.image(display_path).classes(
                    "w-16 h-16 rounded-full cursor-pointer hover:ring-4 hover:ring-blue-300"
                ).on("click", lambda _, path=avatar_path: set_avatar(path))  # 关键：使用 lambda 捕获正确的 path

        ui.separator()

        # --- 密码修改 ---
        ui.label("账户安全").classes("text-xl font-semibold")

        # 步骤 2: 点击按钮，直接调用导入的函数
        ui.button("修改密码", on_click=lambda: create_password_dialog(current_user)).props("icon=lock")
