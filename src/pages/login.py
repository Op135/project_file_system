# -*- encoding: utf-8 -*-
import logging

from nicegui import app, ui

from ..utils import handle_key

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


# 密码比对函数
def _submit_new_password(dialog, new_pwd, confirm_pwd, target_username):
    if new_pwd.value != confirm_pwd.value:
        ui.notify(
            "两次输入密码不一致",
            type="warning",
            position="bottom",
            timeout=3000,
            progress=True,
            close_button="✖",
        )
        return
    try:
        success = app.state.user_service.update_password(target_username, new_pwd.value)
        if success:
            ui.notify(
                "密码设置成功，正在跳转...",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            dialog.close()  # 成功后关闭对话框
    except Exception as e:
        ui.notify(
            f"密码设置失败: {str(e)}",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


# 密码设置函数
def create_password_dialog(target_username: str):
    with (
        ui.dialog().props("persistent w-full") as dialog,
        ui.card().classes("w-1/4 p-4 bg-white shadow-md"),
    ):
        with ui.column().classes("w-full p-4"):
            ui.label("请设置密码").classes("text-lg")
            new_pwd = ui.input("新密码", password=True, password_toggle_button=True).props("autofocus")
            confirm_pwd = ui.input("确认密码", password=True, password_toggle_button=True).props("")

        with ui.row().classes("w-full p-4 flex-nowrap"):
            ui.button(
                "提交", on_click=lambda: _submit_new_password(dialog, new_pwd, confirm_pwd, target_username)
            ).classes("w-1/2")
            ui.button("取消", on_click=lambda: dialog.close()).classes("w-1/2")
    dialog.open()


@ui.page("/login")
def login_page(redirect_to: str = ""):
    # 用于记录键盘按键状态
    app.storage.client.setdefault("key_state", {})

    # 登录处理函数
    def try_login():
        # 处理非空密码情况
        input_username = str(username_input.value).strip()
        input_password = str(password_input.value).strip()

        try:
            # 获取对应用户的密码与角色组成的字典{'password': 'xxx', 'role': 'user'}
            user_info = app.state.user_service.get_user(input_username)
            if not user_info:
                ui.notify(
                    "用户不存在",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return

            # 正常密码验证流程
            if str(user_info.get("password", "")) == input_password:
                app.storage.user.update(
                    {
                        "current_user": input_username,
                        "is_admin": check_admin_role(input_username),
                        "current_role": user_info.get("role", "anonymous"),
                    }
                )
                target_path = redirect_to if redirect_to.startswith("/") and not redirect_to.startswith("//") else "/main"
                ui.navigate.to(target_path)
            else:
                ui.notify(
                    "密码错误",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
        except Exception as e:
            ui.notify(
                f"登录失败: {str(e)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 修改密码处理函数
    def change_password():
        # 处理非空密码情况
        input_username = str(username_input.value).strip()
        input_password = str(password_input.value).strip()

        try:
            # 获取对应用户的密码与角色组成的字典{'password': 'xxx', 'role': 'user'}
            user_info = app.state.user_service.get_user(input_username)
            if not user_info:
                ui.notify(
                    "用户不存在",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                return

            # 正常密码验证流程
            if str(user_info.get("password", "")) == input_password:
                create_password_dialog(input_username)
            else:
                ui.notify(
                    "密码错误",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
        except Exception as e:
            ui.notify(
                f"密码修改触发失败: {str(e)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 返回用户是否为管理员的布尔值
    def check_admin_role(username: str) -> bool:
        try:
            return app.state.users_data.get(username, {}).get("role") == "admin"
        except Exception as e:
            ui.notify(
                f"权限验证失败: {str(e)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            return False

    # 实时检测是否需要设置初始密码
    def check_initial_password():
        input_username = username_input.value.strip()
        if not input_username:  # or not enable_event:
            return
        try:
            # 获取对应用户的密码与角色组成的字典{'password': 'xxx', 'role': 'user'}
            user_info = app.state.user_service.get_user(input_username)
            # 条件1：用户存在且密码为空
            if user_info and user_info.get("password") is None:
                create_password_dialog(input_username)  # 直接弹出密码设置
        except Exception as e:
            ui.notify(
                f"用户查询失败: {str(e)}",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 回车登录
    def enter_try_login():
        if app.storage.client["key_state"].get("enter", 0) == 1:
            app.storage.client["key_state"]["enter"] = 0
            try_login()

    # 登录页面
    with ui.dialog().props("persistent") as dialog_login, ui.card().classes("w-1/3 p-4 bg-white shadow-md -space-y-6"):
        # 创建卡片组件
        ui.label("用户登录").classes("text-lg p-4")  # 显示文本内容
        with ui.column().classes("w-full p-4 space-y-2"):
            # 创建UI元素的引用
            username_input = (
                ui.input(label="用户名").classes("w-full").props('autofocus outlined :dense="dense" color="amber-8"')
            )
            password_input = (
                ui.input(label="密码", password=True, password_toggle_button=True)
                .classes("w-full")
                .props('outlined :dense="dense" color="amber-8"')
            )
        with ui.row().classes("w-full p-4 flex-nowrap"):
            ui.button("登录", on_click=lambda: try_login()).classes("w-1/2").props('outline color="amber-8"')
            ui.button("修改密码", on_click=lambda: change_password()).classes("w-1/2").props('outline color="amber-8"')
            # ui.button("关闭", on_click=lambda: dialog_login.close()).classes("w-1/3").props('outline color="amber-8"')
    dialog_login.open()
    # 添加全局键盘事件跟踪
    # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
    ui.keyboard(on_key=handle_key, ignore=["select", "button", "textarea"])
    # 监控用户是否按下回车键
    ui.timer(0.5, lambda: enter_try_login())

    # 添加实时检测
    username_input.on("blur", check_initial_password)  # 失去焦点时检测
