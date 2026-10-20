"""移交提醒的业务摘要，与执行表的事项、项目及应执行内容保持一致。"""

from ...ecn_management_config import ECNState, get_ecn_special_confirmations, get_ecn_scheme_target_projects
from ...notification_content import build_textcard_description, truncate_notification_text


def get_special_message_item(record: dict, key: str, assignee: str) -> dict[str, str]:
    """按事项键提取业务字段，取消通知也可读取已不属于原负责人的事项。"""
    item = next(
        (item for item in record.get("change_items", []) if isinstance(item, dict) and str(item.get("item_id")) == key),
        {},
    )
    return {
        "assignee": assignee,
        "subject": "ERP相关变更"
        if key == "__erp__"
        else str(item.get("change_type") or item.get("title") or "资料变更"),
        "projects": "—"
        if key == "__erp__"
        else "、".join(get_ecn_scheme_target_projects({"target_projects": item.get("projects", [])})) or "—",
        "content": "ERP相关变更已执行完毕" if key == "__erp__" else str(item.get("new_content") or "无"),
    }


def get_special_message_items(record: dict, names: list[str]) -> list[dict[str, str]]:
    if record.get("workflow", {}).get("current_state") != ECNState.ECN_EXECUTING:
        return []
    result = []
    for key, confirmation in get_ecn_special_confirmations(record.get("execution_info")).items():
        if confirmation.get("assignee") not in names or confirmation.get("confirmed") is True:
            continue
        if confirmation.get("suppress_assignment_notice_for") == confirmation.get("assignee"):
            continue
        result.append(get_special_message_item(record, key, str(confirmation["assignee"])))
    return result


def build_special_card(
    record: dict,
    items: list[dict[str, str]],
    *,
    is_cc: bool,
    cancelled: bool = False,
    observer_names: list[str] | None = None,
) -> tuple[str, str]:
    """生成只保留识别信息与明确动作的特定事项卡片。"""
    first = items[0]
    nature = "研发经理抄送" if is_cc else "移交待办"
    if cancelled and not is_cc:
        nature = "取消告知"
    lines = [f"单号：{record.get('ecn_id') or '—'}"]
    if cancelled:
        lines.append("结果：原任务已取消，无需继续处理")
        lines.append(f"事项：{truncate_notification_text(first['subject'], 30)}")
    else:
        lines.append(f"待办：确认{truncate_notification_text(first['subject'], 30)}")
    lines.append(f"项目：{truncate_notification_text(first['projects'], 36)}")
    if is_cc:
        names = list(dict.fromkeys(observer_names or [item["assignee"] for item in items]))
        lines.append(f"责任人：{truncate_notification_text('、'.join(names), 36)}")
    if len(items) > 1:
        lines.append(f"另有 {len(items) - 1} 项待办，点击查看")
    return (
        "🔧【ECN工程变更】移交任务取消" if cancelled else "🔧【ECN工程变更】特定事项待确认",
        build_textcard_description(nature, lines),
    )
