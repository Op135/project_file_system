"""Fail-closed checks for the local debugging module."""

from __future__ import annotations

import os
import ipaddress


_TRUE_VALUES = {"1", "true", "yes", "on"}


def assert_local_dev_environment(listen_host: str) -> None:
    if os.environ.get("APP_ENV", "").strip().casefold() != "development":
        raise RuntimeError("本地调试模块只允许在 APP_ENV=development 时启用")
    enabled = os.environ.get("ENABLE_DEV_IDENTITY_SWITCHER", "").strip().casefold()
    if enabled not in _TRUE_VALUES:
        raise RuntimeError("本地调试模块未显式启用")
    if not os.environ.get("DEV_TOOLS_PASSWORD", ""):
        raise RuntimeError("必须通过 DEV_TOOLS_PASSWORD 设置本地调试解锁密码")
    try:
        address = ipaddress.ip_address(listen_host)
    except ValueError as exc:
        raise RuntimeError("本地调试服务必须使用明确的回环 IP 地址") from exc
    if not address.is_loopback:
        raise RuntimeError("本地调试服务只能监听 127.0.0.1 或 ::1")


def dev_tools_password() -> str:
    value = os.environ.get("DEV_TOOLS_PASSWORD", "")
    if not value:
        raise RuntimeError("本地调试解锁密码未配置")
    return value


def allowed_wecom_recipients() -> tuple[str, ...]:
    raw = os.environ.get("DEV_WECOM_RECIPIENT_ALLOWLIST", "")
    normalized = raw
    for separator in ("，", ",", "、", ";", "；", "\n"):
        normalized = normalized.replace(separator, "|")
    return tuple(dict.fromkeys(item.strip() for item in normalized.split("|") if item.strip()))
