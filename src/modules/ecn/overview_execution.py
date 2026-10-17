# -*- encoding: utf-8 -*-
import copy
import asyncio
import json
import hashlib
import logging
import uuid
from pathlib import Path
from collections.abc import Awaitable, Callable

from nicegui import (
    app,
)

from ... import (
    db_storage,
    overview_storage,
)
from ...ecn_management_config import (
    ECN_EXECUTION_RESULT_FAILED,
    ECN_EXECUTION_RESULT_SUCCESS,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_UPDATE,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    ECNState,
    build_ecn_scheme_snapshot,
    get_ecn_overview_project_new_data,
)
from ...overview_operation import (
    append_overview_timestamp,
    get_automatic_overview_reason,
)
from .editing import get_pending_overview_anchor_source
from .overview_locks import active_overview_row_exists
from .overview_files import publish_overview_upload

logger = logging.getLogger(__name__)

OverviewProgressCallback = Callable[[str, dict, str], Awaitable[None]]


def build_overview_activation_state(req_max_ver: object) -> tuple[str, dict[str, bool]]:
    """与正常概述录入一致：无项目需求时从需求 V0.0 节点录入。"""
    version_index = int(float(str(req_max_ver or "0.0")))
    normalized_version = f"{version_index}.0"
    return normalized_version, {
        f"{index}.0": f"{index}.0" == normalized_version for index in range(0, version_index + 1)
    }


def deactivate_overview_chip_for_ecn(
    chip: dict,
    req_ver: str,
    ecn_id: str,
    operation_time: str,
    scheme_author: str,
    reason: str,
    source_operation_id: str = "",
) -> dict:
    """生成 ECN 失活后的旧 Chip；保留录入节点并记录方案提供人的本次操作。"""
    scheme_author = str(scheme_author or "").strip()
    if not scheme_author:
        raise ValueError("方案未记录实际提供人，无法执行")
    result = copy.deepcopy(chip)
    result.setdefault("select_activ_dic", {})[req_ver] = False
    result["enabled"] = False
    result["bg_color"] = "bg-grey-5"
    result["icon"] = "block"
    history_key = append_overview_timestamp(
        result,
        creator=scheme_author,
        reason=reason,
        operation_time=operation_time,
        source_id=ecn_id,
    )
    if source_operation_id:
        result["timestamp"][history_key]["source_operation_id"] = source_operation_id
    return result


def _has_ecn_source_marker(chip: object, ecn_id: str, source_operation_id: str) -> bool:
    """判断确定性重试是否已经把当前 ECN 操作写入概述记录。"""
    if not isinstance(chip, dict):
        return False
    history = chip.get("timestamp", {})
    if not isinstance(history, dict):
        return False
    for entry in history.values():
        if (
            isinstance(entry, dict)
            and str(entry.get("source_id") or "") == ecn_id
            and str(entry.get("source_operation_id") or "") == source_operation_id
        ):
            if entry.get("result_fingerprint") != _overview_result_fingerprint(chip):
                raise ValueError("已执行概述内容或状态已变化，不能直接认定执行成功，请核对处理。")
            return True
    return False


def _overview_result_fingerprint(chip: dict) -> str:
    snapshot = copy.deepcopy(chip)
    history = snapshot.get("timestamp", {})
    if isinstance(history, dict):
        for entry in history.values():
            if isinstance(entry, dict):
                entry.pop("result_fingerprint", None)
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _stamp_execution_result(chip: dict, ecn_id: str, source_operation_id: str) -> None:
    fingerprint = _overview_result_fingerprint(chip)
    for entry in chip.get("timestamp", {}).values():
        if isinstance(entry, dict) and entry.get("source_id") == ecn_id and entry.get("source_operation_id") == source_operation_id:
            entry["result_fingerprint"] = fingerprint


async def execute_ecn_overview_schemes(
    ecn_data: dict,
    operation_time: str,
    *,
    progress_callback: OverviewProgressCallback | None = None,
) -> dict:
    """逐条、逐项目执行系统内资料方案并返回可持久化的结果清单。"""
    execution_info = ecn_data.setdefault("execution_info", {})
    stored_results = execution_info.setdefault("overview_results", {})
    results = copy.deepcopy(stored_results) if isinstance(stored_results, dict) else {}
    ecn_id = str(ecn_data.get("ecn_id") or "")
    run_id = str(execution_info.get("overview_run_id") or "")
    change_items = [item for item in ecn_data.get("change_items", []) if isinstance(item, dict)]
    # 待新增表格子项只能在其基准列方案成功后执行；保持其它方案原有顺序。
    change_items.sort(
        key=lambda item: int(
            any(
                isinstance(state, dict)
                and bool(get_pending_overview_anchor_source(state.get("anchor_row_id")))
                for state in (
                    item.get("project_states", {}).values()
                    if isinstance(item.get("project_states"), dict)
                    else []
                )
            )
        )
    )

    def deterministic_uuid(item_id: str, project: str, purpose: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"ecn:{ecn_id}:{item_id}:{project}:{purpose}"))

    generated_row_ids: dict[tuple[str, str], str] = {}
    for scheme_item in change_items:
        if scheme_item.get("type") != "overview_update":
            continue
        scheme_item_id = str(scheme_item.get("item_id") or "")
        if scheme_item.get("label") != scheme_item.get("first_col_label"):
            continue
        for scheme_project, scheme_state in scheme_item.get("project_states", {}).items():
            if isinstance(scheme_state, dict) and scheme_state.get("action") == ECN_OVERVIEW_ACTION_ADD:
                generated_row_ids[(scheme_item_id, str(scheme_project))] = deterministic_uuid(
                    scheme_item_id,
                    str(scheme_project),
                    "row",
                )

    def create_new_chip_template(item: dict, project: str, new_data: dict, reason: str) -> tuple[dict, str]:
        item_id = str(item.get("item_id") or "")
        scheme_author = str(item.get("author") or "").strip()
        if not scheme_author:
            raise ValueError("方案未记录实际提供人，无法执行")
        processing_type = item.get("config_processing_type", "text")
        icon_map = {
            "file": "attachment",
            "search": "saved_search",
            "svn": "saved_search",
            "image": "image",
            "video": "play_circle",
        }
        req_max_ver, new_activ_dic = build_overview_activation_state(
            app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
        )
        new_chip = {
            "id": deterministic_uuid(item_id, project, "chip"),
            "role": item["role"],
            "type": processing_type,
            "icon": icon_map.get(processing_type),
            "enabled": True,
            "bg_color": "bg-light-blue-1",
            "content": new_data.get("content", ""),
            "creator": scheme_author,
            "req_ver": req_max_ver,
            "select_activ_dic": new_activ_dic,
            "timestamp": {
                operation_time: {
                    "creator": scheme_author,
                    "reason": reason,
                    "source_id": ecn_id,
                    "select_activ_dic": copy.deepcopy(new_activ_dic),
                }
            },
        }
        for data_key in ["test_select_data", "file_type", "url_path", "local_file_path", "warehouse"]:
            if data_key in new_data:
                new_chip[data_key] = copy.deepcopy(new_data[data_key])
        return new_chip, req_max_ver

    for item in change_items:
        if item.get("type") != "overview_update":
            continue
        item_id = str(item.get("item_id") or "").strip()
        if not item_id:
            continue
        previous_result = results.get(item_id, {})
        if isinstance(previous_result, dict) and previous_result.get("status") == ECN_EXECUTION_RESULT_SUCCESS:
            item["execute_status"] = ECN_EXECUTION_RESULT_SUCCESS
            continue

        project_states = item.get("project_states", {})
        project_results = (
            copy.deepcopy(previous_result.get("projects", {}))
            if isinstance(previous_result, dict) and isinstance(previous_result.get("projects"), dict)
            else {}
        )
        if not isinstance(project_states, dict) or not project_states:
            results[item_id] = {
                "status": ECN_EXECUTION_RESULT_FAILED,
                "message": "方案未关联目标项目，不能执行",
                "projects": project_results,
                "time": operation_time,
            }
            item["execute_status"] = ECN_EXECUTION_RESULT_FAILED
            if progress_callback is not None:
                await progress_callback(
                    item_id,
                    copy.deepcopy(results[item_id]),
                    ECN_EXECUTION_RESULT_FAILED,
                )
            continue

        for project, project_state in project_states.items():
            project = str(project)
            existing_project_result = project_results.get(project, {})
            if (
                isinstance(existing_project_result, dict)
                and existing_project_result.get("status") == ECN_EXECUTION_RESULT_SUCCESS
            ):
                continue
            try:
                if not isinstance(project_state, dict):
                    raise ValueError("项目执行配置无效")
                action = project_state.get("action")
                chip_id = project_state.get("chip_id")
                anchor_row_id = project_state.get("anchor_row_id")
                project_new_data = get_ecn_overview_project_new_data(item.get("new_data", {}), project_state)
                label = item["label"]
                if action not in {
                    ECN_OVERVIEW_ACTION_DEACTIVATE,
                    ECN_OVERVIEW_ACTION_UPDATE,
                    ECN_OVERVIEW_ACTION_ADD,
                }:
                    raise ValueError(f"不支持的执行动作：{action or '未配置'}")

                operation_state = {"applied": False, "error": ""}
                installed_file: Path | None = None
                source_operation_id = f"{ecn_id}:{item_id}:{project}:{action}"
                scheme_author = str(item.get("author") or "").strip()
                if not scheme_author:
                    raise ValueError("方案未记录实际提供人，无法执行")

                async def require_live_execution(_current_project, connection):
                    async with connection.execute(
                        f"SELECT value FROM {db_storage.TABLE_NAME} WHERE key = ?", ("ecn_management_data",)
                    ) as cursor:
                        row = await cursor.fetchone()
                    records = json.loads(row[0]) if row else {}
                    live = records.get(ecn_id) if isinstance(records, dict) else None
                    if not isinstance(live, dict):
                        operation_state["error"] = "ECN单据已不存在，已停止执行"
                        return False
                    live_execution = live.get("execution_info", {})
                    if (
                        not run_id or live.get("workflow", {}).get("current_state") != ECNState.ECN_EXECUTING
                        or live_execution.get("stage") != ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
                        or live_execution.get("overview_run_id") != run_id
                    ):
                        operation_state["error"] = "ECN执行阶段或批次已变化，已停止旧任务"
                        return False
                    live_item = next((value for value in live.get("change_items", [])
                        if isinstance(value, dict) and str(value.get("item_id") or "") == item_id), None)
                    if build_ecn_scheme_snapshot(live_item) != build_ecn_scheme_snapshot(item):
                        operation_state["error"] = "方案内容已变化，已停止旧任务"
                        return False
                    return True

                def apply_to_latest_label(current_label: object):
                    label_data = copy.deepcopy(current_label) if isinstance(current_label, dict) else {}
                    if action in {ECN_OVERVIEW_ACTION_DEACTIVATE, ECN_OVERVIEW_ACTION_UPDATE}:
                        if not chip_id:
                            operation_state["error"] = (
                                "未记录需要失效的原数据"
                                if action == ECN_OVERVIEW_ACTION_DEACTIVATE
                                else "未记录需要更换的原数据"
                            )
                            return db_storage.ATOMIC_NO_UPDATE
                        old_chip = label_data.get(str(chip_id))
                        if not isinstance(old_chip, dict):
                            operation_state["error"] = (
                                "需要失效的原数据不存在"
                                if action == ECN_OVERVIEW_ACTION_DEACTIVATE
                                else "需要更换的原数据不存在"
                            )
                            return db_storage.ATOMIC_NO_UPDATE
                    else:
                        old_chip = None

                    if action == ECN_OVERVIEW_ACTION_DEACTIVATE:
                        assert isinstance(old_chip, dict)
                        if _has_ecn_source_marker(old_chip, ecn_id, source_operation_id):
                            operation_state["applied"] = True
                            return db_storage.ATOMIC_NO_UPDATE
                        req_max_ver, _ = build_overview_activation_state(
                            app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                        )
                        label_data[str(chip_id)] = deactivate_overview_chip_for_ecn(
                            old_chip,
                            req_max_ver,
                            ecn_id,
                            operation_time,
                            scheme_author,
                            get_automatic_overview_reason("ecn_deactivate"),
                            source_operation_id,
                        )
                    else:
                        new_chip, req_max_ver = create_new_chip_template(
                            item,
                            project,
                            project_new_data,
                            get_automatic_overview_reason(
                                "ecn_add" if action == ECN_OVERVIEW_ACTION_ADD else "ecn_replace_new"
                            ),
                        )
                        existing_new = label_data.get(str(new_chip["id"]))
                        if existing_new is not None:
                            if _has_ecn_source_marker(existing_new, ecn_id, source_operation_id):
                                if action == ECN_OVERVIEW_ACTION_UPDATE and not _has_ecn_source_marker(old_chip, ecn_id, source_operation_id):
                                    raise ValueError("原概述缺少本次失活记录，请核对处理。")
                                operation_state["applied"] = True
                                return db_storage.ATOMIC_NO_UPDATE
                            operation_state["error"] = "目标位置已存在其它概述数据"
                            return db_storage.ATOMIC_NO_UPDATE

                        if action == ECN_OVERVIEW_ACTION_UPDATE:
                            assert isinstance(old_chip, dict)
                            new_chip["row_id"] = old_chip.get("row_id")
                            new_chip["select_activ_dic"] = copy.deepcopy(old_chip.get("select_activ_dic", {}))
                            new_chip["select_activ_dic"][req_max_ver] = True
                            new_chip["timestamp"][operation_time]["select_activ_dic"] = copy.deepcopy(
                                new_chip["select_activ_dic"]
                            )
                            new_chip["timestamp"][operation_time]["source_operation_id"] = source_operation_id
                            label_data[str(chip_id)] = deactivate_overview_chip_for_ecn(
                                old_chip,
                                req_max_ver,
                                ecn_id,
                                operation_time,
                                scheme_author,
                                get_automatic_overview_reason("ecn_replace_old"),
                                source_operation_id,
                            )
                        elif label == item.get("first_col_label", ""):
                            new_chip["row_id"] = generated_row_ids[(item_id, project)]
                        elif anchor_row_id and str(anchor_row_id).startswith("PENDING_NEW_"):
                            source_item_id = get_pending_overview_anchor_source(anchor_row_id)
                            source_project_result = results.get(source_item_id, {}).get("projects", {}).get(project, {})
                            if (
                                (source_item_id, project) not in generated_row_ids
                                or not isinstance(source_project_result, dict)
                                or source_project_result.get("status") != ECN_EXECUTION_RESULT_SUCCESS
                            ):
                                operation_state["error"] = "关联的新增基准行尚未成功执行"
                                return db_storage.ATOMIC_NO_UPDATE
                            new_chip["row_id"] = generated_row_ids[(source_item_id, project)]
                        else:
                            new_chip["row_id"] = anchor_row_id
                        new_chip["timestamp"][operation_time]["source_operation_id"] = source_operation_id
                        label_data[str(new_chip["id"])] = new_chip

                    operation_state["applied"] = True
                    return label_data

                async def apply_to_latest_project(current_project):
                    nonlocal installed_file
                    project_data = copy.deepcopy(current_project) if isinstance(current_project, dict) else {}
                    first_label = str(item.get("first_col_label") or "")
                    if (
                        action == ECN_OVERVIEW_ACTION_ADD and first_label and str(label) != first_label
                    ):
                        source_id = get_pending_overview_anchor_source(anchor_row_id)
                        bound_row = generated_row_ids.get((source_id, project), "") if source_id else str(anchor_row_id or "")
                        version = str(app.storage.general.get("project_req_max_ver", {}).get(project, "0.0"))
                        if not active_overview_row_exists(project_data, first_label, bound_row, version):
                            operation_state["error"] = "绑定的表格基准行已不存在或失活，不能新增子参数"
                            return db_storage.ATOMIC_NO_UPDATE
                    try:
                        label_data = apply_to_latest_label(project_data.get(str(label)))
                    except ValueError as exc:
                        operation_state["error"] = str(exc)
                        return db_storage.ATOMIC_NO_UPDATE
                    if not isinstance(label_data, dict):
                        return db_storage.ATOMIC_NO_UPDATE
                    if action != ECN_OVERVIEW_ACTION_DEACTIVATE and item.get("config_processing_type") in {"file", "image", "video"}:
                        config = app.storage.general.get("over_config_data_flat", {}).get(str(label), {})
                        published, installed_file = await asyncio.to_thread(publish_overview_upload, item, project, ecn_id, config)
                        label_data[deterministic_uuid(item_id, project, "chip")].update(published)
                    # 文件发布后的最终元数据也必须进入指纹；旧节点失活与新节点一起校验。
                    if action in {ECN_OVERVIEW_ACTION_UPDATE, ECN_OVERVIEW_ACTION_DEACTIVATE}:
                        _stamp_execution_result(label_data[str(chip_id)], ecn_id, source_operation_id)
                    if action in {ECN_OVERVIEW_ACTION_ADD, ECN_OVERVIEW_ACTION_UPDATE}:
                        _stamp_execution_result(label_data[deterministic_uuid(item_id, project, "chip")], ecn_id, source_operation_id)
                    project_data[str(label)] = label_data
                    return project_data

                saved = await overview_storage.atomic_deep_update(
                    [f"{project}_over_data"],
                    apply_to_latest_project,
                    allow_ecn=True,
                    transaction_guard=require_live_execution,
                    storage=db_storage,
                )
                if not saved or operation_state["error"] or not operation_state["applied"]:
                    if installed_file is not None:
                        await asyncio.to_thread(installed_file.unlink, missing_ok=True)
                    raise ValueError(operation_state["error"] or "概述资料原子保存失败")

                project_results[project] = {
                    "status": ECN_EXECUTION_RESULT_SUCCESS,
                    "message": "执行成功",
                    "time": operation_time,
                }
            except Exception as exc:
                logger.exception("ECN系统内资料方案执行失败：%s / %s", item_id, project)
                project_results[project] = {
                    "status": ECN_EXECUTION_RESULT_FAILED,
                    "message": str(exc),
                    "time": operation_time,
                }

            if progress_callback is not None:
                pending_projects = [
                    value
                    for value in project_states
                    if project_results.get(str(value), {}).get("status")
                    not in {ECN_EXECUTION_RESULT_SUCCESS, ECN_EXECUTION_RESULT_FAILED}
                ]
                failed_projects_now = [
                    value
                    for value in project_states
                    if project_results.get(str(value), {}).get("status") == ECN_EXECUTION_RESULT_FAILED
                ]
                checkpoint_status = (
                    ECN_EXECUTION_RESULT_FAILED
                    if failed_projects_now
                    else "running"
                    if pending_projects
                    else ECN_EXECUTION_RESULT_SUCCESS
                )
                await progress_callback(
                    item_id,
                    {
                        "status": checkpoint_status,
                        "message": (
                            "执行中"
                            if pending_projects
                            else "全部项目执行成功"
                            if not failed_projects_now
                            else "执行失败项目：" + "、".join(map(str, failed_projects_now))
                        ),
                        "projects": copy.deepcopy(project_results),
                        "time": operation_time,
                    },
                    checkpoint_status,
                )

        failed_projects = [
            project
            for project in project_states
            if project_results.get(str(project), {}).get("status") != ECN_EXECUTION_RESULT_SUCCESS
        ]
        item_status = ECN_EXECUTION_RESULT_FAILED if failed_projects else ECN_EXECUTION_RESULT_SUCCESS
        results[item_id] = {
            "status": item_status,
            "message": (
                "全部项目执行成功" if not failed_projects else "执行失败项目：" + "、".join(map(str, failed_projects))
            ),
            "projects": project_results,
            "time": operation_time,
        }
        item["execute_status"] = item_status

    return results
