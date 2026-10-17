import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import overview_storage
from src.ecn_management_config import ECN_EXECUTION_RESULT_SUCCESS, ECN_OVERVIEW_ACTION_UPDATE
from src.modules.ecn import overview_execution
from src.pages.ecn_management import (
    append_ecn_approval_log_once,
    build_overview_activation_state,
    deactivate_overview_chip_for_ecn,
)
from tests.test_error_management_concurrency import load_isolated_db_storage


class ECNManagementRecordTests(unittest.TestCase):
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
        with patch.object(overview_execution, "db_storage", self.storage), patch.object(
            overview_execution,
            "app",
            fake_app,
        ):
            results = await overview_execution.execute_ecn_overview_schemes(
                record,
                "2026-10-17 12:00:00",
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

if __name__ == "__main__":
    unittest.main()
