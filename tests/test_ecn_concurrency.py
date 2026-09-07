"""使用隔离 SQLite 连接验证 ECN 并发，不读取或写入运行数据库。"""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.ecn_management_config import (
    ECN_ITEM_STATUS_NEEDS_IMPROVEMENT,
    ECN_PARTICIPANT_STATUS_CONFIRMED,
    ECN_PARTICIPANT_STATUS_EDITING,
    ECNState,
    reject_ecn_scheme_items,
)
from src.modules.ecn import actions
from src.modules.ecn.approval_transaction import ApprovalTransaction
from src.modules.ecn.editing import ECNConflict, merge_fields, sync_review_snapshot
from src.modules.ecn.models import get_ecn_template
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
    return value


class ECNConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "ecn.db"
        self.left = load_isolated_db_storage("ecn_left", path)
        self.right = load_isolated_db_storage("ecn_right", path)
        await self.left.init_db()
        await self.right.init_db()
        self.service = SimpleNamespace(storage_mode="legacy_excel")

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
                actions.save_review(value["ecn_id"], value, baseline, left, "张三", "研发", storage=self.left),
                actions.save_review(value["ecn_id"], value, baseline, right, "李四", "研发", storage=self.right),
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
            left = await actions.save_review(value["ecn_id"], value, baseline, first, "张三", "研发", storage=self.left)
            right = await actions.save_review(
                value["ecn_id"], value, baseline, second, "李四", "研发", storage=self.right
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
                value["ecn_id"], value, first, original, "张三", "研发", storage=self.left
            )
            stale = await actions.edit_scheme(
                value["ecn_id"], value, second, original, "张三", "研发", storage=self.right
            )
            deleted = await actions.edit_scheme(
                value["ecn_id"], value, None, original, "张三", "研发", delete=True, storage=self.right
            )
        self.assertTrue(saved.ok)
        self.assertFalse(stale.ok)
        self.assertFalse(deleted.ok)
        self.assertEqual((await self.fresh())["change_items"][0]["new_content"], "第一次修改")

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
                    value["ecn_id"], value, updated, value["change_items"][0], "张三", "研发", storage=self.right
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

    async def test_parallel_legacy_approvals_preserve_both_votes_and_ignore_stale_form(self):
        value = record(ECNState.ECN_REVIEWING)
        value["workflow"].update(
            current_phase="ECN_SCHEME_REVIEW_PHASE", current_step_index=2, pending_roles=["工程NPI", "质量经理", "PMC"]
        )
        changed = copy.deepcopy(value)
        changed["basic_info"]["reason_desc"] = "最新申请说明"
        await self.store(changed)
        results = await asyncio.gather(
            self.action(value, "approve", user="工程师", role="工程NPI"),
            self.action(value, "approve", user="质量", role="质量经理", storage=self.right),
        )
        self.assertTrue(all(result.ok for result in results))
        saved = await self.fresh()
        self.assertEqual(saved["workflow"]["step_approvals"], {"工程NPI": True, "质量经理": True})
        self.assertEqual(saved["basic_info"]["reason_desc"], "最新申请说明")
        last = await self.action(saved, "approve", user="物资", role="PMC")
        self.assertTrue(last.ok)
        assert last.record is not None
        self.assertEqual(last.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)

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
                expected["ecn_id"], expected, expected["review_info"], submitted, "张三", "研发", storage=self.left
            )
        self.assertFalse(result.ok)

    async def test_unchanged_rejected_item_cannot_be_marked_revised(self):
        value = record()
        reject_ecn_scheme_items(value, ["S1"], "审核员", "研发经理", "需修改", "2026-09-07 10:00:00")
        await self.store(value)
        original = value["change_items"][0]
        with patch.object(actions, "can_edit_ecn_scheme", return_value=True):
            result = await actions.edit_scheme(
                value["ecn_id"], value, copy.deepcopy(original), original, "张三", "研发", storage=self.left
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
        from src.permission_catalog import ECN_CREATE_PERMISSION, ECN_ECR_APPROVE_PERMISSION
        from tests.test_approval_workflow import ApprovalWorkflowTests

        self.fixture = ApprovalWorkflowTests()
        self.fixture.setUp()
        self.service = self.fixture.service
        self.service.set_position_permissions(self.fixture.requester_position_id, [ECN_CREATE_PERMISSION])
        self.service.set_position_permissions(self.fixture.approver_position_id, [ECN_ECR_APPROVE_PERMISSION])
        self.service.set_position_permissions(self.fixture.observer_position_id, [ECN_ECR_APPROVE_PERMISSION])
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
