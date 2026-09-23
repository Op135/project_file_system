"""ECN 企业微信待办提醒：复用首页角标规则，调试转发与正式收件人严格分离。"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from html import escape

from nicegui import app

from ... import db_storage
from ...ecn_access import (
    build_ecn_access_snapshot,
    can_confirm_ecn_material_spec,
    can_execute_ecn_assistant_stage,
    can_approve_ecn_validation_report,
    can_view_ecn_validation_report,
    can_view_ecn,
    get_ecn_execution_assignment_issues,
    is_ecn_pending_for_user,
    resolve_ecn_material_spec_responsibility,
)
from ...ecn_management_config import (
    ECN_DATA_KEY,
    ECN_WECOM_CONFIG,
    ECNState,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_STAGE_OVERVIEW_FAILED,
    ECN_EXECUTION_STAGE_OVERVIEW_RUNNING,
    get_ecn_scheme_target_projects,
    get_ecn_material_execution_specs,
    get_ecn_missing_material_code_items,
    get_ecn_level_code,
    get_ecn_validation_report,
    is_ecn_scheme_ready_for_review,
    get_ecn_special_confirmations,
    ECN_LEVEL_COMPLEX,
)
from ...wecom_service import resolve_wecom_recipients, send_wecom_text_message, send_wecom_textcard_message
from ...notification_recipients import is_system_admin_username
from .special_task_messages import get_special_message_item, get_special_message_items, build_special_card
from .task_labels import compact_material_confirmation_label, execution_scheme_no

logger = logging.getLogger(__name__)
NOTIFICATION_STATE_KEY = "ecn_wecom_notification_state"
_scan_lock = asyncio.Lock()


def material_task_summary(record: dict, item_id: str, spec: dict) -> str:
    """把内部方案UUID和责任项键转换为通知中可直接理解的业务摘要。"""
    items = [item for item in record.get("change_items", []) if isinstance(item, dict)]
    item = next((item for item in items if str(item.get("item_id")) == str(item_id)), {})
    scheme_no = execution_scheme_no(record, item_id)
    projects = "、".join(get_ecn_scheme_target_projects({"target_projects": item.get("projects", [])})) or "—"
    change_type = str(item.get("change_type") or "物料变更")
    level = str(spec.get("level") or "未指定范围")
    responsible = compact_material_confirmation_label(spec)
    project = str(spec.get("project") or "").strip()
    if project and responsible.startswith(f"{project} · "):
        responsible = responsible[len(project) + 3 :]
    return f"物料方案 {scheme_no}｜项目：{projects}｜{change_type}｜追溯：{level}｜确认：{responsible}"


def collect_pending_users(record: dict, service, access_snapshot: dict | None = None) -> dict[str, str]:
    """保留首页可见且确有该单待办的在职用户，不按岗位名称额外扩大正式收件范围。"""
    snapshot = access_snapshot or build_ecn_access_snapshot(service)
    assignment_issues = get_ecn_execution_assignment_issues(
        record, user_service=service, access_snapshot=snapshot
    )
    pending: dict[str, str] = {}
    users = snapshot.get("users", {})
    for username, info in users.items() if isinstance(users, dict) else []:
        if (
            is_system_admin_username(username)
            or not isinstance(info, dict)
            or info.get("status", "active") != "active"
        ):
            continue
        role = str(info.get("role") or "")
        if can_view_ecn(
            role, username, user_service=service, access_snapshot=snapshot
        ) and is_ecn_pending_for_user(
            record,
            username,
            role,
            user_service=service,
            access_snapshot=snapshot,
            assignment_issues=assignment_issues,
        ):
            pending[username] = role
    return pending


def pending_task_details(
    record: dict,
    pending: dict[str, str],
    service,
    access_snapshot: dict | None = None,
) -> dict[str, list[str]]:
    """细化当前用户可执行的物料责任项，用于正文和去重指纹。"""
    state = record.get("workflow", {}).get("current_state")
    execution = record.get("execution_info", {})
    stage = execution.get("stage")
    special_tasks: dict[str, list[str]] = {name: [] for name in pending}
    snapshot = access_snapshot or build_ecn_access_snapshot(service)
    assignment_issues = get_ecn_execution_assignment_issues(
        record, user_service=service, access_snapshot=snapshot
    )
    if state == ECNState.ECN_EXECUTING:
        for item in get_special_message_items(record, list(pending)):
            special_tasks[item["assignee"]].append(
                f"特定事项\n事项/方案：{item['subject']}\n项目：{item['projects']}\n应执行内容：{item['content']}"
            )
    if state == ECNState.ECN_EXECUTING and stage == ECN_EXECUTION_STAGE_MATERIAL:
        items = {str(item.get("item_id")): item for item in record.get("change_items", []) if isinstance(item, dict)}
        tasks = special_tasks
        for item_id, entry in execution.get("material_confirmations", {}).items():
            for raw_spec in get_ecn_material_execution_specs(items.get(str(item_id), {}), entry):
                spec = resolve_ecn_material_spec_responsibility(
                    raw_spec,
                    user_service=service,
                    access_snapshot=snapshot,
                )
                if spec.get("available") is not True:
                    continue
                for name, role in pending.items():
                    if can_confirm_ecn_material_spec(
                        spec,
                        role,
                        name,
                        user_service=service,
                        access_snapshot=snapshot,
                    ):
                        tasks[name].append(material_task_summary(record, str(item_id), spec))
        for issue in assignment_issues:
            for name, role in pending.items():
                if not can_execute_ecn_assistant_stage(
                    role, name, user_service=service, access_snapshot=snapshot
                ):
                    continue
                item = items.get(issue["item_id"], {})
                entry = execution.get("material_confirmations", {}).get(issue["item_id"], {})
                spec = next(
                    (
                        current
                        for current in get_ecn_material_execution_specs(item, entry)
                        if str(current.get("key")) == issue["key"]
                    ),
                    {},
                )
                tasks[name].append(
                    f"负责人异常，请改派\n{material_task_summary(record, issue['item_id'], spec)}\n"
                    f"原负责人：{issue['owner']}"
                )
        return tasks
    if state == ECNState.MATERIAL_CODE_PENDING:
        code_tasks = []
        for item in get_ecn_missing_material_code_items(record):
            fields = item.get("fields", [])
            field_names = [str(value) for value in fields] if isinstance(fields, list) else []
            code_tasks.append(
                f"补充物料料号\n方案：{item['scheme_no']}\n缺少：{'、'.join(field_names)}"
            )
        return {name: list(code_tasks) for name in pending}
    if state == ECNState.ECN_SCHEMING and get_ecn_level_code(record) == ECN_LEVEL_COMPLEX:
        validation_tasks: dict[str, list[str]] = {name: [] for name in pending}
        items = record.get("change_items", [])
        for index, item in enumerate(items if isinstance(items, list) else [], start=1):
            if not isinstance(item, dict):
                continue
            report = get_ecn_validation_report(item)
            if report.get("required") is not True:
                continue
            status = str(report.get("status") or "pending_upload")
            author = str(item.get("author") or "")
            subject = str(item.get("label") or item.get("change_type") or "变更方案")
            if author in validation_tasks and status in {"pending_upload", "rejected"}:
                suffix = f"；审核意见：{report.get('review_note')}" if status == "rejected" and report.get("review_note") else ""
                validation_tasks[author].append(
                    f"上传验证报告｜方案 #{index:02d}｜{subject}{suffix}"
                )
            if status == "pending_review":
                for name, role in pending.items():
                    if name == author:
                        continue
                    if can_view_ecn_validation_report(
                        role, name, user_service=service, access_snapshot=snapshot
                    ) and can_approve_ecn_validation_report(
                        role, name, user_service=service, access_snapshot=snapshot
                    ):
                        validation_tasks[name].append(
                            f"审批验证报告｜方案 #{index:02d}｜{subject}｜出具人：{author or '未知'}"
                        )
        if any(validation_tasks.values()):
            return {
                name: tasks or ["完善影响评估或本人方案"]
                for name, tasks in validation_tasks.items()
            }
    for issue in assignment_issues:
        if issue["kind"] == "special":
            detail = get_special_message_item(record, issue["key"], issue["owner"])
            issue_text = (
                f"负责人异常，请改派\n事项/方案：{detail['subject']}\n项目：{detail['projects']}\n"
                f"应执行内容：{detail['content']}\n原负责人：{issue['owner']}"
            )
        else:
            issue_text = (
                f"负责人异常，请改派\n责任项：{issue['item_id']} / {issue['key']}\n原负责人：{issue['owner']}"
            )
        for name, role in pending.items():
            if can_execute_ecn_assistant_stage(
                role, name, user_service=service, access_snapshot=snapshot
            ):
                special_tasks[name].append(issue_text)
    if state == ECNState.ECN_EXECUTING:
        description = {
            ECN_EXECUTION_STAGE_ASSISTANT: "核对资料及ERP完成情况，并启动系统内资料执行",
            ECN_EXECUTION_STAGE_OVERVIEW_RUNNING: "系统内资料正在执行，请关注执行结果",
            ECN_EXECUTION_STAGE_OVERVIEW_FAILED: "系统内资料执行失败，请检查并重试",
        }.get(stage, "处理执行待办")
    elif state in {ECNState.DRAFT, ECNState.REJECTED}:
        description = "完善并提交ECR申请" if state == ECNState.DRAFT else "修改被驳回的ECR申请并重新提交"
    elif state in {ECNState.ECR_REVIEWING, ECNState.ECN_REVIEWING}:
        description = "处理当前审批节点"
    elif is_ecn_scheme_ready_for_review(record):
        description = "所有参与人已确认且方案覆盖完整，请发起方案评审"
    else:
        description = "完善影响评估或本人方案；被驳回方案请整改后重新确认"

    def needs_assistant_reminder(name: str, role: str) -> bool:
        if state != ECNState.ECN_EXECUTING:
            return True
        if not can_execute_ecn_assistant_stage(
            role, name, user_service=service, access_snapshot=snapshot
        ):
            return False
        entries = list(get_ecn_special_confirmations(execution).values())
        self_returned = any(
            item.get("suppress_assignment_notice_for") == name and item.get("confirmed") is not True for item in entries
        )
        if self_returned and stage == ECN_EXECUTION_STAGE_ASSISTANT:
            # 仅免去自己回收事项的提醒；其他未移交事项仍按原流程通知。
            return any(
                not item.get("assignee")
                and item.get("confirmed") is not True
                and item.get("suppress_assignment_notice_for") != name
                for item in entries
            )
        return True

    return {
        name: special_tasks[name] + ([description] if needs_assistant_reminder(name, role) else [])
        for name, role in pending.items()
    }


def build_notification_fingerprint(record: dict, tasks: dict, config: dict) -> str:
    workflow = record.get("workflow", {})
    payload = {
        "state": workflow.get("current_state"),
        "phase": workflow.get("current_phase"),
        "round": workflow.get("approval_round"),
        "step": workflow.get("current_step_index"),
        "stage": record.get("execution_info", {}).get("stage"),
        "participants": workflow.get("scheme_participants", {}),
        "tasks": tasks,
        "special_assignments": {
            key: item.get("assignment_revision")
            for key, item in get_ecn_special_confirmations(record.get("execution_info")).items()
            if item.get("assignee") and item.get("confirmed") is not True
        },
        "test_mode": config["test_mode"],
        "test_notify_targets": config["test_notify_targets"] if config["test_mode"] else [],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build_personal_task_tokens(record: dict, name: str, tasks: list[str]) -> list[str]:
    """生成个人待办的稳定标识；任务减少不应让其余人员重新收到提醒。"""
    workflow = record.get("workflow", {})
    workflow = workflow if isinstance(workflow, dict) else {}
    execution = record.get("execution_info", {})
    execution = execution if isinstance(execution, dict) else {}
    participants = workflow.get("scheme_participants", {})
    participant_status = participants.get(name) if isinstance(participants, dict) else None
    context = {
        "state": workflow.get("current_state"),
        "phase": workflow.get("current_phase"),
        "round": workflow.get("approval_round"),
        "step": workflow.get("current_step_index"),
        "stage": execution.get("stage"),
        "participant_status": participant_status,
    }
    return sorted(
        hashlib.sha256(
            json.dumps(
                {"context": context, "task": str(task)},
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        for task in dict.fromkeys(tasks)
    )


def build_personal_notification_fingerprint(name: str, task_tokens: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"name": name, "tasks": task_tokens},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


async def resolve_delivery_targets(pending: dict[str, str], config: dict, service) -> dict[str, list[str]]:
    """返回企业微信账号到原始待办用户名的映射；调试解析失败绝不回退到正式人员。"""
    if config["test_mode"]:
        touser = await resolve_wecom_recipients(config["test_notify_targets"], fallback_touser="")
        return {userid: list(pending) for userid in touser.split("|") if userid and userid != "@all"}
    result: dict[str, list[str]] = {}
    database_mode = getattr(service, "storage_mode", "legacy_excel") == "database"
    bindings = service.list_wecom_bindings() if database_mode else {}
    for name in pending:
        if is_system_admin_username(name):
            continue
        if database_mode:
            touser = str(bindings.get(name, {}).get("external_userid") or "").strip()
        else:
            touser = await resolve_wecom_recipients([{"name": name}], fallback_touser="")
        if not touser:
            logger.warning("ECN待办人员未匹配到企业微信账号，跳过：%s", name)
        for userid in touser.split("|"):
            if userid and userid != "@all":
                result.setdefault(userid, []).append(name)
    return result


def build_notification_content(
    record: dict, tasks: dict[str, list[str]], names: list[str], config: dict, *, is_cc: bool = False
) -> str:
    basic = record.get("basic_info", {})
    lines = [
        "【ECN待办提醒 · 调试转发】"
        if config["test_mode"]
        else ("【ECN待办提醒 · 研发经理抄送】" if is_cc else "【ECN待办提醒】"),
        f"单号：{record.get('ecn_id', '')}",
        f"主题：{basic.get('title') or '工程变更申请'}",
        f"申请人：{basic.get('applicant', '')}",
        f"当前状态：{record.get('workflow', {}).get('current_state', '')}",
        f"{'原应通知人员' if config['test_mode'] else '待处理人员'}：{'、'.join(names)}",
    ]
    for name in names:
        lines.append(f"{name}：{'；'.join(tasks.get(name, []))}")
    if config["test_mode"]:
        lines.append("调试模式：本消息仅发给配置的调试收件人，未发给上述实际待办人员。")
    elif is_cc:
        lines.append("观察抄送：汇总上述人员的待办提醒，便于观察提醒内容及频度；如含本人待办，请按原职责处理。")
    text = "\n".join(lines)
    # 企业微信文本有长度限制；完整责任项可从系统列表查看。
    if len(text.encode("utf-8")) > 1700:
        text = text.encode("utf-8")[:1600].decode("utf-8", errors="ignore") + "\n更多待办请进入系统查看。"
    return text


def build_notification_card(
    record: dict,
    tasks: dict[str, list[str]],
    names: list[str],
    config: dict,
    *,
    is_cc: bool = False,
    include_special: bool = True,
) -> tuple[str, str]:
    """用户内容先转义再按字节裁剪，保留完整HTML标签及实体。"""
    special = get_special_message_items(record, names) if include_special else []
    if special:
        return build_special_card(
            record,
            special,
            test_mode=config["test_mode"],
            is_cc=is_cc,
            observer_names=names,
        )
    state = record.get("workflow", {}).get("current_state", "")
    event = (
        "待发起方案评审"
        if is_ecn_scheme_ready_for_review(record)
        else {
            ECNState.DRAFT: "申请待提交",
            ECNState.REJECTED: "申请待修改",
            ECNState.ECR_REVIEWING: "ECR待审批",
            ECNState.ECN_REVIEWING: "方案待审批",
            ECNState.MATERIAL_CODE_PENDING: "物料料号待补充",
            ECNState.ECN_SCHEMING: "方案待完善与确认",
            ECNState.ECN_EXECUTING: "执行待办",
        }.get(state, "待办提醒")
    )
    if state == ECNState.ECN_EXECUTING and any(
        task.startswith("特定事项") for name in names for task in tasks.get(name, [])
    ):
        event = "特定事项待确认"
    title = f"🔧【ECN工程变更】{event}"
    nature = (
        "调试转发 · 未通知实际处理人"
        if config["test_mode"]
        else ("研发经理抄送 · 含本人待办时请处理" if is_cc else "待办提醒")
    )
    basic = record.get("basic_info", {})
    display_names = list(dict.fromkeys(names))
    names_text = "、".join(display_names)
    all_tasks = list(dict.fromkeys(task for name in names for task in tasks.get(name, [])))
    display_tasks = all_tasks[:3]
    lines = [
        f"单号：{record.get('ecn_id', '')}",
        f"主题：{str(basic.get('title') or '工程变更申请')[:45]}",
        f"{'原应通知人员' if config['test_mode'] else '待处理人员'}：{names_text}",
        *[f"待办：{task}" for task in display_tasks],
    ]
    if len(all_tasks) > len(display_tasks):
        lines.append(f"另有{len(all_tasks) - len(display_tasks)}项待办，请进入系统查看")
    prefix = f'<div class="gray">{escape(nature)}</div><div class="normal">'
    suffix = "</div>"
    hint = "…（进入系统查看完整待办）"
    budget = 512 - len((prefix + suffix + hint).encode("utf-8"))
    parts: list[str] = []
    used = 0
    for char in "\n".join(lines):
        # 客户端会忽略正文中的br，使用与灰色说明相同的独立div分行。
        part = '</div><div class="normal">' if char == "\n" else escape(char)
        size = len(part.encode("utf-8"))
        if used + size > budget:
            parts.append(hint)
            break
        parts.append(part)
        used += size
    return title, prefix + "".join(parts) + suffix


async def check_and_send_ecn_reminders(*, config=None, user_service=None, storage=None) -> tuple[int, int]:
    """定时扫描最新待办；独立通知状态保存在SQLite中，不改动ECN业务单据。"""
    settings = config if config is not None else ECN_WECOM_CONFIG
    if not settings["enabled"] or _scan_lock.locked():
        return 0, 0
    service = user_service or getattr(app.state, "user_service", None)
    if service is None:
        return 0, 0
    storage = storage or db_storage
    sent, failed = 0, 0
    async with _scan_lock:
        all_records = await storage.get_fresh_item(ECN_DATA_KEY, {})
        if not isinstance(all_records, dict):
            return 0, 0
        access_snapshot = build_ecn_access_snapshot(service)
        for ecn_id, record in all_records.items():
            if not isinstance(record, dict):
                continue
            from .execution_review_notifications import send_execution_review_notices
            from .transfer_notifications import send_transfer_cancellations

            try:
                notice_sent, notice_failed = await send_execution_review_notices(
                    ecn_id, record, settings, service, storage
                )
                sent += notice_sent
                failed += notice_failed
            except Exception:
                logger.exception("ECN执行复核撤销告知检查失败，下次重试：%s", ecn_id)
                failed += 1
            try:
                notice_sent, notice_failed = await send_transfer_cancellations(
                    ecn_id, record, settings, service, storage
                )
                sent += notice_sent
                failed += notice_failed
            except Exception:
                logger.exception("特定事项取消告知检查失败，下次重试：%s", ecn_id)
                failed += 1
            pending = collect_pending_users(record, service, access_snapshot)
            if not pending:
                # 清除已解决待办的去重状态，后续重新出现同一待办时可以再次通知。
                await storage.del_deep_item([NOTIFICATION_STATE_KEY, ecn_id])
                continue
            tasks = pending_task_details(record, pending, service, access_snapshot)
            pending = {name: role for name, role in pending.items() if tasks.get(name)}
            if not pending:
                continue
            initial_tokens = {
                name: build_personal_task_tokens(record, name, tasks[name])
                for name in pending
            }
            # 先解析初始个人路线，再读取一次最新业务状态；解析期间单据若已变化，本轮不发送旧待办。
            resolved_targets = await resolve_delivery_targets(pending, settings, service)
            initial_targets: dict[str, dict[str, list[str]]] = {
                name: {} for name in pending
            }
            for recipient, names in resolved_targets.items():
                for name in names:
                    if name in initial_targets:
                        initial_targets[name][recipient] = [name]
            cc_recipients: set[str] = set()
            if not settings["test_mode"] and settings["cc_manager_enabled"]:
                try:
                    cc_users = await resolve_wecom_recipients([{"position": "研发经理"}], fallback_touser="")
                    cc_recipients = {userid for userid in cc_users.split("|") if userid and userid != "@all"}
                    if not cc_recipients:
                        logger.warning("ECN研发经理抄送未匹配到微信账号：%s", ecn_id)
                except Exception:
                    logger.exception("ECN研发经理抄送解析失败：%s", ecn_id)
            # 同一单据的一轮收件人发送共用一次最新状态和权限快照。
            fresh_records = await storage.get_fresh_item(ECN_DATA_KEY, {})
            fresh = fresh_records.get(ecn_id, {}) if isinstance(fresh_records, dict) else {}
            fresh_access_snapshot = build_ecn_access_snapshot(service)
            fresh_pending = collect_pending_users(fresh, service, fresh_access_snapshot)
            fresh_tasks = pending_task_details(fresh, fresh_pending, service, fresh_access_snapshot)
            fresh_pending = {
                name: role for name, role in fresh_pending.items() if fresh_tasks.get(name)
            }
            if not fresh_pending:
                await storage.del_deep_item([NOTIFICATION_STATE_KEY, ecn_id])
                continue
            fresh_tokens = {
                name: build_personal_task_tokens(fresh, name, fresh_tasks[name])
                for name in fresh_pending
            }

            # 已经不再有待办的人从状态中移除；同一任务日后重新出现时按新责任通知。
            notification_states = await storage.get_fresh_item(NOTIFICATION_STATE_KEY, {})
            ecn_state = (
                notification_states.get(ecn_id, {})
                if isinstance(notification_states, dict)
                else {}
            )
            known_users = ecn_state.get("users", {}) if isinstance(ecn_state, dict) else {}
            for stale_name in set(known_users if isinstance(known_users, dict) else {}) - set(fresh_pending):
                await storage.del_deep_item(
                    [NOTIFICATION_STATE_KEY, ecn_id, "users", str(stale_name)]
                )

            for name, role in fresh_pending.items():
                if initial_tokens.get(name) != fresh_tokens[name]:
                    continue
                current_tokens = fresh_tokens[name]

                # 先从所有既有投递路线中移除已经完成的任务标识。这样同一责任以后重新出现时会再次通知。
                def prune_completed(current):
                    user_state = current if isinstance(current, dict) else {}
                    recipients = user_state.get("recipients", {})
                    if not isinstance(recipients, dict):
                        recipients = {}
                    current_set = set(current_tokens)
                    for delivery in recipients.values():
                        if not isinstance(delivery, dict):
                            continue
                        notified = delivery.get("notified_task_tokens", [])
                        delivery["notified_task_tokens"] = [
                            token for token in notified if token in current_set
                        ] if isinstance(notified, list) else []
                    user_state["recipients"] = recipients
                    return user_state

                await storage.atomic_deep_update(
                    [NOTIFICATION_STATE_KEY, ecn_id, "users", name],
                    prune_completed,
                )

                personal_targets = initial_targets.get(name, {})
                delivery_jobs: dict[str, bool] = {
                    recipient: False for recipient in personal_targets
                }
                if personal_targets and not settings["test_mode"]:
                    for recipient in cc_recipients:
                        # 研发经理本人就是实际收件人时只发实际待办，不重复抄送。
                        delivery_jobs.setdefault(recipient, True)
                if not delivery_jobs:
                    logger.warning(
                        "ECN个人待办没有可用收件人，未发送：%s %s（test_mode=%s）",
                        ecn_id,
                        name,
                        settings["test_mode"],
                    )
                    continue

                fingerprint = build_personal_notification_fingerprint(name, current_tokens)
                for recipient, is_cc in delivery_jobs.items():
                    route_key = f"{'cc' if is_cc else 'primary'}:{recipient}"
                    state_path = [
                        NOTIFICATION_STATE_KEY,
                        ecn_id,
                        "users",
                        name,
                        "recipients",
                        route_key,
                    ]
                    now = time.time()
                    token = uuid.uuid4().hex
                    claimed = False

                    def claim(current):
                        nonlocal claimed
                        delivery = current if isinstance(current, dict) else {}
                        notified = delivery.get("notified_task_tokens", [])
                        notified_set = set(notified) if isinstance(notified, list) else set()
                        current_set = set(current_tokens)
                        notified_set &= current_set
                        has_new_responsibility = bool(current_set - notified_set)
                        repeat_due = (
                            delivery.get("sent_at", 0)
                            <= now - settings["repeat_hours"] * 3600
                        )
                        if delivery.get("lease_until", 0) > now:
                            return storage.ATOMIC_NO_UPDATE
                        if not has_new_responsibility and not repeat_due:
                            if set(notified if isinstance(notified, list) else []) != notified_set:
                                delivery["notified_task_tokens"] = sorted(notified_set)
                                return delivery
                            return storage.ATOMIC_NO_UPDATE
                        if delivery.get("attempted_at", 0) > now - settings["retry_seconds"]:
                            return storage.ATOMIC_NO_UPDATE
                        delivery.update(
                            token=token,
                            lease_until=now + 300,
                            attempted_at=now,
                        )
                        claimed = True
                        return delivery

                    if not await storage.atomic_deep_update(state_path, claim) or not claimed:
                        continue
                    success = False
                    try:
                        if settings["public_base_url"]:
                            title, description = build_notification_card(
                                fresh, fresh_tasks, [name], settings, is_cc=is_cc
                            )
                            success, message = await send_wecom_textcard_message(
                                description,
                                recipient,
                                title=title,
                                link_url=f"{settings['public_base_url']}/ecn_management",
                                module="ecn_management",
                                business_key=f"{ecn_id}:{name}:{fingerprint}",
                            )
                        else:
                            # 卡片必须带有效链接；未配置系统地址时保留文字提醒。
                            success, message = await send_wecom_text_message(
                                build_notification_content(
                                    fresh, fresh_tasks, [name], settings, is_cc=is_cc
                                ),
                                recipient,
                                module="ecn_management",
                                business_key=f"{ecn_id}:{name}:{fingerprint}",
                                message_type="pending",
                                link_url="",
                                # ECN自身重试会重新检查待办/调试开关，避免全局重试发送旧任务或通知其它人员。
                                retry_tracking=False,
                                alert_on_max_failure=False,
                            )
                        sent += int(success)
                        failed += int(not success)
                        if not success:
                            logger.warning("ECN微信发送失败，将按配置重试：%s %s", ecn_id, message)
                    except Exception:
                        failed += 1
                        logger.exception("ECN微信发送异常：%s", ecn_id)
                    finally:

                        def finish(current):
                            delivery = current if isinstance(current, dict) else {}
                            if delivery.get("token") != token:
                                return storage.ATOMIC_NO_UPDATE
                            delivery["lease_until"] = 0
                            if success:
                                delivery["sent_at"] = time.time()
                                delivery["notified_task_tokens"] = list(current_tokens)
                            return delivery

                        await storage.atomic_deep_update(state_path, finish)
    return sent, failed


def init_ecn_reminder_task() -> None:
    if not ECN_WECOM_CONFIG["enabled"]:
        logger.info("ECN企业微信提醒已通过配置禁用。")
        return

    async def check():
        try:
            sent, failed = await check_and_send_ecn_reminders()
            if sent or failed:
                logger.info("ECN微信提醒：成功 %s 条，失败 %s 条", sent, failed)
        except Exception:
            logger.exception("ECN微信待办检查失败，等待下次检查")

    app.timer(ECN_WECOM_CONFIG["initial_delay_seconds"], check, once=True)
    app.timer(ECN_WECOM_CONFIG["check_interval_seconds"], check)
