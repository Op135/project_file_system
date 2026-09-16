# -*- encoding: utf-8 -*-
import copy
import logging
import time
import uuid
from datetime import (
    datetime,
)
from typing import (
    Any,
    Literal,
)

from nicegui import (
    app,
    ui,
)
from nicegui.client import (
    Client,
)

from ... import (
    db_storage,
)
from ...config import (
    ECNState,
)
from ...ecn_access import (
    build_ecn_access_snapshot,
    can_confirm_ecn_material_spec,
    can_execute_ecn_assistant_stage,
    get_active_ecn_actor_role,
    is_ecn_material_spec_orphaned,
    resolve_ecn_material_spec_responsibility,
)
from ...ecn_management_config import (
    ECN_EXECUTION_RESULT_FAILED,
    ECN_EXECUTION_RESULT_PENDING,
    ECN_EXECUTION_RESULT_RUNNING,
    ECN_EXECUTION_RESULT_SUCCESS,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_COMPLETED,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    ECN_SCHEME_GROUP_MATERIAL,
    ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT,
    ECN_TRACEABILITY_LEVELS,
    classify_ecn_change_item,
    get_ecn_material_change_display,
    get_ecn_material_execution_specs,
    get_ecn_scheme_target_projects,
    get_ecn_stage_index,
    is_ecn_assistant_execution_ready,
    is_ecn_material_disposition_required,
    is_ecn_material_execution_closed,
    is_ecn_special_execution_complete,
)

# 仅记录当前进程内实际仍在运行的系统内资料任务，用于区分“正在执行”与异常中断后遗留的运行状态。
from .models import (
    append_ecn_approval_log_once,
)
from .overview_execution import (
    execute_ecn_overview_schemes,
)
from .repository import (
    atomic_ecn_deep_update,
)
from .special_tasks import update_special_task
from .special_tasks_ui import open_material_transfer_dialog, render_transfer_button
from .task_labels import compact_material_confirmation_label, material_confirmation_tooltip_text

logger = logging.getLogger(__name__)
ACTIVE_ECN_OVERVIEW_EXECUTIONS: set[str] = set()
ECN_OVERVIEW_EXECUTION_LEASE_SECONDS = 600


def build_execution_panel(
    tab_exec,
    local_data,
    wf,
    current_user,
    current_role,
    can_execute_assistant,
    refresh_list,
    *,
    panel_container=None,
):
    # --- [TAB 4] ECN 分阶段执行 ---
    panel = (
        panel_container
        if panel_container is not None
        else ui.tab_panel(tab_exec)
        .props("id=ecn-execution-tab-panel")
        .classes("gap-4 p-2 mx-auto overflow-y-auto overflow-x-hidden")
    )
    with panel:
        execution_container = ui.column().classes("w-full gap-4")
        material_task_controls: dict[str, dict[str, dict[str, Any]]] = {}
        material_status_controls: dict[str, dict[str, Any]] = {}
        execution_access_snapshot = {"value": build_ecn_access_snapshot(app.state.user_service)}

        def get_execution_change_items() -> dict[str, dict]:
            return {
                str(item.get("item_id")): item
                for item in local_data.get("change_items", [])
                if isinstance(item, dict) and item.get("item_id")
            }

        def execution_scheme_no(item_id: str) -> str:
            for index, item in enumerate(local_data.get("change_items", []), start=1):
                if isinstance(item, dict) and str(item.get("item_id")) == str(item_id):
                    return f"#{index:02d}"
            return "#--"

        def notify_execution_safely(
            event_client: Client,
            message: str,
            notification_type: Literal["positive", "negative", "warning", "info", "ongoing"],
            timeout_ms: int | None = None,
        ) -> None:
            """通知不应因执行表格重绘或客户端离线而中断后台落盘。"""
            try:
                with event_client:
                    if timeout_ms is None:
                        ui.notify(message, type=notification_type)
                    else:
                        ui.notify(message, type=notification_type, timeout=timeout_ms)
            except Exception:
                logger.warning("ECN执行通知发送失败，后台流程继续：%s", message)

        async def capture_execution_scroll_state(event_client: Client) -> dict[str, float]:
            """保存执行页签纵向位置和物料表横向位置。"""
            try:
                state = await event_client.run_javascript(
                    """
                    const panel = document.getElementById('ecn-execution-tab-panel');
                    const table = document.getElementById('ecn-material-execution-scroll');
                    return {
                        panelY: panel ? panel.scrollTop : 0,
                        tableX: table ? table.scrollLeft : 0,
                    };
                    """
                )
            except Exception:
                return {}
            if not isinstance(state, dict):
                return {}
            return {
                "panelY": float(state.get("panelY") or 0),
                "tableX": float(state.get("tableX") or 0),
            }

        async def restore_execution_scroll_state(
            event_client: Client,
            scroll_state: dict[str, float],
        ) -> None:
            """执行区重绘后在DOM更新完成时恢复滚动位置。"""
            if not scroll_state:
                return
            panel_y = float(scroll_state.get("panelY") or 0)
            table_x = float(scroll_state.get("tableX") or 0)
            try:
                await event_client.run_javascript(
                    f"""
                    const restoreEcnExecutionScroll = () => {{
                        const panel = document.getElementById('ecn-execution-tab-panel');
                        const table = document.getElementById('ecn-material-execution-scroll');
                        if (panel) panel.scrollTop = {panel_y};
                        if (table) table.scrollLeft = {table_x};
                    }};
                    requestAnimationFrame(() => requestAnimationFrame(restoreEcnExecutionScroll));
                    setTimeout(restoreEcnExecutionScroll, 80);
                    """
                )
            except Exception:
                pass

        def execution_scheme_projects(item: dict) -> list[str]:
            return get_ecn_scheme_target_projects({"target_projects": item.get("projects", [])})

        def execution_scheme_title(item: dict, *, include_projects: bool = True) -> str:
            category = classify_ecn_change_item(item)
            if category == ECN_SCHEME_GROUP_MATERIAL:
                old_value, new_value = get_ecn_material_change_display(item)
                return f"{item.get('change_type') or '物料变更'}：{old_value or '无'} → {new_value or '无'}"
            projects = "、".join(execution_scheme_projects(item))
            if category == ECN_SCHEME_GROUP_OVERVIEW_DOCUMENT:
                overview_config = app.storage.general.get("over_config_data_flat", {}).get(
                    item.get("label"),
                    {},
                )
                overview_title = overview_config.get("title") if isinstance(overview_config, dict) else None
                subject = (
                    " · ".join(part for part in [item.get("role"), overview_title or item.get("label")] if part)
                    or "系统内资料变更"
                )
            else:
                subject = item.get("change_type") or item.get("title") or "资料变更"
            return f"{subject}" + (f"（{projects}）" if include_projects and projects else "")

        # 执行页表格默认全部居中。需要将某列改为靠左时，只需把列名加入对应集合。
        execution_left_aligned_columns: dict[str, set[str]] = {
            "assistant": {"执行前", "应执行内容"},
            "overview": {"系统内资料方案"},
            "material": {
                "变更前",
                "变更后",
                "文件",
                "供应商",
                "零件仓",
                "生产在线",
                "半成品仓",
                "成品仓",
                "客户/在途",
            },
        }

        def execution_column_alignment(
            table: str,
            column: str,
            *,
            flex_column: bool = False,
        ) -> str:
            align_left = column in execution_left_aligned_columns.get(table, set())
            text_class = "text-left" if align_left else "text-center"
            if flex_column:
                return (
                    f"{text_class} content-center flex flex-col justify-center "
                    f"{'items-start' if align_left else 'items-center'}"
                )
            return (
                f"{text_class} content-center flex items-center {'justify-start' if align_left else 'justify-center'}"
            )

        def sync_execution_local_data() -> bool:
            fresh_data = db_storage.get_deep_item(["ecn_management_data", local_data["ecn_id"]])
            if not isinstance(fresh_data, dict):
                return False
            local_data["execution_info"] = copy.deepcopy(fresh_data.get("execution_info", {}))
            local_data["change_items"] = copy.deepcopy(fresh_data.get("change_items", []))
            fresh_workflow = fresh_data.get("workflow", {})
            if isinstance(fresh_workflow, dict):
                wf.clear()
                wf.update(copy.deepcopy(fresh_workflow))
            local_data["approval_log"] = copy.deepcopy(fresh_data.get("approval_log", []))
            return True

        def get_material_execution_runtime(item_id: str) -> tuple[dict, dict, list[dict], dict]:
            execution_info = local_data.get("execution_info", {})
            material_confirmations = (
                execution_info.get("material_confirmations", {}) if isinstance(execution_info, dict) else {}
            )
            material_entry = (
                material_confirmations.get(str(item_id), {}) if isinstance(material_confirmations, dict) else {}
            )
            material_entry = material_entry if isinstance(material_entry, dict) else {}
            item = get_execution_change_items().get(str(item_id), {})
            specs = get_ecn_material_execution_specs(
                item,
                material_entry,
            )
            tasks = material_entry.get("traceability_tasks", {})
            return item, material_entry, specs, tasks if isinstance(tasks, dict) else {}

        def can_cancel_material_confirmation(
            spec: dict,
            confirmation: dict,
            specs: list[dict],
            tasks: dict,
            item_closed: bool,
        ) -> bool:
            if item_closed or confirmation.get("confirmed") is not True:
                return False
            if str(confirmation.get("user") or "") != current_user:
                return False
            level = str(spec.get("level") or "")
            stage_index = get_ecn_stage_index(spec.get("stage_index", 0))
            return not any(
                str(other_spec.get("level") or "") == level
                and get_ecn_stage_index(other_spec.get("stage_index", 0)) > stage_index
                and isinstance(tasks.get(str(other_spec.get("key"))), dict)
                and tasks[str(other_spec.get("key"))].get("confirmed") is True
                for other_spec in specs
            )

        def refresh_material_execution_controls(item_ids: list[str] | None = None) -> None:
            execution_info = local_data.get("execution_info", {})
            material_is_active = (
                isinstance(execution_info, dict)
                and execution_info.get("stage") == ECN_EXECUTION_STAGE_MATERIAL
                and wf.get("current_state") == ECNState.ECN_EXECUTING
            )
            target_ids = item_ids or list(material_task_controls)
            for item_id in target_ids:
                _, material_entry, specs, tasks = get_material_execution_runtime(item_id)
                item_closed = material_entry.get("status") == "closed"
                specs_by_key = {str(spec.get("key")): spec for spec in specs}
                for key, controls in material_task_controls.get(str(item_id), {}).items():
                    spec = specs_by_key.get(str(key), {})
                    confirmation = tasks.get(str(key), {})
                    confirmation = confirmation if isinstance(confirmation, dict) else {}
                    checked = confirmation.get("confirmed") is True
                    available = spec.get("available") is True
                    can_confirm = (
                        material_is_active
                        and not item_closed
                        and not checked
                        and available
                        and can_confirm_ecn_material_spec(
                            spec,
                            current_role,
                            current_user,
                            access_snapshot=execution_access_snapshot["value"],
                        )
                    )
                    can_cancel = (
                        material_is_active
                        and can_confirm_ecn_material_spec(
                            spec,
                            current_role,
                            current_user,
                            access_snapshot=execution_access_snapshot["value"],
                        )
                        and can_cancel_material_confirmation(
                            spec,
                            confirmation,
                            specs,
                            tasks,
                            item_closed,
                        )
                    )
                    checkbox = controls.get("checkbox")
                    tooltip = controls.get("tooltip")
                    if checkbox is not None:
                        checkbox.set_value(checked)
                        checkbox.enable() if can_confirm or can_cancel else checkbox.disable()
                    if tooltip is not None:
                        tooltip.set_text(
                            material_confirmation_tooltip_text(
                                spec,
                                confirmation,
                                available,
                                can_cancel,
                            )
                        )

                status_controls = material_status_controls.get(str(item_id), {})
                status_badge = status_controls.get("badge")
                progress_label = status_controls.get("progress")
                if status_badge is not None:
                    status_badge.set_text("已关闭" if item_closed else "执行中")
                    status_badge.props(f"color={'green' if item_closed else 'orange'}")
                if progress_label is not None:
                    completed_count = sum(
                        1 for task in tasks.values() if isinstance(task, dict) and task.get("confirmed") is True
                    )
                    progress_label.set_text(f"{completed_count}/{len(specs)}")

        def is_last_pending_material_confirmation(item_id: str, confirmation_key: str) -> bool:
            execution_info = local_data.get("execution_info", {})
            if not is_ecn_special_execution_complete(execution_info):
                return False
            material_confirmations = (
                execution_info.get("material_confirmations", {}) if isinstance(execution_info, dict) else {}
            )
            if not isinstance(material_confirmations, dict):
                return False
            pending_keys: list[tuple[str, str]] = []
            for current_item_id, material_entry in material_confirmations.items():
                if not isinstance(material_entry, dict) or material_entry.get("status") == "closed":
                    continue
                item = get_execution_change_items().get(str(current_item_id), {})
                specs = get_ecn_material_execution_specs(
                    item,
                    material_entry,
                )
                tasks = material_entry.get("traceability_tasks", {})
                tasks = tasks if isinstance(tasks, dict) else {}
                for spec in specs:
                    key = str(spec.get("key"))
                    confirmation = tasks.get(key, {})
                    if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
                        pending_keys.append((str(current_item_id), key))
            return pending_keys == [(str(item_id), str(confirmation_key))]

        async def update_assistant_execution_confirmation(
            confirmation_kind: str,
            item_id: str | None,
            confirmed: bool,
            baseline: dict,
        ):
            event_client = ui.context.client
            scroll_state = await capture_execution_scroll_state(event_client)
            key = "__erp__" if confirmation_kind == "erp" else str(item_id)
            result = await update_special_task(
                str(local_data["ecn_id"]),
                key,
                baseline,
                username=current_user,
                role=current_role,
                service=app.state.user_service,
                confirmed=confirmed,
            )
            success = result.ok and result.record is not None
            blocked = {"reason": result.message}
            if success and not blocked["reason"]:
                sync_execution_local_data()
                render_execution_tab()
            else:
                notify_execution_safely(
                    event_client,
                    blocked["reason"] or "确认状态保存失败，请重试。",
                    "warning",
                )
                sync_execution_local_data()
                render_execution_tab()
            await restore_execution_scroll_state(event_client, scroll_state)

        async def run_overview_execution():
            event_client = ui.context.client
            execution_ecn_id = str(local_data.get("ecn_id") or "")
            blocked = {"reason": ""}
            operation_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            operation_epoch = time.time()
            operation_id = uuid.uuid4().hex

            def claim_execution(current_ecn):
                if not isinstance(current_ecn, dict):
                    blocked["reason"] = "ECN数据不存在。"
                    return db_storage.ATOMIC_NO_UPDATE
                current_wf = current_ecn.get("workflow", {})
                execution_info = current_ecn.get("execution_info", {})
                stage = execution_info.get("stage")
                actor_role = get_active_ecn_actor_role(
                    current_user,
                    current_role,
                    user_service=app.state.user_service,
                )
                if current_wf.get("current_state") != ECNState.ECN_EXECUTING:
                    blocked["reason"] = "当前ECN已不在执行确认状态。"
                    return db_storage.ATOMIC_NO_UPDATE
                if actor_role is None or not can_execute_ecn_assistant_stage(
                    actor_role,
                    current_user,
                    user_service=app.state.user_service,
                ):
                    blocked["reason"] = "当前用户无权触发系统内资料执行。"
                    return db_storage.ATOMIC_NO_UPDATE
                allowed_stages = [
                    ECN_EXECUTION_STAGE_ASSISTANT,
                    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
                    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
                ]
                if stage not in allowed_stages:
                    blocked["reason"] = "系统内资料正在执行或已经执行完成，请勿重复操作。"
                    return db_storage.ATOMIC_NO_UPDATE
                if stage == ECN_EXECUTION_STAGE_OVERVIEW_RUNNING:
                    started_epoch = execution_info.get("overview_started_epoch")
                    lease_active = (
                        isinstance(started_epoch, (int, float))
                        and operation_epoch - float(started_epoch) < ECN_OVERVIEW_EXECUTION_LEASE_SECONDS
                    )
                    if execution_ecn_id in ACTIVE_ECN_OVERVIEW_EXECUTIONS or lease_active:
                        blocked["reason"] = "系统内资料仍在执行，请勿重复操作。"
                        return db_storage.ATOMIC_NO_UPDATE
                if stage == ECN_EXECUTION_STAGE_ASSISTANT and not is_ecn_assistant_execution_ready(execution_info):
                    blocked["reason"] = "请先确认所有未移交的事项/资料及ERP。"
                    return db_storage.ATOMIC_NO_UPDATE

                execution_info["stage"] = ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
                execution_info["overview_started_by"] = current_user
                execution_info["overview_started_role"] = actor_role
                execution_info["overview_started_time"] = operation_time
                execution_info["overview_started_epoch"] = operation_epoch
                execution_info["overview_run_id"] = operation_id
                for result in execution_info.get("overview_results", {}).values():
                    if isinstance(result, dict) and result.get("status") != ECN_EXECUTION_RESULT_SUCCESS:
                        result["status"] = ECN_EXECUTION_RESULT_RUNNING
                        result["message"] = "正在执行"
                return current_ecn

            claimed = await atomic_ecn_deep_update(
                ["ecn_management_data", local_data["ecn_id"]],
                claim_execution,
            )
            if not claimed or blocked["reason"]:
                notify_execution_safely(
                    event_client,
                    blocked["reason"] or "未能启动系统内资料执行。",
                    "warning",
                )
                sync_execution_local_data()
                render_execution_tab()
                return

            ACTIVE_ECN_OVERVIEW_EXECUTIONS.add(execution_ecn_id)
            notify_execution_safely(
                event_client,
                "已开始逐条执行系统内资料方案，请稍候。",
                "info",
                4000,
            )
            sync_execution_local_data()
            render_execution_tab()
            fresh_data = db_storage.get_deep_item(["ecn_management_data", local_data["ecn_id"]])
            if not isinstance(fresh_data, dict):
                ACTIVE_ECN_OVERVIEW_EXECUTIONS.discard(execution_ecn_id)
                notify_execution_safely(event_client, "无法读取待执行ECN数据。", "negative")
                return

            try:
                overview_results = await execute_ecn_overview_schemes(
                    fresh_data,
                    operation_time,
                )
            except Exception as exc:
                logger.exception("ECN系统内资料批量执行异常：%s", local_data.get("ecn_id"))
                overview_results = copy.deepcopy(fresh_data.get("execution_info", {}).get("overview_results", {}))
                for result in overview_results.values():
                    if isinstance(result, dict) and result.get("status") != ECN_EXECUTION_RESULT_SUCCESS:
                        result["status"] = ECN_EXECUTION_RESULT_FAILED
                        result["message"] = str(exc)

            executed_item_statuses = {
                str(item.get("item_id")): item.get("execute_status")
                for item in fresh_data.get("change_items", [])
                if isinstance(item, dict) and item.get("item_id")
            }
            all_overview_succeeded = all(
                isinstance(result, dict) and result.get("status") == ECN_EXECUTION_RESULT_SUCCESS
                for result in overview_results.values()
            )
            finish_state = {"applied": False}

            def finish_execution(current_ecn):
                if not isinstance(current_ecn, dict):
                    return db_storage.ATOMIC_NO_UPDATE
                execution_info = current_ecn.setdefault("execution_info", {})
                if (
                    execution_info.get("stage") != ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
                    or execution_info.get("overview_run_id") != operation_id
                ):
                    return db_storage.ATOMIC_NO_UPDATE
                finish_state["applied"] = True
                execution_info["overview_results"] = copy.deepcopy(overview_results)
                for item in current_ecn.get("change_items", []):
                    if isinstance(item, dict) and str(item.get("item_id")) in executed_item_statuses:
                        item["execute_status"] = executed_item_statuses[str(item.get("item_id"))]

                approval_log = current_ecn.setdefault("approval_log", [])
                if all_overview_succeeded:
                    material_confirmations = execution_info.get("material_confirmations", {})
                    if (
                        isinstance(material_confirmations, dict) and material_confirmations
                    ) or not is_ecn_special_execution_complete(execution_info):
                        execution_info["stage"] = ECN_EXECUTION_STAGE_MATERIAL
                        action_text = "系统内资料执行完成，继续跟进物料及移交事项"
                    else:
                        execution_info["stage"] = ECN_EXECUTION_STAGE_COMPLETED
                        execution_info["completed_time"] = operation_time
                        current_ecn.setdefault("workflow", {})["current_state"] = ECNState.CLOSED
                        current_ecn["workflow"]["pending_roles"] = []
                        action_text = "系统内资料执行完成，ECN关闭"
                else:
                    execution_info["stage"] = ECN_EXECUTION_STAGE_OVERVIEW_FAILED
                    action_text = "系统内资料执行存在失败项"
                append_ecn_approval_log_once(
                    approval_log,
                    {
                        "user": current_user,
                        "role": execution_info.get("overview_started_role") or current_role,
                        "action": action_text,
                        "time": operation_time,
                    },
                )
                return current_ecn

            try:
                finished = bool(
                    await atomic_ecn_deep_update(
                        ["ecn_management_data", local_data["ecn_id"]],
                        finish_execution,
                    )
                    and finish_state["applied"]
                )
            except Exception:
                logger.exception("ECN系统内资料执行结果保存异常：%s", execution_ecn_id)
                finished = False
            finally:
                ACTIVE_ECN_OVERVIEW_EXECUTIONS.discard(execution_ecn_id)
            sync_execution_local_data()
            render_execution_tab()
            if finished and all_overview_succeeded:
                notify_execution_safely(
                    event_client,
                    "系统内资料方案全部执行成功。",
                    "positive",
                )
            elif finished:
                notify_execution_safely(
                    event_client,
                    "存在执行失败项，请查看结果并重试。",
                    "negative",
                )
            else:
                notify_execution_safely(
                    event_client,
                    "执行结果保存失败，请刷新后确认。",
                    "negative",
                )

        async def request_final_material_confirmation() -> bool:
            with (
                ui.dialog().props("persistent") as confirm_dialog,
                ui.card().classes("w-[440px] max-w-[92vw] p-5 gap-3"),
            ):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("warning_amber", color="orange", size="sm")
                    ui.label("确认完成最后一项？").classes("text-lg font-bold text-slate-800")
                ui.label("这是本ECN物料执行阶段最后一个待确认项。确认后将完成物料执行并触发后续关闭处理。").classes(
                    "text-sm text-slate-600 leading-relaxed"
                )
                ui.label("请再次核对实际执行结果无误后再确认。 ").classes(
                    "text-sm font-semibold text-orange-700 bg-orange-50 px-3 py-2 rounded"
                )
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button(
                        "返回检查",
                        on_click=lambda: confirm_dialog.submit(False),
                    ).props("flat color=grey no-caps")
                    ui.button(
                        "确认最后一项",
                        icon="check_circle",
                        on_click=lambda: confirm_dialog.submit(True),
                    ).props("color=positive no-caps")
            return bool(await confirm_dialog)

        async def update_material_execution_confirmation(
            item_id: str,
            confirmation_key: str,
            confirmed: bool,
            final_confirmation_acknowledged: bool = False,
        ):
            event_client = ui.context.client
            scroll_state = await capture_execution_scroll_state(event_client)
            blocked: dict[str, Any] = {
                "reason": "",
                "requires_final_confirmation": False,
            }
            operation_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            def update_confirmation(current_ecn):
                if not isinstance(current_ecn, dict):
                    blocked["reason"] = "ECN数据不存在。"
                    return db_storage.ATOMIC_NO_UPDATE
                current_wf = current_ecn.get("workflow", {})
                execution_info = current_ecn.get("execution_info", {})
                actor_role = get_active_ecn_actor_role(
                    current_user,
                    current_role,
                    user_service=app.state.user_service,
                )
                if actor_role is None:
                    blocked["reason"] = "当前账号已停用或不存在，不能确认执行项。"
                    return db_storage.ATOMIC_NO_UPDATE
                if (
                    current_wf.get("current_state") != ECNState.ECN_EXECUTING
                    or execution_info.get("stage") != ECN_EXECUTION_STAGE_MATERIAL
                ):
                    blocked["reason"] = "当前已不在物料执行确认阶段。"
                    return db_storage.ATOMIC_NO_UPDATE

                item = next(
                    (
                        current_item
                        for current_item in current_ecn.get("change_items", [])
                        if isinstance(current_item, dict) and str(current_item.get("item_id")) == str(item_id)
                    ),
                    None,
                )
                material_confirmations = execution_info.get("material_confirmations", {})
                material_entry = (
                    material_confirmations.get(str(item_id)) if isinstance(material_confirmations, dict) else None
                )
                if not isinstance(material_entry, dict) or material_entry.get("status") == "closed":
                    blocked["reason"] = "该物料方案已关闭或不在执行清单中。"
                    return db_storage.ATOMIC_NO_UPDATE
                specs = get_ecn_material_execution_specs(
                    item,
                    material_entry,
                )
                spec = next(
                    (current_spec for current_spec in specs if str(current_spec.get("key")) == str(confirmation_key)),
                    None,
                )
                if not isinstance(spec, dict):
                    blocked["reason"] = "该物料追溯责任项已发生变化。"
                    return db_storage.ATOMIC_NO_UPDATE
                traceability_tasks = material_entry.get("traceability_tasks", {})
                target = traceability_tasks.get(str(confirmation_key)) if isinstance(traceability_tasks, dict) else None
                if not isinstance(target, dict):
                    blocked["reason"] = "该物料责任项不存在。"
                    return db_storage.ATOMIC_NO_UPDATE

                if confirmed:
                    if target.get("confirmed") is True:
                        blocked["reason"] = "该物料责任项已经确认完成。"
                        return db_storage.ATOMIC_NO_UPDATE
                    if spec.get("available") is not True:
                        blocked["reason"] = "该责任项尚未进入所属追溯范围的当前负责人节点。"
                        return db_storage.ATOMIC_NO_UPDATE
                    if not can_confirm_ecn_material_spec(
                        spec,
                        actor_role,
                        current_user,
                        user_service=app.state.user_service,
                    ):
                        blocked["reason"] = "当前用户没有该物料追溯责任项的执行权限。"
                        return db_storage.ATOMIC_NO_UPDATE
                    pending_task_count = sum(
                        1
                        for current_entry in material_confirmations.values()
                        if isinstance(current_entry, dict)
                        for current_task in (
                            current_entry.get("traceability_tasks", {}).values()
                            if isinstance(current_entry.get("traceability_tasks"), dict)
                            else []
                        )
                        if isinstance(current_task, dict) and current_task.get("confirmed") is not True
                    )
                    if pending_task_count == 1 and not final_confirmation_acknowledged:
                        blocked["requires_final_confirmation"] = True
                        blocked["reason"] = "这是最后一个待确认项，需要二次确认。"
                        return db_storage.ATOMIC_NO_UPDATE
                else:
                    if not can_confirm_ecn_material_spec(
                        spec,
                        actor_role,
                        current_user,
                        user_service=app.state.user_service,
                    ):
                        blocked["reason"] = "当前用户没有该物料追溯责任项的执行权限。"
                        return db_storage.ATOMIC_NO_UPDATE
                    if target.get("confirmed") is not True:
                        blocked["reason"] = "该物料责任项尚未确认，无需取消。"
                        return db_storage.ATOMIC_NO_UPDATE
                    if str(target.get("user") or "") != current_user:
                        blocked["reason"] = "只能由原确认人取消该项确认。"
                        return db_storage.ATOMIC_NO_UPDATE
                    level = str(spec.get("level") or "")
                    stage_index = get_ecn_stage_index(spec.get("stage_index", 0))
                    later_stage_confirmed = any(
                        str(other_spec.get("level") or "") == level
                        and get_ecn_stage_index(other_spec.get("stage_index", 0)) > stage_index
                        and isinstance(traceability_tasks.get(str(other_spec.get("key"))), dict)
                        and traceability_tasks[str(other_spec.get("key"))].get("confirmed") is True
                        for other_spec in specs
                    )
                    if later_stage_confirmed:
                        blocked["reason"] = "后续负责人节点已有确认记录，不能取消前序确认。"
                        return db_storage.ATOMIC_NO_UPDATE

                target["confirmed"] = bool(confirmed)
                target["user"] = current_user
                target["role"] = actor_role
                target["time"] = operation_time
                target.setdefault("history", []).append(
                    {
                        "confirmed": bool(confirmed),
                        "user": current_user,
                        "role": actor_role,
                        "time": operation_time,
                    }
                )

                approval_log = current_ecn.setdefault("approval_log", [])
                if is_ecn_material_execution_closed(material_entry):
                    material_entry["status"] = "closed"
                    material_entry["closed_time"] = operation_time
                    append_ecn_approval_log_once(
                        approval_log,
                        {
                            "user": current_user,
                            "role": actor_role,
                            "action": f"物料方案 {execution_scheme_no(item_id)} 执行确认关闭",
                            "time": operation_time,
                        },
                    )

                active_entries = [entry for entry in material_confirmations.values() if isinstance(entry, dict)]
                if (
                    active_entries
                    and all(entry.get("status") == "closed" for entry in active_entries)
                    and is_ecn_special_execution_complete(execution_info)
                ):
                    execution_info["stage"] = ECN_EXECUTION_STAGE_COMPLETED
                    execution_info["completed_time"] = operation_time
                    current_wf["current_state"] = ECNState.CLOSED
                    current_wf["pending_roles"] = []
                    append_ecn_approval_log_once(
                        approval_log,
                        {
                            "user": current_user,
                            "role": actor_role,
                            "action": "全部物料方案执行确认完成，ECN关闭",
                            "time": operation_time,
                        },
                    )
                return current_ecn

            success = await atomic_ecn_deep_update(
                ["ecn_management_data", local_data["ecn_id"]],
                update_confirmation,
            )
            if success and not blocked["reason"]:
                sync_execution_local_data()
                execution_info = local_data.get("execution_info", {})
                if (
                    isinstance(execution_info, dict)
                    and execution_info.get("stage") == ECN_EXECUTION_STAGE_MATERIAL
                    and wf.get("current_state") == ECNState.ECN_EXECUTING
                ):
                    refresh_material_execution_controls([str(item_id)])
                else:
                    render_execution_tab()
            elif blocked.get("requires_final_confirmation"):
                sync_execution_local_data()
                refresh_material_execution_controls([str(item_id)])
                await restore_execution_scroll_state(event_client, scroll_state)
                if await request_final_material_confirmation():
                    await update_material_execution_confirmation(
                        item_id,
                        confirmation_key,
                        True,
                        True,
                    )
                return
            else:
                notify_execution_safely(
                    event_client,
                    blocked["reason"] or "确认状态保存失败，请重试。",
                    "warning",
                )
                sync_execution_local_data()
                refresh_material_execution_controls([str(item_id)])
            await restore_execution_scroll_state(event_client, scroll_state)

        async def handle_material_confirmation_change(
            event,
            item_id: str,
            confirmation_key: str,
        ) -> None:
            confirmed = bool(event.value)
            if not confirmed:
                _, _, _, traceability_tasks = get_material_execution_runtime(item_id)
                current_confirmation = traceability_tasks.get(str(confirmation_key), {})
                if not isinstance(current_confirmation, dict) or current_confirmation.get("confirmed") is not True:
                    refresh_material_execution_controls([str(item_id)])
                    return
            if confirmed and is_last_pending_material_confirmation(item_id, confirmation_key):
                if await request_final_material_confirmation():
                    await update_material_execution_confirmation(
                        item_id,
                        confirmation_key,
                        True,
                        True,
                    )
                else:
                    refresh_material_execution_controls([str(item_id)])
                return
            await update_material_execution_confirmation(
                item_id,
                confirmation_key,
                confirmed,
            )

        def render_execution_tab():
            execution_container.clear()
            material_task_controls.clear()
            material_status_controls.clear()
            access_snapshot = build_ecn_access_snapshot(app.state.user_service)
            execution_access_snapshot["value"] = access_snapshot
            with execution_container:
                execution_info = local_data.get("execution_info", {})
                if not isinstance(execution_info, dict) or not execution_info.get("stage"):
                    message = (
                        "方案评审已通过，请先在“ECN-方案”页签补齐全部物料料号；补齐后自动生成执行清单。"
                        if wf.get("current_state") == ECNState.MATERIAL_CODE_PENDING
                        else "方案评审全部通过后，系统将在这里生成分阶段执行清单。"
                    )
                    ui.label(message).classes(
                        "text-gray-500 m-8 text-center bg-white p-4 border rounded"
                    )
                    return

                stage = str(execution_info.get("stage") or "")
                stage_labels = {
                    ECN_EXECUTION_STAGE_ASSISTANT: "研发助理确认中",
                    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING: "系统内资料执行中",
                    ECN_EXECUTION_STAGE_OVERVIEW_FAILED: "系统内资料执行异常",
                    ECN_EXECUTION_STAGE_MATERIAL: "物料执行确认中",
                    ECN_EXECUTION_STAGE_COMPLETED: "执行完成",
                }
                stage_colors = {
                    ECN_EXECUTION_STAGE_ASSISTANT: "orange",
                    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING: "blue",
                    ECN_EXECUTION_STAGE_OVERVIEW_FAILED: "red",
                    ECN_EXECUTION_STAGE_MATERIAL: "purple",
                    ECN_EXECUTION_STAGE_COMPLETED: "green",
                }
                if (
                    stage == ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
                    and str(local_data.get("ecn_id") or "") not in ACTIVE_ECN_OVERVIEW_EXECUTIONS
                ):
                    stage_labels[stage] = "系统内资料执行已中断"
                    stage_colors[stage] = "red"
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("ECN执行进度").classes("text-xl font-bold text-slate-800")
                    ui.badge(
                        stage_labels.get(stage, str(stage)),
                        color=stage_colors.get(stage, "grey"),
                    ).props("outline")

                item_map = get_execution_change_items()
                assistant_can_operate = (
                    stage == ECN_EXECUTION_STAGE_ASSISTANT
                    and wf.get("current_state") == ECNState.ECN_EXECUTING
                    and can_execute_assistant
                )
                with ui.card().classes("w-full p-0 gap-0 border border-slate-300 shadow-sm overflow-hidden"):
                    with ui.row().classes(
                        "w-full items-center justify-between px-4 py-2.5 bg-slate-300 text-slate-900"
                    ):
                        ui.label("1. 资料准备与执行确认").classes("font-bold text-base")
                        ui.label("责任：研发助理").classes("text-xs text-slate-600")

                    ordinary_confirmations = execution_info.get("ordinary_confirmations", {})
                    erp_confirmation = execution_info.get("erp_confirmation", {})
                    erp_checked = isinstance(erp_confirmation, dict) and erp_confirmation.get("confirmed") is True
                    assistant_table_grid = (
                        "grid grid-cols-[64px_72px_minmax(140px,0.5fr)_minmax(200px,1fr)_"
                        "minmax(200px,1fr)_minmax(200px,1fr)_minmax(130px,0.5fr)_140px]"
                    )
                    with ui.column().classes("w-full gap-0 border-t border-slate-300"):
                        ui.label("1.1 特定事项/资料执行结果").classes(
                            "w-full px-4 py-2 text-sm font-bold text-slate-700 bg-slate-100"
                        )
                        with ui.element("div").classes("w-full overflow-x-auto"):
                            with ui.element("div").classes("min-w-[1340px] w-full"):
                                with ui.element("div").classes(
                                    f"{assistant_table_grid} bg-slate-100 border-t border-slate-300 "
                                    "text-xs font-bold text-slate-600"
                                ):
                                    for header in [
                                        "完成",
                                        "编号",
                                        "事项/方案",
                                        "项目",
                                        "执行前",
                                        "应执行内容",
                                        "确认记录",
                                        "移交负责人",
                                    ]:
                                        ui.label(header).classes(
                                            "px-3 py-2 border-r border-slate-300 last:border-r-0 "
                                            + execution_column_alignment("assistant", header)
                                        )

                                assistant_rows = (
                                    list(ordinary_confirmations.items())
                                    if isinstance(ordinary_confirmations, dict)
                                    else []
                                )
                                for row_index, (item_id, confirmation) in enumerate(assistant_rows):
                                    item = item_map.get(str(item_id), {})
                                    confirmation = confirmation if isinstance(confirmation, dict) else {}
                                    checked = confirmation.get("confirmed") is True
                                    row_bg = "bg-white" if row_index % 2 == 0 else "bg-slate-50/70"
                                    with ui.element("div").classes(
                                        f"{assistant_table_grid} {row_bg} border-t border-slate-200 "
                                        "items-stretch text-sm text-slate-700"
                                    ):
                                        with ui.element("div").classes(
                                            "px-3 py-2 border-r border-slate-200 flex items-center "
                                            + execution_column_alignment("assistant", "完成")
                                        ):
                                            checkbox = ui.checkbox(
                                                value=checked,
                                                on_change=lambda e, current_id=str(item_id), snapshot=copy.deepcopy(confirmation): (
                                                    update_assistant_execution_confirmation(
                                                        "ordinary",
                                                        current_id,
                                                        bool(e.value),
                                                        snapshot,
                                                    )
                                                ),
                                            ).props("dense color=green")
                                            if not (
                                                wf.get("current_state") == ECNState.ECN_EXECUTING
                                                and (
                                                    confirmation.get("assignee") == current_user
                                                    if confirmation.get("assignee")
                                                    else assistant_can_operate
                                                )
                                            ):
                                                checkbox.props("disable")
                                        ui.label(execution_scheme_no(str(item_id))).classes(
                                            "px-3 py-2 border-r border-slate-200 font-mono font-bold "
                                            "flex items-center " + execution_column_alignment("assistant", "编号")
                                        )
                                        ui.label(execution_scheme_title(item, include_projects=False)).classes(
                                            "px-3 py-2 border-r border-slate-200 font-semibold break-words "
                                            + execution_column_alignment("assistant", "事项/方案")
                                        )
                                        ui.label("、".join(execution_scheme_projects(item)) or "—").classes(
                                            "px-3 py-2 border-r border-slate-200 break-words "
                                            + execution_column_alignment("assistant", "项目")
                                        )
                                        ui.label(str(item.get("old_content") or "无")).classes(
                                            "px-3 py-2 border-r border-slate-200 break-all "
                                            + execution_column_alignment("assistant", "执行前")
                                        )
                                        ui.label(str(item.get("new_content") or "无")).classes(
                                            "px-3 py-2 border-r border-slate-200 break-all font-medium "
                                            + execution_column_alignment("assistant", "应执行内容")
                                        )
                                        ui.label(
                                            (
                                                f"{confirmation.get('user', '未知')}（{confirmation.get('role', '')}）\n"
                                                f"{confirmation.get('time', '')}"
                                            )
                                            if checked
                                            else "待确认"
                                        ).classes(
                                            "px-3 py-2 whitespace-pre-line text-xs "
                                            + execution_column_alignment("assistant", "确认记录")
                                            + " "
                                            + ("text-emerald-700" if checked else "text-slate-400")
                                        )

                                        render_transfer_button(
                                            str(local_data["ecn_id"]),
                                            str(item_id),
                                            confirmation,
                                            current_user,
                                            current_role,
                                            wf.get("current_state") == ECNState.ECN_EXECUTING,
                                            lambda: (
                                                sync_execution_local_data(),
                                                render_execution_tab(),
                                            ),
                                            access_snapshot=access_snapshot,
                                        )

                                erp_row_bg = "bg-white" if len(assistant_rows) % 2 == 0 else "bg-slate-50/70"
                                with ui.element("div").classes(
                                    f"{assistant_table_grid} {erp_row_bg} border-t border-slate-200 "
                                    "items-stretch text-sm text-slate-700"
                                ):
                                    with ui.element("div").classes(
                                        "px-3 py-2 border-r border-slate-200 flex items-center "
                                        + execution_column_alignment("assistant", "完成")
                                    ):
                                        erp_checkbox = ui.checkbox(
                                            value=erp_checked,
                                            on_change=lambda e, snapshot=copy.deepcopy(erp_confirmation): (
                                                update_assistant_execution_confirmation(
                                                    "erp",
                                                    None,
                                                    bool(e.value),
                                                    snapshot,
                                                )
                                            ),
                                        ).props("dense color=green")
                                        if not (
                                            wf.get("current_state") == ECNState.ECN_EXECUTING
                                            and (
                                                erp_confirmation.get("assignee") == current_user
                                                if erp_confirmation.get("assignee")
                                                else assistant_can_operate
                                            )
                                        ):
                                            erp_checkbox.props("disable")
                                    ui.label("ERP").classes(
                                        "px-3 py-2 border-r border-slate-200 font-mono font-bold "
                                        "flex items-center " + execution_column_alignment("assistant", "编号")
                                    )
                                    ui.label("ERP相关变更").classes(
                                        "px-3 py-2 border-r border-slate-200 font-semibold "
                                        + execution_column_alignment("assistant", "事项/方案")
                                    )
                                    ui.label("—").classes(
                                        "px-3 py-2 border-r border-slate-200 text-slate-400 "
                                        + execution_column_alignment("assistant", "项目")
                                    )
                                    ui.label("—").classes(
                                        "px-3 py-2 border-r border-slate-200 text-slate-400 "
                                        + execution_column_alignment("assistant", "执行前")
                                    )
                                    ui.label("ERP相关变更已执行完毕").classes(
                                        "px-3 py-2 border-r border-slate-200 font-medium "
                                        + execution_column_alignment("assistant", "应执行内容")
                                    )
                                    ui.label(
                                        (
                                            f"{erp_confirmation.get('user', '未知')}（{erp_confirmation.get('role', '')}）\n"
                                            f"{erp_confirmation.get('time', '')}"
                                        )
                                        if erp_checked
                                        else "待确认"
                                    ).classes(
                                        "px-3 py-2 whitespace-pre-line text-xs "
                                        + execution_column_alignment("assistant", "确认记录")
                                        + " "
                                        + ("text-emerald-700" if erp_checked else "text-slate-400")
                                    )

                                    render_transfer_button(
                                        str(local_data["ecn_id"]),
                                        "__erp__",
                                        erp_confirmation,
                                        current_user,
                                        current_role,
                                        wf.get("current_state") == ECNState.ECN_EXECUTING,
                                        lambda: (sync_execution_local_data(), render_execution_tab()),
                                        access_snapshot=access_snapshot,
                                    )

                    overview_results = execution_info.get("overview_results", {})
                    with ui.column().classes("w-full gap-0 border-t border-slate-300"):
                        ui.label("1.2 系统内资料方案执行结果").classes(
                            "w-full px-4 py-2 text-sm font-bold text-slate-700 bg-slate-100"
                        )
                        if isinstance(overview_results, dict) and overview_results:
                            status_meta = {
                                ECN_EXECUTION_RESULT_PENDING: ("schedule", "待执行", "text-slate-400"),
                                ECN_EXECUTION_RESULT_RUNNING: ("sync", "执行中", "text-blue-600"),
                                ECN_EXECUTION_RESULT_SUCCESS: ("check_circle", "成功", "text-green-600"),
                                ECN_EXECUTION_RESULT_FAILED: ("error", "失败", "text-red-600"),
                            }
                            result_table_grid = (
                                "grid grid-cols-[72px_minmax(180px,0.6fr)_minmax(240px,1.2fr)_"
                                "120px_minmax(200px,0.6fr)]"
                            )
                            with ui.element("div").classes("w-full overflow-x-auto"):
                                with ui.element("div").classes("min-w-[1050px] w-full"):
                                    with ui.element("div").classes(
                                        f"{result_table_grid} bg-slate-100 border-t border-slate-300 "
                                        "text-xs font-bold text-slate-600"
                                    ):
                                        for header in [
                                            "编号",
                                            "系统内资料方案",
                                            "项目",
                                            "执行状态",
                                            "执行说明",
                                        ]:
                                            ui.label(header).classes(
                                                "px-3 py-2 border-r border-slate-300 last:border-r-0 "
                                                + execution_column_alignment("overview", header)
                                            )
                                    for row_index, (item_id, result) in enumerate(overview_results.items()):
                                        item = item_map.get(str(item_id), {})
                                        result = result if isinstance(result, dict) else {}
                                        result_status = str(result.get("status") or "")
                                        icon_name, status_text, status_class = status_meta.get(
                                            result_status,
                                            ("help", "未知", "text-slate-400"),
                                        )
                                        row_bg = "bg-white" if row_index % 2 == 0 else "bg-slate-50/70"
                                        with ui.element("div").classes(
                                            f"{result_table_grid} {row_bg} border-t border-slate-200 "
                                            "items-stretch text-sm text-slate-700"
                                        ):
                                            ui.label(execution_scheme_no(str(item_id))).classes(
                                                "px-3 py-2 border-r border-slate-200 font-mono font-bold "
                                                + execution_column_alignment("overview", "编号")
                                            )
                                            ui.label(execution_scheme_title(item, include_projects=False)).classes(
                                                "px-3 py-2 border-r border-slate-200 font-semibold break-words "
                                                + execution_column_alignment("overview", "系统内资料方案")
                                            )
                                            ui.label("、".join(execution_scheme_projects(item)) or "—").classes(
                                                "px-3 py-2 border-r border-slate-200 break-words "
                                                + execution_column_alignment("overview", "项目")
                                            )
                                            with ui.row().classes(
                                                "px-3 py-2 border-r border-slate-200 items-center gap-1 "
                                                "flex-nowrap " + execution_column_alignment("overview", "执行状态")
                                            ):
                                                ui.icon(icon_name, size="xs").classes(status_class)
                                                ui.label(status_text).classes(f"text-xs font-bold {status_class}")
                                            message = str(result.get("message") or "")
                                            with ui.row().classes(
                                                "px-3 py-2 items-center gap-1 flex-nowrap min-w-0 "
                                                + execution_column_alignment("overview", "执行说明")
                                            ):
                                                ui.label(message or "—").classes(
                                                    "text-xs text-slate-600 break-words min-w-0"
                                                )
                                                if message:
                                                    ui.icon("info", size="xs").classes(
                                                        "text-slate-400 cursor-help shrink-0"
                                                    ).tooltip(message)
                        else:
                            ui.label("本单没有需要后台落盘的系统内资料方案。 ").classes(
                                "w-full px-4 py-3 text-sm text-slate-400 bg-white"
                            )

                    overview_execution_interrupted = (
                        stage == ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
                        and str(local_data.get("ecn_id") or "") not in ACTIVE_ECN_OVERVIEW_EXECUTIONS
                    )
                    if (
                        stage
                        in [
                            ECN_EXECUTION_STAGE_ASSISTANT,
                            ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
                        ]
                        or overview_execution_interrupted
                    ):
                        with ui.row().classes(
                            "w-full justify-end items-center gap-3 px-4 py-3 border-t border-slate-200 bg-slate-50"
                        ):
                            if stage == ECN_EXECUTION_STAGE_ASSISTANT:
                                ready = is_ecn_assistant_execution_ready(execution_info)
                                ui.label(
                                    "所有未移交事项勾选后即可执行；移交事项独立跟进。"
                                    if not ready
                                    else "未移交事项已确认，可执行系统内资料；移交事项独立跟进。"
                                ).classes("text-xs text-slate-500")
                                action_label = "执行系统内资料"
                            elif stage == ECN_EXECUTION_STAGE_OVERVIEW_FAILED:
                                ready = True
                                ui.label("仅重试失败项目；已经成功的项目不会重复执行。 ").classes(
                                    "text-xs text-red-600"
                                )
                                action_label = "重试失败项"
                            else:
                                ready = True
                                ui.label("检测到上次执行已中断，可从未完成项目继续执行。").classes(
                                    "text-xs text-amber-700"
                                )
                                action_label = "恢复中断的执行"
                            action_button = ui.button(
                                action_label,
                                icon="play_arrow",
                                on_click=run_overview_execution,
                            ).props("color=primary no-caps")
                            if not ready or not can_execute_assistant:
                                action_button.props("disable")

                material_is_active = (
                    stage == ECN_EXECUTION_STAGE_MATERIAL and wf.get("current_state") == ECNState.ECN_EXECUTING
                )
                with ui.card().classes("w-full p-0 gap-0 border border-slate-300 shadow-sm overflow-hidden"):
                    with ui.row().classes(
                        "w-full items-center justify-between px-4 py-2.5 bg-slate-300 text-slate-900"
                    ):
                        ui.label("2. 物料追溯执行确认").classes("font-bold text-base")
                        ui.label("各追溯范围独立；范围内负责人同节点并行、节点间串行；负责人同时落实旧料处置").classes(
                            "text-xs text-slate-600"
                        )

                    material_confirmations = execution_info.get("material_confirmations", {})
                    if stage in [
                        ECN_EXECUTION_STAGE_ASSISTANT,
                        ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
                        ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
                    ]:
                        ui.label("完成第一阶段且系统内资料全部执行成功后开放。 ").classes(
                            "w-full px-4 py-4 text-sm text-slate-400 bg-slate-50"
                        )
                    elif isinstance(material_confirmations, dict) and material_confirmations:
                        material_grid_columns = [
                            "72px",
                            "minmax(130px, 0.6fr)",
                            "minmax(90px, 0.3fr)",
                            "minmax(210px, 1fr)",
                            "minmax(210px, 1fr)",
                            "minmax(100px, 0.4fr)",
                            *["minmax(100px, 0.5fr)" for _ in ECN_TRACEABILITY_LEVELS],
                            "100px",
                        ]
                        material_grid_style = f"grid-template-columns: {' '.join(material_grid_columns)};"
                        material_table_min_width = 1012 + len(ECN_TRACEABILITY_LEVELS) * 100
                        with (
                            ui.element("div")
                            .props("id=ecn-material-execution-scroll")
                            .classes("w-full overflow-x-auto")
                        ):
                            with ui.element("div").classes("w-full").style(f"min-width: {material_table_min_width}px;"):
                                headers = [
                                    "编号",
                                    "项目",
                                    "变更类别",
                                    "变更前",
                                    "变更后",
                                    "旧料处理方式",
                                    *ECN_TRACEABILITY_LEVELS,
                                    "执行总状态",
                                ]
                                with (
                                    ui.element("div")
                                    .classes(
                                        "grid bg-slate-100 border-t border-slate-300 text-xs font-bold text-slate-600"
                                    )
                                    .style(material_grid_style)
                                ):
                                    for header in headers:
                                        ui.label(header).classes(
                                            "px-3 py-2 border-r border-slate-300 last:border-r-0 "
                                            + execution_column_alignment("material", header)
                                        )

                                for scheme_index, (item_id, material_entry) in enumerate(
                                    material_confirmations.items()
                                ):
                                    item = item_map.get(str(item_id), {})
                                    material_entry = material_entry if isinstance(material_entry, dict) else {}
                                    item_closed = material_entry.get("status") == "closed"
                                    specs = get_ecn_material_execution_specs(
                                        item,
                                        material_entry,
                                    )
                                    specs = [
                                        resolve_ecn_material_spec_responsibility(
                                            spec,
                                            user_service=app.state.user_service,
                                            access_snapshot=access_snapshot,
                                        )
                                        for spec in specs
                                    ]
                                    traceability_tasks = material_entry.get("traceability_tasks", {})
                                    specs_by_level = {
                                        level: [spec for spec in specs if str(spec.get("level") or "") == level]
                                        for level in ECN_TRACEABILITY_LEVELS
                                    }
                                    row_bg = "bg-white" if scheme_index % 2 == 0 else "bg-blue-50/35"
                                    with (
                                        ui.element("div")
                                        .classes(
                                            f"grid {row_bg} border-t border-slate-300 "
                                            "items-stretch text-sm text-slate-700"
                                        )
                                        .style(material_grid_style)
                                    ):
                                        ui.label(execution_scheme_no(str(item_id))).classes(
                                            "px-3 py-3 border-r border-slate-200 font-mono font-bold "
                                            + execution_column_alignment("material", "编号")
                                        )
                                        ui.label("\n".join(execution_scheme_projects(item)) or "—").classes(
                                            "px-3 py-3 border-r border-slate-200 font-semibold "
                                            "break-words whitespace-pre-line "
                                            + execution_column_alignment("material", "项目")
                                        )
                                        ui.label(str(item.get("change_type") or "—")).classes(
                                            "px-3 py-3 border-r border-slate-200 font-semibold break-words "
                                            + execution_column_alignment("material", "变更类别")
                                        )
                                        old_material, new_material = get_ecn_material_change_display(item)
                                        ui.label(old_material or "—").classes(
                                            "px-3 py-3 border-r border-slate-200 font-semibold "
                                            "break-words whitespace-pre-line "
                                            + execution_column_alignment("material", "变更前")
                                        )
                                        ui.label(new_material or "—").classes(
                                            "px-3 py-3 border-r border-slate-200 font-semibold "
                                            "break-words whitespace-pre-line "
                                            + execution_column_alignment("material", "变更后")
                                        )
                                        with ui.element("div").classes(
                                            "px-3 py-3 border-r border-slate-200 min-w-0 "
                                            + execution_column_alignment(
                                                "material",
                                                "旧料处理方式",
                                                flex_column=True,
                                            )
                                        ):
                                            if not is_ecn_material_disposition_required(item.get("change_type")):
                                                ui.label("不适用").classes("text-sm text-slate-400")
                                            else:
                                                disposition_measure = str(item.get("disposition_measure") or "").strip()
                                                disposition_color = {
                                                    "报废": "text-red-700",
                                                    "返工": "text-orange-600",
                                                    "有条件用完止": "text-amber-600",
                                                }.get(disposition_measure, "text-slate-700")
                                                ui.label(disposition_measure or "未配置").classes(
                                                    f"font-semibold {disposition_color} break-words"
                                                )
                                                disposition_condition = str(
                                                    item.get("disposition_condition") or ""
                                                ).strip()
                                                if disposition_condition:
                                                    ui.label(f"条件：{disposition_condition}").classes(
                                                        "mt-1 text-xs text-slate-500 break-words"
                                                    )

                                        for level in ECN_TRACEABILITY_LEVELS:
                                            level_specs = specs_by_level[level]
                                            with ui.element("div").classes(
                                                "px-2 py-2 border-r border-slate-200 min-w-0 "
                                                + execution_column_alignment(
                                                    "material",
                                                    level,
                                                    flex_column=True,
                                                )
                                            ):
                                                if not level_specs:
                                                    ui.label("—").classes("text-sm text-slate-300")
                                                    continue
                                                grouped_specs: dict[int, list[dict]] = {}
                                                for spec in level_specs:
                                                    stage_index = get_ecn_stage_index(spec.get("stage_index", 0))
                                                    grouped_specs.setdefault(stage_index, []).append(spec)
                                                show_stage = len(grouped_specs) > 1
                                                for stage_index, stage_specs in grouped_specs.items():
                                                    if show_stage:
                                                        ui.label(f"串行节点 {stage_index + 1}").classes(
                                                            "text-[11px] font-semibold text-slate-400"
                                                        )
                                                    for spec in stage_specs:
                                                        key = str(spec.get("key"))
                                                        confirmation = (
                                                            traceability_tasks.get(key, {})
                                                            if isinstance(traceability_tasks, dict)
                                                            else {}
                                                        )
                                                        confirmation = (
                                                            confirmation if isinstance(confirmation, dict) else {}
                                                        )
                                                        checked = confirmation.get("confirmed") is True
                                                        available = spec.get("available") is True
                                                        can_confirm = (
                                                            material_is_active
                                                            and not item_closed
                                                            and not checked
                                                            and available
                                                            and can_confirm_ecn_material_spec(
                                                                spec,
                                                                current_role,
                                                                current_user,
                                                                access_snapshot=access_snapshot,
                                                            )
                                                        )
                                                        can_cancel = (
                                                            material_is_active
                                                            and can_confirm_ecn_material_spec(
                                                                spec,
                                                                current_role,
                                                                current_user,
                                                                access_snapshot=access_snapshot,
                                                            )
                                                            and can_cancel_material_confirmation(
                                                                spec,
                                                                confirmation,
                                                                specs,
                                                                traceability_tasks
                                                                if isinstance(traceability_tasks, dict)
                                                                else {},
                                                                item_closed,
                                                            )
                                                        )
                                                        checkbox = (
                                                            ui.checkbox(
                                                                compact_material_confirmation_label(spec),
                                                                value=checked,
                                                                on_change=lambda e, current_id=str(item_id), current_key=key: (
                                                                    handle_material_confirmation_change(
                                                                        e,
                                                                        current_id,
                                                                        current_key,
                                                                    )
                                                                ),
                                                            )
                                                            .props("dense color=green")
                                                            .classes(
                                                                "w-full text-xs "
                                                                + execution_column_alignment(
                                                                    "material",
                                                                    level,
                                                                )
                                                            )
                                                        )
                                                        if not can_confirm and not can_cancel:
                                                            checkbox.props("disable")
                                                        with checkbox:
                                                            tooltip = ui.tooltip(
                                                                material_confirmation_tooltip_text(
                                                                    spec,
                                                                    confirmation,
                                                                    available,
                                                                    can_cancel,
                                                                )
                                                            ).classes("text-xs whitespace-pre-line")
                                                        material_task_controls.setdefault(
                                                            str(item_id),
                                                            {},
                                                        )[key] = {
                                                            "checkbox": checkbox,
                                                            "tooltip": tooltip,
                                                        }
                                                        orphaned = (
                                                            material_is_active
                                                            and not checked
                                                            and available
                                                            and is_ecn_material_spec_orphaned(
                                                                spec,
                                                                user_service=app.state.user_service,
                                                                access_snapshot=access_snapshot,
                                                            )
                                                        )
                                                        if orphaned:
                                                            ui.label("负责人已停用、离职或无可用权限").classes(
                                                                "text-[11px] font-semibold text-red-600"
                                                            )
                                                            if can_execute_ecn_assistant_stage(
                                                                current_role,
                                                                current_user,
                                                                access_snapshot=access_snapshot,
                                                            ):

                                                                def refresh_after_transfer():
                                                                    if sync_execution_local_data():
                                                                        render_execution_tab()

                                                                ui.button(
                                                                    "立即改派",
                                                                    icon="person_add",
                                                                    on_click=lambda _, current_id=str(item_id), current_key=key, current_confirmation=confirmation: (
                                                                        open_material_transfer_dialog(
                                                                            str(local_data["ecn_id"]),
                                                                            current_id,
                                                                            current_key,
                                                                            current_confirmation,
                                                                            current_user,
                                                                            current_role,
                                                                            refresh_after_transfer,
                                                                        )
                                                                    ),
                                                                ).props("flat dense size=sm color=negative")

                                        total_count = len(specs)
                                        completed_count = sum(
                                            1
                                            for task in (
                                                traceability_tasks.values()
                                                if isinstance(traceability_tasks, dict)
                                                else []
                                            )
                                            if isinstance(task, dict) and task.get("confirmed") is True
                                        )
                                        with ui.element("div").classes(
                                            "px-3 py-3 flex flex-col justify-center gap-1 "
                                            + execution_column_alignment(
                                                "material",
                                                "执行总状态",
                                                flex_column=True,
                                            )
                                        ):
                                            status_badge = ui.badge(
                                                "已关闭" if item_closed else "执行中",
                                                color="green" if item_closed else "orange",
                                            ).props("outline")
                                            progress_label = ui.label(f"{completed_count}/{total_count}").classes(
                                                "text-xs text-slate-500"
                                            )
                                            material_status_controls[str(item_id)] = {
                                                "badge": status_badge,
                                                "progress": progress_label,
                                            }
                    else:
                        ui.label("本单没有物料变更方案；系统内资料和所有特定事项完成后自动关闭ECN。 ").classes(
                            "w-full px-4 py-4 text-sm text-slate-400 bg-white"
                        )

        render_execution_tab()

    return (
        render_execution_tab,
        material_task_controls,
        capture_execution_scroll_state,
        execution_container,
        refresh_material_execution_controls,
        restore_execution_scroll_state,
    )
