"""身份、组织和权限配置使用的稳定编码规则。"""

from __future__ import annotations

import re

STABLE_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{2,63}")
STABLE_CODE_HINT = "3–64 位，以英文字母开头，可使用字母、数字、点、下划线和横线"


def normalize_stable_code(value: object) -> str:
    """去除首尾空白并统一转换成小写编码。"""
    return str(value or "").strip().lower()


def validate_stable_code(value: object) -> str:
    """返回空字符串表示有效，否则返回适合界面展示的中文错误。"""
    normalized = normalize_stable_code(value)
    if not normalized:
        return "编码不能为空"
    if not STABLE_CODE_PATTERN.fullmatch(normalized):
        return f"编码格式不正确：{STABLE_CODE_HINT}"
    return ""
