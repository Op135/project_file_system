"""特定事项人员移交选择及独立跟进窗口。"""

import copy
from nicegui import app, ui
from ... import db_storage
from ...ecn_access import (
    build_ecn_access_snapshot,
    can_execute_ecn_assistant_stage,
    can_view_ecn,
    has_ecn_material_execution_permission,
    is_active_ecn_user,
)
from ...ecn_management_config import ECNState, get_ecn_special_confirmations
from .special_tasks import update_special_task
from .material_tasks import update_material_task_assignee


def _transfer_candidates(service, *, require_material_permission: bool = False) -> dict[str, tuple[str, str]]:
    candidates: dict[str, tuple[str, str]] = {}
    orgs = {str(item["org_unit_id"]): item["name"] for item in service.list_org_units()}
    positions = {str(item["position_id"]): item["name"] for item in service.list_positions()}
    for name, info in service.load_users().items():
        if info.get("status", "active") != "active" or not can_view_ecn(
            str(info.get("role") or ""), name, user_service=service
        ):
            continue
        if require_material_permission and not has_ecn_material_execution_permission(name, user_service=service):
            continue
        membership = service.get_primary_membership(name)
        candidates[name] = (
            orgs.get(str(membership.get("org_unit_id")), "未分配部门"),
            positions.get(str(membership.get("position_id")), str(info.get("role") or "未分配岗位")),
        )
    return candidates


def _render_person_filters(candidates: dict[str, tuple[str, str]]):
    department = ui.select(
        ["全部", *sorted({item[0] for item in candidates.values()})], value="全部", label="部门"
    ).classes("w-full")
    position = ui.select(["全部"], value="全部", label="岗位").classes("w-full")
    person = ui.select({}, label="具体接收人", with_input=True).classes("w-full")

    def filter_people():
        person.set_options(
            {
                name: name
                for name, (dep, pos) in candidates.items()
                if (department.value == "全部" or dep == department.value)
                and (position.value == "全部" or pos == position.value)
            }
        )
        person.set_value(None)

    def filter_positions():
        position.set_options(
            [
                "全部",
                *sorted(
                    {
                        pos
                        for dep, pos in candidates.values()
                        if department.value == "全部" or dep == department.value
                    }
                ),
            ]
        )
        position.set_value("全部")
        filter_people()

    department.on_value_change(filter_positions)
    position.on_value_change(filter_people)
    filter_positions()
    return person


def open_transfer_dialog(ecn_id: str, key: str, confirmation: dict, username: str, role: str, refresh):
    baseline = copy.deepcopy(confirmation)
    service = app.state.user_service
    candidates = _transfer_candidates(service)
    with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
        ui.label("移交特定事项").classes("text-lg font-bold")
        ui.label("按部门、岗位筛选，指定一名在职且有ECN查看权限的人员。").classes("text-xs text-slate-500")
        person = _render_person_filters(candidates)

        async def save(target=None):
            chosen = target if isinstance(target, str) else person.value
            if chosen is None:
                ui.notify("请选择具体接收人", type="warning")
                return
            result = await update_special_task(
                ecn_id, key, baseline, username=username, role=role, service=service, assignee=str(chosen)
            )
            if result.ok and result.record is not None:
                dialog.close()
                refresh()
                ui.notify("负责人已更新，后台将按通知配置发送提醒", type="positive")
            else:
                ui.notify(result.message, type="warning")

        with ui.row():
            ui.button("保存移交", on_click=save)
            ui.button("收回给执行助理", on_click=lambda: save("")).props("flat")
            ui.button("取消", on_click=dialog.close).props("flat")
    dialog.open()


def render_transfer_button(
    ecn_id: str,
    key: str,
    confirmation: dict,
    username: str,
    role: str,
    active: bool,
    refresh,
    *,
    access_snapshot: dict | None = None,
):
    with ui.column().classes("px-3 py-2 gap-1 items-center justify-center text-center self-stretch"):
        assignee = str(confirmation.get("assignee") or "")
        assignee_active = not assignee or is_active_ecn_user(
            assignee,
            user_service=app.state.user_service,
            access_snapshot=access_snapshot,
        )
        ui.label(
            (assignee or "执行助理") + ("（账号已停用/无权限）" if not assignee_active else "")
        ).classes("text-xs " + ("text-red-600 font-semibold" if not assignee_active else ""))
        if active and confirmation.get("confirmed") is not True and can_execute_ecn_assistant_stage(
            role,
            username,
            access_snapshot=access_snapshot,
        ):
            ui.button(
                "调整人员" if confirmation.get("assignee") else "移交",
                icon="person_add",
                on_click=lambda: open_transfer_dialog(ecn_id, key, confirmation, username, role, refresh),
            ).props("flat dense size=sm")


def open_material_transfer_dialog(
    ecn_id: str,
    item_id: str,
    key: str,
    confirmation: dict,
    username: str,
    role: str,
    refresh,
):
    baseline = copy.deepcopy(confirmation)
    service = app.state.user_service
    candidates = _transfer_candidates(service, require_material_permission=True)
    with ui.dialog() as dialog, ui.card().classes("w-[520px] max-w-full"):
        ui.label("改派物料执行责任项").classes("text-lg font-bold")
        ui.label("原负责人已停用、离职或失去权限，请指定一名具备ECN执行权限的在职人员。") \
            .classes("text-xs text-slate-500")
        person = _render_person_filters(candidates)

        async def save(assignee: str | None = None):
            chosen = assignee if isinstance(assignee, str) else person.value
            if chosen is None:
                ui.notify("请选择具体接收人", type="warning")
                return
            result = await update_material_task_assignee(
                ecn_id,
                item_id,
                key,
                baseline,
                username=username,
                role=role,
                service=service,
                assignee=str(chosen),
            )
            if result.ok and result.record is not None:
                dialog.close()
                refresh()
                ui.notify("物料责任项已改派，后台将按通知配置提醒新负责人", type="positive")
            else:
                ui.notify(result.message, type="warning")

        with ui.row():
            ui.button("保存改派", on_click=save)
            if confirmation.get("manual_assignee"):
                ui.button("恢复原责任路线", on_click=lambda: save("")).props("flat")
            ui.button("取消", on_click=dialog.close).props("flat")
    dialog.open()


async def open_special_tasks_dialog(ecn_id: str, username: str, role: str, refresh_list):
    if not can_view_ecn(role, username):
        ui.notify("没有ECN查看权限", type="warning")
        return
    with ui.dialog() as dialog, ui.card().classes("w-[1000px] max-w-full"):
        ui.label(f"{ecn_id} · 特定事项跟进").classes("text-lg font-bold")

        @ui.refreshable
        async def render():
            records = await db_storage.get_fresh_item("ecn_management_data", {})
            record = records.get(ecn_id)
            if not isinstance(record, dict):
                ui.label("单据已删除")
                return
            items = {str(item.get("item_id")): item for item in record.get("change_items", [])}
            entries = get_ecn_special_confirmations(record.get("execution_info"))
            access_snapshot = build_ecn_access_snapshot(app.state.user_service)
            assigned = {
                key: item for key, item in entries.items() if item.get("assignee") or item.get("assignment_history")
            }
            if not assigned:
                ui.label("暂无移交事项")
            for key, item in assigned.items():
                baseline = copy.deepcopy(item)
                with ui.row().classes("w-full items-center border-b py-2"):
                    ui.label(
                        "ERP更新" if key == "__erp__" else f"{key} · {items.get(key, {}).get('new_content', '')}"
                    ).classes("flex-1 whitespace-pre-line")
                    ui.label(
                        f"{item.get('assignee') or '执行助理'} · "
                        f"{'已完成' if item.get('confirmed') else '待确认'}"
                        + (
                            " · 账号已停用/无权限"
                            if item.get("assignee")
                            and item.get("confirmed") is not True
                            and not is_active_ecn_user(
                                str(item.get("assignee")),
                                user_service=app.state.user_service,
                                access_snapshot=access_snapshot,
                            )
                            else ""
                        )
                    ).classes(
                        "text-red-600 font-semibold"
                        if item.get("assignee")
                        and item.get("confirmed") is not True
                        and not is_active_ecn_user(
                            str(item.get("assignee")),
                            user_service=app.state.user_service,
                            access_snapshot=access_snapshot,
                        )
                        else ""
                    )
                    ui.label(f"{item.get('user', '')} {item.get('time', '')}").classes("text-xs text-slate-500")

                    async def confirm(key=key, baseline=baseline):
                        result = await update_special_task(
                            ecn_id,
                            key,
                            baseline,
                            username=username,
                            role=role,
                            service=app.state.user_service,
                            confirmed=True,
                        )
                        if result.ok and result.record is not None:
                            render.refresh()
                            refresh_list()
                        else:
                            ui.notify(result.message, type="warning")

                    active = record.get("workflow", {}).get("current_state") == ECNState.ECN_EXECUTING
                    if active and item.get("assignee") == username and item.get("confirmed") is not True:
                        ui.button("确认完成", on_click=confirm).props("dense")
                    render_transfer_button(
                        ecn_id,
                        key,
                        item,
                        username,
                        role,
                        active,
                        lambda: (render.refresh(), refresh_list()),
                        access_snapshot=access_snapshot,
                    )

        await render()
        ui.button("关闭", on_click=dialog.close).props("flat")
    dialog.open()
