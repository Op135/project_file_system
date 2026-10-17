"""关键项目概述新增的配置、审批申请和生效逻辑。"""

from __future__ import annotations

import asyncio
import copy
import logging
import shutil
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from nicegui import app

from . import db_storage
from . import overview_storage
from .approval_workflow import (
    advance_approval_sequence,
    create_approval_sequence_assignments,
    is_assigned_approver,
)
from .project_overview_access import can_edit_overview_item, overview_workflow_error_message

OVERVIEW_ADD_APPROVAL_CONFIG_KEY = "add_approval_required"
OVERVIEW_ADD_REQUESTS_KEY = "overview_add_approval_requests"
OVERVIEW_ADD_WORKFLOW_EVENT = "add_review"
OVERVIEW_ADD_TASK_KEY = "overview_add_review"
OVERVIEW_ADD_STAGING_DIR = Path(__file__).resolve().parents[1] / ".overview_add_staging"

_review_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OverviewAddSaveResult:
    status: str
    message: str
    request_id: str = ""

    @property
    def effective(self) -> bool:
        return self.status == "saved"

    @property
    def pending(self) -> bool:
        return self.status == "pending"


def requires_overview_add_approval(config: object) -> bool:
    """配置缺失时默认不审批，只有显式布尔值 true 才启用。"""
    return isinstance(config, dict) and config.get(OVERVIEW_ADD_APPROVAL_CONFIG_KEY) is True


def normalize_overview_add_impacts(
    config: object,
    impact_mode: object,
    related_labels: object,
) -> tuple[str, list[str]]:
    """只固化配置允许的关联项，避免客户端注入其它概述标签。"""
    configured = []
    if isinstance(config, dict) and isinstance(config.get("impact_list"), list):
        configured = list(dict.fromkeys(str(label) for label in config["impact_list"] if str(label)))
    mode = str(impact_mode or "none")
    if mode == "all":
        return "all", configured
    if mode == "selected" and isinstance(related_labels, list):
        selected = {str(label) for label in related_labels}
        normalized = [label for label in configured if label in selected]
        if normalized:
            return "selected", normalized
    return "none", []


def validate_overview_add_approval_config(overview_config: object) -> None:
    """校验概述项审批开关，拒绝字符串等易误判配置。"""
    if not isinstance(overview_config, dict):
        raise ValueError("overview_config.json 根节点必须是对象")
    for dimension, groups in overview_config.items():
        if not isinstance(groups, dict):
            continue
        for group_name, items in groups.items():
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if not isinstance(item, dict) or OVERVIEW_ADD_APPROVAL_CONFIG_KEY not in item:
                    continue
                if not isinstance(item[OVERVIEW_ADD_APPROVAL_CONFIG_KEY], bool):
                    raise ValueError(
                        f"{dimension} / {group_name} 第 {index + 1} 项的 "
                        f"{OVERVIEW_ADD_APPROVAL_CONFIG_KEY} 必须是布尔值"
                    )


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _service(user_service=None):
    return user_service or getattr(app.state, "user_service", None)


def _supersede_current_assignment(service, request_id: str, assignment: dict[str, Any]) -> None:
    task_key = str(assignment.get("current_task_key") or "").strip()
    if task_key:
        service.supersede_pending_work_assignments(
            module="project_overview",
            entity_id=request_id,
            task_key=task_key,
        )


def _safe_staging_path(value: object) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).resolve()
    if not path.is_relative_to(OVERVIEW_ADD_STAGING_DIR.resolve()):
        return None
    return path


def _cleanup_staged_file(request: dict[str, Any]) -> None:
    payload = request.get("payload", {})
    if not isinstance(payload, dict):
        return
    staged_path = _safe_staging_path(payload.get("staged_file_path"))
    if staged_path is None:
        return
    staged_path.unlink(missing_ok=True)
    try:
        staged_path.parent.rmdir()
    except OSError:
        pass


async def _insert_request(record: dict[str, Any]) -> bool:
    request_id = str(record["id"])
    project = str(record.get("project") or "")
    label = str(record.get("label") or "")
    payload = record.get("payload", {})
    candidate = payload.get("chip", {}) if isinstance(payload, dict) else {}

    def is_same_candidate(other: object) -> bool:
        if not isinstance(other, dict) or other.get("status") != "pending":
            return False
        if str(other.get("project") or "") != project or str(other.get("label") or "") != label:
            return False
        other_payload = other.get("payload", {})
        other_chip = other_payload.get("chip", {}) if isinstance(other_payload, dict) else {}
        if not isinstance(candidate, dict) or not isinstance(other_chip, dict):
            return False
        if candidate.get("type") == "test":
            return (
                other_chip.get("content") == candidate.get("content")
                and other_chip.get("test_select_data") == candidate.get("test_select_data")
                and other_chip.get("row_id") == candidate.get("row_id")
            )
        if candidate.get("type") == "svn":
            return (
                other_chip.get("content") == candidate.get("content")
                and other_chip.get("warehouse") == candidate.get("warehouse")
                and other_chip.get("row_id") == candidate.get("row_id")
            )
        return (
            other_chip.get("content") == candidate.get("content")
            and other_chip.get("row_id") == candidate.get("row_id")
        )

    def insert(records):
        records = records or {}
        if request_id in records or any(is_same_candidate(other) for other in records.values()):
            return db_storage.ATOMIC_NO_UPDATE
        records[request_id] = copy.deepcopy(record)
        return records

    return await db_storage.atomic_deep_update([OVERVIEW_ADD_REQUESTS_KEY], insert)


async def save_or_submit_overview_addition(
    *,
    project: str,
    label: str,
    chip: dict[str, Any],
    config: dict[str, Any],
    requester_username: str,
    requester_role: str,
    uploaded_file_path: Path | None = None,
    impact_mode: str = "none",
    related_labels: list[str] | None = None,
    user_service=None,
) -> OverviewAddSaveResult:
    """普通概述直接保存；关键概述只保存审批候选快照。"""
    if not requires_overview_add_approval(config):
        saved = await overview_storage.set_deep_item(
            [f"{project}_over_data", label, str(chip.get("id") or "")],
            copy.deepcopy(chip),
            raise_on_failure=False,
        )
        return OverviewAddSaveResult(
            "saved" if saved else "error",
            "内容已添加" if saved else "概述保存失败",
        )

    service = _service(user_service)
    if service is None:
        return OverviewAddSaveResult("error", "关键概述必须使用数据库审批流程，当前身份服务不可用")

    request_id = str(uuid.uuid4())
    staged_file_path = ""
    final_file_path = ""
    source_path: Path | None = None
    assignment: dict[str, Any] | None = None
    try:
        if uploaded_file_path is not None:
            source_path = uploaded_file_path.resolve()
            if not source_path.is_file():
                return OverviewAddSaveResult("error", "待审批上传文件已不存在")
            staging_dir = OVERVIEW_ADD_STAGING_DIR / request_id
            staging_dir.mkdir(parents=True, exist_ok=False)
            staged_path = staging_dir / source_path.name
            shutil.move(str(source_path), str(staged_path))
            staged_file_path = str(staged_path)
            final_file_path = str(source_path)

        workflow_result = create_approval_sequence_assignments(
            service,
            module="project_overview",
            event=OVERVIEW_ADD_WORKFLOW_EVENT,
            entity_id=request_id,
            task_key=OVERVIEW_ADD_TASK_KEY,
            requester_username=requester_username,
        )
        if workflow_result.get("status") != "matched":
            if staged_file_path:
                _cleanup_staged_file({"payload": {"staged_file_path": staged_file_path}})
            return OverviewAddSaveResult(
                "error",
                overview_workflow_error_message(workflow_result, "关键概述新增申请"),
            )
        raw_assignment = workflow_result.get("assignment")
        if not isinstance(raw_assignment, dict):
            if staged_file_path:
                _cleanup_staged_file({"payload": {"staged_file_path": staged_file_path}})
            return OverviewAddSaveResult("error", "关键概述新增申请未生成有效审批快照")
        assignment = raw_assignment

        now_text = _now_text()
        normalized_impact_mode, normalized_related_labels = normalize_overview_add_impacts(
            config,
            impact_mode,
            related_labels,
        )
        record: dict[str, Any] = {
            "id": request_id,
            "project": project,
            "label": label,
            "title": str(config.get("title") or label),
            "submitter": requester_username,
            "submitter_role": requester_role,
            "status": "pending",
            "created_at": now_text,
            "updated_at": now_text,
            "workflow_assignment": copy.deepcopy(assignment),
            "review_log": [],
            "reject_reason": "",
            "payload": {
                "chip": copy.deepcopy(chip),
                "config": copy.deepcopy(config),
                "staged_file_path": staged_file_path,
                "final_file_path": final_file_path,
                "impact_mode": normalized_impact_mode,
                "related_labels": normalized_related_labels,
            },
        }
        if not await _insert_request(record):
            _supersede_current_assignment(service, request_id, assignment)
            _cleanup_staged_file(record)
            return OverviewAddSaveResult("error", "关键概述新增申请保存失败")
        return OverviewAddSaveResult("pending", "已提交审批，通过后才会生效", request_id)
    except Exception as exc:
        if assignment is not None:
            _supersede_current_assignment(service, request_id, assignment)
        if staged_file_path:
            _cleanup_staged_file({"payload": {"staged_file_path": staged_file_path}})
        return OverviewAddSaveResult("error", f"关键概述新增申请提交失败：{exc}")


def can_review_overview_add_request(
    request: object,
    username: str,
    *,
    user_service=None,
) -> bool:
    """只有当前节点的具体待办人员可以审批。"""
    if not isinstance(request, dict) or request.get("status") != "pending" or not username:
        return False
    assignment = request.get("workflow_assignment")
    request_id = str(request.get("id") or "")
    if not isinstance(assignment, dict) or not request_id:
        return False
    service = _service(user_service)
    if service is None:
        return False
    task_key = str(assignment.get("current_task_key") or "")
    return bool(
        task_key
        and is_assigned_approver(
            service,
            module="project_overview",
            entity_id=request_id,
            task_key=task_key,
            username=username,
        )
    )


def get_overview_add_pending_count(
    requests: object,
    username: str,
    *,
    user_service=None,
) -> int:
    if not isinstance(requests, dict):
        return 0
    return sum(
        1
        for request in requests.values()
        if (
            isinstance(request, dict)
            and (
                can_review_overview_add_request(request, username, user_service=user_service)
                or (
                    request.get("submitter") == username
                    and request.get("status") in {"rejected", "failed"}
                )
            )
        )
    )


async def _update_request(request_id: str, changes: dict[str, Any]) -> bool:
    def update(request):
        if not request:
            return db_storage.ATOMIC_NO_UPDATE
        request.update(copy.deepcopy(changes))
        request["updated_at"] = _now_text()
        return request

    return await db_storage.atomic_deep_update(
        [OVERVIEW_ADD_REQUESTS_KEY, request_id],
        update,
    )


def _refresh_candidate_versions(chip: dict[str, Any], project: str) -> None:
    from .overview_batch_operations import build_select_activ_dic

    req_max_ver = str(app.storage.general.get("project_req_max_ver", {}).get(project, "0.0"))
    select_activ_dic = build_select_activ_dic(req_max_ver)
    chip["req_ver"] = req_max_ver
    chip["select_activ_dic"] = select_activ_dic
    timestamps = chip.get("timestamp")
    if isinstance(timestamps, dict) and timestamps:
        first_record = next(iter(timestamps.values()))
        if isinstance(first_record, dict):
            first_record["select_activ_dic"] = copy.deepcopy(select_activ_dic)


async def _execute_approved_add(request: dict[str, Any]) -> tuple[bool, str]:
    from .components import OverviewVersionManager
    from .overview_batch_operations import (
        apply_related_overview_impacts,
        insert_overview_chip,
        is_table_child_state_allowed,
    )
    from .utils import overview_role_update, refresh_overview_pending_labels

    project = str(request.get("project") or "")
    label = str(request.get("label") or "")
    payload = request.get("payload")
    if not project or not label or not isinstance(payload, dict):
        return False, "申请数据不完整"
    chip = copy.deepcopy(payload.get("chip"))
    config_snapshot = payload.get("config")
    if not isinstance(chip, dict) or not isinstance(config_snapshot, dict):
        return False, "申请候选内容不完整"
    live_config = app.storage.general.get("over_config_data_flat", {}).get(label)
    config = copy.deepcopy(live_config) if isinstance(live_config, dict) else copy.deepcopy(config_snapshot)
    for key in ("role", "group_name", "is_table_group", "first_col_label"):
        if key not in config and key in config_snapshot:
            config[key] = config_snapshot[key]
    submitter = str(request.get("submitter") or "")
    submitter_role = str(request.get("submitter_role") or "")
    if not can_edit_overview_item(config, submitter_role, submitter):
        return False, "申请人已不再具有该概述项的编辑权限"
    project_state = app.storage.general.get("project_summary", {}).get(project, {}).get("state")
    allowed_states = config.get("allowed_state")
    if isinstance(allowed_states, list) and allowed_states and project_state not in allowed_states:
        return False, "项目当前状态已不允许添加该概述"

    _refresh_candidate_versions(chip, project)
    req_max_ver = str(chip.get("req_ver") or "0.0")
    if (
        config.get("is_table_group") is True
        and label != str(config.get("first_col_label") or "")
        and not is_table_child_state_allowed(
            project,
            str(config.get("first_col_label") or ""),
            chip.get("row_id"),
            req_max_ver,
            True,
        )
    ):
        return False, "表格绑定行已不存在、失活或不允许新增激活项"

    staged_path = _safe_staging_path(payload.get("staged_file_path"))
    final_path_text = str(payload.get("final_file_path") or "").strip()
    installed_file: Path | None = None
    if staged_path is not None:
        if not staged_path.is_file() or not final_path_text:
            return False, "待审批上传文件已不存在"
        upload_root_text = str(config.get("upload_path") or "").strip()
        if not upload_root_text:
            return False, "概述项未配置正式上传目录"
        expected_final_path = (Path(upload_root_text) / Path(str(chip.get("content") or "")).name).resolve()
        final_path = Path(final_path_text).resolve()
        if final_path != expected_final_path:
            return False, "待审批上传文件的正式保存路径无效"
        if final_path.exists():
            return False, "审批期间正式目录出现同名文件，已拒绝覆盖"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_path), str(final_path))
        installed_file = final_path

    try:
        inserted, message = await insert_overview_chip(project, label, chip)
    except Exception as exc:
        if installed_file is not None and staged_path is not None and installed_file.is_file():
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(installed_file), str(staged_path))
        return False, f"概述写入失败：{exc}"
    if not inserted:
        if installed_file is not None and staged_path is not None and installed_file.is_file():
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(installed_file), str(staged_path))
        return False, message
    if staged_path is not None:
        try:
            staged_path.parent.rmdir()
        except OSError:
            pass
    OverviewVersionManager.bump(project, label)
    _, related_labels = normalize_overview_add_impacts(
        config,
        payload.get("impact_mode"),
        payload.get("related_labels"),
    )
    changed_related: set[str] = set()
    if related_labels:
        changed_related = await apply_related_overview_impacts(
            project=project,
            related_labels=related_labels,
            source_content=str(chip.get("content") or ""),
            source_state=True,
            operation_type="add_chip",
            creator=submitter,
            config_flat=app.storage.general.get("over_config_data_flat", {}),
            overview_role=app.storage.general.get("overview_role", {}),
        )
        for related_label in changed_related:
            OverviewVersionManager.bump(project, related_label)
        refresh_overview_pending_labels(project, changed_related)
    try:
        overview_role_update(project, str(config.get("role") or ""))
    except Exception:
        logger.warning("关键概述生效后刷新负责人信息失败：%s / %s", project, label, exc_info=True)
    if changed_related:
        return True, f"审批通过，概述已生效，并将 {len(changed_related)} 个关联概述项转为待确认"
    return True, "审批通过，概述已生效"


async def approve_overview_add_request(
    request_id: str,
    reviewer: str,
    *,
    user_service=None,
) -> dict[str, Any]:
    """推进当前审批节点；全部节点通过后才把候选概述写入正式数据。"""
    service = _service(user_service)
    if service is None:
        return {"status": "error", "message": "身份服务不可用"}
    async with _review_locks[request_id]:
        request = db_storage.get_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id], {})
        if not can_review_overview_add_request(request, reviewer, user_service=service):
            return {"status": "forbidden", "message": "申请已处理或当前账号没有审批待办"}
        assignment = request.get("workflow_assignment")
        if not isinstance(assignment, dict):
            return {"status": "error", "message": "审批快照无效"}
        advance_result = advance_approval_sequence(
            service,
            module="project_overview",
            entity_id=request_id,
            assignment=assignment,
            username=reviewer,
        )
        advance_status = str(advance_result.get("status") or "")
        updated_assignment = advance_result.get("assignment")
        if advance_status not in {"node_pending", "advanced", "completed"} or not isinstance(
            updated_assignment,
            dict,
        ):
            return {
                "status": "error",
                "message": str(advance_result.get("message") or "审批节点推进失败"),
            }
        review_log = list(request.get("review_log") or [])
        review_log.append(
            {
                "action": "approve",
                "user": reviewer,
                "time": _now_text(),
                "node_index": assignment.get("current_node_index", 0),
            }
        )
        if advance_status != "completed":
            await _update_request(
                request_id,
                {
                    "workflow_assignment": updated_assignment,
                    "review_log": review_log,
                },
            )
            return {
                "status": advance_status,
                "message": str(advance_result.get("message") or "当前审批节点已处理"),
                "assignment": updated_assignment,
            }

        try:
            effective, message = await _execute_approved_add(request)
        except Exception as exc:
            logger.error("关键概述审批通过后的生效处理失败：%s", request_id, exc_info=True)
            effective, message = False, f"审批已完成，但概述生效失败：{exc}"
        final_status = "approved" if effective else "failed"
        await _update_request(
            request_id,
            {
                "status": final_status,
                "workflow_assignment": updated_assignment,
                "review_log": review_log,
                "reviewer": reviewer,
                "finished_at": _now_text(),
                "result": {"ok": effective, "message": message},
            },
        )
        return {"status": final_status, "message": message, "assignment": updated_assignment}


async def reject_overview_add_request(
    request_id: str,
    reviewer: str,
    reason: str,
    *,
    user_service=None,
) -> dict[str, str]:
    service = _service(user_service)
    if service is None:
        return {"status": "error", "message": "身份服务不可用"}
    async with _review_locks[request_id]:
        request = db_storage.get_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id], {})
        if not can_review_overview_add_request(request, reviewer, user_service=service):
            return {"status": "forbidden", "message": "申请已处理或当前账号没有审批待办"}
        assignment = request.get("workflow_assignment")
        if not isinstance(assignment, dict):
            return {"status": "error", "message": "审批快照无效"}
        _supersede_current_assignment(service, request_id, assignment)
        rejected_assignment = copy.deepcopy(assignment)
        rejected_assignment["status"] = "rejected"
        review_log = list(request.get("review_log") or [])
        review_log.append(
            {"action": "reject", "user": reviewer, "note": reason, "time": _now_text()}
        )
        await _update_request(
            request_id,
            {
                "status": "rejected",
                "reject_reason": reason,
                "workflow_assignment": rejected_assignment,
                "review_log": review_log,
            },
        )
        return {"status": "rejected", "message": "关键概述新增申请已驳回"}


async def resubmit_overview_add_request(
    request_id: str,
    submitter: str,
    *,
    chip: dict[str, Any],
    config: dict[str, Any],
    uploaded_file_path: Path | None = None,
    impact_mode: str = "none",
    related_labels: list[str] | None = None,
    user_service=None,
) -> OverviewAddSaveResult:
    """使用项目概述页重新录入的候选内容，沿用原申请记录再次送审。"""
    service = _service(user_service)
    if service is None:
        return OverviewAddSaveResult("error", "身份服务不可用", request_id)

    async with _review_locks[request_id]:
        request = db_storage.get_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id], {})
        if (
            not isinstance(request, dict)
            or request.get("submitter") != submitter
            or request.get("status") != "rejected"
        ):
            return OverviewAddSaveResult("error", "申请状态已变化，当前无法重新提交", request_id)
        payload = request.get("payload")
        if not isinstance(payload, dict):
            return OverviewAddSaveResult("error", "申请候选内容不完整", request_id)
        label = str(request.get("label") or "")
        if not requires_overview_add_approval(config):
            return OverviewAddSaveResult("error", "该概述项当前已不需要新增审批，请撤回后重新添加", request_id)
        if not can_edit_overview_item(config, str(request.get("submitter_role") or ""), submitter):
            return OverviewAddSaveResult("error", "当前账号已无权编辑该概述项", request_id)
        if label != str(config.get("label") or label):
            return OverviewAddSaveResult("error", "重新提交的概述项与原申请不一致", request_id)

        workflow_result = create_approval_sequence_assignments(
            service,
            module="project_overview",
            event=OVERVIEW_ADD_WORKFLOW_EVENT,
            entity_id=request_id,
            task_key=OVERVIEW_ADD_TASK_KEY,
            requester_username=submitter,
        )
        if workflow_result.get("status") != "matched":
            return OverviewAddSaveResult(
                "error",
                overview_workflow_error_message(workflow_result, "关键概述新增申请"),
                request_id,
            )
        assignment = workflow_result.get("assignment")
        if not isinstance(assignment, dict):
            return OverviewAddSaveResult("error", "关键概述新增申请未生成有效审批快照", request_id)

        old_staged_path = _safe_staging_path(payload.get("staged_file_path"))
        new_staged_path: Path | None = None
        final_file_path = ""
        if uploaded_file_path is not None:
            source_path = uploaded_file_path.resolve()
            if not source_path.is_file():
                _supersede_current_assignment(service, request_id, assignment)
                return OverviewAddSaveResult("error", "重新上传的文件已不存在", request_id)
            staging_dir = OVERVIEW_ADD_STAGING_DIR / request_id
            staging_dir.mkdir(parents=True, exist_ok=True)
            new_staged_path = staging_dir / f"replacement-{uuid.uuid4().hex}-{source_path.name}"
            try:
                shutil.move(str(source_path), str(new_staged_path))
            except OSError as exc:
                _supersede_current_assignment(service, request_id, assignment)
                return OverviewAddSaveResult("error", f"重新暂存送审文件失败：{exc}", request_id)
            final_file_path = str(source_path)

        review_log = list(request.get("review_log") or [])
        review_log.append(
            {
                "action": "resubmit",
                "user": submitter,
                "time": _now_text(),
                "note": "修改后重新提交",
            }
        )
        updated_payload = copy.deepcopy(payload)
        normalized_impact_mode, normalized_related_labels = normalize_overview_add_impacts(
            config,
            impact_mode,
            related_labels,
        )
        updated_payload.update(
            {
                "chip": copy.deepcopy(chip),
                "config": copy.deepcopy(config),
                "staged_file_path": str(new_staged_path or ""),
                "final_file_path": final_file_path,
                "impact_mode": normalized_impact_mode,
                "related_labels": normalized_related_labels,
            }
        )
        saved = await _update_request(
            request_id,
            {
                "status": "pending",
                "reject_reason": "",
                "workflow_assignment": copy.deepcopy(assignment),
                "review_log": review_log,
                "payload": updated_payload,
                "reviewer": "",
                "finished_at": "",
                "result": {},
            },
        )
        if not saved:
            _supersede_current_assignment(service, request_id, assignment)
            if new_staged_path is not None:
                new_staged_path.unlink(missing_ok=True)
            return OverviewAddSaveResult("error", "申请重新提交失败，请稍后重试", request_id)
        if old_staged_path is not None and old_staged_path != new_staged_path:
            old_staged_path.unlink(missing_ok=True)
            try:
                old_staged_path.parent.rmdir()
            except OSError:
                pass
        return OverviewAddSaveResult("pending", "申请已修改并重新提交审批", request_id)


async def withdraw_overview_add_request(
    request_id: str,
    submitter: str,
    *,
    user_service=None,
) -> dict[str, str]:
    service = _service(user_service)
    async with _review_locks[request_id]:
        request = db_storage.get_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id], {})
        if (
            not isinstance(request, dict)
            or request.get("submitter") != submitter
            or request.get("status") not in {"pending", "rejected", "failed"}
        ):
            return {"status": "forbidden", "message": "申请状态已变化，当前无法撤销"}
        assignment = request.get("workflow_assignment")
        if service is not None and isinstance(assignment, dict):
            _supersede_current_assignment(service, request_id, assignment)
        _cleanup_staged_file(request)
        deleted = await db_storage.del_deep_item([OVERVIEW_ADD_REQUESTS_KEY, request_id])
        return {
            "status": "withdrawn" if deleted else "error",
            "message": "申请已撤销" if deleted else "申请撤销失败",
        }
