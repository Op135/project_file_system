"""生产测试项汇总表查看权限判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .permission_catalog import PROJECT_TEST_SUMMARY_VIEW_PERMISSION


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_view_project_test_summary(
    current_role: object,
    current_user: str,
    *,
    user_service=None,
) -> bool:
    """数据库模式按稳定权限授权，旧 Excel 模式保持登录用户均可查看。"""
    return can(
        _service(user_service),
        current_user,
        PROJECT_TEST_SUMMARY_VIEW_PERMISSION,
        legacy_role=str(current_role or ""),
        legacy_allowed_roles=None,
    )
