# -*- encoding: utf-8 -*-
import copy
import logging
import uuid

from nicegui import (
    app,
)

from ... import (
    db_storage,
)
from ...ecn_management_config import (
    ECN_EXECUTION_RESULT_FAILED,
    ECN_EXECUTION_RESULT_SUCCESS,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_UPDATE,
    get_ecn_overview_project_new_data,
)
from ...overview_operation import (
    append_overview_timestamp,
    get_automatic_overview_reason,
)
from .repository import (
    save_ecn_deep_item,
)

logger = logging.getLogger(__name__)


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
    append_overview_timestamp(
        result,
        creator=scheme_author,
        reason=reason,
        operation_time=operation_time,
        source_id=ecn_id,
    )
    return result


async def execute_ecn_overview_schemes(ecn_data: dict, operation_time: str) -> dict:
    """逐条、逐项目执行系统内资料方案并返回可持久化的结果清单。"""
    execution_info = ecn_data.setdefault("execution_info", {})
    stored_results = execution_info.setdefault("overview_results", {})
    results = copy.deepcopy(stored_results) if isinstance(stored_results, dict) else {}
    ecn_id = str(ecn_data.get("ecn_id") or "")
    change_items = [item for item in ecn_data.get("change_items", []) if isinstance(item, dict)]

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
                "message": "方案没有可执行的项目配置",
                "projects": project_results,
                "time": operation_time,
            }
            item["execute_status"] = ECN_EXECUTION_RESULT_FAILED
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

                if action == ECN_OVERVIEW_ACTION_DEACTIVATE:
                    if not chip_id:
                        raise ValueError("未记录需要失效的原数据")
                    path = [f"{project}_over_data", label, chip_id]
                    old_chip = db_storage.get_deep_item(path)
                    if not old_chip:
                        raise ValueError("需要失效的原数据不存在")
                    scheme_author = str(item.get("author") or "").strip()
                    if not scheme_author:
                        raise ValueError("方案未记录实际提供人，无法执行")
                    req_max_ver, _ = build_overview_activation_state(
                        app.storage.general.get("project_req_max_ver", {}).get(project, "0.0")
                    )
                    await save_ecn_deep_item(
                        path,
                        deactivate_overview_chip_for_ecn(
                            old_chip,
                            req_max_ver,
                            ecn_id,
                            operation_time,
                            scheme_author,
                            get_automatic_overview_reason("ecn_deactivate"),
                        ),
                    )
                elif action == ECN_OVERVIEW_ACTION_UPDATE:
                    if not chip_id:
                        raise ValueError("未记录需要更换的原数据")
                    path = [f"{project}_over_data", label, chip_id]
                    old_chip = db_storage.get_deep_item(path)
                    if not old_chip:
                        raise ValueError("需要更换的原数据不存在")
                    new_chip, req_max_ver = create_new_chip_template(
                        item,
                        project,
                        project_new_data,
                        get_automatic_overview_reason("ecn_replace_new"),
                    )
                    new_chip["row_id"] = old_chip.get("row_id")
                    new_chip["select_activ_dic"] = copy.deepcopy(old_chip.get("select_activ_dic", {}))
                    new_chip["select_activ_dic"][req_max_ver] = True
                    new_chip["timestamp"][operation_time]["select_activ_dic"] = copy.deepcopy(
                        new_chip["select_activ_dic"]
                    )
                    await save_ecn_deep_item(
                        path,
                        deactivate_overview_chip_for_ecn(
                            old_chip,
                            req_max_ver,
                            ecn_id,
                            operation_time,
                            str(item.get("author") or "").strip(),
                            get_automatic_overview_reason("ecn_replace_old"),
                        ),
                    )
                    await save_ecn_deep_item(
                        [f"{project}_over_data", label, new_chip["id"]],
                        new_chip,
                    )
                elif action == ECN_OVERVIEW_ACTION_ADD:
                    new_chip, _ = create_new_chip_template(
                        item,
                        project,
                        project_new_data,
                        get_automatic_overview_reason("ecn_add"),
                    )
                    if label == item.get("first_col_label", ""):
                        new_chip["row_id"] = generated_row_ids[(item_id, project)]
                    elif anchor_row_id and str(anchor_row_id).startswith("PENDING_NEW_"):
                        source_item_id = str(anchor_row_id).replace("PENDING_NEW_", "", 1)
                        new_chip["row_id"] = generated_row_ids.get(
                            (source_item_id, project),
                            deterministic_uuid(source_item_id, project, "row"),
                        )
                    else:
                        new_chip["row_id"] = anchor_row_id
                    await save_ecn_deep_item(
                        [f"{project}_over_data", label, new_chip["id"]],
                        new_chip,
                    )
                else:
                    raise ValueError(f"不支持的执行动作：{action or '未配置'}")

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
