"""需求项结构及完整问题清单的访问权限判断。"""

from __future__ import annotations

from nicegui import app

from .access_control import can
from .permission_catalog import QUESTION_TREE_VIEW_PERMISSION


QUESTION_TREE_LEGACY_VIEW_KEYWORDS = ("销售", "研发", "boss", "admin")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def can_view_question_tree(current_role: object, current_user: str, *, user_service=None) -> bool:
    """判断用户能否查看需求项结构及其完整打印清单。"""
    role = str(current_role or "")
    legacy_allowed = any(keyword in role for keyword in QUESTION_TREE_LEGACY_VIEW_KEYWORDS)
    return can(
        _service(user_service),
        current_user,
        QUESTION_TREE_VIEW_PERMISSION,
        legacy_role=role,
        legacy_allowed_roles=(role,) if legacy_allowed else (),
    )
