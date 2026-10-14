"""Optional runtime gates for external effects and scheduled notification tasks.

Production does not install a policy, so existing behavior remains unchanged. A local-only
launcher may install a policy before NiceGUI startup to block or redirect external effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Protocol


@dataclass(frozen=True)
class WeComDeliveryDecision:
    allowed: bool
    recipients: tuple[str, ...]
    content_prefix: str = ""
    reason: str = ""
    redirected: bool = False
    replacement_link_base: str = ""


class RuntimeEffectPolicy(Protocol):
    def decide_wecom_delivery(
        self,
        *,
        recipients: tuple[str, ...],
        module: str,
        business_key: str,
        message_type: str,
    ) -> WeComDeliveryDecision: ...

    def allows_background_task(self, task_key: str) -> bool: ...


_policy_lock = RLock()
_policy: RuntimeEffectPolicy | None = None


def install_runtime_effect_policy(policy: RuntimeEffectPolicy) -> None:
    """Install the process-wide local runtime policy before application startup."""
    global _policy
    with _policy_lock:
        if _policy is not None and _policy is not policy:
            raise RuntimeError("运行时外部副作用策略已经安装，不能重复替换")
        _policy = policy


def clear_runtime_effect_policy() -> None:
    """Reset to production defaults; intended for tests only."""
    global _policy
    with _policy_lock:
        _policy = None


def has_runtime_effect_policy() -> bool:
    """Return whether this process is running with a local external-effect policy."""
    with _policy_lock:
        return _policy is not None


def decide_wecom_delivery(
    recipients: tuple[str, ...],
    *,
    module: str,
    business_key: str,
    message_type: str,
) -> WeComDeliveryDecision:
    with _policy_lock:
        policy = _policy
    if policy is None:
        return WeComDeliveryDecision(allowed=True, recipients=recipients)
    return policy.decide_wecom_delivery(
        recipients=recipients,
        module=module,
        business_key=business_key,
        message_type=message_type,
    )


def allows_background_task(task_key: str) -> bool:
    with _policy_lock:
        policy = _policy
    return True if policy is None else policy.allows_background_task(task_key)
