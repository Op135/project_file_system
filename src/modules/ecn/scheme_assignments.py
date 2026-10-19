"""ECN方案填写人的候选人解析与指定窗口。"""

import copy

from nicegui import app, ui

from ...ecn_access import can_edit_ecn_scheme, can_view_ecn
from ...notification_recipients import is_system_admin_username
from .actions import set_scheme_assignees


def get_scheme_writer_candidates(service) -> dict[str, str]:
    """返回在职且同时拥有ECN查看、方案编写权限的具体账号。"""
    candidates: dict[str, str] = {}
    for username, info in service.load_users().items():
        if (
            is_system_admin_username(username)
            or not isinstance(info, dict)
            or info.get("status", "active") != "active"
        ):
            continue
        role = str(info.get("role") or "")
        if not can_view_ecn(role, username, user_service=service) or not can_edit_ecn_scheme(
            role, username, user_service=service
        ):
            continue
        membership = service.get_primary_membership(username)
        position = (
            str(membership.get("position_name") or "")
            if isinstance(membership, dict)
            else ""
        )
        candidates[username] = f"{username}（{position or role or '未分配岗位'}）"
    return candidates


def open_scheme_assignment_dialog(
    local_data: dict,
    actor_username: str,
    actor_role: str,
    on_success,
) -> None:
    """打开多选窗口，并以打开时的指定人快照执行并发校验。"""
    service = app.state.user_service
    candidates = get_scheme_writer_candidates(service)
    workflow = local_data.get("workflow", {})
    workflow = workflow if isinstance(workflow, dict) else {}
    current = workflow.get("scheme_assignees", [])
    selected = [
        str(name)
        for name in current
        if isinstance(current, list) and str(name) in candidates
    ]
    expected = copy.deepcopy(local_data)

    with ui.dialog() as dialog, ui.card().classes("w-[620px] max-w-[94vw] p-5 gap-3"):
        ui.label("指定方案填写人").classes("text-lg font-bold text-slate-800")
        ui.label(
            "被指定人员会进入方案参与状态，并按现有ECN待办机制收到企业微信提醒；"
            "只有在职且具备ECN查看和方案编写权限的人员可选。"
        ).classes("text-xs text-slate-500")
        selector = ui.select(
            options=candidates,
            value=selected,
            multiple=True,
            with_input=True,
            label="方案填写人",
        ).props("outlined use-chips options-dense").classes("w-full")

        async def save() -> None:
            raw_value = selector.value
            names = [str(value) for value in raw_value] if isinstance(raw_value, list) else []
            result = await set_scheme_assignees(
                str(local_data.get("ecn_id") or ""),
                expected,
                names,
                actor_username,
                actor_role,
                user_service=service,
            )
            if result.ok and result.record is not None:
                dialog.close()
                on_success(result.record)
                ui.notify("方案填写人已更新，后台将按现有频率提醒待填写人员。", type="positive")
            else:
                ui.notify(result.message, type="warning")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=dialog.close).props("flat color=grey")
            ui.button("保存指定人员", icon="group_add", on_click=save).props("color=primary")
    dialog.open()
