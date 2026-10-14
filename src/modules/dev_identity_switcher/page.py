"""NiceGUI control page for local identity and external-effect debugging."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any, cast

from nicegui import app, ui

from ...runtime_controls import install_runtime_effect_policy
from ...wecom_service import send_wecom_text_message
from .guard import allowed_wecom_recipients, assert_local_dev_environment, dev_tools_password
from .state import DevRuntimeSettings, LocalDevRuntimePolicy, WeComMode

logger = logging.getLogger(__name__)
_installed = False
_policy: LocalDevRuntimePolicy | None = None
_IDENTITY_KEYS = ("current_user", "current_user_id", "current_role", "is_admin")


def _active_policy() -> LocalDevRuntimePolicy:
    if _policy is None:
        raise RuntimeError("本地调试策略尚未安装")
    return _policy


def _is_unlocked() -> bool:
    return app.storage.user.get("dev_tools_unlocked") is True


def _current_identity() -> dict[str, Any]:
    return {key: app.storage.user.get(key) for key in _IDENTITY_KEYS}


def _activate_identity(username: str) -> None:
    service = app.state.user_service
    user = service.get_user(username)
    if not isinstance(user, dict) or user.get("status") != "active":
        raise ValueError("目标账号不存在或不在职")
    if not app.storage.user.get("dev_original_identity"):
        app.storage.user["dev_original_identity"] = _current_identity()
    role = str(user.get("role") or "anonymous")
    app.storage.user.update(
        {
            "current_user": username,
            "current_user_id": user.get("user_id"),
            "current_role": role,
            "is_admin": role.casefold() == "admin",
            "dev_impersonating": True,
        }
    )


def _restore_identity() -> None:
    original = app.storage.user.get("dev_original_identity")
    for key in _IDENTITY_KEYS:
        app.storage.user.pop(key, None)
    if isinstance(original, dict):
        for key in _IDENTITY_KEYS:
            value = original.get(key)
            if value is not None:
                app.storage.user[key] = value
    app.storage.user.pop("dev_original_identity", None)
    app.storage.user.pop("dev_impersonating", None)


def _settings_summary(settings: DevRuntimeSettings) -> str:
    labels = {"blocked": "强制关闭", "redirect": "测试转发", "live": "真实发送（15分钟）"}
    return (
        f"企业微信：{labels[settings.wecom_mode]}；"
        f"后台通知任务：{'启用' if settings.background_tasks_enabled else '关闭'}"
    )


def install_dev_identity_switcher(*, listen_host: str, state_path: Path | None = None) -> None:
    global _installed, _policy
    if _installed:
        return
    assert_local_dev_environment(listen_host)
    resolved_state_path = state_path or Path(__file__).parents[3] / ".local" / "dev_runtime_controls.json"
    _policy = LocalDevRuntimePolicy(resolved_state_path)
    install_runtime_effect_policy(_policy)
    _installed = True

    @ui.page("/dev/identity-switcher")
    async def dev_identity_switcher_page() -> None:
        ui.page_title("本地调试控制中心")
        ui.add_css(
            "body { background: #f8fafc; }"
            ".dev-warning { border: 2px solid #f59e0b; background: #fffbeb; }"
        )

        @ui.refreshable
        def render() -> None:
            with ui.column().classes("w-full max-w-5xl mx-auto p-6 gap-4"):
                with ui.card().classes("w-full dev-warning p-4"):
                    ui.label("本地开发专用 · 当前使用服务器数据副本").classes(
                        "text-xl font-bold text-amber-800"
                    )
                    ui.label("该页面不会修改用户密码；外部发送默认被拦截。").classes("text-amber-700")

                if not _is_unlocked():
                    with ui.card().classes("w-full p-5"):
                        ui.label("解锁调试工具").classes("text-lg font-bold")
                        password = ui.input("DEV_TOOLS_PASSWORD", password=True, password_toggle_button=True).classes(
                            "w-full max-w-md"
                        )

                        def unlock() -> None:
                            if not hmac.compare_digest(str(password.value or ""), dev_tools_password()):
                                ui.notify("调试解锁密码不正确", type="negative")
                                return
                            app.storage.user["dev_tools_unlocked"] = True
                            render.refresh()

                        ui.button("解锁", icon="lock_open", on_click=unlock).props("color=amber-8")
                    return

                current_user = str(app.storage.user.get("current_user") or "未登录")
                current_role = str(app.storage.user.get("current_role") or "-")
                with ui.card().classes("w-full p-5"):
                    ui.label("身份模拟").classes("text-lg font-bold")
                    ui.label(f"当前身份：{current_user}（{current_role}）").classes("text-red-700 font-bold")
                    users = app.state.user_service.load_users()
                    options = {
                        username: f"{username}｜{user.get('display_name') or username}｜{user.get('role') or '-'}"
                        for username, user in users.items()
                        if isinstance(user, dict) and user.get("status") == "active"
                    }
                    selected_user = ui.select(options, label="切换到在职用户", with_input=True).classes(
                        "w-full max-w-2xl"
                    )

                    def switch_identity() -> None:
                        username = str(selected_user.value or "").strip()
                        if not username:
                            ui.notify("请选择目标用户", type="warning")
                            return
                        try:
                            _activate_identity(username)
                        except ValueError as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        ui.notify(f"已切换为 {username}，即将进入主页", type="positive")
                        ui.navigate.to("/main")

                    with ui.row().classes("items-center"):
                        ui.button("切换身份", icon="manage_accounts", on_click=switch_identity)

                        def restore_identity() -> None:
                            _restore_identity()
                            ui.notify("已恢复进入调试工具前的身份", type="positive")
                            render.refresh()

                        ui.button("恢复原身份", icon="person", on_click=restore_identity).props("outline")
                        ui.button("打开系统主页", icon="home", on_click=lambda: ui.navigate.to("/main")).props(
                            "flat"
                        )

                settings = _active_policy().snapshot()
                allowlist = allowed_wecom_recipients()
                with ui.card().classes("w-full p-0 overflow-hidden"):
                    with ui.row().classes(
                        "w-full items-center justify-between gap-3 px-5 py-4 bg-slate-50 border-b border-slate-200"
                    ):
                        with ui.row().classes("items-center gap-3"):
                            ui.icon("shield", size="28px").classes("text-amber-600")
                            with ui.column().classes("gap-0"):
                                ui.label("外部副作用控制").classes("text-lg font-bold text-slate-800")
                                ui.label("本地运行时总闸门，不改动四份业务 JSON 配置").classes(
                                    "text-sm text-slate-500"
                                )
                        ui.chip(_settings_summary(settings), icon="info").props("outline color=primary")

                    with ui.column().classes("w-full p-5 gap-5"):
                        with ui.element("div").classes(
                            "w-full grid grid-cols-1 lg:grid-cols-2 gap-4 items-start"
                        ):
                            with ui.card().classes("w-full p-4 border border-slate-200 shadow-none gap-3"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label("1").classes(
                                        "w-6 h-6 rounded-full bg-blue-100 text-blue-700 text-center font-bold"
                                    )
                                    ui.label("企业微信发送策略").classes("font-bold text-slate-800")
                                ui.label("每次启动都会恢复为强制关闭。").classes("text-sm text-slate-500")
                                mode = ui.radio(
                                    {
                                        "blocked": "强制关闭｜只记录，不发送",
                                        "redirect": "测试转发｜全部改发白名单",
                                        "live": "真实发送｜15 分钟后自动关闭",
                                    },
                                    value=settings.wecom_mode,
                                ).props("color=primary").classes("w-full")

                                with ui.column().classes(
                                    "w-full gap-1 rounded-lg bg-blue-50 p-3"
                                ).bind_visibility_from(mode, "value", lambda value: value == "redirect"):
                                    ui.label("测试转发收件人").classes("text-sm font-bold text-blue-900")
                                    redirect = ui.select(
                                        list(allowlist),
                                        value=list(settings.redirect_recipients),
                                        label="企业微信 userid 白名单",
                                        multiple=True,
                                    ).props("outlined dense").classes("w-full")
                                    ui.label("候选人来自 DEV_WECOM_RECIPIENT_ALLOWLIST。所有业务收件人都会被替换。") \
                                        .classes("text-xs text-blue-700")

                                with ui.column().classes(
                                    "w-full gap-1 rounded-lg bg-red-50 p-3"
                                ).bind_visibility_from(mode, "value", lambda value: value == "live"):
                                    ui.label("高风险确认").classes("text-sm font-bold text-red-800")
                                    confirmation = ui.input(
                                        "输入确认短语：允许真实发送"
                                    ).props("outlined dense").classes("w-full")
                                    ui.label("保存后将按业务配置向真实人员发送，15 分钟后自动关闭。") \
                                        .classes("text-xs text-red-700")

                            with ui.card().classes("w-full p-4 border border-slate-200 shadow-none gap-3"):
                                with ui.row().classes("items-center gap-2"):
                                    ui.label("2").classes(
                                        "w-6 h-6 rounded-full bg-violet-100 text-violet-700 text-center font-bold"
                                    )
                                    ui.label("后台任务").classes("font-bold text-slate-800")
                                ui.label(
                                    "统一控制定时提醒、发送失败重试和企业微信通讯录刷新。"
                                ).classes("text-sm text-slate-500")
                                with ui.row().classes(
                                    "w-full items-center justify-between gap-3 rounded-lg border border-slate-200 p-3"
                                ):
                                    with ui.column().classes("gap-0"):
                                        ui.label("允许后台任务运行").classes("font-medium text-slate-800")
                                        ui.label("默认关闭；不受业务 JSON 中启用值影响。") \
                                            .classes("text-xs text-slate-500")
                                    background = ui.switch(value=settings.background_tasks_enabled).props(
                                        "color=primary"
                                    )
                                with ui.row().classes("w-full items-start gap-2 rounded-lg bg-amber-50 p-3"):
                                    ui.icon("lightbulb", size="20px").classes("text-amber-600")
                                    ui.label(
                                        "只测试页面交互时保持关闭；需要验证定时任务时再临时开启。"
                                    ).classes("text-sm text-amber-800 flex-1")

                    def save_controls() -> None:
                        selected_mode = cast(WeComMode, str(mode.value or "blocked"))
                        if selected_mode == "live" and str(confirmation.value or "").strip() != "允许真实发送":
                            ui.notify("真实发送必须输入确认短语", type="negative")
                            return
                        selected_recipients = tuple(
                            str(value).strip() for value in (redirect.value or []) if str(value).strip()
                        )
                        try:
                            updated = _active_policy().update(
                                wecom_mode=selected_mode,
                                redirect_recipients=selected_recipients,
                                background_tasks_enabled=bool(background.value),
                            )
                        except ValueError as exc:
                            ui.notify(str(exc), type="negative")
                            return
                        ui.notify(_settings_summary(updated), type="positive")
                        render.refresh()

                    with ui.row().classes(
                        "w-full items-center justify-between gap-3 px-5 pb-5"
                    ):
                        ui.label("选择只在当前本地进程生效，重启后恢复安全默认。").classes(
                            "text-sm text-slate-500"
                        )
                        ui.button("保存控制策略", icon="security", on_click=save_controls).props(
                            "color=amber-8 unelevated"
                        )

                with ui.card().classes("w-full p-5 gap-3"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("send", size="24px").classes("text-blue-600")
                        with ui.column().classes("gap-0"):
                            ui.label("消息通道自检").classes("text-lg font-bold text-slate-800")
                            ui.label("按上方已保存的策略发送一条手工测试消息。").classes(
                                "text-sm text-slate-500"
                            )
                    test_content = ui.textarea(
                        "测试消息",
                        value="本地调试控制中心发送测试消息",
                    ).props("outlined rows=3").classes("w-full")

                    async def send_test() -> None:
                        target = "|".join(allowlist)
                        if not target:
                            ui.notify("请先配置 DEV_WECOM_RECIPIENT_ALLOWLIST", type="warning")
                            return
                        success, message = await send_wecom_text_message(
                            str(test_content.value or ""),
                            target,
                            module="dev_identity_switcher",
                            business_key="manual_test",
                            message_type="dev_test",
                            retry_tracking=False,
                        )
                        ui.notify(message, type="positive" if success else "negative", timeout=8000)

                    with ui.row().classes("w-full justify-end"):
                        ui.button("发送测试消息", icon="send", on_click=send_test).props("outline color=primary")

                with ui.row().classes("w-full justify-end"):
                    def lock_tools() -> None:
                        app.storage.user.pop("dev_tools_unlocked", None)
                        render.refresh()

                    ui.button("锁定控制中心", icon="lock", on_click=lock_tools).props("flat color=grey")

        render()

    logger.warning("本地调试身份切换器已启用：/dev/identity-switcher")
