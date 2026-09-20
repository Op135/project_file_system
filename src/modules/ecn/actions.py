"""ECN 应用服务：所有写操作在最新单据上复核权限、阶段及编辑冲突。"""

import asyncio
import copy
import uuid
from datetime import (
    datetime,
)
from pathlib import (
    Path,
)

from nicegui import (
    app,
)

from ... import (
    db_storage,
)
from ...ecn_access import (
    can_create_ecn_request,
    can_edit_ecn_material_codes,
    can_edit_ecn_impact,
    can_edit_ecn_scheme,
    can_submit_ecn_scheme_review,
    get_active_ecn_actor_role,
)
from ...ecn_management_config import (
    ECN_REQUIRE_REJECTED_ITEM_SELECTION,
    ECN_WECOM_CONFIG,
    ECNState,
    ECN_SCHEME_GROUP_MATERIAL,
    apply_ecn_material_code_values,
    classify_ecn_change_item,
    get_ecn_material_code_entries,
    get_ecn_missing_material_code_items,
    get_ecn_scheme_coverage,
    is_ecn_scheme_ready_for_review,
    reject_ecn_scheme_items,
)
from ...ecn_workflow import (
    build_ecn_execution_info_from_workflows,
    cancel_ecr_approval,
    ecn_workflow_error_message,
    finish_ecr_approval,
    finish_scheme_approval,
    get_ecr_pending_usernames,
    get_scheme_pending_usernames,
    is_ecn_database_workflow_enabled,
    is_ecr_assigned_approver,
    is_scheme_assigned_approver,
    start_ecr_approval,
    start_scheme_approval,
)
from .approval_transaction import (
    ApprovalTransaction,
)
from .editing import (
    ECNConflict,
    confirm_participant,
    delete_scheme,
    merge_fields,
    require_current_context,
    save_scheme,
    update_review,
)
from .models import (
    append_ecn_approval_log_once,
    get_ecn_template,
)
from .repository import (
    mutate_record,
)
from ...issue_workflow_utils import schedule_background_task
from ...workflow_notifications import send_workflow_completion_cc
from .attachments import cleanup_staged, publish_pending, validate_scheme_attachments


def require_permission(check, role, user, service):
    if not check(role, user, user_service=service):
        raise ECNConflict("当前用户没有执行此操作的权限。")


def require_active_actor_role(user: str, role: str, service) -> str:
    actor_role = get_active_ecn_actor_role(user, role, user_service=service)
    if actor_role is None:
        raise ECNConflict("当前账号已停用或不存在，不能执行此操作。")
    return actor_role


async def save_review(ecn_id, expected, baseline, submitted, user, role, *, user_service=None, storage=None):
    async def operation(current, connection):
        actor_role = require_active_actor_role(user, role, user_service)
        require_permission(can_edit_ecn_impact, actor_role, user, user_service)
        return update_review(current, expected, baseline, submitted, user)

    return await mutate_record(ecn_id, operation, storage=storage)


async def edit_scheme(ecn_id, expected, item, original, user, role, *, delete=False, user_service=None, storage=None):
    async def operation(current, connection):
        actor_role = require_active_actor_role(user, role, user_service)
        require_permission(can_edit_ecn_scheme, actor_role, user, user_service)
        if delete:
            return delete_scheme(current, expected, original, user)
        validate_scheme_attachments(current, item, user)
        return save_scheme(current, expected, item, original, user)

    return await mutate_record(ecn_id, operation, storage=storage)


async def set_participant_status(ecn_id, expected, user, role, status, *, user_service=None, storage=None):
    async def operation(current, connection):
        actor_role = require_active_actor_role(user, role, user_service)
        require_permission(can_edit_ecn_scheme, actor_role, user, user_service)
        return confirm_participant(current, expected, user, status)

    return await mutate_record(ecn_id, operation, storage=storage)


def validate_request(record):
    basic = record["basic_info"]
    checks = [
        (basic.get("nature"), "请选择变更性质"),
        (any(basic.get("reasons", {}).values()), "请至少勾选一项变更原因"),
        (record.get("target_projects"), "请至少添加一个变更对象"),
        (basic.get("requirements"), "请至少填写一条变更要求"),
        (str(basic.get("reason_desc") or "").strip(), "请填写原因说明"),
    ]
    for valid, message in checks:
        if not valid:
            raise ECNConflict(message)
    if basic.get("reasons", {}).get("其他") and not str(basic.get("other_reason_desc") or "").strip():
        raise ECNConflict("请填写其他说明")


def validate_scheme_review(record):
    if not is_ecn_scheme_ready_for_review(record):
        coverage = get_ecn_scheme_coverage(record)
        missing = []
        for key, title in (
            ("missing_requirements", "遗漏变更要求"),
            ("missing_docs", "遗漏资料"),
            ("missing_materials", "遗漏物料"),
            ("incomplete_material_schemes", "物料追溯或处置未完整"),
        ):
            if coverage[key]:
                missing.append(f"{title}：{'、'.join(sorted(coverage[key]))}")
        raise ECNConflict("\n".join(missing) or "需要所有方案参与人确认完成后，才能发起评审。")


def enter_next_phase(record, phase, project_sales, service):
    workflow = record["workflow"]
    workflow["pending_roles"] = []
    workflow["step_approvals"] = {}
    workflow["current_step_index"] = 0
    if phase == "ECR_PHASE":
        workflow["current_state"] = ECNState.ECN_SCHEMING
        workflow["current_phase"] = "ECN_SCHEME_PHASE"
    else:
        if get_ecn_missing_material_code_items(record):
            workflow["current_state"] = ECNState.MATERIAL_CODE_PENDING
            workflow["current_phase"] = "ECN_MATERIAL_CODE_PHASE"
        else:
            enter_execution_phase(record, project_sales, service)


def enter_execution_phase(record: dict, project_sales, service) -> None:
    """在料号齐全后生成执行清单；失败时由外层单据事务整体回滚。"""
    workflow = record["workflow"]
    workflow["current_state"] = ECNState.ECN_EXECUTING
    workflow["current_phase"] = "ECN_EXECUTION_PHASE"
    try:
        record["execution_info"] = build_ecn_execution_info_from_workflows(
            record.get("change_items", []),
            project_sales,
            str(record.get("basic_info", {}).get("applicant") or ""),
            user_service=service,
        )
    except ValueError as exc:
        raise ECNConflict(str(exc)) from exc


async def update_material_codes(
    ecn_id: str,
    expected_item: dict,
    submitted_codes: dict[str, str],
    user: str,
    role: str,
    *,
    user_service=None,
    project_sales=None,
    storage=None,
):
    """补充单条物料方案料号，并在最后一项补齐时原子进入执行阶段。"""
    service = user_service or getattr(app.state, "user_service", None)

    async def operation(current, connection):
        del connection
        actor_role = require_active_actor_role(user, role, service)
        require_permission(can_edit_ecn_material_codes, actor_role, user, service)
        workflow = current.get("workflow", {})
        if not isinstance(workflow, dict) or workflow.get("current_state") != ECNState.MATERIAL_CODE_PENDING:
            raise ECNConflict("当前已不在料号补充阶段，料号不能修改。")
        item_id = str(expected_item.get("item_id") or "")
        items = current.get("change_items", [])
        if not isinstance(items, list):
            raise ECNConflict("物料方案数据异常，请刷新后重试。")
        item = next(
            (value for value in items if isinstance(value, dict) and str(value.get("item_id") or "") == item_id),
            None,
        )
        if item is None or classify_ecn_change_item(item) != ECN_SCHEME_GROUP_MATERIAL:
            raise ECNConflict("物料方案已不存在，请刷新后重试。")
        material_change = item.get("material_change", {})
        expected_change = expected_item.get("material_change", {})
        if not isinstance(material_change, dict) or not isinstance(expected_change, dict):
            raise ECNConflict("物料方案数据异常，请刷新后重试。")
        current_entries = get_ecn_material_code_entries(item)
        expected_entries = get_ecn_material_code_entries(expected_item)
        if not current_entries:
            raise ECNConflict("当前方案没有可填写的料号字段。")
        current_by_token = {entry["token"]: entry for entry in current_entries}
        expected_by_token = {entry["token"]: entry for entry in expected_entries}
        if {
            token: entry["label"] for token, entry in current_by_token.items()
        } != {
            token: entry["label"] for token, entry in expected_by_token.items()
        }:
            raise ECNConflict("该方案物料信息已被其他页面修改，请刷新后重新录入。")
        if set(submitted_codes) != set(expected_by_token):
            raise ECNConflict("提交的料号字段与当前方案不一致，请刷新后重试。")
        normalized = {token: str(submitted_codes.get(token) or "").strip() for token in expected_by_token}
        missing = [
            entry["label"]
            for token, entry in expected_by_token.items()
            if not normalized[token]
        ]
        if missing:
            raise ECNConflict("请填写：" + "、".join(missing))
        for token, current_entry in current_by_token.items():
            expected_entry = expected_by_token[token]
            if current_entry["value"] != expected_entry["value"]:
                raise ECNConflict("该方案料号已被其他页面修改，请刷新后重新录入。")
        if all(current_by_token[token]["value"] == normalized[token] for token in current_by_token):
            raise ECNConflict("料号未发生变化。")
        if not apply_ecn_material_code_values(item, normalized):
            raise ECNConflict("物料方案结构已变化，请刷新后重新录入。")
        scheme_index = items.index(item) + 1
        append_ecn_approval_log_once(
            current.setdefault("approval_log", []),
            {
                "user": user,
                "role": actor_role,
                "action": f"补充物料料号（方案 #{scheme_index:02d}）",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        if not get_ecn_missing_material_code_items(current):
            enter_execution_phase(current, project_sales or {}, service)
        return current

    return await mutate_record(ecn_id, operation, storage=storage)


def transition(current, expected, baseline, action, user, role, note, rejected_ids, service, project_sales):
    """纯单据更新与延迟待办命令；调用方负责统一事务提交。"""
    require_current_context(current, expected)
    workflow = current["workflow"]
    state = workflow["current_state"]
    phase = workflow["current_phase"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    action_names = {
        "save_draft": "保存草稿",
        "submit_ecr": "发起申请",
        "withdraw": "撤回修改",
        "cancel": "作废变更",
        "initiate_scheme_review": "发起方案评审",
        "approve": "同意",
        "reject": "驳回",
    }
    if action not in action_names:
        raise ECNConflict("不支持的ECN操作。")
    if action in {"save_draft", "submit_ecr", "withdraw", "cancel"}:
        require_permission(can_create_ecn_request, role, user, service)
        if current["basic_info"].get("applicant") != user:
            raise ECNConflict("只能维护本人申请的ECR。")
        allowed = (
            {ECNState.DRAFT, ECNState.REJECTED} if action in {"save_draft", "submit_ecr"} else {ECNState.ECR_REVIEWING}
        )
        if state not in allowed:
            raise ECNConflict("当前阶段不允许此操作，请关闭后重新打开。")
        if action in {"save_draft", "submit_ecr"}:
            # 审批、撤回与作废不写回申请表，更不写回方案或执行快照。
            for key in ("basic_info", "target_projects"):
                current[key] = merge_fields(current[key], baseline[key], expected[key], key)
            if current["basic_info"].get("applicant") != user:
                raise ECNConflict("申请人不能修改。")
    if action == "save_draft":
        workflow["current_state"] = ECNState.DRAFT
    elif action in {"submit_ecr", "initiate_scheme_review"}:
        is_ecr = action == "submit_ecr"
        if is_ecr:
            validate_request(current)
            basic = current["basic_info"]
            basic["title"] = (
                f"{','.join(current['target_projects'][:2])}等 - {'/'.join(k for k, v in basic['reasons'].items() if v)}变更"
            )
        else:
            require_permission(can_submit_ecn_scheme_review, role, user, service)
            validate_scheme_review(current)
        workflow["approval_round"] = str(uuid.uuid4())
        result = (start_ecr_approval if is_ecr else start_scheme_approval)(
            current["ecn_id"],
            current["basic_info"]["applicant"],
            user_service=service,
        )
        if result.get("status") != "matched":
            raise ECNConflict(ecn_workflow_error_message(result, "ECR申请" if is_ecr else "ECN方案评审"))
        workflow["ecr_workflow_assignment" if is_ecr else "scheme_workflow_assignment"] = result["assignment"]
        pending = (get_ecr_pending_usernames if is_ecr else get_scheme_pending_usernames)(
            current, user_service=service
        )
        workflow["route_type"] = "CONFIGURED_WORKFLOW"
        workflow.update(
            current_state=ECNState.ECR_REVIEWING if is_ecr else ECNState.ECN_REVIEWING,
            current_phase="ECR_PHASE" if is_ecr else "ECN_SCHEME_REVIEW_PHASE",
            current_step_index=0,
            pending_roles=copy.deepcopy(pending),
            step_approvals={},
        )
    elif action in {"withdraw", "cancel"}:
        cancel_ecr_approval(current, user_service=service)
        workflow.update(
            current_state=ECNState.DRAFT if action == "withdraw" else ECNState.CANCEL,
            current_step_index=0,
            pending_roles=[],
            step_approvals={},
            approval_round=str(uuid.uuid4()),
        )
    elif action in {"approve", "reject"}:
        if (state, phase) not in {
            (ECNState.ECR_REVIEWING, "ECR_PHASE"),
            (ECNState.ECN_REVIEWING, "ECN_SCHEME_REVIEW_PHASE"),
        }:
            raise ECNConflict("当前阶段不允许审批。")
        is_ecr = phase == "ECR_PHASE"
        checker = is_ecr_assigned_approver if is_ecr else is_scheme_assigned_approver
        if not checker(current, user, user_service=service):
            raise ECNConflict("当前用户没有该节点的有效审批待办。")
        if action == "reject" and not is_ecr:
            if ECN_REQUIRE_REJECTED_ITEM_SELECTION and not rejected_ids:
                raise ECNConflict("请至少选择一个需要改进的方案。")
            valid_ids = {item.get("item_id") for item in current.get("change_items", [])}
            if set(rejected_ids) - valid_ids:
                raise ECNConflict("所选方案已变化，请刷新后重新选择。")
            if not note.strip():
                raise ECNConflict("请填写驳回意见。")
        result = (finish_ecr_approval if is_ecr else finish_scheme_approval)(
            current,
            user,
            rejected=action == "reject",
            user_service=service,
        )
        if result.get("status") not in {"node_pending", "advanced", "completed", "rejected"}:
            raise ECNConflict(str(result.get("message") or "审批失败"))
        workflow["ecr_workflow_assignment" if is_ecr else "scheme_workflow_assignment"] = result["assignment"]
        workflow["current_step_index"] = result["assignment"]["current_node_index"]
        workflow["step_approvals"] = {}
        workflow["pending_roles"] = (get_ecr_pending_usernames if is_ecr else get_scheme_pending_usernames)(
            current,
            user_service=service,
        )
        completed = result["status"] == "completed"
        if action == "reject":
            workflow["pending_roles"] = []
            workflow["step_approvals"] = {}
            workflow["current_state"] = ECNState.REJECTED if is_ecr else ECNState.ECN_SCHEMING
            if not is_ecr:
                workflow["current_phase"] = "ECN_SCHEME_PHASE"
                reject_ecn_scheme_items(current, rejected_ids, user, role, note, now)
        elif completed:
            enter_next_phase(current, phase, project_sales, service)
    log = {"user": user, "role": role, "action": action_names[action], "time": now}
    if action in {"approve", "reject"}:
        log["note"] = note
        if rejected_ids:
            log["rejected_item_ids"] = rejected_ids
    append_ecn_approval_log_once(current.setdefault("approval_log", []), log)
    return current


async def execute_action(
    expected,
    baseline,
    action,
    user,
    role,
    *,
    is_new=False,
    note="",
    rejected_ids=None,
    user_service=None,
    project_sales=None,
    storage=None,
):
    service = user_service or getattr(app.state, "user_service", None)
    storage = storage or db_storage
    new_record = None
    published_paths: list[Path] = []
    staged_ecr_attachments = [
        entry
        for entry in expected.get("basic_info", {}).get("attachments", [])
        if isinstance(entry, dict) and entry.get("pending_path")
    ] if is_new else []
    if is_new:
        new_record = get_ecn_template()
        new_record["basic_info"] = copy.deepcopy(expected["basic_info"])
        new_record["basic_info"]["applicant"] = user
        new_record["target_projects"] = copy.deepcopy(expected.get("target_projects", []))
        new_record["timestamp"] = copy.deepcopy(expected.get("timestamp", {}))

    async def operation(current, connection):
        actor_role = require_active_actor_role(user, role, service)
        proxy = None
        if service is not None and is_ecn_database_workflow_enabled(user_service=service):
            # 当前部署的身份与业务数据必须共库，保证下面的待办SQL与单据一同提交。
            if Path(service.identity_store.db_path).resolve() != Path(storage.DB_PATH).resolve():
                raise ECNConflict("ECN审批需要身份数据与业务数据使用同一数据库。")
            proxy = ApprovalTransaction(service)
        submitted = copy.deepcopy(expected)
        original = copy.deepcopy(baseline)
        if is_new:
            if action not in {"save_draft", "submit_ecr"}:
                raise ECNConflict("新建单据只能保存草稿或提交申请。")
            submitted_files = submitted.get("basic_info", {}).get("attachments", [])
            if not isinstance(submitted_files, list) or len(submitted_files) != len(staged_ecr_attachments):
                raise ECNConflict("申请附件数据异常，请重新上传")
            submitted["workflow"] = copy.deepcopy(current["workflow"])
            submitted["basic_info"]["file_no"] = current["ecn_id"]
            original["basic_info"]["file_no"] = current["ecn_id"]
        else:
            # 已保存单据的附件只允许通过独立的附件事务修改，表单三方合并不得覆盖它们。
            submitted["basic_info"]["attachments"] = copy.deepcopy(
                original["basic_info"].get("attachments", [])
            )
        updated = transition(
            current,
            submitted,
            original,
            action,
            user,
            actor_role,
            note,
            list(rejected_ids or []),
            proxy or service,
            project_sales or {},
        )
        if is_new and staged_ecr_attachments:
            basic = updated["basic_info"]
            finalized = []
            for attachment in staged_ecr_attachments:
                if attachment.get("uploaded_by") != user:
                    raise ECNConflict("暂存附件的上传人不匹配")
                published, path = await asyncio.to_thread(
                    publish_pending, attachment, updated["ecn_id"], user, "ecr"
                )
                published_paths.append(path)
                finalized.append(published)
            basic["attachments"] = finalized
        if proxy is not None:
            await proxy.flush(connection)
        return updated

    try:
        result = await mutate_record(expected.get("ecn_id"), operation, new_record=new_record, storage=storage)
    except Exception:
        for path in published_paths:
            path.unlink(missing_ok=True)
        raise
    if result.ok:
        cleanup_staged(staged_ecr_attachments)
        if action == "approve" and result.record is not None:
            completed_phase = str(baseline.get("workflow", {}).get("current_phase") or "")
            assignment_key = (
                "ecr_workflow_assignment"
                if completed_phase == "ECR_PHASE"
                else "scheme_workflow_assignment"
            )
            assignment = result.record.get("workflow", {}).get(assignment_key, {})
            if isinstance(assignment, dict) and assignment.get("status") == "completed":
                ecn_id = str(result.record.get("ecn_id") or "")
                title = str(result.record.get("basic_info", {}).get("title") or "—")
                event_name = "ECR申请审批" if completed_phase == "ECR_PHASE" else "ECN方案评审"
                link_url = (
                    f"{ECN_WECOM_CONFIG['public_base_url']}/ecn_management"
                    if ECN_WECOM_CONFIG["public_base_url"]
                    else ""
                )
                schedule_background_task(
                    send_workflow_completion_cc(
                        assignment,
                        title=f"【ECN工程变更】{event_name}已通过",
                        lines=(
                            f"单号：{ecn_id}",
                            f"主题：{title}",
                            "结果：全部审批节点已通过",
                            f"最终审批人：{user}",
                        ),
                        link_url=link_url,
                        module="ecn_management",
                        business_key=(
                            f"{ecn_id}:{assignment_key}:"
                            f"{result.record.get('workflow', {}).get('approval_round', '')}:completion_cc"
                        ),
                        user_service=service,
                    ),
                    f"{event_name}完成抄送",
                )
    else:
        for path in published_paths:
            path.unlink(missing_ok=True)
    return result
