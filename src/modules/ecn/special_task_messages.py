"""移交提醒的业务摘要，与执行表的事项、项目及应执行内容保持一致。"""

from html import escape

from ...ecn_management_config import ECNState, get_ecn_special_confirmations, get_ecn_scheme_target_projects


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


def _escaped_summary(value: str, budget: int) -> str:
    """先转义再限制UTF-8字节数，截断不破坏HTML实体。"""
    value = " ".join(value.split())
    if len(escape(value).encode("utf-8")) <= budget:
        return escape(value)
    result = ""
    for char in value:
        part = escape(char)
        if len((result + part + "…").encode("utf-8")) > budget:
            break
        result += part
    return result + "…"


def build_special_card(
    record: dict,
    items: list[dict[str, str]],
    *,
    test_mode: bool,
    is_cc: bool,
    cancelled: bool = False,
    observer_names: list[str] | None = None,
) -> tuple[str, str]:
    """优先分配三项业务字段的空间，接收人和单号放在后面。"""
    first = items[0]
    nature = "调试转发 · 未通知实际处理人" if test_mode else "研发经理抄送" if is_cc else "移交待办"
    if cancelled and not test_mode and not is_cc:
        nature = "取消告知"
    prefix = f'<div class="gray">{nature}</div>'
    if cancelled:
        prefix += '<div class="normal">已取消，无需继续处理</div>'
    observer_footer = ""
    if test_mode or is_cc:
        names = list(dict.fromkeys(observer_names or [item["assignee"] for item in items]))
        names_label = "原应通知人员" if test_mode else "待处理人员"
        observer_footer = f'<div class="gray">{names_label}：{escape("、".join(names))}</div>'
    footer = observer_footer + (
        f'<div class="gray">{"原负责人" if cancelled else "接收人"}：{_escaped_summary(first["assignee"], 30)}</div>'
        f'<div class="gray">单号：{_escaped_summary(str(record.get("ecn_id") or ""), 40)}</div>'
    )
    if len(items) > 1:
        footer += f'<div class="gray">另有{len(items) - 1}项，详情查看</div>'
    labels = [("事项/方案", "subject"), ("项目", "projects"), ("应执行内容", "content")]
    overhead = "".join(f'<div class="normal">{label}：</div>' for label, _ in labels)
    remaining = 512 - len((prefix + footer + overhead).encode("utf-8"))
    parts = []
    for index, (label, key) in enumerate(labels):
        # 前两项各留四分之一预算，其余优先留给应执行内容。
        budget = remaining if index == 2 else max(3, remaining // (4 - index))
        value = _escaped_summary(first[key], budget)
        remaining -= len(value.encode("utf-8"))
        parts.append(f'<div class="normal">{label}：{value}</div>')
    return "🔧【ECN工程变更】移交任务取消" if cancelled else "🔧【ECN工程变更】特定事项待确认", prefix + "".join(
        parts
    ) + footer
