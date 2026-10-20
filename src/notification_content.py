"""企业微信通知的紧凑文案与文本卡片摘要构建工具。"""

from __future__ import annotations

import re
from html import escape, unescape
from typing import Iterable


def normalize_notification_text(value: object) -> str:
    """把业务文本压缩成适合通知单行展示的内容。"""
    return " ".join(str(value or "").split())


def truncate_notification_text(value: object, max_chars: int = 42) -> str:
    """按可读字符数截断通知摘要，完整内容应从详情页查看。"""
    text = normalize_notification_text(value)
    if len(text) <= max_chars:
        return text
    return f"{text[: max(1, max_chars - 1)]}…"


def build_compact_text_notification(
    title: str,
    identifier: str,
    action: str,
    detail: str = "",
) -> str:
    """生成最多四行的文本通知。"""
    lines = [
        truncate_notification_text(title, 36),
        truncate_notification_text(identifier, 64),
        truncate_notification_text(action, 64),
        truncate_notification_text(detail, 64) if detail else "",
    ]
    return "\n".join(line for line in lines if line)


def _escaped_within_bytes(value: object, byte_budget: int, *, suffix: str = "…") -> str:
    """转义并按 UTF-8 字节裁剪，避免截断 HTML 实体。"""
    text = normalize_notification_text(value)
    escaped = escape(text)
    if len(escaped.encode("utf-8")) <= byte_budget:
        return escaped
    suffix_size = len(suffix.encode("utf-8"))
    result: list[str] = []
    used = 0
    for char in text:
        part = escape(char)
        size = len(part.encode("utf-8"))
        if used + size + suffix_size > byte_budget:
            break
        result.append(part)
        used += size
    return "".join(result) + (suffix if result else "")


def build_textcard_description(
    note: str,
    lines: Iterable[object],
    *,
    max_bytes: int = 512,
    overflow_hint: str = "点击查看完整详情",
) -> str:
    """生成合法且不超过企业微信字节限制的文本卡片摘要。"""
    note_overhead = len('<div class="gray"></div>'.encode("utf-8"))
    safe_note = _escaped_within_bytes(note, max(8, min(96, max_bytes - note_overhead)))
    prefix = f'<div class="gray">{safe_note}</div>'
    normal_prefix = '<div class="normal">'
    suffix = "</div>"
    overflow_block = f'{normal_prefix}{escape(overflow_hint)}{suffix}'
    blocks: list[str] = []
    normalized_lines = [normalize_notification_text(line) for line in lines]
    normalized_lines = [line for line in normalized_lines if line]

    for index, line in enumerate(normalized_lines):
        reserve = len(overflow_block.encode("utf-8")) if index < len(normalized_lines) - 1 else 0
        used = len((prefix + "".join(blocks)).encode("utf-8"))
        available = max_bytes - used - len((normal_prefix + suffix).encode("utf-8")) - reserve
        if available <= len("…".encode("utf-8")):
            if used + len(overflow_block.encode("utf-8")) <= max_bytes:
                blocks.append(overflow_block)
            break
        escaped_line = _escaped_within_bytes(line, available)
        block = f"{normal_prefix}{escaped_line}{suffix}"
        if len((prefix + "".join(blocks) + block).encode("utf-8")) > max_bytes:
            break
        blocks.append(block)
        if escaped_line != escape(line):
            break
    result = prefix + "".join(blocks)
    return result if len(result.encode("utf-8")) <= max_bytes else prefix[:max_bytes]


def compact_oversized_textcard_description(description: str, *, max_bytes: int = 512) -> str:
    """为发送层收口超长摘要，包括运行时额外添加的调试前缀。"""
    if len(description.encode("utf-8")) <= max_bytes:
        return description
    plain = unescape(re.sub(r"<[^>]*>", "\n", description))
    lines = [line for line in (normalize_notification_text(item) for item in plain.splitlines()) if line]
    return build_textcard_description("通知摘要", lines, max_bytes=max_bytes)

