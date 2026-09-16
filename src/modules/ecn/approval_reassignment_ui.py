"""ECN审批节点审核人调整窗口。"""

import copy

from nicegui import app, ui

from ...ecn_access import can_view_ecn
from .approval_reassignment import reassign_ecn_approval_reviewer
from .special_tasks_ui import _render_person_filters


def _reviewer_candidates(service, permission_code: str, excluded: set[str]) -> dict[str, tuple[str, str]]:
    orgs = {str(item["org_unit_id"]): str(item["name"]) for item in service.list_org_units()}
    positions = {str(item["position_id"]): str(item["name"]) for item in service.list_positions()}
    users = service.load_users()
    names = service.list_usernames_with_permission(permission_code, include_system_admin=True)
    candidates: dict[str, tuple[str, str]] = {}
    for username in names:
        info = users.get(username, {})
        if (
            username in excluded
            or not isinstance(info, dict)
            or info.get("status", "active") != "active"
            or not can_view_ecn(str(info.get("role") or ""), username, user_service=service)
        ):
            continue
        membership = service.get_primary_membership(username)
        candidates[username] = (
            orgs.get(str(membership.get("org_unit_id") or ""), "未分配部门"),
            positions.get(
                str(membership.get("position_id") or ""),
                str(info.get("role") or "未分配岗位"),
            ),
        )
    return candidates


def open_approval_reassignment_dialog(
    ecn_id: str,
    assignment_key: str,
    node_index: int,
    node: dict,
    source_username: str,
    actor_username: str,
    on_success,
) -> None:
    service = app.state.user_service
    baseline_node = copy.deepcopy(node)
    required_permission = str(node.get("required_permission_code") or "")
    assignees = {str(value) for value in node.get("assignee_usernames", []) if str(value)}
    candidates = _reviewer_candidates(service, required_permission, assignees)
    with ui.dialog() as dialog, ui.card().classes("w-[560px] max-w-[94vw] p-5 gap-3"):
        ui.label("调整审批节点审核人").classes("text-lg font-bold text-slate-800")
        ui.label(
            f"{node.get('name') or f'节点{node_index + 1}'}：{source_username} → 新接手人"
        ).classes("text-sm text-slate-700")
        ui.label("只列出在职、可查看ECN且具备本节点审批权限的人员。").classes(
            "text-xs text-slate-500"
        )
        if candidates:
            person = _render_person_filters(candidates)
        else:
            person = None
            ui.label("当前没有其他符合条件的接手人，请先配置岗位审批权限。 ").classes(
                "text-sm font-semibold text-red-600"
            )

        async def save() -> None:
            if person is None or not person.value:
                ui.notify("请选择具体接手人。", type="warning")
                return
            result = await reassign_ecn_approval_reviewer(
                ecn_id,
                assignment_key,
                node_index,
                source_username,
                str(person.value),
                baseline_node,
                actor_username=actor_username,
                user_service=service,
            )
            if result.ok and result.record is not None:
                dialog.close()
                on_success(result.record)
                ui.notify("审批人已调整，原待办已取消并转给新审核人。", type="positive")
            else:
                ui.notify(result.message, type="warning")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=dialog.close).props("flat color=grey")
            save_button = ui.button("确认移交", icon="person_add", on_click=save).props("color=primary")
            if not candidates:
                save_button.disable()
    dialog.open()
