"""Conservative matching between system users and WeCom contacts."""

from __future__ import annotations

import unicodedata
from typing import Any


def normalize_identity_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return "".join(text.split())


def build_wecom_user_match_plan(
    users: dict[str, dict[str, Any]],
    contacts: list[dict[str, Any]],
    existing_bindings: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a deterministic plan and auto-match only unambiguous identities.

    Match priority:

    1. system username equals WeCom userid;
    2. system username/display name equals one unique WeCom name.

    Existing bindings and inactive users/contacts are excluded.  When several
    system users claim one contact, all competing suggestions become ambiguous.
    """
    bindings = existing_bindings or {}
    bound_external_ids = {
        str(binding.get("external_userid", "")).strip()
        for binding in bindings.values()
        if binding.get("external_userid")
    }
    available_contacts = [
        contact
        for contact in contacts
        if contact.get("userid")
        and contact.get("is_active", True)
        and str(contact.get("userid")) not in bound_external_ids
    ]
    by_userid: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for contact in available_contacts:
        by_userid.setdefault(normalize_identity_text(contact.get("userid")), []).append(contact)
        by_name.setdefault(normalize_identity_text(contact.get("name")), []).append(contact)

    plan: list[dict[str, Any]] = []
    for username, user in users.items():
        if username in bindings or user.get("status", "active") != "active":
            continue
        normalized_username = normalize_identity_text(username)
        normalized_display_name = normalize_identity_text(user.get("display_name") or username)
        userid_matches = by_userid.get(normalized_username, [])
        name_candidates: list[dict[str, Any]] = []
        seen_userids: set[str] = set()
        for name_key in {normalized_username, normalized_display_name}:
            if not name_key:
                continue
            for contact in by_name.get(name_key, []):
                external_userid = str(contact.get("userid", ""))
                if external_userid not in seen_userids:
                    seen_userids.add(external_userid)
                    name_candidates.append(contact)

        if len(userid_matches) == 1:
            contact = userid_matches[0]
            status = "matched"
            reason = "系统用户名与企业微信账号完全一致"
        elif len(userid_matches) > 1:
            contact = None
            status = "ambiguous"
            reason = "存在重复的企业微信账号候选"
        elif len(name_candidates) == 1:
            contact = name_candidates[0]
            status = "matched"
            reason = "系统姓名与企业微信姓名唯一一致"
        elif len(name_candidates) > 1:
            contact = None
            status = "ambiguous"
            reason = f"存在 {len(name_candidates)} 位同名企业微信成员"
        else:
            contact = None
            status = "unmatched"
            reason = "未找到账号或唯一同名成员"

        plan.append(
            {
                "username": username,
                "display_name": user.get("display_name") or username,
                "status": status,
                "reason": reason,
                "contact": contact,
            }
        )

    # A contact cannot be assigned to multiple system users.  Mark all such
    # collisions ambiguous instead of relying on iteration order.
    claims: dict[str, list[dict[str, Any]]] = {}
    for item in plan:
        contact = item.get("contact")
        if item["status"] == "matched" and isinstance(contact, dict):
            claims.setdefault(str(contact.get("userid", "")), []).append(item)
    for external_userid, items in claims.items():
        if external_userid and len(items) > 1:
            for item in items:
                item["status"] = "ambiguous"
                item["reason"] = "多个系统用户匹配到同一个企业微信账号"
                item["contact"] = None

    return sorted(plan, key=lambda item: normalize_identity_text(item["display_name"]))


def suggest_contact_for_user(
    username: str,
    user: dict[str, Any],
    contacts: list[dict[str, Any]],
    existing_bindings: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    plan = build_wecom_user_match_plan(
        {username: user},
        contacts,
        existing_bindings,
    )
    return plan[0] if plan else {
        "username": username,
        "display_name": user.get("display_name") or username,
        "status": "unmatched",
        "reason": "未找到候选",
        "contact": None,
    }
