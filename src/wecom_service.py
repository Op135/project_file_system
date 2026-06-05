# -*- encoding: utf-8 -*-
import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from .config import (
    WECOM_AGENT_ID,
    WECOM_API_BASE,
    WECOM_CONTACT_CACHE_TTL_SECONDS,
    WECOM_CONTACT_ROOT_DEPARTMENT_ID,
    WECOM_CONTACTS_SECRET,
    WECOM_CORP_ID,
    WECOM_CORP_SECRET,
    WECOM_DEFAULT_TOUSER,
    WECOM_LOG_RETENTION_DAYS,
    WECOM_MAX_RETRY_COUNT,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
WECOM_LOG_DIR = BASE_DIR / "data" / "wecom_logs"
WECOM_RETRY_STATE_PATH = BASE_DIR / "data" / "wecom_retry_state.json"
WECOM_CONTACTS_CACHE_PATH = BASE_DIR / "data" / "wecom_contacts.json"
_wecom_file_lock = asyncio.Lock()


def split_wecom_users(touser: str) -> list[str]:
    if not touser:
        return [WECOM_DEFAULT_TOUSER]
    normalized = str(touser)
    for sep in ["，", ",", "、", ";", "；", "\n"]:
        normalized = normalized.replace(sep, "|")
    users = [item.strip() for item in normalized.split("|") if item.strip()]
    return users or [WECOM_DEFAULT_TOUSER]


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _log_path(now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return WECOM_LOG_DIR / f"wecom_{now.strftime('%Y%m%d')}.jsonl"


def _retry_key(module: str, business_key: str, recipient: str, content: str) -> str:
    raw_key = f"{module}|{business_key}|{recipient}|{_content_hash(content)}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _is_wecom_config_missing(secret: str) -> bool:
    return WECOM_CORP_ID == "your_corp_id" or not secret or secret == "your_corp_secret"


async def _get_wecom_access_token(secret: str, purpose: str = "企业微信") -> tuple[bool, str]:
    if _is_wecom_config_missing(secret):
        return False, f"{purpose} CorpID 或 Secret 未配置，请先设置环境变量"

    try:
        async with httpx.AsyncClient(base_url=WECOM_API_BASE, timeout=10.0, trust_env=False) as client:
            token_response = await client.get(
                "/cgi-bin/gettoken",
                params={"corpid": WECOM_CORP_ID, "corpsecret": secret},
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            if token_data.get("errcode") != 0:
                return False, f"{purpose} access_token 获取失败：{token_data.get('errmsg', token_data)}"
            return True, token_data.get("access_token", "")
    except httpx.HTTPError as exc:
        logger.exception("%s access_token 请求失败", purpose)
        return False, f"{purpose} access_token 请求失败：{exc}"
    except Exception as exc:
        logger.exception("%s access_token 获取异常", purpose)
        return False, f"{purpose} access_token 获取异常：{exc}"


def _split_text_values(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            values.extend(_split_text_values(item))
        return values
    normalized = str(value)
    for sep in ["，", ",", "、", ";", "；", "\n", "|"]:
        normalized = normalized.replace(sep, "|")
    return [item.strip() for item in normalized.split("|") if item.strip()]


def load_wecom_contacts_cache() -> dict:
    if not WECOM_CONTACTS_CACHE_PATH.exists():
        return {}
    try:
        with open(WECOM_CONTACTS_CACHE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("读取企业微信通讯录缓存失败", exc_info=True)
        return {}


async def _write_wecom_contacts_cache(cache_data: dict) -> None:
    async with _wecom_file_lock:
        WECOM_CONTACTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = WECOM_CONTACTS_CACHE_PATH.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(cache_data, file, ensure_ascii=False, indent=2)
        os.replace(temp_path, WECOM_CONTACTS_CACHE_PATH)


def _wecom_contacts_cache_age_seconds(cache_data: dict) -> int | None:
    updated_at = cache_data.get("updated_at", "")
    if not updated_at:
        return None
    try:
        updated_time = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return int((datetime.now() - updated_time).total_seconds())


def _normalize_wecom_department(raw_department: dict) -> dict:
    return {
        "id": str(raw_department.get("id", "")),
        "name": raw_department.get("name", ""),
        "parentid": str(raw_department.get("parentid", "")),
        "order": raw_department.get("order", 0),
    }


def _normalize_wecom_contact(raw_user: dict, department_map: dict[str, str]) -> dict:
    department_ids = [str(department_id) for department_id in raw_user.get("department", [])]
    department_names = [department_map.get(department_id, department_id) for department_id in department_ids]
    enable = raw_user.get("enable", 1)
    status = raw_user.get("status", "")
    is_active = str(enable) != "0" and str(status or "1") == "1"
    return {
        "userid": raw_user.get("userid", ""),
        "name": raw_user.get("name", ""),
        "department_ids": department_ids,
        "departments": department_names,
        "main_department_id": str(raw_user.get("main_department", "")),
        "position": raw_user.get("position", ""),
        "status": status,
        "enable": enable,
        "is_active": is_active,
    }


async def sync_wecom_contacts() -> tuple[bool, str]:
    token_success, token_or_message = await _get_wecom_access_token(WECOM_CONTACTS_SECRET, "企业微信通讯录")
    if not token_success:
        return False, token_or_message

    try:
        async with httpx.AsyncClient(base_url=WECOM_API_BASE, timeout=20.0, trust_env=False) as client:
            department_response = await client.get(
                "/cgi-bin/department/list",
                params={"access_token": token_or_message},
            )
            department_response.raise_for_status()
            department_data = department_response.json()
            if department_data.get("errcode") != 0:
                return False, f"企业微信部门列表获取失败：{department_data.get('errmsg', department_data)}"

            agent_response = await client.get(
                "/cgi-bin/agent/get",
                params={"access_token": token_or_message, "agentid": WECOM_AGENT_ID},
            )
            agent_response.raise_for_status()
            agent_data = agent_response.json()

            if agent_data.get("errcode") == 0:
                visible_department_ids = (agent_data.get("allow_partys") or {}).get("partyid", []) or []
                visible_userids = [
                    item.get("userid", "")
                    for item in (agent_data.get("allow_userinfos") or {}).get("user", []) or []
                    if item.get("userid")
                ]
                sync_scope = "agent_visible_scope"
            else:
                visible_department_ids = [WECOM_CONTACT_ROOT_DEPARTMENT_ID]
                visible_userids = []
                sync_scope = "configured_root_department"

            raw_user_map = {}
            user_list_errors = []
            for department_id in visible_department_ids:
                user_response = await client.get(
                    "/cgi-bin/user/list",
                    params={
                        "access_token": token_or_message,
                        "department_id": department_id,
                        "fetch_child": 1,
                    },
                )
                user_response.raise_for_status()
                user_data = user_response.json()
                if user_data.get("errcode") != 0:
                    user_list_errors.append(f"部门 {department_id}: {user_data.get('errmsg', user_data)}")
                    continue
                for raw_user in user_data.get("userlist", []):
                    if raw_user.get("userid"):
                        raw_user_map[raw_user["userid"]] = raw_user

            for userid in visible_userids:
                if userid in raw_user_map:
                    continue
                user_response = await client.get(
                    "/cgi-bin/user/get",
                    params={"access_token": token_or_message, "userid": userid},
                )
                user_response.raise_for_status()
                raw_user = user_response.json()
                if raw_user.get("errcode") == 0 and raw_user.get("userid"):
                    raw_user_map[userid] = raw_user
                else:
                    user_list_errors.append(f"成员 {userid}: {raw_user.get('errmsg', raw_user)}")
    except httpx.HTTPError as exc:
        logger.exception("企业微信通讯录同步请求失败")
        return False, f"企业微信通讯录同步请求失败：{exc}"
    except Exception as exc:
        logger.exception("企业微信通讯录同步异常")
        return False, f"企业微信通讯录同步异常：{exc}"

    departments = [_normalize_wecom_department(item) for item in department_data.get("department", [])]
    department_map = {item["id"]: item["name"] for item in departments if item.get("id")}
    contacts = [
        _normalize_wecom_contact(item, department_map)
        for item in raw_user_map.values()
        if item.get("userid")
    ]
    if not contacts and user_list_errors:
        return False, f"企业微信成员列表获取失败：{'；'.join(user_list_errors)}"

    contacts = sorted(contacts, key=lambda item: ((item.get("departments") or [""])[0], item.get("name", "")))
    cache_data = {
        "updated_at": _now_str(),
        "root_department_id": WECOM_CONTACT_ROOT_DEPARTMENT_ID,
        "sync_scope": sync_scope,
        "visible_department_ids": [str(item) for item in visible_department_ids],
        "visible_userids": visible_userids,
        "department_count": len(departments),
        "contact_count": len(contacts),
        "departments": departments,
        "contacts": contacts,
    }
    await _write_wecom_contacts_cache(cache_data)
    error_suffix = f"，部分读取失败 {len(user_list_errors)} 项" if user_list_errors else ""
    return True, f"企业微信通讯录已同步：{len(contacts)} 人，{len(departments)} 个可见部门{error_suffix}"


async def refresh_wecom_contacts_if_stale(force: bool = False) -> tuple[bool, str]:
    cache_data = load_wecom_contacts_cache()
    cache_age = _wecom_contacts_cache_age_seconds(cache_data)
    has_cache = bool(cache_data.get("contacts"))
    if not force and has_cache and cache_age is not None and cache_age <= WECOM_CONTACT_CACHE_TTL_SECONDS:
        return True, f"企业微信通讯录缓存有效，已缓存 {cache_age} 秒"

    success, message = await sync_wecom_contacts()
    if success:
        return success, message
    if has_cache:
        logger.warning("%s，已沿用本地企业微信通讯录缓存", message)
        return True, f"{message}，已沿用本地缓存"
    return False, message


def _contact_matches_exact(contact_values, expected_values) -> bool:
    expected = set(_split_text_values(expected_values))
    if not expected:
        return True
    values = set(_split_text_values(contact_values))
    return bool(values & expected)


def _contact_matches_contains(contact_values, expected_values) -> bool:
    expected = _split_text_values(expected_values)
    if not expected:
        return True
    values = _split_text_values(contact_values)
    return any(keyword in value for keyword in expected for value in values)


def _target_matches_contact(target: dict, contact: dict) -> bool:
    if not target.get("include_inactive", False) and not contact.get("is_active", True):
        return False
    if "name" in target and not _contact_matches_exact(contact.get("name", ""), target.get("name")):
        return False
    if "names" in target and not _contact_matches_exact(contact.get("name", ""), target.get("names")):
        return False
    if "department" in target and not _contact_matches_exact(
        [*contact.get("departments", []), *contact.get("department_ids", [])],
        target.get("department"),
    ):
        return False
    if "department_id" in target and not _contact_matches_exact(contact.get("department_ids", []), target.get("department_id")):
        return False
    if "department_ids" in target and not _contact_matches_exact(contact.get("department_ids", []), target.get("department_ids")):
        return False
    if "department_contains" in target and not _contact_matches_contains(
        contact.get("departments", []),
        target.get("department_contains"),
    ):
        return False
    if "position" in target and not _contact_matches_exact(contact.get("position", ""), target.get("position")):
        return False
    if "position_contains" in target and not _contact_matches_contains(
        contact.get("position", ""),
        target.get("position_contains"),
    ):
        return False
    return True


def _normalize_recipient_targets(targets) -> list:
    if not targets:
        return []
    return targets if isinstance(targets, list) else [targets]


async def resolve_wecom_recipients(
    targets,
    fallback_touser: str = WECOM_DEFAULT_TOUSER,
    *,
    refresh_if_stale: bool = True,
) -> str:
    target_list = _normalize_recipient_targets(targets)
    direct_userids = []
    rule_targets = []
    for target in target_list:
        if isinstance(target, str):
            direct_userids.extend(_split_text_values(target))
        elif isinstance(target, dict):
            if "userid" in target or "userids" in target:
                direct_userids.extend(_split_text_values(target.get("userid")))
                direct_userids.extend(_split_text_values(target.get("userids")))
            else:
                rule_targets.append(target)

    if refresh_if_stale and rule_targets:
        await refresh_wecom_contacts_if_stale()

    cache_data = load_wecom_contacts_cache()
    recipients = []
    seen = set()

    def add_recipient(userid: str) -> None:
        if userid and userid not in seen:
            seen.add(userid)
            recipients.append(userid)

    for userid in direct_userids:
        add_recipient(userid)

    for target in rule_targets:
        for contact in cache_data.get("contacts", []):
            if _target_matches_contact(target, contact):
                add_recipient(contact.get("userid", ""))

    if recipients:
        return "|".join(recipients)

    logger.warning("企业微信收件人规则未匹配到成员，已回落到默认接收人：%s", fallback_touser)
    return fallback_touser


async def _append_log(record: dict) -> None:
    async with _wecom_file_lock:
        WECOM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = _log_path()
        with open(log_file, "a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        await _cleanup_old_logs_locked()


async def _cleanup_old_logs_locked() -> None:
    if WECOM_LOG_RETENTION_DAYS <= 0 or not WECOM_LOG_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=WECOM_LOG_RETENTION_DAYS)
    for path in WECOM_LOG_DIR.glob("wecom_*.jsonl"):
        try:
            date_part = path.stem.replace("wecom_", "")
            file_date = datetime.strptime(date_part, "%Y%m%d")
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
            except OSError:
                logger.warning("删除过期企业微信日志失败: %s", path, exc_info=True)


async def _read_retry_state() -> dict:
    async with _wecom_file_lock:
        return _read_retry_state_locked()


async def _write_retry_state(state: dict) -> None:
    async with _wecom_file_lock:
        _write_retry_state_locked(state)


def _read_retry_state_locked() -> dict:
    if not WECOM_RETRY_STATE_PATH.exists():
        return {}
    try:
        with open(WECOM_RETRY_STATE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("读取企业微信重试状态失败", exc_info=True)
        return {}


def _write_retry_state_locked(state: dict) -> None:
    WECOM_RETRY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = WECOM_RETRY_STATE_PATH.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, WECOM_RETRY_STATE_PATH)


async def _set_retry_failure(
    *,
    module: str,
    business_key: str,
    recipient: str,
    content: str,
    message: str,
) -> int:
    async with _wecom_file_lock:
        state = _read_retry_state_locked()
        key = _retry_key(module, business_key, recipient, content)
        item = state.get(key, {})
        attempts = int(item.get("attempts", 0)) + 1
        state[key] = {
            "module": module,
            "business_key": business_key,
            "recipient": recipient,
            "content": content,
            "content_hash": _content_hash(content),
            "attempts": attempts,
            "last_error": message,
            "last_attempt_at": _now_str(),
            "alerted": bool(item.get("alerted", False)),
        }
        _write_retry_state_locked(state)
        return attempts


async def _clear_retry_failure(module: str, business_key: str, recipient: str, content: str) -> None:
    async with _wecom_file_lock:
        state = _read_retry_state_locked()
        key = _retry_key(module, business_key, recipient, content)
        if key in state:
            del state[key]
            _write_retry_state_locked(state)


async def _mark_retry_alerted(key: str) -> None:
    async with _wecom_file_lock:
        state = _read_retry_state_locked()
        if key in state:
            state[key]["alerted"] = True
            state[key]["alerted_at"] = _now_str()
            _write_retry_state_locked(state)


async def _send_one_text_message(content: str, recipient: str) -> tuple[bool, str]:
    token_success, token_or_message = await _get_wecom_access_token(WECOM_CORP_SECRET)
    if not token_success:
        return False, token_or_message

    try:
        async with httpx.AsyncClient(base_url=WECOM_API_BASE, timeout=10.0, trust_env=False) as client:
            send_response = await client.post(
                "/cgi-bin/message/send",
                params={"access_token": token_or_message},
                json={
                    "touser": recipient,
                    "msgtype": "text",
                    "agentid": int(WECOM_AGENT_ID),
                    "text": {"content": content},
                    "safe": 0,
                },
            )
            send_response.raise_for_status()
            send_data = send_response.json()
            invalid_user = send_data.get("invaliduser")
            if send_data.get("errcode") != 0 or invalid_user:
                return False, f"企业微信消息发送失败：{send_data.get('errmsg', send_data)} invaliduser={invalid_user}"

            return True, "企业微信消息已发送"
    except ValueError:
        return False, "企业微信 AgentID 配置不正确，请使用数字"
    except httpx.HTTPError as exc:
        logger.exception("企业微信消息请求失败")
        return False, f"企业微信接口请求失败：{exc}"
    except Exception as exc:
        logger.exception("企业微信消息发送异常")
        return False, f"企业微信通知发送异常：{exc}"


async def send_wecom_text_message(
    content: str,
    touser: str = WECOM_DEFAULT_TOUSER,
    *,
    module: str = "common",
    business_key: str = "",
    message_type: str = "text",
    link_url: str = "",
    link_label: str = "查看详情",
    retry_tracking: bool = True,
    alert_on_max_failure: bool = True,
) -> tuple[bool, str]:
    if link_url and link_url not in content:
        content = f"{content.rstrip()}\n{link_label}：{link_url}"

    recipients = split_wecom_users(touser)
    results = []
    overall_success = True

    for recipient in recipients:
        success, message = await _send_one_text_message(content, recipient)
        overall_success = overall_success and success
        log_record = {
            "log_id": uuid.uuid4().hex,
            "time": _now_str(),
            "module": module,
            "business_key": business_key,
            "message_type": message_type,
            "recipient": recipient,
            "content_hash": _content_hash(content),
            "content": content,
            "success": success,
            "message": message,
        }
        await _append_log(log_record)

        if success:
            if retry_tracking:
                await _clear_retry_failure(module, business_key, recipient, content)
        elif retry_tracking:
            attempts = await _set_retry_failure(
                module=module,
                business_key=business_key,
                recipient=recipient,
                content=content,
                message=message,
            )
            if alert_on_max_failure and attempts >= WECOM_MAX_RETRY_COUNT:
                await _send_retry_failure_alert(module, business_key, recipient, content, attempts, message)
        results.append(f"{recipient}: {message}")

    return overall_success, "；".join(results)


async def _send_retry_failure_alert(
    module: str,
    business_key: str,
    recipient: str,
    content: str,
    attempts: int,
    message: str,
) -> None:
    state = await _read_retry_state()
    key = _retry_key(module, business_key, recipient, content)
    if state.get(key, {}).get("alerted"):
        return

    alert_content = (
        "企业微信消息多次发送失败\n"
        f"模块：{module}\n"
        f"业务：{business_key or '-'}\n"
        f"接收人：{recipient}\n"
        f"失败次数：{attempts}\n"
        f"最近错误：{message}\n"
        f"内容摘要：{content[:200]}"
    )
    await send_wecom_text_message(
        alert_content,
        WECOM_DEFAULT_TOUSER,
        module="wecom_service",
        business_key=f"failure_alert:{key}",
        message_type="failure_alert",
        retry_tracking=False,
        alert_on_max_failure=False,
    )
    await _mark_retry_alerted(key)


async def retry_failed_wecom_messages() -> tuple[int, int]:
    state = await _read_retry_state()
    retry_items = [
        (key, item)
        for key, item in state.items()
        if int(item.get("attempts", 0)) < WECOM_MAX_RETRY_COUNT
    ]
    success_count = 0
    fail_count = 0

    for _, item in retry_items:
        success, _ = await send_wecom_text_message(
            item.get("content", ""),
            item.get("recipient", WECOM_DEFAULT_TOUSER),
            module=item.get("module", "common"),
            business_key=item.get("business_key", ""),
            message_type="retry",
            retry_tracking=True,
            alert_on_max_failure=True,
        )
        if success:
            success_count += 1
        else:
            fail_count += 1

    return success_count, fail_count
