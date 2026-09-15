"""ECN待办与审批记录使用的业务名称，避免向用户暴露内部键。"""

from __future__ import annotations

import re
from typing import Any

from ...ecn_management_config import get_ecn_scheme_target_projects


def _change_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in record.get("change_items", []) if isinstance(item, dict)]


def _find_item(record: dict[str, Any], item_id: object) -> tuple[dict[str, Any], int | None]:
    normalized = str(item_id)
    for index, item in enumerate(_change_items(record), start=1):
        if str(item.get("item_id")) == normalized:
            return item, index
    return {}, None


def execution_scheme_no(record: dict[str, Any], item_id: object) -> str:
    """按方案在单据中的顺序生成稳定、易读的显示编号。"""
    _, index = _find_item(record, item_id)
    return f"#{index:02d}" if index is not None else "#--"


def special_task_label(record: dict[str, Any], key: str) -> str:
    if key == "__erp__":
        return "ERP相关变更"
    item, index = _find_item(record, key)
    subject = str(item.get("change_type") or item.get("title") or "资料变更")
    return f"特定事项 #{index:02d} {subject}" if index is not None else "特定事项（原方案）"


def material_task_label(record: dict[str, Any], item_id: str, task: dict[str, Any]) -> str:
    item, _ = _find_item(record, item_id)
    scheme_no = execution_scheme_no(record, item_id)
    change_type = str(item.get("change_type") or "物料变更")
    project = str(task.get("project") or "").strip()
    if not project:
        project = "、".join(get_ecn_scheme_target_projects({"target_projects": item.get("projects", [])}))
    level = str(task.get("level") or "").strip()
    responsible = str(task.get("label") or task.get("responsible_key") or "").strip()
    details = " · ".join(value for value in (project, level, responsible) if value)
    suffix = f"（{details}）" if details else ""
    return f"物料方案 {scheme_no} {change_type}{suffix}"


def compact_material_confirmation_label(spec: dict[str, Any]) -> str:
    """生成表格复选框短标签；完整的责任变化说明仍放在悬浮提示中。"""
    project = str(spec.get("project") or "").strip()
    responsible_type = str(spec.get("responsible_type") or "")
    responsible_key = str(spec.get("responsible_key") or "").strip()
    raw_users = spec.get("users", [])
    users = (
        "、".join(str(value).strip() for value in raw_users if str(value).strip())
        if isinstance(raw_users, (list, tuple, set))
        else ""
    )
    task_key = str(spec.get("key") or "")
    if responsible_type == "hierarchy_users" and users:
        if responsible_key == "销售主管" and "::项目销售::" in task_key:
            detail = f"{users}（代确认）"
        else:
            detail = " ".join(value for value in (responsible_key, users) if value)
        return " · ".join(value for value in (project, detail) if value)
    return str(spec.get("label") or "待确认负责人")


def material_confirmation_tooltip_text(
    spec: dict[str, Any],
    confirmation: dict[str, Any],
    available: bool,
    can_cancel: bool,
) -> str:
    """说明责任人的匹配依据与节点状态，避免重复复选框中已经显示的姓名。"""
    lines: list[str] = []
    responsible_type = str(spec.get("responsible_type") or "")
    responsible_key = str(spec.get("responsible_key") or "").strip()
    task_key = str(spec.get("key") or "")
    resolution_mode = str(spec.get("resolution_mode") or "")
    if spec.get("manual_assignment") is True or responsible_type == "assigned_user":
        lines.append("分配依据：研发助理人工改派")
    elif responsible_type == "project_sales":
        lines.append("分配依据：项目资料中的销售负责人")
    elif responsible_key == "销售主管" and "::项目销售::" in task_key:
        lines.append("分配依据：项目未识别到销售负责人，由销售主管代确认")
    elif resolution_mode == "manager_escalation":
        original = str(spec.get("escalated_from") or responsible_key or "原责任层级")
        lines.append(f"分配依据：{original}当前无人可处理，沿直属上级逐级转交")
    elif responsible_key:
        lines.append(f"分配依据：按“{responsible_key}”岗位自动匹配")

    if confirmation.get("confirmed") is True:
        confirmer = str(confirmation.get("user") or "未知")
        role = str(confirmation.get("role") or "").strip()
        identity = f"{confirmer}（{role}）" if role else confirmer
        lines.append(f"当前状态：已由 {identity} 确认")
        confirmed_at = str(confirmation.get("time") or "").strip()
        if confirmed_at:
            lines.append(f"确认时间：{confirmed_at}")
        if can_cancel:
            lines.append("当前操作：可取消本次确认")
    elif not available:
        lines.append("当前状态：等待本追溯范围的前序节点")
    else:
        lines.append("当前状态：等待确认")
    return "\n".join(lines)


def humanize_ecn_log_action(record: dict[str, Any], action: object) -> str:
    """转换既有审批记录中的UUID和内部特殊键；新旧记录均只展示业务名称。"""
    text = str(action or "流程记录")
    for item in _change_items(record):
        item_id = str(item.get("item_id") or "")
        if item_id:
            text = text.replace(f"特定事项 {item_id}", special_task_label(record, item_id))
            marker = f"物料责任项 {item_id} / "
            if marker in text:
                task_key, separator, remainder = text.partition(marker)[2].partition(" 改派：")
                task = (
                    record.get("execution_info", {})
                    .get("material_confirmations", {})
                    .get(item_id, {})
                    .get("traceability_tasks", {})
                    .get(task_key, {})
                )
                label = material_task_label(record, item_id, task if isinstance(task, dict) else {})
                text = f"{label} 改派：{remainder}" if separator else label
    text = text.replace("特定事项 __erp__", "ERP相关变更")
    text = re.sub(
        r"特定事项 [0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
        "特定事项（原方案）",
        text,
    )
    return text
