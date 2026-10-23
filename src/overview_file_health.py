"""激活状态文件性概述的定时健康检测与快照。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

from nicegui import app

from . import db_storage
from .utils import validate_search_path, validate_svn_url

logger = logging.getLogger(__name__)
OVERVIEW_FILE_HEALTH_KEY = "overview_file_health_snapshot"
OVERVIEW_FILE_HEALTH_REMINDER_KEY = "overview_file_health_reminders"
FILE_TYPES = {"image", "video", "file", "search", "svn"}
_scan_lock = asyncio.Lock()


class HealthGroup(TypedDict):
    name: str
    total: int
    unhealthy: list[dict]


def collect_active_file_overviews(
    project_versions: Mapping[str, object],
    configs: Mapping[str, object],
    owners: Mapping[str, object],
) -> list[dict[str, str]]:
    """只收集当前需求版本明确激活的文件性概述。"""
    entries: list[dict[str, str]] = []
    for project, version_value in project_versions.items():
        version = str(version_value)
        project_data = db_storage.get_item(f"{project}_over_data", {})
        if not isinstance(project_data, Mapping):
            continue
        project_owners = owners.get(project, {})
        for label, chips in project_data.items():
            config = configs.get(label, {})
            if not isinstance(config, Mapping) or not isinstance(chips, Mapping):
                continue
            role = str(config.get("role") or "未分类")
            owner_data = project_owners.get(role, {}) if isinstance(project_owners, Mapping) else {}
            raw_owner = owner_data.get("latest_user", "") if isinstance(owner_data, Mapping) else ""
            owner = str(raw_owner or "").split("：", 1)[-1].strip() or "未指定负责人"
            for chip_id, chip in chips.items():
                if not isinstance(chip, Mapping):
                    continue
                kind = str(chip.get("type") or config.get("processing_type") or "")
                states = chip.get("select_activ_dic", {})
                if (
                    kind not in FILE_TYPES
                    or chip.get("enabled") is False
                    or not isinstance(states, Mapping)
                    or states.get(version) is not True
                ):
                    continue
                entries.append({
                    "project": str(project),
                    "version": version,
                    "label": str(label),
                    "chip_id": str(chip_id),
                    "title": str(config.get("title") or label),
                    "role": role,
                    "owner": owner,
                    "type": kind,
                    "content": str(chip.get("content") or ""),
                    "local_file_path": str(chip.get("local_file_path") or ""),
                    "upload_path": str(config.get("upload_path") or ""),
                    "url_path": str(chip.get("url_path") or ""),
                })
    return entries


async def check_file_overview(entry: dict[str, str], config: Mapping[str, object]) -> dict[str, str | bool]:
    """沿用概述页面的文件目录和 SVN 校验规则。"""
    result: dict[str, str | bool] = {**entry, "healthy": False, "reason": ""}
    try:
        if entry["type"] == "search":
            valid, _url, _file_type, _local_path, message = await validate_search_path(
                entry["content"], dict(config), [entry["project"]]
            )
            result["healthy"] = bool(valid)
            result["reason"] = "" if valid else str(message or "服务器文件不可访问")
        elif entry["type"] == "svn":
            valid, _url, _file_type, message = await validate_svn_url(
                entry["content"], dict(config), [entry["project"]]
            )
            result["healthy"] = bool(valid)
            result["reason"] = "" if valid else str(message or "SVN 链接不可访问")
        else:
            local_path = entry["local_file_path"]
            if not local_path and entry["upload_path"] and entry["content"]:
                local_path = str(Path(entry["upload_path"]) / entry["content"])
            result["healthy"] = bool(local_path) and await asyncio.to_thread(Path(local_path).is_file)
            result["reason"] = "" if result["healthy"] else "服务器文件不存在或路径不可访问"
    except Exception:
        logger.exception("文件性概述健康检测失败: project=%s, label=%s", entry["project"], entry["label"])
        result["reason"] = "检测时发生错误"
    return result


async def scan_overview_file_health() -> dict[str, object]:
    """完成全部检测后一次性替换快照，避免页面读到半成品。"""
    async with _scan_lock:
        general = app.storage.general
        configs = general.get("over_config_data_flat", {})
        versions = general.get("project_req_max_ver", {})
        owners = general.get("overview_role", {})
        if not all(isinstance(value, Mapping) for value in (configs, versions, owners)):
            raise ValueError("概述检测所需的项目或配置数据不可用")
        entries = collect_active_file_overviews(versions, configs, owners)
        semaphore = asyncio.Semaphore(6)

        async def inspect(entry: dict[str, str]) -> dict[str, str | bool]:
            async with semaphore:
                config = configs.get(entry["label"], {})
                return await check_file_overview(entry, config if isinstance(config, Mapping) else {})

        results = await asyncio.gather(*(inspect(entry) for entry in entries))
        snapshot: dict[str, object] = {
            "checked_at": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "items": results,
        }
        if not await db_storage.set_item(OVERVIEW_FILE_HEALTH_KEY, snapshot):
            raise RuntimeError("概述文件健康快照保存失败")
        return snapshot


def summarize_file_health(items: list[dict], field: str) -> list[HealthGroup]:
    """按职能或负责人汇总健康与异常数量。"""
    groups: dict[str, HealthGroup] = defaultdict(lambda: {"name": "", "total": 0, "unhealthy": []})
    for item in items:
        name = str(item.get(field) or "未分类")
        group = groups[name]
        group["name"] = name
        group["total"] += 1
        if not item.get("healthy"):
            group["unhealthy"].append(item)
    return sorted(groups.values(), key=lambda group: (-len(group["unhealthy"]), group["name"]))


def unhealthy_items_for_owner(snapshot: Mapping[str, object], username: str) -> list[dict]:
    """按当前职能负责人筛选异常，避免负责人更换后提醒旧负责人。"""
    items = snapshot.get("items", [])
    owners = app.storage.general.get("overview_role", {})
    if not isinstance(items, list) or not isinstance(owners, Mapping):
        return []
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or item.get("healthy") is not False:
            continue
        project_owners = owners.get(str(item.get("project") or ""), {})
        role_data = project_owners.get(str(item.get("role") or ""), {}) if isinstance(project_owners, Mapping) else {}
        raw_owner = role_data.get("latest_user", "") if isinstance(role_data, Mapping) else ""
        owner = str(raw_owner or "").split("：", 1)[-1].strip()
        if owner == username:
            result.append(item)
    return result


async def claim_file_health_reminder(username: str, scan_date: str) -> bool:
    """跨页面和服务进程原子认领负责人当天的一次提醒。"""
    claimed = False

    def update(current: object) -> dict[str, object] | object:
        nonlocal claimed
        data = dict(current) if isinstance(current, dict) else {}
        day = data.get(scan_date, {})
        shown = dict(day) if isinstance(day, dict) else {}
        if shown.get(username):
            return db_storage.ATOMIC_NO_UPDATE
        shown[username] = True
        claimed = True
        return {scan_date: shown}

    saved = await db_storage.atomic_deep_update([OVERVIEW_FILE_HEALTH_REMINDER_KEY], update)
    return saved and claimed
