"""Local-only launcher with identity switching and external-effect controls."""

from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("ENABLE_DEV_IDENTITY_SWITCHER", "1")

if not os.environ.get("DEV_TOOLS_PASSWORD"):
    raise RuntimeError("请先设置 DEV_TOOLS_PASSWORD，再启动 run_local_debug.py")

from src import main as main_app  # noqa: E402,F401  # 注册正式页面与启动钩子
from src.config import IMG_DIR, ST  # noqa: E402
from src.modules.dev_identity_switcher import install_dev_identity_switcher  # noqa: E402
from src.nicegui_bootstrap import ui  # noqa: E402

install_dev_identity_switcher(listen_host="127.0.0.1")


if __name__ in {"__main__", "__mp_main__"}:
    reload_enabled = os.environ.get("NICEGUI_RELOAD", "true").strip().casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }
    ui.run(
        title="百炼光研发管理系统｜本地调试",
        favicon=f"{IMG_DIR}/RFRF.png",
        host="127.0.0.1",
        port=int(os.environ.get("DEV_SERVER_PORT", "8080")),
        storage_secret=ST,
        dark=False,
        reload=reload_enabled,
        reconnect_timeout=30.0 if reload_enabled else 3.0,
        uvicorn_logging_level="info",
    )
