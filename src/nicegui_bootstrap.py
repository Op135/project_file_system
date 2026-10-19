"""在加载 NiceGUI 全局存储时安装并发与原子写保护。"""

from pathlib import Path

from .storage_safety import install_safe_nicegui_general_storage, nicegui_storage_startup_guard

PROJECT_ROOT = Path(__file__).resolve().parent.parent

with nicegui_storage_startup_guard(PROJECT_ROOT):
    # 必须在启动锁内导入 NiceGUI；这是本模块唯一允许的条件导入。
    from nicegui import app, ui  # noqa: E402

    install_safe_nicegui_general_storage(app.storage.general)

__all__ = ["app", "ui"]
