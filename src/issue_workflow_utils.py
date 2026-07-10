# -*- encoding: utf-8 -*-
"""问题跟进类流程的公共工具函数。

这里仅放不依赖具体模块配置、数据结构和页面 UI 的小工具。生产异常、样品问题等模块可以共用，
但各自的业务配置仍应放在自己的配置加载器中。
"""

import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def schedule_background_task(coro, task_name: str) -> None:
    """让不应阻塞页面交互的协程后台执行，并把异常写入日志。"""
    task = asyncio.create_task(coro)

    def log_task_exception(done_task):
        try:
            done_task.result()
        except Exception:
            logger.exception("%s后台任务执行失败", task_name)

    task.add_done_callback(log_task_exception)


def split_people(value: str) -> list[str]:
    """把页面中常见的中文、英文分隔符统一解析为人员名称列表。"""
    if not value:
        return []
    normalized = value
    for sep in ["，", ",", "、", ";", "；", "\n"]:
        normalized = normalized.replace(sep, "|")
    return [item.strip() for item in normalized.split("|") if item.strip()]


def merge_wecom_recipients(*recipient_values: str) -> str:
    """合并多个企业微信收件人字符串，保持原顺序并去重。"""
    recipients = []
    seen = set()
    for value in recipient_values:
        for recipient in split_people(value):
            if recipient not in seen:
                recipients.append(recipient)
                seen.add(recipient)
    return "|".join(recipients)


def unique_nonempty_texts(values) -> list[str]:
    """清洗文本列表，去空并保留第一次出现的顺序。"""
    result = []
    seen = set()
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def parse_date(value: str):
    """兼容常见日期格式，无法识别时返回 None。"""
    if not value:
        return None
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"]:
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_time_of_day(value: str):
    """解析 HH:MM 或 HH:MM:SS 时间；无法识别时返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    for fmt in ["%H:%M", "%H:%M:%S"]:
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    return None


def normalize_time_window(value, default: dict) -> dict | None:
    """标准化提醒检查时间窗口配置；无效时返回 None 交由调用方回退。"""
    if not isinstance(value, dict):
        return None

    enabled = value.get("enabled", default.get("enabled", True))
    if not isinstance(enabled, bool):
        return None

    start_value = value.get("start", default.get("start", ""))
    end_value = value.get("end", default.get("end", ""))
    if not enabled:
        start_time = parse_time_of_day(start_value) or parse_time_of_day(default.get("start", "00:00"))
        end_time = parse_time_of_day(end_value) or parse_time_of_day(default.get("end", "23:59"))
        return {
            "enabled": False,
            "start": start_time.strftime("%H:%M") if start_time else "00:00",
            "end": end_time.strftime("%H:%M") if end_time else "23:59",
        }

    start_time = parse_time_of_day(start_value)
    end_time = parse_time_of_day(end_value)
    if start_time is None or end_time is None or start_time == end_time:
        return None

    return {
        "enabled": enabled,
        "start": start_time.strftime("%H:%M"),
        "end": end_time.strftime("%H:%M"),
    }


def is_time_in_window(window: dict, now: datetime | None = None) -> bool:
    """判断当前时间是否处于配置窗口内；窗口禁用或无效时视为不限制。"""
    normalized = normalize_time_window(window, {"enabled": False, "start": "00:00", "end": "23:59"})
    if normalized is None or not normalized["enabled"]:
        return True

    current_time = (now or datetime.now()).time()
    start_time = parse_time_of_day(normalized["start"])
    end_time = parse_time_of_day(normalized["end"])
    if start_time is None or end_time is None:
        return True

    if start_time < end_time:
        return start_time <= current_time <= end_time
    return current_time >= start_time or current_time <= end_time


def is_current_responsible(owner_text: str, current_user: str, current_role: str) -> bool:
    """负责人字段可填写姓名或角色，因此同时使用当前用户名和当前角色进行匹配。"""
    for owner in split_people(owner_text):
        if owner in [current_user, current_role] or owner in str(current_role) or owner in str(current_user):
            return True
    return False
