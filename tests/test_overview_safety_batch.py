"""七类审核缺口的隔离回归；不使用运行数据库或共享盘。"""
import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import components, overview_batch_operations as batch, overview_corrections as corrections, overview_storage
from src import overview_add_approval as add, overview_request_storage as requests
from src.approval_transaction import ApprovalTransaction
from tests.test_error_management_concurrency import load_isolated_db_storage


class OverviewSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = load_isolated_db_storage("overview_safety_db", self.root / "test.db")
        await self.db.init_db()
        self.patches = [patch.object(module, "db_storage", self.db)
                        for module in (batch, corrections, overview_storage, add)]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.db.close_db()
        self.temp.cleanup()

    async def test_stale_resubmission_and_missing_request_are_rejected(self):
        await self.db.set_item(batch.BATCH_OVERVIEW_REQUESTS_KEY, {"r": {"status": "processing"}})
        self.assertFalse(await batch.update_batch_overview_request("r", {"status": "pending"}))
        self.assertFalse(await batch.update_batch_overview_request("missing", {"status": "approved"}))
        self.assertEqual(self.db.get_item(batch.BATCH_OVERVIEW_REQUESTS_KEY)["r"]["status"], "processing")

    async def test_existing_correction_target_is_frozen_at_submission(self):
        before = {"id": "c", "type": "file", "content": "old.pdf", "file_type": "application/pdf"}
        after = {**before, "content": "selected.pdf"}
        (self.root / "old.pdf").write_bytes(b"old")
        target = self.root / "selected.pdf"
        target.write_bytes(b"selected")
        config = {"label": "docs", "upload_path": str(self.root)}
        ok, message, audit = corrections.build_media_file_audit(before, config, "", after=after)
        self.assertTrue(ok, message)
        target.write_bytes(b"changed")
        request = {"project": "P1", "label": "docs", "chip_id": "c", "action": "correct",
                   "payload": {"before_snapshot": before, "before_fingerprint": corrections.chip_snapshot_fingerprint(before),
                               "after_snapshot": after, "config": config, **audit}}
        await self.db.set_item("P1_over_data", {"docs": {"c": before}})
        fake = SimpleNamespace(storage=SimpleNamespace(general={"over_config_data_flat": {"docs": config}}))
        with patch.object(corrections, "app", fake), patch.object(corrections, "can_edit_overview_item", return_value=True):
            result = await corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertEqual(self.db.get_item("P1_over_data")["docs"]["c"], before)

    async def test_related_reason_failure_rolls_back_state(self):
        before = {"docs": {"c": {"select_activ_dic": {"1.0": True}, "enabled": True}}}
        await self.db.set_item("P1_over_data", before)
        with patch.object(requests, "write_json", new=AsyncMock(side_effect=OSError("reason write failed"))):
            with self.assertRaises(RuntimeError):
                await batch.apply_related_overview_impacts(project="P1", related_labels=["docs"], source_content="source",
                    source_state=True, operation_type="add_chip", creator="owner", config_flat={}, overview_role={})
        self.assertEqual(await self.db.get_fresh_item("P1_over_data"), before)
        self.assertFalse(await self.db.get_fresh_item("P1_over_related_record", {}))

    async def test_related_impacts_recover_null_record_placeholders(self):
        chip = {"id": "c", "creator": "owner", "select_activ_dic": {"1.0": True}}
        previous = {"2026-10-01 09:00:00": {"record": {"旧影响": {"operate_chip_content": "历史内容"}}}}
        for old_records, has_history in (
            (None, False),
            ({"docs": None}, False),
            ({"docs": {"c": None}}, False),
            ({"docs": {"c": {**previous, "open": None}}}, True),
        ):
            with self.subTest(old_records=old_records):
                await self.db.set_item("P1_over_data", {"docs": {"c": chip}})
                await self.db.set_item("P1_over_related_record", old_records)
                changed = await batch.apply_related_overview_impacts(
                    project="P1", related_labels=["docs"], source_content="关键概述",
                    source_state=True, operation_type="add_chip", creator="owner",
                    config_flat={}, overview_role={},
                )
                self.assertEqual(changed, {"docs"})
                saved_chip = (await self.db.get_fresh_item("P1_over_data"))["docs"]["c"]
                self.assertIsNone(saved_chip["select_activ_dic"]["1.0"])
                saved_records = (await self.db.get_fresh_item("P1_over_related_record"))["docs"]["c"]
                opened = saved_records["open"]
                self.assertEqual(
                    next(iter(opened["record"].values()))["operate_chip_content"], "关键概述"
                )
                if has_history:
                    self.assertEqual(saved_records["2026-10-01 09:00:00"], previous["2026-10-01 09:00:00"])

    async def test_missing_related_overview_has_no_impact(self):
        await self.db.set_item("P1_over_data", {"docs": {}})
        changed = await batch.apply_related_overview_impacts(
            project="P1", related_labels=["not-existing"], source_content="关键概述",
            source_state=True, operation_type="add_chip", creator="owner",
            config_flat={}, overview_role={},
        )
        self.assertFalse(changed)
        self.assertFalse(await self.db.get_fresh_item("P1_over_related_record", {}))

    async def test_state_and_reason_archive_commit_or_roll_back_together(self):
        chip = {"id": "c", "type": "text", "content": "value", "enabled": None,
                "select_activ_dic": {"1.0": None}, "timestamp": {}}
        pending = {"docs": {"c": {"open": {"record": {"source": {"reason": "待处理"}}}}}}
        await self.db.set_item("P1_over_data", {"docs": {"c": chip}})
        await self.db.set_item("P1_over_related_record", pending)
        with patch.object(requests, "write_json", new=AsyncMock(side_effect=OSError("archive failed"))):
            changed, _, _ = await batch.update_overview_chip_state("P1", "docs", "c", "1.0", True, "owner")
        self.assertFalse(changed)
        self.assertEqual((await self.db.get_fresh_item("P1_over_data"))["docs"]["c"], chip)
        self.assertEqual(await self.db.get_fresh_item("P1_over_related_record"), pending)
        changed, _, _ = await batch.update_overview_chip_state("P1", "docs", "c", "1.0", True, "owner")
        self.assertTrue(changed)
        records = await self.db.get_fresh_item("P1_over_related_record")
        self.assertNotIn("open", records["docs"]["c"])
        self.assertEqual(len(records["docs"]["c"]), 1)

    async def test_regular_overview_page_uses_the_same_archive_transaction(self):
        chip = {"id": "c", "type": "text", "enabled": None,
                "select_activ_dic": {"1.0": None}, "timestamp": {}}
        await self.db.set_item("P1_over_data", {"docs": {"c": chip}})
        await self.db.set_item("P1_over_related_record", {"docs": {"c": {"open": {"record": {}}}}})
        with patch.object(components, "db_storage", self.db):
            with patch.object(requests, "write_json", new=AsyncMock(side_effect=OSError("archive failed"))):
                with self.assertRaises(overview_storage.OverviewWriteError):
                    await components._save_overview_activation_atomically("P1", "docs", "c", {"1.0": None},
                        {"1.0": True}, "owner", "confirmed", None)
            self.assertEqual((await self.db.get_fresh_item("P1_over_data"))["docs"]["c"], chip)
            changed = await components._save_overview_activation_atomically("P1", "docs", "c", {"1.0": None},
                {"1.0": True}, "owner", "confirmed", None)
        self.assertIs(changed, True)
        self.assertNotIn("open", (await self.db.get_fresh_item("P1_over_related_record"))["docs"]["c"])

    async def test_row_cascade_archives_child_reasons_in_same_write(self):
        first = {"id": "first", "row_id": "row", "type": "text", "enabled": True,
                 "select_activ_dic": {"1.0": True}, "timestamp": {}}
        child = {"id": "child", "row_id": "row", "type": "text", "enabled": None,
                 "select_activ_dic": {"1.0": None}, "timestamp": {}}
        before = {"first": {"first": first}, "child": {"child": child}}
        records = {"first": {"first": {"open": {"record": {}}}},
                   "child": {"child": {"open": {"record": {}}}}}
        await self.db.set_item("P1_over_data", before)
        await self.db.set_item("P1_over_related_record", records)
        with patch.object(requests, "write_json", new=AsyncMock(side_effect=OSError("archive failed"))):
            changed, _, _ = await batch.update_overview_chip_state("P1", "first", "first", "1.0", False,
                "owner", group_configs=[{"label": "first"}, {"label": "child"}])
        self.assertFalse(changed)
        self.assertEqual(await self.db.get_fresh_item("P1_over_data"), before)
        changed, _, _ = await batch.update_overview_chip_state("P1", "first", "first", "1.0", False,
            "owner", group_configs=[{"label": "first"}, {"label": "child"}])
        self.assertTrue(changed)
        saved = await self.db.get_fresh_item("P1_over_related_record")
        self.assertNotIn("open", saved["first"]["first"])
        self.assertNotIn("open", saved["child"]["child"])

    async def test_key_add_request_and_assignment_are_one_transaction(self):
        with closing(sqlite3.connect(self.root / "test.db")) as connection, connection:
            connection.execute("CREATE TABLE iam_users(user_id TEXT PRIMARY KEY, username TEXT, status TEXT)")
            connection.execute("INSERT INTO iam_users VALUES ('u1', 'reviewer', 'active')")
            connection.execute("CREATE TABLE work_assignments(assignment_id TEXT PRIMARY KEY, module TEXT, entity_id TEXT, "
                "task_key TEXT, assignment_type TEXT, assignee_user_id TEXT, status TEXT, source_policy_code TEXT, "
                "created_at TEXT, updated_at TEXT, completed_at TEXT, "
                "UNIQUE(module, entity_id, task_key, assignee_user_id))")

        def workflow(proxy, *, entity_id, **_kwargs):
            proxy.replace_work_assignments(module="project_overview", entity_id=entity_id, task_key="review",
                assignee_usernames=["reviewer"], source_policy_code="policy")
            return {"status": "matched", "assignment": {"current_task_key": "review", "status": "pending"}}

        service = SimpleNamespace()
        args = {"project": "P1", "label": "critical", "chip": {"id": "c", "content": "value"},
                "config": {"label": "critical", "add_approval_required": True},
                "requester_username": "owner", "requester_role": "editor", "user_service": service}
        with patch.object(add, "create_approval_sequence_assignments", side_effect=workflow):
            with patch.object(ApprovalTransaction, "flush", new=AsyncMock(side_effect=OSError("assignment failed"))):
                failed = await add.save_or_submit_overview_addition(**args)
            self.assertEqual(failed.status, "error")
            self.assertFalse(await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY, {}))
            saved = await add.save_or_submit_overview_addition(**args)
        self.assertEqual(saved.status, "pending")
        assert saved.request_id is not None
        with closing(sqlite3.connect(self.root / "test.db")) as connection, connection:
            pending = connection.execute("SELECT entity_id FROM work_assignments WHERE status='pending'").fetchall()
        self.assertEqual(pending, [(saved.request_id,)])
        self.assertIn(saved.request_id, await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY))

    async def test_key_add_resubmission_rollback_keeps_old_application_and_todos(self):
        with closing(sqlite3.connect(self.root / "test.db")) as connection, connection:
            connection.execute("CREATE TABLE iam_users(user_id TEXT PRIMARY KEY, username TEXT, status TEXT)")
            connection.execute("INSERT INTO iam_users VALUES ('u1', 'reviewer', 'active')")
            connection.execute("CREATE TABLE work_assignments(assignment_id TEXT PRIMARY KEY, module TEXT, entity_id TEXT, "
                "task_key TEXT, assignment_type TEXT, assignee_user_id TEXT, status TEXT, source_policy_code TEXT, "
                "created_at TEXT, updated_at TEXT, completed_at TEXT, "
                "UNIQUE(module, entity_id, task_key, assignee_user_id))")
        old = {"id": "r", "project": "P1", "label": "critical", "submitter": "owner",
               "submitter_role": "editor", "status": "rejected", "payload": {"chip": {"id": "c"}}}
        await self.db.set_item(add.OVERVIEW_ADD_REQUESTS_KEY, {"r": old})

        def workflow(proxy, **_kwargs):
            proxy.replace_work_assignments(module="project_overview", entity_id="r", task_key="review",
                assignee_usernames=["reviewer"], source_policy_code="policy")
            return {"status": "matched", "assignment": {"current_task_key": "review", "status": "pending"}}

        with patch.object(add, "create_approval_sequence_assignments", side_effect=workflow), \
             patch.object(add, "can_edit_overview_item", return_value=True), \
             patch.object(ApprovalTransaction, "flush", new=AsyncMock(side_effect=OSError("assignment failed"))):
            result = await add.resubmit_overview_add_request("r", "owner", chip={"id": "c", "content": "new"},
                config={"label": "critical", "add_approval_required": True}, user_service=SimpleNamespace())
        self.assertEqual(result.status, "error")
        self.assertEqual(await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY), {"r": old})
        with closing(sqlite3.connect(self.root / "test.db")) as connection, connection:
            self.assertFalse(connection.execute("SELECT 1 FROM work_assignments").fetchone())
        with patch.object(add, "create_approval_sequence_assignments", side_effect=workflow), \
             patch.object(add, "can_edit_overview_item", return_value=True):
            result = await add.resubmit_overview_add_request("r", "owner", chip={"id": "c", "content": "new"},
                config={"label": "critical", "add_approval_required": True}, user_service=SimpleNamespace())
        self.assertEqual(result.status, "pending")
        self.assertEqual((await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY))["r"]["status"], "pending")
        with closing(sqlite3.connect(self.root / "test.db")) as connection, connection:
            self.assertEqual(connection.execute("SELECT entity_id FROM work_assignments WHERE status='pending'").fetchall(),
                             [("r",)])

    async def test_pending_add_edit_restarts_review_atomically_and_rejects_old_snapshot(self):
        with closing(sqlite3.connect(self.root / "test.db")) as connection, connection:
            connection.execute("CREATE TABLE iam_users(user_id TEXT PRIMARY KEY, username TEXT, status TEXT)")
            connection.execute("INSERT INTO iam_users VALUES ('u1', 'reviewer', 'active')")
            connection.execute("CREATE TABLE work_assignments(assignment_id TEXT PRIMARY KEY, module TEXT, entity_id TEXT, "
                "task_key TEXT, assignment_type TEXT, assignee_user_id TEXT, status TEXT, source_policy_code TEXT, "
                "created_at TEXT, updated_at TEXT, completed_at TEXT, "
                "UNIQUE(module, entity_id, task_key, assignee_user_id))")
            connection.execute("INSERT INTO work_assignments VALUES "
                "('old', 'project_overview', 'r', 'old-review', 'approval', 'u1', 'pending', 'policy', '', '', NULL)")
        old = {"id": "r", "project": "P1", "label": "critical", "submitter": "owner",
               "submitter_role": "editor", "status": "pending", "review_log": [],
               "workflow_assignment": {"current_task_key": "old-review", "status": "pending"},
               "payload": {"chip": {"id": "chip-original", "type": "text", "content": "旧内容"},
                           "staged_file_path": ""}}
        await self.db.set_item(add.OVERVIEW_ADD_REQUESTS_KEY, {"r": old})

        def workflow(proxy, **_kwargs):
            proxy.replace_work_assignments(module="project_overview", entity_id="r", task_key="new-review",
                assignee_usernames=["reviewer"], source_policy_code="policy")
            return {"status": "matched", "assignment": {"current_task_key": "new-review", "status": "pending"}}

        fake_app = SimpleNamespace(storage=SimpleNamespace(general={
            "over_config_data_flat": {"critical": {"label": "critical"}},
            "project_summary": {"P1": {"state": "研发"}},
        }))
        args = {"request_id": "r", "submitter": "owner",
                "chip": {"id": "new-id", "type": "text", "content": "新内容"},
                "config": {"label": "critical", "add_approval_required": True},
                "expected": copy.deepcopy(old), "user_service": SimpleNamespace()}
        with patch.object(add, "app", fake_app), \
             patch.object(add, "create_approval_sequence_assignments", side_effect=workflow), \
             patch.object(add, "can_edit_overview_item", return_value=True):
            with patch.object(ApprovalTransaction, "flush", new=AsyncMock(side_effect=OSError("todo failed"))):
                failed = await add.resubmit_overview_add_request(**args)
            self.assertEqual(failed.status, "error")
            self.assertEqual(await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY), {"r": old})
            with closing(sqlite3.connect(self.root / "test.db")) as connection:
                self.assertEqual(connection.execute("SELECT task_key, status FROM work_assignments").fetchall(),
                                 [("old-review", "pending")])

            saved = await add.resubmit_overview_add_request(**args)
            self.assertEqual(saved.status, "pending")
            latest = (await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY))["r"]
            self.assertEqual(latest["payload"]["chip"]["content"], "新内容")
            self.assertEqual(latest["payload"]["chip"]["id"], "chip-original")
            with closing(sqlite3.connect(self.root / "test.db")) as connection:
                self.assertEqual(connection.execute(
                    "SELECT task_key, status FROM work_assignments ORDER BY task_key").fetchall(),
                    [("new-review", "pending"), ("old-review", "superseded")])
            stale_edit = await add.resubmit_overview_add_request(**args)
            self.assertEqual(stale_edit.status, "error")
            with patch.object(add, "can_review_overview_add_request", return_value=True):
                stale_review, _ = await add._advance_add_review(
                    "r", "reviewer", SimpleNamespace(), expected=old)
            self.assertIsNone(stale_review)

    async def test_withdraw_does_not_remove_claimed_request(self):
        original = {"status": "pending", "submitter": "owner"}
        await self.db.set_item("requests", {"r": {**original, "status": "processing"}})
        self.assertFalse(await requests.withdraw_request(self.db, "requests", "r", "owner", original))
        self.assertEqual(self.db.get_item("requests")["r"]["status"], "processing")

    async def test_recovery_and_reconciliation_never_replay_business(self):
        await self.db.set_item("requests", {"r": {"status": "processing"},
            "done": {"status": "processing", "completion_checkpoint": {"status": "approved"}},
            "partial": {"status": "processing", "completion_checkpoint": {"status": "interrupted"}}})
        self.assertTrue(await requests.recover_processing(self.db, "requests"))
        self.assertEqual(self.db.get_item("requests")["done"]["status"], "approved")
        self.assertFalse(await requests.reconcile_interrupted(self.db, "requests", "r", "owner", "", lambda _: True))
        self.assertTrue(await requests.reconcile_interrupted(self.db, "requests", "partial", "owner", "核对后另行处理", lambda _: True))
        self.assertEqual(self.db.get_item("requests")["partial"]["status"], "reconciled")

    async def test_key_review_sql_failure_does_not_advance_request(self):
        request = {"status": "pending", "workflow_assignment": {"current_task_key": "review"}}
        await self.db.set_item(add.OVERVIEW_ADD_REQUESTS_KEY, {"r": request})
        from src.approval_transaction import ApprovalTransaction
        with patch.object(add, "can_review_overview_add_request", return_value=True), \
             patch.object(ApprovalTransaction, "flush", new=AsyncMock(side_effect=OSError("assignment save failed"))):
            result, _ = await add._advance_add_review("r", "reviewer", SimpleNamespace(), reject_reason="reason")
        self.assertIsNone(result)
        self.assertEqual(await self.db.get_fresh_item(add.OVERVIEW_ADD_REQUESTS_KEY), {"r": request})

    async def test_key_add_save_failure_never_reports_success_or_reexecutes(self):
        for writes in ([False], [True, False]):
            with self.subTest(writes=writes):
                execute = AsyncMock(return_value=(True, "done"))
                with patch.object(add, "_advance_add_review", new=AsyncMock(return_value=({"id": "r"}, "completed"))), \
                     patch.object(add, "_execute_approved_add", new=execute), \
                     patch.object(add, "_update_request", new=AsyncMock(side_effect=writes)):
                    result = await add.approve_overview_add_request("r", "reviewer", user_service=SimpleNamespace())
                self.assertEqual(result["status"], "error")
                execute.assert_awaited_once()

    async def test_state_snapshot_rejects_changed_content_and_new_version(self):
        original = {"id": "c", "content": "original", "select_activ_dic": {"1.0": None}}
        fake = SimpleNamespace(storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}}))
        with patch.object(batch, "app", fake):
            changed = {**original, "content": "new content"}
            await self.db.set_item("P1_over_data", {"docs": {"c": changed}})
            ok, _, _ = await batch.update_overview_chip_state("P1", "docs", "c", "1.0", True, "owner", expected_snapshot=original)
            self.assertFalse(ok)
            await self.db.set_item("P1_over_data", {"docs": {"c": original}})
            fake.storage.general["project_req_max_ver"]["P1"] = "2.0"
            ok, _, _ = await batch.update_overview_chip_state("P1", "docs", "c", "1.0", True, "owner", expected_snapshot=original)
            self.assertFalse(ok)
        self.assertEqual(self.db.get_item("P1_over_data")["docs"]["c"], original)

    async def test_correction_result_and_overview_commit_together(self):
        before = {"id": "c", "type": "text", "content": "before"}
        config = {"label": "docs"}
        request = {"id": "r", "status": "processing", "project": "P1", "label": "docs", "chip_id": "c", "action": "correct",
                   "payload": {"before_snapshot": before, "before_fingerprint": corrections.chip_snapshot_fingerprint(before),
                               "after_snapshot": {**before, "content": "after"}, "config": config}}
        fake = SimpleNamespace(storage=SimpleNamespace(general={"over_config_data_flat": {"docs": config}}))
        await self.db.set_item("P1_over_data", {"docs": {"c": before}})
        await self.db.set_item(corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, {"r": request})
        with patch.object(corrections, "app", fake), patch.object(corrections, "can_edit_overview_item", return_value=True), \
             patch.object(corrections, "can_review_correction_request", return_value=True), \
             patch("src.components.OverviewVersionManager.bump"):
            with patch.object(requests, "write_json", new=AsyncMock(side_effect=OSError("result save failed"))):
                result = await corrections.execute_correction_request(request, reviewer="reviewer")
            self.assertFalse(result["ok"])
            self.assertEqual(await self.db.get_fresh_item("P1_over_data"), {"docs": {"c": before}})
            self.assertEqual(self.db.get_item(corrections.OVERVIEW_CORRECTION_REQUESTS_KEY)["r"]["status"], "processing")
            result = await corrections.execute_correction_request(request, reviewer="reviewer")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.db.get_item("P1_over_data")["docs"]["c"]["content"], "after")
        self.assertEqual(self.db.get_item(corrections.OVERVIEW_CORRECTION_REQUESTS_KEY)["r"]["status"], "approved")

    async def test_child_insert_checks_anchor_inside_write(self):
        config = {"label": "child", "is_table_group": True, "first_col_label": "first"}
        await self.db.set_item("P1_over_data", {"first": {}})
        inserted, _ = await batch.insert_overview_chip("P1", "child", {"id": "c", "row_id": "gone", "content": "value"},
                                                       config=config, req_max_ver="1.0")
        self.assertFalse(inserted)
        self.assertNotIn("child", self.db.get_item("P1_over_data"))

    async def test_same_second_related_reasons_are_not_overwritten(self):
        chip = {"id": "c", "creator": "owner", "select_activ_dic": {"1.0": True}}
        await self.db.set_item("P1_over_data", {"docs": {"c": chip}})
        with patch.object(batch, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 18, 12)
            for source in ("first", "second"):
                await batch.apply_related_overview_impacts(project="P1", related_labels=["docs"], source_content=source,
                    source_state=True, operation_type="add_chip", creator="owner", config_flat={}, overview_role={})
        records = self.db.get_item("P1_over_related_record")["docs"]["c"]["open"]["record"]
        self.assertEqual({entry["operate_chip_content"] for entry in records.values()}, {"first", "second"})

    async def test_batch_state_without_reviewed_snapshot_cannot_change_latest_version(self):
        config = {"label": "docs", "role": "role"}
        before = {"id": "c", "content": "changed", "select_activ_dic": {"1.0": None, "2.0": None}}
        await self.db.set_item("P1_over_data", {"docs": {"c": before}})
        fake = SimpleNamespace(storage=SimpleNamespace(general={"over_config_data_flat": {"docs": config},
            "project_summary": {"P1": {"state": next(iter(batch.BATCH_OVERVIEW_ALLOWED_PROJECT_STATES))}},
            "project_req_max_ver": {"P1": "2.0"}}))
        request = {"submitter": "owner", "payload": {"action": "state", "config": config, "projects": ["P1"],
            "chip_targets": [{"project": "P1", "chip_id": "c"}], "target_state": True}}
        with patch.object(batch, "app", fake), patch.object(batch, "can_edit_overview_item", return_value=True), patch.object(batch, "_publish_batch_overview_changes"):
            result = await batch.execute_batch_overview_request(copy.deepcopy(request))
        self.assertFalse(result["ok"])
        self.assertEqual(self.db.get_item("P1_over_data")["docs"]["c"], before)
