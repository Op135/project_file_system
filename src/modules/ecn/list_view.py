# -*- encoding: utf-8 -*-
from datetime import (
    datetime,
)
from typing import (
    Any,
)

from ...config import (
    ECNState,
)
from ...ecn_access import (
    build_ecn_access_snapshot,
    can_execute_ecn_assistant_stage,
    get_ecn_execution_assignment_issues,
    is_ecn_pending_for_user,
)
from ...ecn_management_config import (
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
    ECN_TRACEABILITY_LEVELS,
    get_ecn_execution_pending_role_keywords,
    get_ecn_execution_pending_usernames,
    get_ecn_pending_approval_roles,
    get_ecn_scheme_coverage,
    get_ecn_scheme_target_projects,
    get_ecn_traceability_closure_summary,
    get_ecn_special_confirmations,
)


def get_ecn_list_progress_summary(ecn_data: Any) -> str:
    """生成ECN首页表格使用的简短流程进度说明。"""
    if not isinstance(ecn_data, dict):
        return "—"
    workflow = ecn_data.get("workflow", {})
    workflow = workflow if isinstance(workflow, dict) else {}
    current_state = str(workflow.get("current_state") or "")
    pending_roles = get_ecn_pending_approval_roles(workflow)
    if pending_roles and current_state not in [
        ECNState.DRAFT,
        ECNState.CLOSED,
        ECNState.CANCEL,
        ECNState.REJECTED,
    ]:
        return f"等待审批：{'、'.join(pending_roles)}"

    if current_state == ECNState.ECN_EXECUTING:
        execution_assignees = [
            *get_ecn_execution_pending_usernames(ecn_data),
            *get_ecn_execution_pending_role_keywords(ecn_data),
        ]
        return f"等待执行确认：{'、'.join(execution_assignees)}" if execution_assignees else "执行处理中"

    if current_state != ECNState.ECN_SCHEMING:
        return "—"
    participants = workflow.get("scheme_participants", {})
    participants = participants if isinstance(participants, dict) else {}
    needs_reconfirmation = [
        str(person) for person, status in participants.items() if status == ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION
    ]
    if needs_reconfirmation:
        return f"方案待改进/重新确认：{'、'.join(needs_reconfirmation)}"
    editing_participants = [
        str(person)
        for person, status in participants.items()
        if status
        not in [
            ECN_PARTICIPANT_STATUS_CONFIRMED,
            ECN_PARTICIPANT_STATUS_NEEDS_RECONFIRMATION,
        ]
    ]
    if editing_participants:
        return f"等待完成方案编写：{'、'.join(editing_participants)}"

    coverage = get_ecn_scheme_coverage(ecn_data)
    missing_labels = []
    if coverage["missing_requirements"]:
        missing_labels.append("变更要求")
    if coverage["missing_docs"]:
        missing_labels.append("资料")
    if coverage["missing_materials"]:
        missing_labels.append("物料")
    if coverage["incomplete_material_schemes"]:
        missing_labels.append("物料追溯/处置配置")
    if missing_labels:
        return f"尚缺{'、'.join(missing_labels)}方案，待补充"
    return "方案已齐，待发起评审" if participants else "等待方案编写人员"


def build_ecn_management_grid_row(
    ecn_data: Any,
    current_user: str,
    current_role: str,
    *,
    include_delete: bool = False,
    user_service=None,
    access_snapshot: dict[str, Any] | None = None,
) -> dict[str, object]:
    """把ECN记录整理为首页AG Grid行数据。"""
    if not isinstance(ecn_data, dict):
        return {}
    basic_info = ecn_data.get("basic_info", {})
    basic_info = basic_info if isinstance(basic_info, dict) else {}
    workflow = ecn_data.get("workflow", {})
    workflow = workflow if isinstance(workflow, dict) else {}
    execution_info = ecn_data.get("execution_info", {})
    execution_info = execution_info if isinstance(execution_info, dict) else {}
    current_state = str(workflow.get("current_state") or "")
    snapshot = access_snapshot or build_ecn_access_snapshot(user_service)
    assignment_issues = get_ecn_execution_assignment_issues(
        ecn_data, user_service=user_service, access_snapshot=snapshot
    )
    is_my_pending = is_ecn_pending_for_user(
        ecn_data,
        current_user,
        current_role,
        user_service=user_service,
        access_snapshot=snapshot,
        assignment_issues=assignment_issues,
    )
    can_reassign = bool(assignment_issues) and can_execute_ecn_assistant_stage(
        current_role,
        current_user,
        user_service=user_service,
        access_snapshot=snapshot,
    )
    traceability_summary = get_ecn_traceability_closure_summary(ecn_data)
    projects = get_ecn_scheme_target_projects(ecn_data)
    summary_text = str(basic_info.get("title") or "").strip()
    if not summary_text:
        summary_text = f"涉及项目：{'、'.join(projects)}" if projects else "—"
    row: dict[str, object] = {
        "record_id": str(ecn_data.get("ecn_id") or ""),
        "detail_action": "详情",
        "delete_action": "删除" if include_delete else "",
        "ecn_id": str(ecn_data.get("ecn_id") or ""),
        "current_state": current_state,
        "attention": "负责人异常·待改派" if can_reassign else "待我处理" if is_my_pending else "",
        "summary": summary_text,
        "projects": "、".join(projects) or "—",
        "applicant": str(basic_info.get("applicant") or "—"),
        "apply_date": format_ecn_list_date(basic_info.get("apply_date")),
        "closed_date": (
            format_ecn_list_date(execution_info.get("completed_time")) if current_state == ECNState.CLOSED else "—"
        ),
        "progress": (
            f"负责人异常：{len(assignment_issues)}项待改派"
            if assignment_issues
            else get_ecn_list_progress_summary(ecn_data)
        ),
        "row_tone": (
            "pending"
            if is_my_pending
            else "rejected"
            if current_state == ECNState.REJECTED
            else "completed"
            if current_state == ECNState.CLOSED
            else "executing"
            if current_state == ECNState.ECN_EXECUTING
            else "normal"
        ),
    }
    for index, level in enumerate(ECN_TRACEABILITY_LEVELS):
        row[f"traceability_{index}"] = traceability_summary[level]
    special = [
        item
        for item in get_ecn_special_confirmations(execution_info).values()
        if item.get("assignee") or item.get("assignment_history")
    ]
    done = sum(item.get("confirmed") is True for item in special)
    row["special_tasks"] = f"{'已完成' if done == len(special) else '待确认'} {done}/{len(special)}" if special else "—"
    row["special_tasks_detail"] = "；".join(
        f"{key}：{item.get('assignee') or '执行助理'}（{'已完成' if item.get('confirmed') else '待确认'}）"
        for key, item in get_ecn_special_confirmations(execution_info).items()
        if item.get("assignee") or item.get("assignment_history")
    )
    return row


def format_ecn_list_date(value: object) -> str:
    """ECN首页只展示申请日期，不展示时分秒。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except ValueError:
        date_prefix = text[:10]
        if len(date_prefix) == 10 and date_prefix[4:5] == "-" and date_prefix[7:8] == "-":
            return date_prefix
        return text


def get_ecn_management_grid_columns(include_delete: bool = False) -> list[dict[str, object]]:
    """返回ECN首页表格列；追溯范围直接按JSON配置顺序生成。"""
    text_filter = "agTextColumnFilter"
    columns: list[dict[str, object]] = [
        {
            "headerName": "操作",
            "field": "detail_action",
            "filter": False,
            "pinned": "left",
            "width": 60,
            "sortable": False,
            "lockPosition": "left",
            "lockPinned": True,
            "suppressMovable": True,
            "cellStyle": {"color": "#2563eb", "fontWeight": "bold", "cursor": "pointer"},
        },
    ]
    if include_delete:
        columns.append(
            {
                "headerName": "管理",
                "field": "delete_action",
                "filter": False,
                "pinned": "left",
                "width": 60,
                "sortable": False,
                "lockPosition": "left",
                "lockPinned": True,
                "suppressMovable": True,
                "cellStyle": {"color": "#dc2626", "fontWeight": "bold", "cursor": "pointer"},
            }
        )
    columns.extend(
        [
            {
                "headerName": "ECN编号",
                "field": "ecn_id",
                "filter": text_filter,
                "pinned": "left",
                "lockPosition": "left",
                "lockPinned": True,
                "suppressMovable": True,
                "width": 145,
            },
            {"headerName": "当前状态", "field": "current_state", "filter": text_filter, "width": 150},
            {"headerName": "关注事项", "field": "attention", "filter": text_filter, "width": 105},
            {
                "headerName": "变更简要",
                "field": "summary",
                "filter": text_filter,
                "width": 260,
                "tooltipField": "summary",
                "cellStyle": {"textAlign": "left"},
            },
            {
                "headerName": "涉及项目",
                "field": "projects",
                "filter": text_filter,
                "width": 200,
                "tooltipField": "projects",
            },
            {"headerName": "申请人", "field": "applicant", "filter": text_filter, "width": 100},
            {"headerName": "申请日期", "field": "apply_date", "filter": text_filter, "width": 115},
            {
                "headerName": "流程进度",
                "field": "progress",
                "filter": text_filter,
                "width": 250,
                "tooltipField": "progress",
                "cellStyle": {"textAlign": "left"},
            },
        ]
    )
    columns.append(
        {
            "headerName": "特定事项",
            "field": "special_tasks",
            "filter": text_filter,
            "width": 135,
            "tooltipField": "special_tasks_detail",
            "cellStyle": {"color": "#2563eb", "cursor": "pointer"},
        }
    )
    for index, level in enumerate(ECN_TRACEABILITY_LEVELS):
        columns.append(
            {
                "headerName": level,
                "field": f"traceability_{index}",
                "filter": text_filter,
                "width": 110,
                "cellClassRules": {
                    "ecn-trace-closed": "value == '已关闭'",
                    "ecn-trace-progress": "value.includes('进行中')",
                    "ecn-trace-pending": "value == '待确认'",
                    "ecn-trace-not-started": "value == '未开始'",
                    "ecn-trace-na": "value == '—'",
                },
            }
        )
    columns.append(
        {
            "headerName": "关闭日期",
            "field": "closed_date",
            "filter": "agDateColumnFilter",
            "width": 115,
        }
    )
    for column in columns:
        cell_style = column.setdefault("cellStyle", {})
        if isinstance(cell_style, dict):
            cell_style.setdefault("textAlign", "center")
        if "width" in column:
            column["minWidth"] = column["width"]
        column["headerClass"] = "ecn-grid-header-center"
        column["wrapHeaderText"] = True
        column["autoHeaderHeight"] = True
    return columns
