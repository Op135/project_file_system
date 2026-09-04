"""记录旧 Excel、旧角色和旧流程兼容分支的实际命中情况。"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass


logger = logging.getLogger(__name__)


def _configured_interval_seconds() -> float:
    """读取限频间隔；异常配置回退为三十分钟。"""
    try:
        return max(60.0, float(os.environ.get("LEGACY_COMPAT_LOG_INTERVAL_SECONDS", "1800")))
    except (TypeError, ValueError):
        return 1800.0


@dataclass
class _HitState:
    total: int = 0
    suppressed: int = 0
    last_logged_at: float = 0.0


_lock = threading.Lock()
_hit_states: dict[tuple[str, str, str], _HitState] = {}


def record_legacy_compatibility_hit(
    category: str,
    feature: str,
    *,
    username: str = "",
    detail: str = "",
    interval_seconds: float | None = None,
) -> bool:
    """限频记录一次真实兼容命中，返回本次是否实际写入日志。

    限频键只包含类别、功能和用户，避免业务单号等动态详情制造大量独立日志。第一次
    命中立即记录，之后默认每三十分钟汇总一次此前被抑制的重复命中。
    """
    normalized_category = str(category or "unknown").strip() or "unknown"
    normalized_feature = str(feature or "unknown").strip() or "unknown"
    normalized_username = str(username or "-").strip() or "-"
    normalized_detail = str(detail or "-").replace("\r", " ").replace("\n", " ").strip() or "-"
    key = (normalized_category, normalized_feature, normalized_username.casefold())
    now = time.monotonic()
    interval = _configured_interval_seconds() if interval_seconds is None else max(0.0, float(interval_seconds))

    with _lock:
        state = _hit_states.setdefault(key, _HitState())
        state.total += 1
        should_log = state.last_logged_at == 0.0 or now - state.last_logged_at >= interval
        if not should_log:
            state.suppressed += 1
            return False
        suppressed = state.suppressed
        state.suppressed = 0
        state.last_logged_at = now
        total = state.total

    logger.warning(
        "LEGACY_COMPAT_HIT category=%s feature=%s user=%s total=%s suppressed=%s detail=%s",
        normalized_category,
        normalized_feature,
        normalized_username,
        total,
        suppressed,
        normalized_detail,
    )
    return True


def reset_legacy_compatibility_telemetry() -> None:
    """清空进程内限频状态，供自动化测试和受控诊断使用。"""
    with _lock:
        _hit_states.clear()
