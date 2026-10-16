"""Persistent local runtime controls; this file never writes business configuration."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Literal

from ...runtime_controls import WeComDeliveryDecision
from .guard import allowed_wecom_recipients

WeComMode = Literal["blocked", "redirect", "live"]


@dataclass
class DevRuntimeSettings:
    wecom_mode: WeComMode = "blocked"
    redirect_recipients: tuple[str, ...] = ()
    background_tasks_enabled: bool = False
    live_until: str = ""


class LocalDevRuntimePolicy:
    def __init__(
        self,
        state_path: Path,
        *,
        recipient_label_resolver: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None,
    ):
        self.state_path = state_path
        self._recipient_label_resolver = recipient_label_resolver
        self._lock = threading.RLock()
        self._settings = self._load()

    def _load(self) -> DevRuntimeSettings:
        if not self.state_path.exists():
            return DevRuntimeSettings()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return DevRuntimeSettings()
        allowed = set(allowed_wecom_recipients())
        recipients = tuple(
            value
            for value in (str(item).strip() for item in raw.get("redirect_recipients", []))
            if value and value in allowed
        )
        return DevRuntimeSettings(
            # 每次进程启动都恢复安全默认，不继承上次的发送或后台任务开关。
            wecom_mode="blocked",
            redirect_recipients=recipients,
            background_tasks_enabled=False,
            live_until="",
        )

    def _save_locked(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self._settings), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def snapshot(self) -> DevRuntimeSettings:
        with self._lock:
            return DevRuntimeSettings(**asdict(self._settings))

    def update(
        self,
        *,
        wecom_mode: WeComMode,
        redirect_recipients: tuple[str, ...],
        background_tasks_enabled: bool,
    ) -> DevRuntimeSettings:
        if wecom_mode not in {"blocked", "redirect", "live"}:
            raise ValueError("未知的企业微信调试模式")
        allowlist = set(allowed_wecom_recipients())
        invalid = [value for value in redirect_recipients if value not in allowlist]
        if invalid:
            raise ValueError(f"调试收件人不在环境变量白名单中：{'、'.join(invalid)}")
        if wecom_mode == "redirect" and not redirect_recipients:
            raise ValueError("测试转发模式至少需要一个白名单收件人")
        live_until = ""
        if wecom_mode == "live":
            live_until = (datetime.now() + timedelta(minutes=15)).isoformat(timespec="seconds")
        with self._lock:
            self._settings = DevRuntimeSettings(
                wecom_mode=wecom_mode,
                redirect_recipients=redirect_recipients,
                background_tasks_enabled=background_tasks_enabled,
                live_until=live_until,
            )
            self._save_locked()
            return self.snapshot()

    def _effective_mode_locked(self) -> WeComMode:
        if self._settings.wecom_mode != "live":
            return self._settings.wecom_mode
        try:
            still_live = datetime.now() < datetime.fromisoformat(self._settings.live_until)
        except ValueError:
            still_live = False
        if still_live:
            return "live"
        self._settings.wecom_mode = "blocked"
        self._settings.live_until = ""
        self._save_locked()
        return "blocked"

    def decide_wecom_delivery(
        self,
        *,
        recipients: tuple[str, ...],
        module: str,
        business_key: str,
        message_type: str,
    ) -> WeComDeliveryDecision:
        del module, business_key, message_type
        with self._lock:
            mode = self._effective_mode_locked()
            redirect_recipients = self._settings.redirect_recipients
        if mode == "blocked":
            return WeComDeliveryDecision(
                allowed=False,
                recipients=(),
                reason="本地调试策略已拦截企业微信发送",
            )
        if mode == "redirect":
            if not redirect_recipients:
                return WeComDeliveryDecision(
                    allowed=False,
                    recipients=(),
                    reason="本地调试转发模式没有可用的白名单收件人",
                )
            original_labels = recipients
            if self._recipient_label_resolver is not None:
                try:
                    original_labels = self._recipient_label_resolver(recipients)
                except Exception:
                    # 姓名仅用于调试提示；反查失败不能阻断原有消息转发。
                    original_labels = recipients
            original = "、".join(original_labels) or "-"
            return WeComDeliveryDecision(
                allowed=True,
                recipients=redirect_recipients,
                content_prefix=f"【本地调试转发｜原收件人：{original}】\n",
                reason="本地调试收件人转发",
                redirected=True,
                replacement_link_base=os.environ.get(
                    "DEV_CARD_BASE_URL",
                    "http://127.0.0.1:8080",
                ).rstrip("/"),
            )
        return WeComDeliveryDecision(allowed=True, recipients=recipients)

    def allows_background_task(self, task_key: str) -> bool:
        del task_key
        with self._lock:
            return self._settings.background_tasks_enabled
