"""生产测试项汇总表查看权限判断。"""

from __future__ import annotations

from urllib.parse import urlencode

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


def build_project_test_summary_url(project_name: object) -> str:
    """生成测试项汇总地址；项目名为空时拒绝生成不完整路由。"""
    normalized_project_name = str(project_name or "").strip()
    if not normalized_project_name:
        return ""
    # 项目名允许包含斜杠，必须放在查询参数中，避免被路由器误判为新的路径层级。
    return f"/report/test_summary?{urlencode({'project_name': normalized_project_name})}"
