"""使用隔离 SQLite 连接验证 ECN 并发，不读取或写入运行数据库。"""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.ecn_management_config import (
    ECN_EXECUTION_STAGE_COMPLETED,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_EXECUTION_VERIFICATION_CORRECTION_REQUIRED,
    ECN_EXECUTION_VERIFICATION_VERIFIED,
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_LEVEL_COMPLEX,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECNState,
    build_ecn_scheme_snapshot,
    get_ecn_execution_verification_label,
    get_ecn_traceability_closure_summary,
    reject_ecn_scheme_items,
)
from src.modules.ecn import actions
from src.modules.ecn import validation_reports
from src.modules.ecn import execution_review
from src.modules.ecn.approval_transaction import ApprovalTransaction
from src.modules.ecn.approval_reassignment import reassign_ecn_approval_reviewer
from src.modules.ecn.editing import (
    ECNConflict,
    merge_fields,
    sync_review_snapshot,
    validate_overview_scheme_dependencies,
    reconcile_removed_scheme_associations,
)
from src.modules.ecn.models import get_ecn_template
from src.modules.ecn.detail import detail_requires_reopen, get_current_approval_display
from src.modules.ecn.repository import delete_record
from src.user_service import UserService
from tests.test_error_management_concurrency import load_isolated_db_storage


def record(state=ECNState.ECN_SCHEMING):
    value = get_ecn_template()
    value["ecn_id"] = "ECN26090701"
    value["basic_info"].update(
        applicant="张三", file_no=value["ecn_id"], reason_desc="修正设计", requirements=[{"idx": 1, "content": "改进"}]
    )
    value["basic_info"]["reasons"]["设计改善"] = True
    value["target_projects"] = ["P1"]
    value["workflow"].update(current_state=state, current_phase="ECN_SCHEME_PHASE", approval_round="round-1")
    value["change_items"] = [
        {
            "item_id": "S1",
            "author": "张三",
            "type": "text_desc",
            "scheme_category": "ordinary_document",
            "projects": ["P1"],
            "req_idxs": [1],
            "linked_docs": [],
            "linked_materials": [],
            "change_type": "其它",
            "old_content": "旧内容",
            "new_content": "新内容",
        }
    ]
    value["workflow"]["scheme_participants"] = {"张三": ECN_PARTICIPANT_STATUS_CONFIRMED}
    value["workflow"]["trial_production"] = {"required": False}
    return value


class ECNConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def test_removed_association_invalidates_only_affected_required_reports(self):
        for field, removed in (("projects", "P2"), ("req_idxs", "removed-req"), ("linked_docs", "removed-doc"), ("linked_materials", "removed-material")):
            for with_attachment in (True, False):
                with self.subTest(field=field, with_attachment=with_attachment):
                    value = record()
                    item = value["change_items"][0]
                    item[field] = [*item[field], removed]
                    item["validation_report"] = {
                        "required": True, "status": "approved", "reviewed_by": "reviewer",
                        "attachments": [{"id": "R1"}] if with_attachment else [],
                    }
                    unaffected = copy.deepcopy(item)
                    unaffected.update(item_id="S2", projects=["P1"], req_idxs=[1], linked_docs=[], linked_materials=[])
                    value["change_items"].append(unaffected)
                    original_report = copy.deepcopy(unaffected["validation_report"])
                    reconcile_removed_scheme_associations(value, "impact-owner")
                    self.assertEqual(item["validation_report"]["status"], "pending_review" if with_attachment else "pending_upload")
                    self.assertNotIn("reviewed_by", item["validation_report"])
                    self.assertEqual(unaffected["validation_report"], original_report)
        value = record()
        value["change_items"][0].update(linked_docs=["removed-doc"], validation_report={"required": False, "status": "not_required"})
        reconcile_removed_scheme_associations(value, "impact-owner")
        self.assertEqual(value["change_items"][0]["validation_report"], {"required": False, "status": "not_required"})

    def test_detail_reopens_when_state_or_phase_changes(self):
        current = {"current_state": ECNState.DRAFT, "current_phase": "ECR_PHASE"}
        self.assertFalse(detail_requires_reopen(current, copy.deepcopy(current)))
        self.assertTrue(
            detail_requires_reopen(
                current,
                {"current_state": ECNState.ECR_REVIEWING, "current_phase": "ECR_PHASE"},
            )
        )
        self.assertTrue(
            detail_requires_reopen(
                current,
                {"current_state": ECNState.DRAFT, "current_phase": "ECN_SCHEME_PHASE"},
            )
        )

    def test_current_approval_display_reads_approved_users_from_assignment_node(self):
        workflow = {
            "current_phase": "ECR_PHASE",
            "pending_roles": ["李四"],
            "step_approvals": {},
            "ecr_workflow_assignment": {
                "current_node_index": 0,
                "nodes": [{"approved_usernames": ["张三"]}],
            },
        }

        pending, approved = get_current_approval_display(workflow)

        self.assertEqual(pending, ["李四"])
        self.assertEqual(approved, ["张三"])

    def test_request_requirement_date_must_be_a_valid_calendar_date(self):
        value = record(ECNState.DRAFT)

        value["basic_info"]["requirement_date"] = "2026-10-31"
        actions.validate_request(value)

        value["basic_info"]["requirement_date"] = ""
        actions.validate_request(value)

        for invalid_date in ("2026-02-30", "2026-1-2", "明天"):
            with self.subTest(requirement_date=invalid_date):
                value["basic_info"]["requirement_date"] = invalid_date
                with self.assertRaisesRegex(ECNConflict, "需求日期格式无效"):
                    actions.validate_request(value)

    def test_request_rechecks_latest_target_project_states(self):
        value = record(ECNState.DRAFT)
        actions.validate_request(value, {"P1": {"state": "试产"}})

        with self.assertRaisesRegex(ECNConflict, "不处于允许发起ECR"):
            actions.validate_request(value, {"P1": {"state": "研发"}})
        with self.assertRaisesRegex(ECNConflict, "不处于允许发起ECR"):
            actions.validate_request(value, {})

    def test_expanded_projects_are_rechecked_against_latest_project_catalog(self):
        value = record()
        value["review_info"]["expanded_projects_mass"] = ["P2"]
        value["review_info"]["expanded_projects_non_mass"] = ["P3"]
        summary = {"P2": {"state": "量产"}, "P3": {"state": "研发"}}
        actions.validate_impact_projects(value, summary)

        with self.assertRaisesRegex(ECNConflict, "项目库移除或状态分组已变化"):
            actions.validate_impact_projects(value, {"P2": {"state": "研发"}})

    def test_every_scheme_requires_at_least_one_project(self):
        value = record()
        value["change_items"][0]["projects"] = []

        with self.assertRaisesRegex(ECNConflict, "至少关联一个目标项目"):
            actions.validate_scheme_review(value)

    def test_pending_table_anchor_requires_live_first_column_add_scheme(self):
        value = record()
        value["change_items"] = [
            {
                "item_id": "child",
                "author": "张三",
                "type": "overview_update",
                "scheme_category": "overview_document",
                "projects": ["P1"],
                "req_idxs": ["1"],
                "linked_docs": [],
                "linked_materials": [],
                "label": "子参数",
                "first_col_label": "基准参数",
                "project_states": {
                    "P1": {"action": "add", "anchor_row_id": "PENDING_NEW_base"}
                },
            },
            {
                "item_id": "base",
                "author": "张三",
                "type": "overview_update",
                "scheme_category": "overview_document",
                "projects": ["P1"],
                "req_idxs": [],
                "linked_docs": [],
                "linked_materials": [],
                "label": "基准参数",
                "first_col_label": "基准参数",
                "project_states": {"P1": {"action": "add"}},
            },
        ]
        validate_overview_scheme_dependencies(value)
        value["change_items"].pop()
        with self.assertRaisesRegex(ECNConflict, "新增基准行已失效"):
            validate_overview_scheme_dependencies(value)

    async def test_zero_project_scheme_cannot_be_retained_after_scope_removal(self):
        value = record()
        value["review_info"]["expanded_projects_mass"] = ["P2"]
        value["change_items"][0]["projects"] = ["P2"]
        await self.store(value)
        removed = copy.deepcopy(value["review_info"])
        removed["expanded_projects_mass"] = []
        with patch.object(actions, "can_edit_ecn_impact", return_value=True):
            saved = await actions.save_review(
                value["ecn_id"], value, value["review_info"], removed, "张三", "研发", user_service=self.service, storage=self.left
            )
        self.assertTrue(saved.ok, saved.message)
        assert saved.record is not None
        self.assertEqual(saved.record["change_items"][0]["projects"], [])
        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            retained = await actions.retain_scheme_after_association_change(
                value["ecn_id"],
                saved.record,
                "S1",
                "张三",
                "研发",
                user_service=self.service,
                storage=self.left,
            )
        self.assertFalse(retained.ok)
        self.assertIn("至少关联一个目标项目", retained.message)

    async def test_trial_decision_permission_conflict_and_phase(self):
        value = record()
        value["workflow"]["trial_production"] = {}
        await self.store(value)
        with patch.object(actions, "can_submit_ecn_scheme_review", return_value=False):
            denied = await actions.set_trial_production_decision(
                value["ecn_id"], value, True, "张三", "研发", user_service=self.service, storage=self.left,
            )
        self.assertFalse(denied.ok)
        with patch.object(actions, "can_submit_ecn_scheme_review", return_value=True):
            saved = await actions.set_trial_production_decision(
                value["ecn_id"], value, True, "张三", "研发", user_service=self.service, storage=self.left,
            )
            self.assertTrue(saved.ok, saved.message)
            assert saved.record is not None
            self.assertTrue(saved.record["workflow"]["trial_production"]["required"])
            self.assertEqual(saved.record["change_items"], value["change_items"])
            stale = await actions.set_trial_production_decision(
                value["ecn_id"], value, False, "张三", "研发", user_service=self.service, storage=self.right,
            )
            self.assertFalse(stale.ok)
            saved.record["workflow"]["current_state"] = ECNState.ECN_REVIEWING
            await self.store(saved.record)
            locked = await actions.set_trial_production_decision(
                value["ecn_id"], saved.record, False, "张三", "研发", user_service=self.service, storage=self.left,
            )
            self.assertFalse(locked.ok)

    async def test_submit_rejects_project_that_left_allowed_state(self):
        value = record(ECNState.DRAFT)
        value["workflow"]["current_phase"] = "ECR_PHASE"
        await self.store(value)

        with patch.object(actions, "can_create_ecn_request", return_value=True):
            result = await self.action(
                value,
                "submit_ecr",
                project_summary={"P1": {"state": "研发"}},
            )

        self.assertFalse(result.ok)
        self.assertIn("不处于允许发起ECR", result.message)
        self.assertEqual((await self.fresh())["workflow"]["current_state"], ECNState.DRAFT)

    async def test_review_rejects_missing_trial_decision_but_accepts_explicit_false(self):
        value = record()
        actions.validate_scheme_review(value)
        value["workflow"]["trial_production"] = {}
        await self.store(value)
        with patch.object(actions, "can_submit_ecn_scheme_review", return_value=True):
            result = await self.action(value, "initiate_scheme_review")
        self.assertFalse(result.ok)
        self.assertIn("试产", result.message)
        self.assertEqual((await self.fresh())["workflow"]["current_state"], ECNState.ECN_SCHEMING)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "ecn.db"
        self.left = load_isolated_db_storage("ecn_left", path)
        self.right = load_isolated_db_storage("ecn_right", path)
        await self.left.init_db()
        await self.right.init_db()
        self.service = UserService(db_path=path, password_iterations=1_000)
        self.service.modify_user("add", "张三", "123456", "研发经理")
        permission_codes = [
            item["code"]
            for item in self.service.list_permissions()
            if str(item.get("code") or "").startswith("ecn.")
        ]
        role_id = self.service.create_security_role(
            code="test.ecn.all",
            name="ECN测试全权限",
            permission_codes=permission_codes,
        )
        self.service.set_user_security_roles("张三", [role_id])
        for username in ("经理", "审核人", "李四"):
            self.service.modify_user("add", username, "123456", "研发经理")
            self.service.set_user_security_roles(username, [role_id])

    async def asyncTearDown(self):
        await self.left.close_db()
        await self.right.close_db()
        self.temp.cleanup()

    async def store(self, value):
        await self.left.set_item("ecn_management_data", {value["ecn_id"]: value})

    async def fresh(self, ecn_id="ECN26090701"):
        return (await self.left.get_fresh_item("ecn_management_data", {})).get(ecn_id)

    def action(self, value, action, *, user="张三", role="研发经理", storage=None, **kwargs):
        return actions.execute_action(
            copy.deepcopy(value),
            copy.deepcopy(value),
            action,
            user,
            role,
            storage=storage or self.left,
            user_service=self.service,
            **kwargs,
        )

    async def test_concurrent_new_forms_allocate_distinct_numbers_at_commit(self):
        draft = record(ECNState.DRAFT)
        draft["workflow"].update(current_phase="ECR_PHASE")
        draft["ecn_id"] = ""
        draft["basic_info"]["file_no"] = ""
        with patch.object(actions, "can_create_ecn_request", return_value=True):
            results = await asyncio.gather(
                *[
                    self.action(draft, "save_draft", is_new=True, storage=self.left if i % 2 else self.right)
                    for i in range(20)
                ]
            )
        self.assertTrue(all(result.ok for result in results))
        ids = set()
        for result in results:
            assert result.record is not None
            ids.add(result.record["ecn_id"])
        self.assertEqual(len(ids), 20)
        all_records = await self.left.get_fresh_item("ecn_management_data")
        self.assertEqual(set(all_records), ids)
        self.assertTrue(all(v["basic_info"]["file_no"] == k for k, v in all_records.items()))

    async def test_complex_validation_report_designation_and_review_are_transactional(self):
        value = record()
        value["workflow"]["ecn_level"] = ECN_LEVEL_COMPLEX
        await self.store(value)

        with patch.object(validation_reports, "can_designate_ecn_validation_report", return_value=True):
            designated = await validation_reports.set_validation_report_required(
                value["ecn_id"],
                "S1",
                {},
                True,
                "经理",
                "研发经理",
                user_service=self.service,
                storage=self.left,
            )
        self.assertTrue(designated.ok)
        assert designated.record is not None
        report = designated.record["change_items"][0]["validation_report"]
        self.assertEqual(report["status"], "pending_upload")

        report["attachments"] = [{"id": "report-1", "name": "验证报告.pdf"}]
        report["status"] = "pending_review"
        await self.left.set_item("ecn_management_data", {value["ecn_id"]: designated.record})
        with (
            patch.object(validation_reports, "can_view_ecn_validation_report", return_value=True),
            patch.object(validation_reports, "can_approve_ecn_validation_report", return_value=True),
        ):
            self_review = await validation_reports.review_validation_report(
                value["ecn_id"],
                "S1",
                copy.deepcopy(report),
                True,
                "",
                "张三",
                "研发工程师",
                expected_scheme=build_ecn_scheme_snapshot(designated.record["change_items"][0]),
                user_service=self.service,
                storage=self.left,
            )
            self.assertFalse(self_review.ok)
            self.assertIn("本人", self_review.message)
            reviewed = await validation_reports.review_validation_report(
                value["ecn_id"],
                "S1",
                copy.deepcopy(report),
                True,
                "验证结果符合要求",
                "审核人",
                "研发经理",
                expected_scheme=build_ecn_scheme_snapshot(designated.record["change_items"][0]),
                user_service=self.service,
                storage=self.left,
            )
        self.assertTrue(reviewed.ok)
        assert reviewed.record is not None
        self.assertEqual(
            reviewed.record["change_items"][0]["validation_report"]["status"],
            "approved",
        )

    async def test_report_review_rejects_scheme_changed_while_dialog_was_open(self):
        value = record()
        value["workflow"]["ecn_level"] = ECN_LEVEL_COMPLEX
        report = {"required": True, "status": "pending_review", "attachments": [{"id": "report-1"}]}
        value["change_items"][0]["validation_report"] = copy.deepcopy(report)
        expected_scheme = build_ecn_scheme_snapshot(value["change_items"][0])
        await self.store(value)
        updated_item = copy.deepcopy(value["change_items"][0])
        updated_item["new_content"] = "审核窗口打开后修改的新方案"
        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            changed = await actions.edit_scheme(
                value["ecn_id"], value, updated_item, value["change_items"][0], "张三", "研发经理",
                user_service=self.service, storage=self.left,
            )
        self.assertTrue(changed.ok, changed.message)
        assert changed.record is not None
        self.assertEqual(changed.record["change_items"][0]["validation_report"], report)
        with patch.object(validation_reports, "get_active_ecn_actor_role", return_value="研发经理"), patch.object(
            validation_reports, "can_view_ecn_validation_report", return_value=True
        ), patch.object(validation_reports, "can_approve_ecn_validation_report", return_value=True):
            stale = await validation_reports.review_validation_report(
                value["ecn_id"], "S1", report, True, "", "审核人", "研发经理",
                expected_scheme=expected_scheme, user_service=self.service, storage=self.right,
            )
            self.assertFalse(stale.ok)
            self.assertIn("方案内容已变化", stale.message)
            fresh = await validation_reports.review_validation_report(
                value["ecn_id"], "S1", report, True, "", "审核人", "研发经理",
                expected_scheme=build_ecn_scheme_snapshot(changed.record["change_items"][0]),
                user_service=self.service, storage=self.right,
            )
            self.assertTrue(fresh.ok, fresh.message)

    async def test_existing_anchor_is_rechecked_when_saving_scheme(self):
        value = record()
        item = {
            "item_id": "child", "author": "张三", "type": "overview_update", "scheme_category": "overview_document",
            "label": "child", "first_col_label": "base", "projects": ["P1"], "req_idxs": [],
            "linked_docs": [], "linked_materials": [], "project_states": {"P1": {"action": "add", "anchor_row_id": "row-1"}},
        }
        await self.store(value)
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}}))
        with patch.object(actions, "app", fake_app), patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            missing = await actions.edit_scheme(value["ecn_id"], value, item, None, "张三", "研发经理", user_service=self.service, storage=self.left)
            self.assertFalse(missing.ok)
            self.assertIn("基准行", missing.message)
            await self.left.set_item("P1_over_data", {"base": {"base-chip": {"row_id": "row-1", "select_activ_dic": {"1.0": True}}}})
            valid = await actions.edit_scheme(value["ecn_id"], value, item, None, "张三", "研发经理", user_service=self.service, storage=self.left)
            self.assertTrue(valid.ok, valid.message)
            assert valid.record is not None
            other = record()
            other["ecn_id"] = "ECN26090702"
            await self.left.set_item("ecn_management_data", {value["ecn_id"]: valid.record, other["ecn_id"]: other})
            base_change = {
                **copy.deepcopy(item), "item_id": "replace-base", "label": "base", "first_col_label": "base",
                "project_states": {"P1": {"action": "update", "chip_id": "base-chip",
                    "old_data": {"row_id": "row-1", "select_activ_dic": {"1.0": True}}}},
            }
            conflict = await actions.edit_scheme(other["ecn_id"], other, base_change, None, "张三", "研发经理", user_service=self.service, storage=self.right)
            self.assertFalse(conflict.ok)
            self.assertIn("子参数方案依赖", conflict.message)

    async def test_execution_reviewer_revokes_without_erasing_confirmation_then_verifies(self):
        value = record(ECNState.CLOSED)
        value["workflow"]["current_phase"] = "ECN_EXECUTION_PHASE"
        value["execution_info"] = {
            "stage": ECN_EXECUTION_STAGE_COMPLETED,
            "completed_time": "2026-09-22 09:00:00",
            "ordinary_confirmations": {
                "S1": {
                    "confirmed": True,
                    "user": "执行人",
                    "role": "执行岗位",
                    "time": "2026-09-22 08:30:00",
                    "history": [
                        {
                            "confirmed": True,
                            "user": "执行人",
                            "role": "执行岗位",
                            "time": "2026-09-22 08:30:00",
                        }
                    ],
                }
            },
            "erp_confirmation": {
                "confirmed": True,
                "user": "执行人",
                "role": "执行岗位",
                "time": "2026-09-22 08:40:00",
                "history": [],
            },
            "overview_results": {},
            "material_confirmations": {},
        }
        await self.store(value)
        expected = copy.deepcopy(value["execution_info"]["ordinary_confirmations"]["S1"])

        missing_reason = await execution_review.revoke_execution_confirmation(
            value["ecn_id"],
            "special",
            "S1",
            "",
            expected,
            " ",
            [],
            "复核人",
            "质量经理",
            user_service=self.service,
            storage=self.left,
        )
        self.assertFalse(missing_reason.ok)
        self.assertIn("必须填写", missing_reason.message)

        with (
            patch.object(execution_review, "can_view_ecn", return_value=True),
            patch.object(execution_review, "can_verify_ecn_execution", return_value=True),
        ):
            revoked = await execution_review.revoke_execution_confirmation(
                value["ecn_id"],
                "special",
                "S1",
                "",
                expected,
                "现场文件仍未更新",
                [],
                "复核人",
                "质量经理",
                user_service=self.service,
                storage=self.left,
            )
            stale = await execution_review.revoke_execution_confirmation(
                value["ecn_id"],
                "special",
                "S1",
                "",
                expected,
                "重复撤销",
                [],
                "复核人",
                "质量经理",
                user_service=self.service,
                storage=self.left,
            )

        self.assertTrue(revoked.ok)
        assert revoked.record is not None
        confirmation = revoked.record["execution_info"]["ordinary_confirmations"]["S1"]
        self.assertFalse(confirmation["confirmed"])
        self.assertTrue(confirmation["history"][0]["confirmed"])
        self.assertEqual(confirmation["history"][-1]["action"], "verification_revoked")
        self.assertEqual(revoked.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        self.assertEqual(revoked.record["execution_info"]["stage"], ECN_EXECUTION_STAGE_MATERIAL)
        verification = revoked.record["execution_info"]["verification"]
        self.assertEqual(verification["status"], ECN_EXECUTION_VERIFICATION_CORRECTION_REQUIRED)
        self.assertEqual(verification["notices"][verification["last_event_id"]]["recipient"], "执行人")
        self.assertEqual(get_ecn_execution_verification_label(revoked.record), "待整改")
        self.assertFalse(stale.ok)

        corrected = revoked.record
        corrected_confirmation = corrected["execution_info"]["ordinary_confirmations"]["S1"]
        corrected_confirmation.update(
            confirmed=True,
            user="执行人",
            role="执行岗位",
            time="2026-09-22 10:00:00",
        )
        corrected_confirmation.pop("review_revocation", None)
        corrected["execution_info"].update(
            stage=ECN_EXECUTION_STAGE_COMPLETED,
            completed_time="2026-09-22 10:01:00",
        )
        corrected["workflow"].update(current_state=ECNState.CLOSED, pending_roles=[])
        await self.left.set_item("ecn_management_data", {value["ecn_id"]: corrected})

        with (
            patch.object(execution_review, "can_view_ecn", return_value=True),
            patch.object(execution_review, "can_verify_ecn_execution", return_value=True),
        ):
            verified = await execution_review.verify_execution_result(
                value["ecn_id"],
                "复核人",
                "质量经理",
                user_service=self.service,
                storage=self.left,
            )
        self.assertTrue(verified.ok)
        assert verified.record is not None
        self.assertEqual(
            verified.record["execution_info"]["verification"]["status"],
            ECN_EXECUTION_VERIFICATION_VERIFIED,
        )
        self.assertEqual(get_ecn_execution_verification_label(verified.record), "已核验")

    async def test_execution_reviewer_reopens_closed_material_item(self):
        value = record(ECNState.CLOSED)
        value["workflow"]["current_phase"] = "ECN_EXECUTION_PHASE"
        value["change_items"][0].update(
            scheme_category="material",
            change_type="更换",
            traceability_levels=["文件"],
        )
        assignment = {
            "workflow_id": "workflow-file",
            "version_id": "version-1",
            "workflow_name": "文件执行",
        }
        marker_key = "workflow-file:version-1:P1"
        task = {
            "confirmed": True,
            "user": "执行人",
            "role": "研发助理",
            "time": "2026-09-22 08:30:00",
            "history": [{"confirmed": True, "user": "执行人"}],
            "level": "文件",
            "stage_index": 0,
            "label": "研发助理",
            "project": "P1",
            "users": ["执行人"],
            "roles": [],
            "responsible_type": "workflow_users",
            "responsible_key": "研发助理",
            "workflow_assignment": assignment,
        }
        value["execution_info"] = {
            "stage": ECN_EXECUTION_STAGE_COMPLETED,
            "completed_time": "2026-09-22 09:00:00",
            "ordinary_confirmations": {},
            "erp_confirmation": {"confirmed": True, "history": []},
            "overview_results": {},
            "material_confirmations": {
                "S1": {
                    "status": "closed",
                    "closed_time": "2026-09-22 09:00:00",
                    "traceability_tasks": {"T1": task},
                    "workflow_completion_notifications": {
                        marker_key: "2026-09-22 09:00:00"
                    },
                }
            },
        }
        await self.store(value)

        with (
            patch.object(execution_review, "can_view_ecn", return_value=True),
            patch.object(execution_review, "can_verify_ecn_execution", return_value=True),
        ):
            result = await execution_review.revoke_execution_confirmation(
                value["ecn_id"],
                "material",
                "S1",
                "T1",
                copy.deepcopy(task),
                "文件版本仍不正确",
                [],
                "复核人",
                "质量经理",
                user_service=self.service,
                storage=self.left,
            )

        self.assertTrue(result.ok)
        assert result.record is not None
        material = result.record["execution_info"]["material_confirmations"]["S1"]
        self.assertEqual(material["status"], "open")
        self.assertNotIn("closed_time", material)
        self.assertNotIn(marker_key, material["workflow_completion_notifications"])
        self.assertFalse(material["traceability_tasks"]["T1"]["confirmed"])
        self.assertEqual(get_ecn_traceability_closure_summary(result.record)["文件"], "待确认")
        self.assertEqual(result.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)

    async def test_scheme_stage_level_decision_uses_independent_permission(self):
        value = record()
        await self.store(value)

        with patch.object(actions, "can_classify_ecn_level_before_scheme_review", return_value=True):
            result = await actions.set_ecn_level(
                value["ecn_id"],
                copy.deepcopy(value),
                ECN_LEVEL_COMPLEX,
                "before_scheme_review",
                "经理",
                "研发经理",
                user_service=self.service,
                storage=self.left,
            )

        self.assertTrue(result.ok)
        assert result.record is not None
        self.assertEqual(result.record["workflow"]["ecn_level"], ECN_LEVEL_COMPLEX)
        self.assertEqual(
            result.record["workflow"]["ecn_level_decisions"][-1]["timing"],
            "before_scheme_review",
        )

    async def test_stale_level_decision_is_rejected_including_change_back_to_original(self):
        for initial in ("", "general"):
            with self.subTest(initial=initial):
                value = record()
                value["workflow"]["ecn_level"] = initial
                await self.store(value)
                first = await actions.set_ecn_level(value["ecn_id"], value, "complex", "before_scheme_review", "张三", "研发经理", user_service=self.service, storage=self.left)
                self.assertTrue(first.ok, first.message)
                assert first.record is not None
                stale = await actions.set_ecn_level(value["ecn_id"], value, "simple", "before_scheme_review", "经理", "研发经理", user_service=self.service, storage=self.right)
                self.assertFalse(stale.ok)
                self.assertIn("等级判定已被其他页面修改", stale.message)
                self.assertEqual(await self.fresh(), first.record)
                changed_back = await actions.set_ecn_level(value["ecn_id"], first.record, "general", "before_scheme_review", "经理", "研发经理", user_service=self.service, storage=self.right)
                self.assertTrue(changed_back.ok, changed_back.message)
                old_general = copy.deepcopy(value)
                old_general["workflow"]["ecn_level"] = "general"
                stale_history = await actions.set_ecn_level(value["ecn_id"], old_general, "simple", "before_scheme_review", "张三", "研发经理", user_service=self.service, storage=self.left)
                self.assertFalse(stale_history.ok)
                # 非等级字段更新不构成等级冲突。
                assert changed_back.record is not None
                latest = copy.deepcopy(changed_back.record)
                latest["review_info"]["impacts"]["光学部件"] = True
                await self.store(latest)
                updated = await actions.set_ecn_level(value["ecn_id"], changed_back.record, "simple", "before_scheme_review", "张三", "研发经理", user_service=self.service, storage=self.left)
                self.assertTrue(updated.ok, updated.message)
                assert updated.record is not None
                self.assertTrue(updated.record["review_info"]["impacts"]["光学部件"])

    async def test_revoked_view_blocks_old_detail_writes_with_operation_permissions_retained(self):
        value = record()
        await self.store(value)
        codes = [item["code"] for item in self.service.list_permissions() if str(item.get("code") or "").startswith("ecn.") and item["code"] != "ecn.view"]
        role_id = self.service.create_security_role(code="test.ecn.no_view", name="保留操作权限但不可查看", permission_codes=codes)
        self.service.set_user_security_roles("张三", [role_id])
        self.assertFalse(self.service.has_permission("张三", "ecn.view"))
        self.assertTrue(self.service.has_permission("张三", "ecn.scheme.edit"))
        edited = copy.deepcopy(value["change_items"][0])
        edited["new_content"] = "stale page edit"
        operations = [
            lambda: actions.edit_scheme(value["ecn_id"], value, edited, value["change_items"][0], "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: actions.save_review(value["ecn_id"], value, value["review_info"], value["review_info"], "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: actions.set_participant_status(value["ecn_id"], value, "张三", "研发经理", "editing", user_service=self.service, storage=self.left),
            lambda: actions.set_trial_production_decision(value["ecn_id"], value, True, "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: actions.set_ecn_level(value["ecn_id"], value, "simple", "before_scheme_review", "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: actions.update_material_codes(value["ecn_id"], edited, {}, "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: actions.execute_action(value, value, "initiate_scheme_review", "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: validation_reports.set_validation_report_required(value["ecn_id"], "S1", {}, True, "张三", "研发经理", user_service=self.service, storage=self.left),
            lambda: validation_reports.review_validation_report(value["ecn_id"], "S1", {}, True, "", "张三", "研发经理", expected_scheme=build_ecn_scheme_snapshot(edited), user_service=self.service, storage=self.left),
        ]
        for operation in operations:
            result = await operation()
            self.assertFalse(result.ok)
            self.assertIn("ECN查看权限", result.message)
            self.assertEqual(await self.fresh(), value)
        deleted, _ = await delete_record(value["ecn_id"], username="张三", user_service=self.service, storage=self.left)
        self.assertFalse(deleted)
        self.assertEqual(await self.fresh(), value)
        view_role = self.service.create_security_role(code="test.ecn.view.restored", name="恢复查看", permission_codes=["ecn.view"])
        self.service.set_user_security_roles("张三", [role_id, view_role])
        saved = await actions.edit_scheme(value["ecn_id"], value, edited, value["change_items"][0], "张三", "研发经理", user_service=self.service, storage=self.left)
        self.assertTrue(saved.ok, saved.message)

    async def test_independent_impact_fields_and_project_additions_are_preserved(self):
        value = record()
        await self.store(value)
        baseline = value["review_info"]
        left, right = copy.deepcopy(baseline), copy.deepcopy(baseline)
        left["impacts"]["光学部件"] = True
        left["expanded_projects_mass"] = ["P2"]
        right["involved_materials"]["光源"]["新增"] = True
        right["expanded_projects_mass"] = ["P3"]
        with patch.object(actions, "can_edit_ecn_impact", return_value=True):
            results = await asyncio.gather(
                actions.save_review(value["ecn_id"], value, baseline, left, "张三", "研发", user_service=self.service, storage=self.left),
                actions.save_review(value["ecn_id"], value, baseline, right, "李四", "研发", user_service=self.service, storage=self.right),
            )
        self.assertTrue(all(result.ok for result in results))
        saved = await self.fresh()
        self.assertTrue(saved["review_info"]["impacts"]["光学部件"])
        self.assertTrue(saved["review_info"]["involved_materials"]["光源"]["新增"])
        self.assertEqual(set(saved["review_info"]["expanded_projects_mass"]), {"P2", "P3"})
        self.assertEqual(set(saved["workflow"]["impact_handlers"]), {"张三", "李四"})

    async def test_same_text_field_conflict_rejects_entire_second_patch(self):
        value = record()
        await self.store(value)
        baseline = value["review_info"]
        first, second = copy.deepcopy(baseline), copy.deepcopy(baseline)
        first["other_docs_desc"] = "甲修改"
        second["other_docs_desc"] = "乙修改"
        second["impacts"]["光学部件"] = True
        with patch.object(actions, "can_edit_ecn_impact", return_value=True):
            left = await actions.save_review(value["ecn_id"], value, baseline, first, "张三", "研发", user_service=self.service, storage=self.left)
            right = await actions.save_review(
                value["ecn_id"], value, baseline, second, "李四", "研发", user_service=self.service, storage=self.right
            )
        self.assertTrue(left.ok)
        self.assertFalse(right.ok)
        saved = await self.fresh()
        self.assertEqual(saved["review_info"]["other_docs_desc"], "甲修改")
        self.assertFalse(saved["review_info"]["impacts"]["光学部件"])

    async def test_scheme_same_author_stale_edit_and_delete_are_rejected(self):
        value = record()
        await self.store(value)
        original = value["change_items"][0]
        first, second = copy.deepcopy(original), copy.deepcopy(original)
        first["new_content"] = "第一次修改"
        second["new_content"] = "过期窗口修改"
        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            saved = await actions.edit_scheme(
                value["ecn_id"], value, first, original, "张三", "研发", user_service=self.service, storage=self.left
            )
            stale = await actions.edit_scheme(
                value["ecn_id"], value, second, original, "张三", "研发", user_service=self.service, storage=self.right
            )
            deleted = await actions.edit_scheme(
                value["ecn_id"], value, None, original, "张三", "研发", delete=True, user_service=self.service, storage=self.right
            )
        self.assertTrue(saved.ok)
        self.assertFalse(stale.ok)
        self.assertFalse(deleted.ok)
        self.assertEqual((await self.fresh())["change_items"][0]["new_content"], "第一次修改")

    async def test_scheme_save_rejects_association_outside_latest_scope(self):
        value = record()
        await self.store(value)
        original = value["change_items"][0]
        stale = copy.deepcopy(original)
        stale["projects"] = ["P1", "P2"]

        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            result = await actions.edit_scheme(
                value["ecn_id"],
                value,
                stale,
                original,
                "张三",
                "研发",
                user_service=self.service, storage=self.left,
            )

        self.assertFalse(result.ok)
        self.assertIn("移除失效关联", result.message)
        self.assertEqual((await self.fresh())["change_items"][0]["projects"], ["P1"])

    async def test_overview_scheme_save_rechecks_latest_target_snapshot(self):
        value = record()
        value["change_items"] = []
        value["workflow"]["scheme_participants"] = {}
        current_chip = {"id": "chip-1", "content": "当前内容", "enabled": True}
        await self.left.set_item("P1_over_data", {"参数A": {"chip-1": current_chip}})
        await self.store(value)
        item = {
            "item_id": "S2",
            "author": "张三",
            "type": "overview_update",
            "scheme_category": "overview_document",
            "role": "研发",
            "label": "参数A",
            "first_col_label": "参数A",
            "projects": ["P1"],
            "req_idxs": [1],
            "linked_docs": [],
            "linked_materials": [],
            "project_states": {
                "P1": {"action": "update", "chip_id": "chip-1", "old_data": {"content": "过期内容"}}
            },
        }

        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            stale = await actions.edit_scheme(
                value["ecn_id"], value, item, None, "张三", "研发", user_service=self.service, storage=self.left
            )
            item["project_states"]["P1"]["old_data"] = current_chip
            saved = await actions.edit_scheme(
                value["ecn_id"], value, item, None, "张三", "研发", user_service=self.service, storage=self.left
            )

        self.assertFalse(stale.ok)
        self.assertIn("目标概述已变化", stale.message)
        self.assertTrue(saved.ok, saved.message)

    async def test_overview_projects_and_project_states_must_match(self):
        value = record()
        value["change_items"][0].update(
            type="overview_update",
            scheme_category="overview_document",
            role="研发",
            label="参数A",
            first_col_label="参数A",
            project_states={},
        )

        with self.assertRaisesRegex(ECNConflict, "目标项目与逐项目配置不一致"):
            actions.validate_scheme_review(value)

    async def test_removed_association_requires_author_review_but_new_scope_only_blocks_review(self):
        value = record()
        value["workflow"]["ecn_level"] = ECN_LEVEL_COMPLEX
        value["change_items"][0]["validation_report"] = {
            "required": True, "status": "approved", "attachments": [{"id": "report-1"}],
            "reviewed_by": "reviewer", "reviewed_at": "2026-10-17 10:00:00", "review_note": "passed",
        }
        value["review_info"]["expanded_projects_mass"] = ["P2"]
        value["change_items"][0]["projects"] = ["P1", "P2"]
        await self.store(value)

        removed = copy.deepcopy(value["review_info"])
        removed["expanded_projects_mass"] = []
        with patch.object(actions, "can_edit_ecn_impact", return_value=True):
            result = await actions.save_review(
                value["ecn_id"],
                value,
                value["review_info"],
                removed,
                "张三",
                "研发",
                user_service=self.service, storage=self.left,
            )

        self.assertTrue(result.ok, result.message)
        assert result.record is not None
        affected = result.record["change_items"][0]
        self.assertEqual(affected["validation_report"]["status"], "pending_review")
        self.assertEqual(affected["validation_report"]["attachments"], [{"id": "report-1"}])
        self.assertNotIn("reviewed_by", affected["validation_report"])
        self.assertNotIn("reviewed_at", affected["validation_report"])
        self.assertNotIn("review_note", affected["validation_report"])
        self.assertEqual(affected["projects"], ["P1"])
        self.assertEqual(
            affected["association_change_notice"]["removed"]["projects"],
            ["P2"],
        )
        self.assertEqual(
            result.record["workflow"]["scheme_participants"]["张三"],
            ECN_PARTICIPANT_STATUS_EDITING,
        )

        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            blocked = await actions.set_participant_status(
                value["ecn_id"],
                result.record,
                "张三",
                "研发",
                ECN_PARTICIPANT_STATUS_CONFIRMED,
                user_service=self.service,
                storage=self.left,
            )
            retained = await actions.retain_scheme_after_association_change(
                value["ecn_id"],
                result.record,
                "S1",
                "张三",
                "研发",
                user_service=self.service,
                storage=self.left,
            )
        self.assertFalse(blocked.ok)
        self.assertIn("确认保留、修改或删除", blocked.message)
        self.assertTrue(retained.ok, retained.message)
        assert retained.record is not None
        self.assertNotIn("association_change_notice", retained.record["change_items"][0])

        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            confirmed = await actions.set_participant_status(
                value["ecn_id"],
                retained.record,
                "张三",
                "研发",
                ECN_PARTICIPANT_STATUS_CONFIRMED,
                user_service=self.service,
                storage=self.left,
            )
        self.assertTrue(confirmed.ok, confirmed.message)
        assert confirmed.record is not None
        with self.assertRaisesRegex(ECNConflict, "验证报告未审批通过"):
            actions.validate_scheme_review(confirmed.record)
        # 模拟报告重新审批通过，恢复完整基线，再独立验证新增范围的覆盖门禁。
        confirmed.record["change_items"][0]["validation_report"]["status"] = "approved"
        await self.store(confirmed.record)
        actions.validate_scheme_review(confirmed.record)

        added = copy.deepcopy(confirmed.record["review_info"])
        added["involved_docs"]["光学件图纸"] = True
        with patch.object(actions, "can_edit_ecn_impact", return_value=True):
            added_result = await actions.save_review(
                value["ecn_id"],
                confirmed.record,
                confirmed.record["review_info"],
                added,
                "张三",
                "研发",
                user_service=self.service, storage=self.left,
            )
        self.assertTrue(added_result.ok, added_result.message)
        assert added_result.record is not None
        self.assertEqual(
            added_result.record["workflow"]["scheme_participants"]["张三"],
            ECN_PARTICIPANT_STATUS_CONFIRMED,
        )
        self.assertFalse(actions.is_ecn_scheme_ready_for_review(added_result.record))

    async def test_user_without_own_scheme_cannot_confirm_participation(self):
        value = record()
        await self.store(value)
        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            result = await actions.set_participant_status(
                value["ecn_id"],
                copy.deepcopy(value),
                "李四",
                "研发",
                ECN_PARTICIPANT_STATUS_CONFIRMED,
                user_service=self.service,
                storage=self.left,
            )
        self.assertFalse(result.ok)
        self.assertIn("先添加至少一条本人方案", result.message)
        self.assertNotIn("李四", (await self.fresh())["workflow"]["scheme_participants"])

    async def test_review_start_rechecks_latest_confirmation_and_coverage(self):
        value = record()
        for mutate in (
            lambda v: v["workflow"]["scheme_participants"].update(张三=ECN_PARTICIPANT_STATUS_EDITING),
            lambda v: v["review_info"]["involved_docs"].update(光学件图纸=True),
        ):
            changed = copy.deepcopy(value)
            mutate(changed)
            await self.store(changed)
            with patch.object(actions, "can_submit_ecn_scheme_review", return_value=True):
                result = await self.action(value, "initiate_scheme_review")
            self.assertFalse(result.ok)
            self.assertEqual((await self.fresh())["workflow"]["current_state"], ECNState.ECN_SCHEMING)

    async def test_simultaneous_edit_and_review_never_review_unconfirmed_content(self):
        value = record()
        await self.store(value)
        updated = copy.deepcopy(value["change_items"][0])
        updated["new_content"] = "并发修改"
        with (
            patch.object(actions, "can_edit_ecn_scheme", return_value=True),
            patch.object(actions, "can_submit_ecn_scheme_review", return_value=True),
        ):
            results = await asyncio.gather(
                self.action(value, "initiate_scheme_review", storage=self.left),
                actions.edit_scheme(
                    value["ecn_id"], value, updated, value["change_items"][0], "张三", "研发", user_service=self.service, storage=self.right
                ),
            )
        self.assertEqual(sum(result.ok for result in results), 1)
        saved = await self.fresh()
        if saved["workflow"]["current_state"] == ECNState.ECN_REVIEWING:
            self.assertEqual(saved["change_items"][0]["new_content"], "新内容")
            self.assertEqual(saved["workflow"]["scheme_participants"]["张三"], ECN_PARTICIPANT_STATUS_CONFIRMED)

    async def test_deleted_record_is_not_recreated_by_old_form(self):
        draft = record(ECNState.DRAFT)
        with patch.object(actions, "can_create_ecn_request", return_value=True):
            result = await self.action(draft, "save_draft")
        self.assertFalse(result.ok)
        self.assertIsNone(await self.fresh())

    async def test_stale_draft_cannot_overwrite_another_window(self):
        draft = record(ECNState.DRAFT)
        changed = copy.deepcopy(draft)
        changed["basic_info"]["reason_desc"] = "另一窗口已保存"
        await self.store(changed)
        submitted = copy.deepcopy(draft)
        submitted["basic_info"]["reason_desc"] = "旧窗口修改"
        with patch.object(actions, "can_create_ecn_request", return_value=True):
            result = await actions.execute_action(
                submitted, draft, "save_draft", "张三", "研发", user_service=self.service, storage=self.right
            )
        self.assertFalse(result.ok)
        self.assertEqual((await self.fresh())["basic_info"]["reason_desc"], "另一窗口已保存")

    async def test_old_round_and_late_impact_save_cannot_change_reviewing_record(self):
        expected = record()
        current = copy.deepcopy(expected)
        current["workflow"]["approval_round"] = "round-2"
        await self.store(current)
        with patch.object(actions, "can_submit_ecn_scheme_review", return_value=True):
            self.assertFalse((await self.action(expected, "initiate_scheme_review")).ok)
        current["workflow"]["current_state"] = ECNState.ECN_REVIEWING
        await self.store(current)
        submitted = copy.deepcopy(expected["review_info"])
        submitted["other_docs_desc"] = "迟到的自动保存"
        with patch.object(actions, "can_edit_ecn_impact", return_value=True):
            result = await actions.save_review(
                expected["ecn_id"], expected, expected["review_info"], submitted, "张三", "研发", user_service=self.service, storage=self.left
            )
        self.assertFalse(result.ok)

    async def test_unchanged_rejected_item_cannot_be_marked_revised(self):
        value = record()
        reject_ecn_scheme_items(value, ["S1"], "审核员", "研发经理", "需修改", "2026-09-07 10:00:00")
        await self.store(value)
        original = value["change_items"][0]
        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            result = await actions.edit_scheme(
                value["ecn_id"], value, copy.deepcopy(original), original, "张三", "研发", user_service=self.service, storage=self.left
            )
        self.assertFalse(result.ok)
        self.assertEqual((await self.fresh())["change_items"][0]["review_status"], ECN_ITEM_STATUS_NEEDS_IMPROVEMENT)


class ECNMergeTests(unittest.TestCase):
    def test_polling_preserves_dirty_input_and_conflict_baseline(self):
        baseline = {"other_docs_desc": "旧值", "impacts": {"A": False, "B": False}}
        local = copy.deepcopy(baseline)
        local["other_docs_desc"] = "正在输入"
        fresh = {"other_docs_desc": "他人修改", "impacts": {"A": True, "B": False}}
        sync_review_snapshot(local, baseline, fresh)
        self.assertEqual(local["other_docs_desc"], "正在输入")
        self.assertEqual(baseline["other_docs_desc"], "旧值")
        self.assertTrue(local["impacts"]["A"])
        with self.assertRaises(ECNConflict):
            merge_fields(fresh, baseline, local)


class ECNDatabaseApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.permission_catalog import ECN_CREATE_PERMISSION, ECN_ECR_APPROVE_PERMISSION, ECN_VIEW_PERMISSION
        from tests.test_approval_workflow import ApprovalWorkflowTests

        self.fixture = ApprovalWorkflowTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.service.set_position_permissions(
            self.fixture.requester_position_id,
            [ECN_CREATE_PERMISSION, ECN_ECR_APPROVE_PERMISSION, ECN_VIEW_PERMISSION],
        )
        self.service.set_position_permissions(self.fixture.approver_position_id, ["ecn.view", ECN_ECR_APPROVE_PERMISSION])
        self.service.set_position_permissions(self.fixture.observer_position_id, ["ecn.view", ECN_ECR_APPROVE_PERMISSION])
        users = self.service.load_users()
        workflow_id, _ = self.service.save_approval_workflow_draft(
            code="ecn.transaction.test",
            module="ecn",
            event="ecr_review",
            name="事务会签测试",
            priority=1,
            condition={"requester_org_unit_ids": [self.fixture.org_unit_id]},
            approver={"strategy": "users", "user_ids": [users[name]["user_id"] for name in ("李四", "王五")]},
            required_permission_code=ECN_ECR_APPROVE_PERMISSION,
            approval_mode="all",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")
        db_path = Path(self.service.identity_store.db_path)
        self.left = load_isolated_db_storage("ecn_iam_left", db_path)
        self.right = load_isolated_db_storage("ecn_iam_right", db_path)
        await self.left.init_db()
        await self.right.init_db()

    async def asyncTearDown(self):
        await self.left.close_db()
        await self.right.close_db()
        self.fixture.tearDown()

    def pending(self, value):
        assignment = value["workflow"]["ecr_workflow_assignment"]
        return self.service.list_pending_assignment_usernames(
            module="ecn",
            entity_id=value["ecn_id"],
            task_key=assignment["current_task_key"],
        )

    async def create(self, storage=None):
        value = record(ECNState.DRAFT)
        value["workflow"]["current_phase"] = "ECR_PHASE"
        return await actions.execute_action(
            value,
            copy.deepcopy(value),
            "submit_ecr",
            "张三",
            "申请岗位",
            is_new=True,
            user_service=self.service,
            storage=storage or self.left,
        )

    async def test_disabled_user_cannot_submit_from_an_already_open_page(self):
        value = record(ECNState.DRAFT)
        value["workflow"]["current_phase"] = "ECR_PHASE"
        await self.left.set_item("ecn_management_data", {value["ecn_id"]: value})
        self.service.modify_user("deactivate", "张三")
        try:
            result = await actions.execute_action(
                value,
                copy.deepcopy(value),
                "save_draft",
                "张三",
                "申请岗位",
                user_service=self.service,
                storage=self.left,
            )
            self.assertFalse(result.ok)
            self.assertIn("停用", result.message)
        finally:
            self.service.modify_user("activate", "张三")

    async def test_revoked_view_blocks_approval_and_reassignment_without_changing_pending(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        value = created.record
        before = self.pending(value)
        self.service.set_position_permissions(self.fixture.approver_position_id, ["ecn.ecr.approve"])
        denied = await actions.execute_action(value, copy.deepcopy(value), "approve", "李四", "审批岗位", user_service=self.service, storage=self.left)
        self.assertFalse(denied.ok)
        self.assertIn("ECN查看权限", denied.message)
        self.service.set_position_permissions(self.fixture.requester_position_id, ["ecn.request.create", "ecn.ecr.approve", "ecn.approval.reassign"])
        assignment = value["workflow"]["ecr_workflow_assignment"]
        reassigned = await reassign_ecn_approval_reviewer(
            value["ecn_id"], "ecr_workflow_assignment", 0, "李四", "张三", copy.deepcopy(assignment["nodes"][0]),
            actor_username="张三", user_service=self.service, storage=self.right,
        )
        self.assertFalse(reassigned.ok)
        self.assertIn("ECN查看权限", reassigned.message)
        self.assertEqual(self.pending(value), before)
        self.assertEqual((await self.left.get_fresh_item("ecn_management_data", {}))[value["ecn_id"]], value)

    async def test_delete_closes_pending_approval_in_the_same_transaction(self):
        created = await self.create()
        self.assertTrue(created.ok)
        assert created.record is not None
        ecn_id = str(created.record["ecn_id"])
        self.assertTrue(self.pending(created.record))
        deleted, message = await delete_record(
            ecn_id,
            username="admin",
            user_service=self.service,
            storage=self.left,
        )
        self.assertTrue(deleted, message)
        self.assertNotIn(ecn_id, await self.left.get_fresh_item("ecn_management_data", {}))
        assignment = created.record["workflow"]["ecr_workflow_assignment"]
        self.assertEqual(
            self.service.list_pending_assignment_usernames(
                module="ecn",
                entity_id=ecn_id,
                task_key=assignment["current_task_key"],
            ),
            [],
        )

    async def test_ecr_rejection_requires_a_nonempty_note(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None

        blank = await actions.execute_action(
            created.record,
            copy.deepcopy(created.record),
            "reject",
            "李四",
            "审批岗位",
            note="   ",
            user_service=self.service,
            storage=self.left,
        )
        self.assertFalse(blank.ok)
        self.assertIn("驳回意见", blank.message)

        rejected = await actions.execute_action(
            created.record,
            copy.deepcopy(created.record),
            "reject",
            "李四",
            "审批岗位",
            note="  需补充风险评估  ",
            user_service=self.service,
            storage=self.left,
        )
        self.assertTrue(rejected.ok, rejected.message)
        assert rejected.record is not None
        self.assertEqual(rejected.record["workflow"]["current_state"], ECNState.REJECTED)
        self.assertEqual(rejected.record["approval_log"][-1]["note"], "需补充风险评估")

    async def test_ecr_level_change_to_simple_rebuilds_pending_workflow(self):
        from src.permission_catalog import (
            ECN_CREATE_PERMISSION,
            ECN_ECR_APPROVE_PERMISSION,
            ECN_LEVEL_CLASSIFY_ECR_PERMISSION,
            ECN_VIEW_PERMISSION,
        )

        self.service.set_position_permissions(
            self.fixture.requester_position_id,
            [
                ECN_CREATE_PERMISSION,
                ECN_ECR_APPROVE_PERMISSION,
                ECN_LEVEL_CLASSIFY_ECR_PERMISSION,
                ECN_VIEW_PERMISSION,
            ],
        )
        users = self.service.load_users()
        simple_workflow_id, _ = self.service.save_approval_workflow_draft(
            code="ecn.simple.ecr.test",
            module="ecn",
            event="ecr_review_simple",
            name="简单ECN审批",
            priority=1,
            condition={"requester_org_unit_ids": [self.fixture.org_unit_id]},
            approver={"strategy": "users", "user_ids": [users["李四"]["user_id"]]},
            required_permission_code=ECN_ECR_APPROVE_PERMISSION,
            approval_mode="any",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(simple_workflow_id, actor_username="admin")
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        self.assertEqual(set(self.pending(created.record)), {"李四", "王五"})

        changed = await actions.set_ecn_level(
            created.record["ecn_id"],
            copy.deepcopy(created.record),
            "simple",
            "ecr_review",
            "张三",
            "申请岗位",
            user_service=self.service,
            storage=self.left,
        )
        self.assertTrue(changed.ok, changed.message)
        assert changed.record is not None
        assignment = changed.record["workflow"]["ecr_workflow_assignment"]
        self.assertEqual(assignment["workflow_code"], "ecn.simple.ecr.test")
        self.assertEqual(self.pending(changed.record), ["李四"])

    async def approve(self, value, user, storage=None):
        return await actions.execute_action(
            value,
            copy.deepcopy(value),
            "approve",
            user,
            "审批岗位",
            user_service=self.service,
            storage=storage or self.left,
        )

    async def test_authorized_admin_can_transfer_current_approval_task(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        assignment = created.record["workflow"]["ecr_workflow_assignment"]
        baseline_node = copy.deepcopy(assignment["nodes"][0])
        result = await reassign_ecn_approval_reviewer(
            created.record["ecn_id"],
            "ecr_workflow_assignment",
            0,
            "李四",
            "张三",
            baseline_node,
            actor_username="admin",
            user_service=self.service,
            storage=self.left,
        )
        self.assertTrue(result.ok, result.message)
        assert result.record is not None
        self.assertEqual(set(self.pending(result.record)), {"张三", "王五"})
        node = result.record["workflow"]["ecr_workflow_assignment"]["nodes"][0]
        self.assertEqual(set(node["assignee_usernames"]), {"张三", "王五"})
        self.assertEqual(node["reassignment_history"][-1]["from"], "李四")
        self.assertFalse((await self.approve(result.record, "李四")).ok)
        self.assertTrue((await self.approve(result.record, "张三")).ok)

    async def test_disabled_current_reviewer_can_be_replaced(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        node = copy.deepcopy(created.record["workflow"]["ecr_workflow_assignment"]["nodes"][0])
        self.service.modify_user("deactivate", "李四")
        try:
            result = await reassign_ecn_approval_reviewer(
                created.record["ecn_id"],
                "ecr_workflow_assignment",
                0,
                "李四",
                "张三",
                node,
                actor_username="admin",
                user_service=self.service,
                storage=self.left,
            )
            self.assertTrue(result.ok, result.message)
            assert result.record is not None
            self.assertEqual(set(self.pending(result.record)), {"张三", "王五"})
        finally:
            self.service.modify_user("activate", "李四")

    async def test_stale_or_ineligible_approval_transfer_is_rejected(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        assignment = created.record["workflow"]["ecr_workflow_assignment"]
        baseline_node = copy.deepcopy(assignment["nodes"][0])
        first = await reassign_ecn_approval_reviewer(
            created.record["ecn_id"],
            "ecr_workflow_assignment",
            0,
            "李四",
            "张三",
            baseline_node,
            actor_username="admin",
            user_service=self.service,
            storage=self.left,
        )
        self.assertTrue(first.ok, first.message)
        stale = await reassign_ecn_approval_reviewer(
            created.record["ecn_id"],
            "ecr_workflow_assignment",
            0,
            "王五",
            "李四",
            baseline_node,
            actor_username="admin",
            user_service=self.service,
            storage=self.right,
        )
        self.assertFalse(stale.ok)

    async def test_user_without_reassignment_permission_is_rejected(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        node = copy.deepcopy(created.record["workflow"]["ecr_workflow_assignment"]["nodes"][0])
        result = await reassign_ecn_approval_reviewer(
            created.record["ecn_id"],
            "ecr_workflow_assignment",
            0,
            "李四",
            "张三",
            node,
            actor_username="张三",
            user_service=self.service,
            storage=self.left,
        )
        self.assertFalse(result.ok)
        self.assertIn("权限", result.message)

    async def test_future_approval_node_can_be_reassigned_without_creating_early_task(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        value = copy.deepcopy(created.record)
        assignment = value["workflow"]["ecr_workflow_assignment"]
        future_node = copy.deepcopy(assignment["nodes"][0])
        future_node.update(
            node_key="future_review",
            name="后续审批",
            node_index=1,
            assignee_usernames=["李四"],
            approved_usernames=[],
            status="waiting",
        )
        assignment["nodes"].append(future_node)
        await self.left.set_item("ecn_management_data", {value["ecn_id"]: value})
        result = await reassign_ecn_approval_reviewer(
            value["ecn_id"],
            "ecr_workflow_assignment",
            1,
            "李四",
            "张三",
            copy.deepcopy(future_node),
            actor_username="admin",
            user_service=self.service,
            storage=self.left,
        )
        self.assertTrue(result.ok, result.message)
        assert result.record is not None
        updated = result.record["workflow"]["ecr_workflow_assignment"]["nodes"][1]
        self.assertEqual(updated["assignee_usernames"], ["张三"])
        self.assertEqual(set(self.pending(result.record)), {"李四", "王五"})

    async def test_concurrent_creation_binds_assignments_to_correct_allocated_id(self):
        results = await asyncio.gather(self.create(self.left), self.create(self.right))
        self.assertTrue(all(result.ok for result in results), [r.message for r in results])
        assert results[0].record is not None
        assert results[1].record is not None
        self.assertNotEqual(results[0].record["ecn_id"], results[1].record["ecn_id"])
        for result in results:
            assert result.record is not None
            self.assertEqual(set(self.pending(result.record)), {"李四", "王五"})

    async def test_parallel_database_votes_commit_snapshot_and_pending_together(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        value = created.record
        results = await asyncio.gather(self.approve(value, "李四", self.left), self.approve(value, "王五", self.right))
        self.assertTrue(all(result.ok for result in results), [r.message for r in results])
        saved = (await self.left.get_fresh_item("ecn_management_data"))[value["ecn_id"]]
        self.assertEqual(saved["workflow"]["current_state"], ECNState.ECN_SCHEMING)
        assignment = saved["workflow"]["ecr_workflow_assignment"]
        self.assertEqual(set(assignment["nodes"][0]["approved_usernames"]), {"李四", "王五"})
        self.assertEqual(self.pending(value), [])
        self.assertFalse((await self.approve(value, "李四")).ok)

    async def test_approval_failure_rolls_back_pending_and_record(self):
        created = await self.create()
        self.assertTrue(created.ok, created.message)
        assert created.record is not None
        original_flush = ApprovalTransaction.flush

        async def fail_after_writes(proxy, connection):
            await original_flush(proxy, connection)
            raise RuntimeError("模拟待办写入后异常")

        with patch.object(ApprovalTransaction, "flush", fail_after_writes), self.assertLogs(level="ERROR"):
            result = await self.approve(created.record, "李四")
        self.assertFalse(result.ok)
        saved = (await self.left.get_fresh_item("ecn_management_data"))[created.record["ecn_id"]]
        self.assertEqual(saved, created.record)
        self.assertEqual(set(self.pending(saved)), {"李四", "王五"})
        self.assertTrue((await self.approve(saved, "李四")).ok)

    async def test_create_failure_leaves_no_record_or_orphaned_tasks(self):
        original_flush = ApprovalTransaction.flush

        async def fail_after_writes(proxy, connection):
            await original_flush(proxy, connection)
            raise RuntimeError("模拟新建落盘失败")

        with patch.object(ApprovalTransaction, "flush", fail_after_writes), self.assertLogs(level="ERROR"):
            result = await self.create()
        self.assertFalse(result.ok)
        self.assertEqual(await self.left.get_fresh_item("ecn_management_data", {}), {})
        self.assertEqual(self.service.list_pending_work_assignment_refs(module="ecn"), [])


if __name__ == "__main__":
    unittest.main()
