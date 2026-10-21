import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import overview_storage
from src.ecn_management_config import ECN_EXECUTION_RESULT_SUCCESS, ECN_OVERVIEW_ACTION_UPDATE
from src.ecn_management_config import ECNState, ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
from src.modules.ecn import overview_execution
from src.pages.ecn_management import (
    append_ecn_approval_log_once,
    build_overview_activation_state,
    deactivate_overview_chip_for_ecn,
)
from tests.test_error_management_concurrency import load_isolated_db_storage


class ECNManagementRecordTests(unittest.TestCase):
    def test_ecn_lock_message_uses_configured_chinese_overview_title(self):
        self.assertEqual(
            overview_storage._format_ecn_lock_message(
                {"ecn_id": "ECN26101902", "project": "RFFM-1009-A", "label": "light_source"}
            ),
            "概述已被ECN方案占用，普通通道不能修改："
            "ECN26101902 / RFFM-1009-A / 光源选型",
        )

    def test_adjacent_identical_approval_log_is_not_appended_twice(self):
        entry = {
            "user": "张三",
            "role": "研发结构",
            "action": "发起申请",
            "time": "2026-08-14 18:13:45",
        }
        approval_log = [entry.copy()]

        self.assertFalse(append_ecn_approval_log_once(approval_log, entry))
        self.assertEqual(approval_log, [entry])

        next_entry = {**entry, "action": "撤回修改"}
        self.assertTrue(append_ecn_approval_log_once(approval_log, next_entry))
        self.assertEqual(len(approval_log), 2)

    def test_ecn_deactivation_preserves_recorder_and_adds_auditable_history(self):
        original = {
            "creator": "原操作人",
            "req_ver": "1.0",
            "notes": "原录入注释",
            "enabled": True,
            "select_activ_dic": {"1.0": True},
            "timestamp": {
                "2026-08-13 10:00:00": {
                    "creator": "原操作人",
                    "select_activ_dic": {"1.0": True},
                }
            },
        }
        operation_time = "2026-08-14 18:13:45"

        result = deactivate_overview_chip_for_ecn(
            original,
            "2.0",
            "ECN26081401",
            operation_time,
            "方案提供人",
            "ECN失效",
        )

        self.assertEqual(result["creator"], "原操作人")
        self.assertEqual(result["timestamp"][operation_time]["creator"], "方案提供人")
        self.assertEqual(result["timestamp"][operation_time]["reason"], "ECN失效")
        self.assertEqual(result["timestamp"][operation_time]["source_id"], "ECN26081401")
        self.assertEqual(result["select_activ_dic"], {"1.0": True, "2.0": False})
        self.assertEqual(result["req_ver"], "1.0")
        self.assertEqual(result["notes"], "原录入注释")
        self.assertEqual(original["creator"], "原操作人")
        self.assertNotIn("2.0", original["select_activ_dic"])

    def test_no_requirement_project_uses_version_zero_entry_node(self):
        req_ver, activations = build_overview_activation_state(None)

        self.assertEqual(req_ver, "0.0")
        self.assertEqual(activations, {"0.0": True})


class ECNOverviewExecutionTests(unittest.IsolatedAsyncioTestCase):
    def child_record(self):
        return {
            "ecn_id": "ECN26101703", "execution_info": {"overview_results": {}},
            "change_items": [{
                "item_id": "child", "type": "overview_update", "scheme_category": "overview_document",
                "author": "方案人", "role": "研发", "label": "子参数", "first_col_label": "基准参数",
                "config_processing_type": "text", "new_data": {"content": "子内容"},
                "projects": ["P1"], "req_idxs": [], "linked_docs": [], "linked_materials": [],
                "project_states": {"P1": {"action": "add", "anchor_row_id": "row-1"}},
            }],
        }

    async def prepare_execution(self, record):
        record["workflow"] = {"current_state": ECNState.ECN_EXECUTING}
        record.setdefault("execution_info", {}).update(stage=ECN_EXECUTION_STAGE_OVERVIEW_RUNNING, overview_run_id="test-run")
        await self.storage.set_item("ecn_management_data", {record["ecn_id"]: record})

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = load_isolated_db_storage(
            "ecn_overview_atomic",
            Path(self.temp.name) / "overview.db",
        )
        await self.storage.init_db()

    async def asyncTearDown(self):
        await self.storage.close_db()
        self.temp.cleanup()

    async def test_checkpoint_gap_locks_generated_results_and_rejects_changed_retry(self):
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}}))

        async def fail_checkpoint(*args):
            raise RuntimeError("simulated checkpoint failure")

        for action in ("add", "update", "deactivate"):
            with self.subTest(action=action):
                value = self.child_record()
                item = value["change_items"][0]
                item["label"] = "基准参数"
                item["project_states"]["P1"] = {"action": action, "chip_id": "old"}
                await self.prepare_execution(value)
                original = {"基准参数": {"old": {
                    "id": "old", "content": "original", "row_id": "row-1", "enabled": True,
                    "select_activ_dic": {"1.0": True}, "timestamp": {},
                }}}
                await self.storage.set_item("P1_over_data", original)
                retry = copy.deepcopy(value)
                with patch.object(overview_execution, "db_storage", self.storage), patch.object(overview_execution, "app", fake_app):
                    with self.assertRaisesRegex(RuntimeError, "checkpoint failure"):
                        await overview_execution.execute_ecn_overview_schemes(value, "2026-10-17 12:00:00", progress_callback=fail_checkpoint)
                    written = await self.storage.get_fresh_item("P1_over_data", {})
                    chip_id = "old" if action == "deactivate" else next(key for key in written["基准参数"] if key != "old")
                    changed = {**written["基准参数"][chip_id], "content": "unreviewed change"}
                    # 必须通过真实普通写入口验证，不能只断言锁集合。
                    with self.assertRaisesRegex(overview_storage.OverviewWriteError, "已被ECN方案占用"):
                        await overview_storage.set_deep_item(["P1_over_data", "基准参数", chip_id], changed, storage=self.storage)
                    with self.assertRaises(overview_storage.OverviewWriteError):
                        await overview_storage.del_deep_item(["P1_over_data", "基准参数", chip_id], storage=self.storage)
                    # 直接改隔离数据库模拟绕过业务门禁；来源标记仍在，不能因此误判成功。
                    damaged = copy.deepcopy(written)
                    damaged["基准参数"][chip_id] = changed
                    await self.storage.set_item("P1_over_data", damaged)
                    failed = await overview_execution.execute_ecn_overview_schemes(copy.deepcopy(retry), "2026-10-17 12:10:00")
                    self.assertEqual(failed["child"]["status"], "failed")
                    self.assertIn("内容或状态已变化", failed["child"]["projects"]["P1"]["message"])
                    self.assertEqual(await self.storage.get_fresh_item("P1_over_data", {}), damaged)
                    if action == "update":
                        damaged_old = copy.deepcopy(written)
                        damaged_old["基准参数"]["old"]["enabled"] = True
                        await self.storage.set_item("P1_over_data", damaged_old)
                        failed_old = await overview_execution.execute_ecn_overview_schemes(copy.deepcopy(retry), "2026-10-17 12:11:00")
                        self.assertEqual(failed_old["child"]["status"], "failed")
                    await self.storage.set_item("P1_over_data", written)
                    succeeded = await overview_execution.execute_ecn_overview_schemes(copy.deepcopy(retry), "2026-10-17 12:12:00")
                    self.assertEqual(succeeded["child"]["status"], "success")
                    self.assertEqual(await self.storage.get_fresh_item("P1_over_data", {}), written)
                # 进度确认完成后解除临时保护，不永久锁住正式新节点。
                retry["change_items"][0]["execute_status"] = "success"
                await self.storage.set_item("ecn_management_data", {retry["ecn_id"]: retry})
                self.assertTrue(await overview_storage.set_deep_item(["P1_over_data", "基准参数", chip_id], changed, storage=self.storage))

    async def test_existing_anchor_is_locked_until_dependent_scheme_finishes(self):
        value = self.child_record()
        await self.prepare_execution(value)
        base = {"id": "base", "row_id": "row-1", "content": "基准", "select_activ_dic": {"1.0": True}}
        await self.storage.set_item("P1_over_data", {"基准参数": {"base": base}})
        with self.assertRaisesRegex(overview_storage.OverviewWriteError, "ECN方案占用"):
            await overview_storage.del_deep_item(["P1_over_data", "基准参数", "base"], storage=self.storage)
        for changed in ({**base, "content": "改动"}, {**base, "select_activ_dic": {"1.0": False}}):
            with self.assertRaises(overview_storage.OverviewWriteError):
                await overview_storage.set_deep_item(["P1_over_data", "基准参数", "base"], changed, storage=self.storage)
        value["change_items"][0]["execute_status"] = "success"
        await self.storage.set_item("ecn_management_data", {value["ecn_id"]: value})
        self.assertTrue(await overview_storage.del_deep_item(["P1_over_data", "基准参数", "base"], storage=self.storage))

    async def test_execution_rejects_missing_or_inactive_existing_anchor(self):
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}}))
        for active in (None, False, True):
            with self.subTest(active=active):
                value = self.child_record()
                await self.prepare_execution(value)
                base = {} if active is None else {"base": {"row_id": "row-1", "select_activ_dic": {"1.0": active}}}
                await self.storage.set_item("P1_over_data", {"基准参数": base})
                with patch.object(overview_execution, "db_storage", self.storage), patch.object(overview_execution, "app", fake_app):
                    results = await overview_execution.execute_ecn_overview_schemes(value, "2026-10-17 12:00:00")
                self.assertEqual(results["child"]["status"], "success" if active else "failed")
                stored = await self.storage.get_fresh_item("P1_over_data", {})
                self.assertEqual(bool(stored.get("子参数")), bool(active))

    async def test_stale_execution_cannot_write_after_deletion_or_batch_change(self):
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}}))
        for change in ("deleted", "run", "stage", "scheme"):
            with self.subTest(change=change):
                value = self.child_record()
                value["change_items"][0].update(label="基准参数")
                await self.prepare_execution(value)
                snapshot = copy.deepcopy(value)
                if change == "run":
                    value["execution_info"]["overview_run_id"] = "new-run"
                elif change == "stage":
                    value["execution_info"]["stage"] = "failed"
                elif change == "scheme":
                    value["change_items"][0]["new_data"]["content"] = "新版本方案"
                await self.storage.set_item("ecn_management_data", {} if change == "deleted" else {value["ecn_id"]: value})
                await self.storage.set_item("P1_over_data", {})
                with patch.object(overview_execution, "db_storage", self.storage), patch.object(overview_execution, "app", fake_app):
                    results = await overview_execution.execute_ecn_overview_schemes(snapshot, "2026-10-17 12:00:00")
                self.assertEqual(results["child"]["status"], "failed")
                self.assertEqual(await self.storage.get_fresh_item("P1_over_data", {}), {})

    async def test_replace_is_atomic_and_retry_is_idempotent(self):
        old_chip = {
            "id": "old-chip",
            "row_id": "row-1",
            "creator": "原录入人",
            "enabled": True,
            "select_activ_dic": {"1.0": True},
            "timestamp": {},
        }
        await self.storage.set_item("P1_over_data", {"参数A": {"old-chip": old_chip}})
        record = {
            "ecn_id": "ECN26101601",
            "execution_info": {"overview_results": {}},
            "change_items": [
                {
                    "item_id": "scheme-1",
                    "type": "overview_update",
                    "author": "方案人",
                    "role": "研发",
                    "label": "参数A",
                    "first_col_label": "基础型号",
                    "config_processing_type": "text",
                    "new_data": {"content": "新内容"},
                    "project_states": {
                        "P1": {"action": ECN_OVERVIEW_ACTION_UPDATE, "chip_id": "old-chip"}
                    },
                }
            ],
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}})
        )
        await self.prepare_execution(record)
        retry_without_checkpoint = copy.deepcopy(record)

        with patch.object(overview_execution, "db_storage", self.storage), patch.object(
            overview_execution,
            "app",
            fake_app,
        ):
            first = await overview_execution.execute_ecn_overview_schemes(
                record,
                "2026-10-16 10:00:00",
            )
            second = await overview_execution.execute_ecn_overview_schemes(
                retry_without_checkpoint,
                "2026-10-16 10:01:00",
            )

        self.assertEqual(first["scheme-1"]["status"], ECN_EXECUTION_RESULT_SUCCESS)
        self.assertEqual(second["scheme-1"]["status"], ECN_EXECUTION_RESULT_SUCCESS)
        stored = await self.storage.get_fresh_item("P1_over_data", {})
        chips = stored["参数A"]
        self.assertEqual(len(chips), 2)
        self.assertFalse(chips["old-chip"]["enabled"])
        source_entries = [
            entry
            for entry in chips["old-chip"]["timestamp"].values()
            if entry.get("source_id") == record["ecn_id"]
        ]
        self.assertEqual(len(source_entries), 1)

    async def test_unassociated_overview_scheme_is_rejected_during_execution(self):
        checkpoints = []

        async def save_checkpoint(item_id, result, status):
            checkpoints.append((item_id, result, status))

        record = {
            "ecn_id": "ECN26101602",
            "execution_info": {"overview_results": {}},
            "change_items": [
                {
                    "item_id": "scheme-unassociated",
                    "type": "overview_update",
                    "project_states": {},
                }
            ],
        }

        results = await overview_execution.execute_ecn_overview_schemes(
            record,
            "2026-10-16 11:00:00",
            progress_callback=save_checkpoint,
        )

        result = results["scheme-unassociated"]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["message"], "方案未关联目标项目，不能执行")
        self.assertEqual(record["change_items"][0]["execute_status"], "failed")
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0][2], "failed")

    async def test_table_child_waits_for_pending_first_column_scheme(self):
        await self.storage.set_item("P1_over_data", {})
        record = {
            "ecn_id": "ECN26101702",
            "execution_info": {"overview_results": {}},
            "change_items": [
                {
                    "item_id": "child",
                    "type": "overview_update",
                    "author": "方案人",
                    "role": "研发",
                    "label": "子参数",
                    "first_col_label": "基准参数",
                    "config_processing_type": "text",
                    "new_data": {"content": "子内容"},
                    "project_states": {
                        "P1": {"action": "add", "anchor_row_id": "PENDING_NEW_base"}
                    },
                },
                {
                    "item_id": "base",
                    "type": "overview_update",
                    "author": "方案人",
                    "role": "研发",
                    "label": "基准参数",
                    "first_col_label": "基准参数",
                    "config_processing_type": "text",
                    "new_data": {"content": "基准内容"},
                    "project_states": {"P1": {"action": "add"}},
                },
            ],
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(general={"project_req_max_ver": {"P1": "1.0"}})
        )
        await self.prepare_execution(record)

        async def check_pending_anchor_protection(item_id, _result, _status):
            if item_id == "base":
                stored = await self.storage.get_fresh_item("P1_over_data", {})
                base_id = next(iter(stored["基准参数"]))
                with self.assertRaisesRegex(overview_storage.OverviewWriteError, "ECN方案占用"):
                    await overview_storage.del_deep_item(["P1_over_data", "基准参数", base_id], storage=self.storage)

        with patch.object(overview_execution, "db_storage", self.storage), patch.object(
            overview_execution,
            "app",
            fake_app,
        ):
            results = await overview_execution.execute_ecn_overview_schemes(
                record,
                "2026-10-17 12:00:00",
                progress_callback=check_pending_anchor_protection,
            )

        self.assertEqual(results["base"]["status"], ECN_EXECUTION_RESULT_SUCCESS)
        self.assertEqual(results["child"]["status"], ECN_EXECUTION_RESULT_SUCCESS)
        stored = await self.storage.get_fresh_item("P1_over_data", {})
        base_chip = next(iter(stored["基准参数"].values()))
        child_chip = next(iter(stored["子参数"].values()))
        self.assertEqual(child_chip["row_id"], base_chip["row_id"])

    async def test_pending_ecn_scheme_locks_overview_from_normal_storage_writes(self):
        chip = {"id": "chip-1", "content": "审核基线", "enabled": True}
        await self.storage.set_item("P1_over_data", {"参数A": {"chip-1": chip}})
        ecn_record = {
            "ecn_id": "ECN26101701",
            "workflow": {"current_state": "ECN 方案评审中"},
            "change_items": [
                {
                    "item_id": "scheme-1",
                    "type": "overview_update",
                    "label": "参数A",
                    "execute_status": "pending",
                    "project_states": {"P1": {"action": "update", "chip_id": "chip-1"}},
                }
            ],
        }
        await self.storage.set_item("ecn_management_data", {ecn_record["ecn_id"]: ecn_record})

        lock_message = await overview_storage.get_ecn_overview_lock_message(
            "P1", "参数A", "chip-1", storage=self.storage
        )
        self.assertEqual(
            lock_message,
            "概述已被ECN方案占用，普通通道不能修改：ECN26101701 / P1 / 参数A",
        )

        def normal_edit(current):
            current["content"] = "普通通道修改"
            return current

        with self.assertRaisesRegex(overview_storage.OverviewWriteError, "已被ECN方案占用"):
            await overview_storage.set_deep_item(
                ["P1_over_data", "参数A", "chip-1"],
                {**chip, "content": "普通通道修改"},
                storage=self.storage,
            )
        stored = await self.storage.get_fresh_item("P1_over_data", {})
        self.assertEqual(stored["参数A"]["chip-1"]["content"], "审核基线")

        self.assertTrue(
            await overview_storage.atomic_deep_update(
                ["P1_over_data", "参数A", "chip-1"],
                normal_edit,
                allow_ecn=True,
                storage=self.storage,
            )
        )
        stored = await self.storage.get_fresh_item("P1_over_data", {})
        self.assertEqual(stored["参数A"]["chip-1"]["content"], "普通通道修改")

    async def test_editor_precheck_includes_table_row_dependencies(self):
        value = self.child_record()
        await self.prepare_execution(value)
        base = {
            "id": "base",
            "row_id": "row-1",
            "content": "基准",
            "select_activ_dic": {"1.0": True},
        }
        await self.storage.set_item("P1_over_data", {"基准参数": {"base": base}})

        message = await overview_storage.get_ecn_overview_lock_message(
            "P1", "基准参数", "base", storage=self.storage
        )

        self.assertEqual(
            message,
            "概述已被ECN方案占用，普通通道不能修改：ECN26101703 / P1 / 基准参数",
        )

    async def test_child_change_locks_target_and_row_anchor_but_not_sibling(self):
        anchor = {"id": "source", "row_id": "row-1", "content": "白光：5050"}
        target = {"id": "driver", "row_id": "row-1", "content": "1.73±8%A"}
        sibling = {"id": "voltage", "row_id": "row-1", "content": "14V@1.73A"}
        await self.storage.set_item(
            "P1_over_data",
            {
                "光源选型": {"source": anchor},
                "光源驱动If": {"driver": target},
                "光源驱动Vf": {"voltage": sibling},
            },
        )
        ecn_record = {
            "ecn_id": "ECN26101902",
            "workflow": {"current_state": ECNState.ECN_SCHEMING},
            "change_items": [
                {
                    "item_id": "driver-change",
                    "type": "overview_update",
                    "label": "光源驱动If",
                    "first_col_label": "光源选型",
                    "execute_status": "pending",
                    "project_states": {
                        "P1": {"action": "update", "chip_id": "driver"}
                    },
                }
            ],
        }
        await self.storage.set_item("ecn_management_data", {ecn_record["ecn_id"]: ecn_record})

        anchor_message = await overview_storage.get_ecn_overview_lock_message(
            "P1", "光源选型", "source", storage=self.storage
        )
        target_message = await overview_storage.get_ecn_overview_lock_message(
            "P1", "光源驱动If", "driver", storage=self.storage
        )
        sibling_message = await overview_storage.get_ecn_overview_lock_message(
            "P1", "光源驱动Vf", "voltage", storage=self.storage
        )

        self.assertIn("ECN26101902 / P1 / 光源驱动If", anchor_message)
        self.assertIn("ECN26101902 / P1 / 光源驱动If", target_message)
        self.assertEqual(sibling_message, "")
        with self.assertRaisesRegex(overview_storage.OverviewWriteError, "ECN方案占用"):
            await overview_storage.set_deep_item(
                ["P1_over_data", "光源选型", "source"],
                {**anchor, "enabled": False},
                storage=self.storage,
            )
        self.assertTrue(
            await overview_storage.set_deep_item(
                ["P1_over_data", "光源驱动Vf", "voltage"],
                {**sibling, "content": "14V@1.70A"},
                storage=self.storage,
            )
        )

    async def test_anchor_change_locks_entire_table_row_only(self):
        anchor = {"id": "source", "row_id": "row-1", "content": "白光：5050"}
        sibling = {"id": "driver", "row_id": "row-1", "content": "1.73±8%A"}
        other_row = {"id": "other", "row_id": "row-2", "content": "0.7A"}
        await self.storage.set_item(
            "P1_over_data",
            {
                "光源选型": {"source": anchor},
                "光源驱动If": {"driver": sibling, "other": other_row},
            },
        )
        ecn_record = {
            "ecn_id": "ECN26101903",
            "workflow": {"current_state": ECNState.ECN_SCHEMING},
            "change_items": [
                {
                    "item_id": "source-change",
                    "type": "overview_update",
                    "label": "光源选型",
                    "first_col_label": "光源选型",
                    "execute_status": "pending",
                    "project_states": {
                        "P1": {"action": "deactivate", "chip_id": "source"}
                    },
                }
            ],
        }
        await self.storage.set_item("ecn_management_data", {ecn_record["ecn_id"]: ecn_record})

        self.assertTrue(
            await overview_storage.get_ecn_overview_lock_message(
                "P1", "光源驱动If", "driver", storage=self.storage
            )
        )
        self.assertEqual(
            await overview_storage.get_ecn_overview_lock_message(
                "P1", "光源驱动If", "other", storage=self.storage
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
