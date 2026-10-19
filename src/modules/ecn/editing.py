"""在最新单据上合并协作编辑；不依赖 NiceGUI 控件或页面快照写回。"""

import copy
from dataclasses import (
    dataclass,
)
from datetime import datetime
from typing import (
    Any,
)

from ...ecn_management_config import (
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION,
    ECN_OVERVIEW_ACTION_ADD,
    ECN_OVERVIEW_ACTION_DEACTIVATE,
    ECN_OVERVIEW_ACTION_UPDATE,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECNState,
    build_ecn_scheme_snapshot,
    classify_ecn_change_item,
    confirm_revised_scheme_items,
    has_unrevised_rejected_scheme_items,
    mark_rejected_scheme_item_revised,
    merge_ecn_impact_audit_log,
    register_ecn_impact_handler,
    get_ecn_scheme_target_projects,
)


class ECNConflict(ValueError):
    """业务条件已变化，拒绝保存并交由界面提示。"""


_ASSOCIATION_NOTICE_FIELDS = ("projects", "req_idxs", "linked_docs", "linked_materials")


def get_scheme_association_change_removed(item: dict) -> dict[str, list[str]]:
    """返回实际被删除的关联；空壳通知不应阻塞或出现在界面。"""
    notice = item.get("association_change_notice")
    if not isinstance(notice, dict):
        return {}
    removed = notice.get("removed")
    if not isinstance(removed, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for field in _ASSOCIATION_NOTICE_FIELDS:
        values = sorted(_string_set(removed.get(field, [])))
        if values:
            normalized[field] = values
    return normalized


def has_scheme_association_change_notice(item: dict) -> bool:
    return bool(get_scheme_association_change_removed(item))


@dataclass
class ECNResult:
    ok: bool
    message: str = ""
    record: dict | None = None


def workflow_context(record: dict) -> tuple:
    workflow = record.get("workflow", {})
    return tuple(
        workflow.get(key)
        for key in (
            "current_state",
            "current_phase",
            "current_step_index",
            "approval_round",
        )
    )


def require_current_context(current: dict, expected: dict) -> None:
    if not current:
        raise ECNConflict("单据已被删除，请关闭后刷新列表。")
    if workflow_context(current) != workflow_context(expected):
        raise ECNConflict("单据流程已变化，请关闭后重新打开再操作。")


def require_scheming(current: dict, expected: dict) -> None:
    require_current_context(current, expected)
    if current["workflow"]["current_state"] != ECNState.ECN_SCHEMING:
        raise ECNConflict("当前已不在方案编写阶段，不能继续修改或确认。")


def merge_fields(current: Any, baseline: Any, submitted: Any, path: str = "") -> Any:
    """三方合并：未改字段保留后台值；同字段不同修改拒绝；项目集合按增删合并。"""
    if submitted == baseline:
        return copy.deepcopy(current)
    if all(isinstance(value, dict) for value in (current, baseline, submitted)):
        result = copy.deepcopy(current)
        for key in baseline.keys() | submitted.keys():
            if key == "impact_change_log":
                continue
            result[key] = merge_fields(current.get(key), baseline.get(key), submitted.get(key), f"{path}/{key}")
        return result
    if path.rsplit("/", 1)[-1] in {"expanded_projects_mass", "expanded_projects_non_mass"}:
        removed = set(baseline or []) - set(submitted or [])
        added = [value for value in submitted or [] if value not in (baseline or [])]
        return list(dict.fromkeys([value for value in current or [] if value not in removed] + added))
    if current != baseline and current != submitted:
        raise ECNConflict(f"字段“{path.strip('/')}”已被其他页面修改，本次未保存，请刷新后核对。")
    return copy.deepcopy(submitted)


def sync_review_snapshot(local: dict, baseline: dict, fresh: dict) -> None:
    """轮询仅更新未编辑字段；保留正在输入的值和对应旧基线以便保存时检测冲突。"""
    for key in fresh.keys() | baseline.keys():
        if key == "impact_change_log":
            merge_ecn_impact_audit_log(local, fresh.get(key, []))
            baseline[key] = copy.deepcopy(fresh.get(key, []))
        elif all(isinstance(value, dict) for value in (local.get(key), baseline.get(key), fresh.get(key))):
            sync_review_snapshot(local[key], baseline[key], fresh[key])
        elif local.get(key) == baseline.get(key):
            local[key] = copy.deepcopy(fresh.get(key))
            baseline[key] = copy.deepcopy(fresh.get(key))


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item or "").strip()}


def get_scheme_association_scope(record: dict) -> dict[str, set[str]]:
    """返回当前单据允许方案关联的项目、要求、资料和物料。"""
    basic_info = record.get("basic_info", {})
    basic_info = basic_info if isinstance(basic_info, dict) else {}
    review_info = record.get("review_info", {})
    review_info = review_info if isinstance(review_info, dict) else {}
    requirements = basic_info.get("requirements", [])
    requirement_ids = {
        str(requirement.get("idx")).strip()
        for requirement in requirements if isinstance(requirements, list) and isinstance(requirement, dict)
        if requirement.get("idx") not in [None, ""]
    }
    involved_docs = review_info.get("involved_docs", {})
    involved_docs = involved_docs if isinstance(involved_docs, dict) else {}
    involved_materials = review_info.get("involved_materials", {})
    involved_materials = involved_materials if isinstance(involved_materials, dict) else {}
    return {
        "projects": set(get_ecn_scheme_target_projects(record)),
        "req_idxs": requirement_ids,
        "linked_docs": {str(name) for name, selected in involved_docs.items() if selected},
        "linked_materials": {
            f"{material}-{action}"
            for material, actions in involved_materials.items()
            if isinstance(actions, dict)
            for action, selected in actions.items()
            if selected
        },
    }


def validate_scheme_associations(record: dict, item: dict) -> None:
    """在最新单据上校验方案关联及系统内资料的逐项目配置。"""
    category = classify_ecn_change_item(item)
    if category not in {
        ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
        ECN_SCHEME_GROUP_ORDINARY_DOCUMENT,
        ECN_SCHEME_GROUP_MATERIAL,
    }:
        raise ECNConflict("方案类别无效，请重新打开后填写。")
    scope = get_scheme_association_scope(record)
    associations: dict[str, set[str]] = {}
    for field, label in (
        ("projects", "目标项目"),
        ("req_idxs", "变更要求"),
        ("linked_docs", "资料影响"),
        ("linked_materials", "物料影响"),
    ):
        raw = item.get(field, [])
        if not isinstance(raw, list):
            raise ECNConflict(f"{label}关联数据无效，请重新打开方案。")
        normalized = _string_set(raw)
        invalid = normalized - scope[field]
        if invalid:
            raise ECNConflict(f"{label}已发生变化，请移除失效关联：{'、'.join(sorted(invalid))}")
        associations[field] = normalized

    if not associations["projects"]:
        raise ECNConflict("每条方案必须至少关联一个目标项目。")

    if category == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
        project_states = item.get("project_states", {})
        if not isinstance(project_states, dict):
            raise ECNConflict("系统内资料项目配置无效，请重新打开方案。")
        state_projects = {
            str(project).strip()
            for project in project_states
            if str(project or "").strip()
        }
        if state_projects != associations["projects"]:
            raise ECNConflict("系统内资料的目标项目与逐项目配置不一致，请重新打开方案。")
        allowed_actions = {
            ECN_OVERVIEW_ACTION_ADD,
            ECN_OVERVIEW_ACTION_UPDATE,
            ECN_OVERVIEW_ACTION_DEACTIVATE,
        }
        if any(
            not isinstance(state, dict) or state.get("action") not in allowed_actions
            for state in project_states.values()
        ):
            raise ECNConflict("系统内资料存在无效的项目执行动作，请重新配置。")
        if associations["linked_materials"]:
            raise ECNConflict("系统内资料方案不能关联物料影响。")
    elif item.get("project_states") not in [None, {}]:
        raise ECNConflict("非系统内资料方案不能包含逐项目执行配置。")
    if category == ECN_SCHEME_GROUP_ORDINARY_DOCUMENT and associations["linked_materials"]:
        raise ECNConflict("资料变更方案不能关联物料影响。")
    if category == ECN_SCHEME_GROUP_MATERIAL and associations["linked_docs"]:
        raise ECNConflict("物料变更方案不能关联资料影响。")


def get_pending_overview_anchor_source(anchor_row_id: Any) -> str:
    """返回待新增基准行引用的方案 ID；普通既有行引用返回空字符串。"""
    text = str(anchor_row_id or "").strip()
    prefix = "PENDING_NEW_"
    return text[len(prefix):].strip() if text.startswith(prefix) else ""


def validate_overview_scheme_dependencies(record: dict) -> None:
    """保证表格子项引用的是同项目中仍会新增的基准列方案。"""
    items = record.get("change_items", [])
    if not isinstance(items, list):
        raise ECNConflict("方案数据无效，请刷新后重新检查。")
    item_by_id = {
        str(item.get("item_id") or "").strip(): item
        for item in items
        if isinstance(item, dict) and str(item.get("item_id") or "").strip()
    }
    for item in items:
        if not isinstance(item, dict) or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
            continue
        item_id = str(item.get("item_id") or "").strip()
        label = str(item.get("label") or "").strip()
        first_col_label = str(item.get("first_col_label") or "").strip()
        project_states = item.get("project_states", {})
        if not isinstance(project_states, dict):
            continue
        for project, state in project_states.items():
            if not isinstance(state, dict) or state.get("action") != ECN_OVERVIEW_ACTION_ADD:
                continue
            source_id = get_pending_overview_anchor_source(state.get("anchor_row_id"))
            if not source_id:
                if first_col_label and label != first_col_label:
                    row_id = str(state.get("anchor_row_id") or "")
                    if not row_id:
                        raise ECNConflict("新增子参数必须绑定有效的表格基准行。")
                    for base_item in items:
                        if not isinstance(base_item, dict) or base_item.get("label") != first_col_label:
                            continue
                        base_state = base_item.get("project_states", {}).get(project, {})
                        old_data = base_state.get("old_data", {})
                        if (base_state.get("action") == ECN_OVERVIEW_ACTION_DEACTIVATE
                            and isinstance(old_data, dict) and str(old_data.get("row_id") or "") == row_id):
                            raise ECNConflict(f"项目 {project} 的基准行已有失效方案，不能继续绑定新增子参数。")
                continue
            source = item_by_id.get(source_id)
            source_states = source.get("project_states", {}) if isinstance(source, dict) else {}
            source_state = source_states.get(project) if isinstance(source_states, dict) else None
            valid_source = (
                source_id != item_id
                and isinstance(source, dict)
                and classify_ecn_change_item(source) == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT
                and str(source.get("label") or "").strip() == first_col_label
                and str(source.get("first_col_label") or "").strip() == first_col_label
                and label != first_col_label
                and isinstance(source_state, dict)
                and source_state.get("action") == ECN_OVERVIEW_ACTION_ADD
            )
            if not valid_source:
                raise ECNConflict(
                    f"方案 {item_id or '（未编号）'} 在项目 {project} 关联的新增基准行已失效，"
                    "请重新选择表格基准行。"
                )


def invalidate_scheme_validation_report(item: dict) -> None:
    """方案业务内容变化后保留报告附件，但撤销旧内容对应的审批结论。"""
    report = item.get("validation_report")
    if not isinstance(report, dict) or report.get("required") is not True:
        return
    attachments = report.get("attachments", [])
    report["status"] = "pending_review" if isinstance(attachments, list) and attachments else "pending_upload"
    for key in ("reviewed_by", "reviewed_at", "review_note"):
        report.pop(key, None)


def reconcile_removed_scheme_associations(record: dict, user: str) -> set[str]:
    """移除已退出影响范围的关联，并要求受影响方案作者重新查看确认。"""
    scope = get_scheme_association_scope(record)
    affected_authors: set[str] = set()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    labels = {
        "projects": "项目",
        "req_idxs": "变更要求",
        "linked_docs": "资料",
        "linked_materials": "物料",
    }
    items = record.get("change_items", [])
    if not isinstance(items, list):
        return affected_authors
    for item in items:
        if not isinstance(item, dict):
            continue
        removed: dict[str, list[str]] = {}
        for field in labels:
            raw = item.get(field, [])
            values = [str(value).strip() for value in raw] if isinstance(raw, list) else []
            invalid = [value for value in values if value and value not in scope[field]]
            if invalid:
                item[field] = [value for value in values if value in scope[field]]
                removed[field] = invalid
        project_states = item.get("project_states", {})
        if isinstance(project_states, dict):
            invalid_state_projects = [
                str(project) for project in project_states if str(project) not in scope["projects"]
            ]
            for project in invalid_state_projects:
                project_states.pop(project, None)
            if invalid_state_projects:
                removed.setdefault("projects", []).extend(invalid_state_projects)
        if not removed:
            continue
        invalidate_scheme_validation_report(item)
        previous_removed = get_scheme_association_change_removed(item)
        merged_removed: dict[str, list[str]] = {}
        for field in labels:
            merged_removed[field] = sorted(
                _string_set(previous_removed.get(field, []))
                | _string_set(removed.get(field, []))
            )
        item["association_change_notice"] = {
            "removed": merged_removed,
            "changed_by": user,
            "changed_at": now,
        }
        author = str(item.get("author") or "").strip()
        if author:
            affected_authors.add(author)
    participants = record.setdefault("workflow", {}).setdefault("scheme_participants", {})
    if isinstance(participants, dict):
        for author in affected_authors:
            if participants.get(author) != ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION:
                participants[author] = ECN_PARTICIPANT_STATUS_EDITING
    return affected_authors


def update_review(current: dict, expected: dict, baseline: dict, submitted: dict, user: str) -> dict:
    require_scheming(current, expected)
    current["review_info"] = merge_fields(current.get("review_info", {}), baseline, submitted)
    merge_ecn_impact_audit_log(current["review_info"], submitted.get("impact_change_log", []))
    reconcile_removed_scheme_associations(current, user)
    register_ecn_impact_handler(current, user, current["review_info"])
    return current


def save_scheme(current: dict, expected: dict, item: dict, original: dict | None, user: str) -> dict:
    require_scheming(current, expected)
    items = current.setdefault("change_items", [])
    existing = next((value for value in items if value.get("item_id") == item.get("item_id")), None)
    if item.get("author") != user:
        raise ECNConflict("只能保存本人编写的方案。")
    validate_scheme_associations(current, item)
    if original is not None:
        if existing is None or existing.get("author") != user:
            raise ECNConflict("方案已被删除或不属于本人，请刷新。")
        if existing != original:
            raise ECNConflict("该方案在打开编辑窗口后已被修改或确认，本次未保存，请重新打开编辑。")
    elif existing is not None:
        raise ECNConflict("方案已经保存，请勿重复添加。")
    updated = copy.deepcopy(item)
    if existing is not None:
        if existing.get("rejection_history") and build_ecn_scheme_snapshot(existing) == build_ecn_scheme_snapshot(
            updated
        ):
            raise ECNConflict("被驳回方案的内容尚未修改，请完成整改后再保存。")
        updated["review_status"] = existing.get("review_status")
        updated["rejection_history"] = copy.deepcopy(existing.get("rejection_history", []))
        validation_report = existing.get("validation_report", {})
        if isinstance(validation_report, dict) and validation_report:
            updated["validation_report"] = copy.deepcopy(validation_report)
            invalidate_scheme_validation_report(updated)
        if updated["rejection_history"]:
            updated["review_status"] = ECN_ITEM_STATUS_NEEDS_IMPROVEMENT
        mark_rejected_scheme_item_revised(updated)
        items[items.index(existing)] = updated
    else:
        updated.pop("validation_report", None)
        items.append(updated)
    validate_overview_scheme_dependencies(current)
    current["workflow"].setdefault("scheme_participants", {})[user] = (
        ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
        if updated.get("review_status") == ECN_ITEM_STATUS_REVISED_PENDING_CONFIRMATION
        else ECN_PARTICIPANT_STATUS_EDITING
    )
    return current


def assign_scheme_writers(
    current: dict,
    expected: dict,
    assignees: list[str],
) -> dict:
    """并发安全地替换明确指定的方案填写人，并同步其参与状态。"""
    require_scheming(current, expected)
    current_workflow = current.setdefault("workflow", {})
    expected_workflow = expected.get("workflow", {})
    expected_workflow = expected_workflow if isinstance(expected_workflow, dict) else {}
    current_assignees = current_workflow.get("scheme_assignees", [])
    current_assignees = current_assignees if isinstance(current_assignees, list) else []
    expected_assignees = expected_workflow.get("scheme_assignees", [])
    expected_assignees = expected_assignees if isinstance(expected_assignees, list) else []
    if current_assignees != expected_assignees:
        raise ECNConflict("方案填写人已被其他页面调整，请刷新后重新选择。")

    normalized = list(dict.fromkeys(name.strip() for name in assignees if name.strip()))
    previous = {str(name) for name in current_assignees if str(name).strip()}
    selected = set(normalized)
    participants = current_workflow.setdefault("scheme_participants", {})
    if not isinstance(participants, dict):
        raise ECNConflict("方案参与人数据异常，请刷新后重试。")
    for name in selected - previous:
        participants[name] = ECN_PARTICIPANT_STATUS_EDITING
    authors = {
        str(item.get("author") or "").strip()
        for item in current.get("change_items", [])
        if isinstance(item, dict) and str(item.get("author") or "").strip()
    }
    for name in previous - selected:
        if name not in authors:
            participants.pop(name, None)
    current_workflow["scheme_assignees"] = normalized
    return current


def delete_scheme(current: dict, expected: dict, original: dict, user: str) -> dict:
    require_scheming(current, expected)
    items = current.get("change_items", [])
    existing = next((value for value in items if value.get("item_id") == original.get("item_id")), None)
    if existing is None or existing.get("author") != user or existing != original:
        raise ECNConflict("方案已变化或不属于本人，请刷新后再删除。")
    items.remove(existing)
    validate_overview_scheme_dependencies(current)
    participants = current["workflow"].setdefault("scheme_participants", {})
    if any(value.get("author") == user for value in items):
        participants[user] = ECN_PARTICIPANT_STATUS_EDITING
    else:
        participants.pop(user, None)
    return current


def acknowledge_scheme_association_change(
    current: dict,
    expected: dict,
    item_id: str,
    user: str,
) -> dict:
    """方案作者确认影响范围删减后仍保留该方案。"""
    require_scheming(current, expected)
    items = current.get("change_items", [])
    item = next(
        (
            value
            for value in items
            if isinstance(value, dict) and str(value.get("item_id") or "") == str(item_id)
        ),
        None,
    ) if isinstance(items, list) else None
    if not isinstance(item, dict) or item.get("author") != user:
        raise ECNConflict("受影响方案已变化或不属于本人，请刷新后重试。")
    expected_items = expected.get("change_items", [])
    expected_item = next(
        (
            value
            for value in expected_items
            if isinstance(value, dict) and str(value.get("item_id") or "") == str(item_id)
        ),
        None,
    ) if isinstance(expected_items, list) else None
    if not isinstance(expected_item, dict) or item != expected_item:
        raise ECNConflict("受影响方案或删除提示已更新，请刷新后重新核对。")
    if not has_scheme_association_change_notice(item):
        raise ECNConflict("该方案已完成影响范围复核，请刷新。")
    validate_scheme_associations(current, item)
    item.pop("association_change_notice", None)
    participants = current.setdefault("workflow", {}).setdefault("scheme_participants", {})
    if isinstance(participants, dict) and participants.get(user) != ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION:
        participants[user] = ECN_PARTICIPANT_STATUS_EDITING
    return current


def confirm_participant(current: dict, expected: dict, user: str, status: str) -> dict:
    require_scheming(current, expected)
    if status not in {ECN_PARTICIPANT_STATUS_CONFIRMED, ECN_PARTICIPANT_STATUS_EDITING}:
        raise ECNConflict("无效的方案确认状态。")
    if status == ECN_PARTICIPANT_STATUS_CONFIRMED:

        def own_items(record):
            return [item for item in record.get("change_items", []) if item.get("author") == user]

        if not own_items(current):
            raise ECNConflict("请先添加至少一条本人方案，再确认完成。")
        if any(has_scheme_association_change_notice(item) for item in own_items(current)):
            raise ECNConflict("存在因影响范围删除而需要确认的方案，请先确认保留、修改或删除。")
        if own_items(current) != own_items(expected):
            raise ECNConflict("本人的方案已变化，请核对最新内容后再确认。")
        if ECN_REQUIRE_REVISION_BEFORE_RECONFIRMATION and has_unrevised_rejected_scheme_items(current, user):
            raise ECNConflict("仍有被驳回方案尚未修改，请先整改后再确认。")
        confirm_revised_scheme_items(current, user)
    current["workflow"].setdefault("scheme_participants", {})[user] = status
    return current
