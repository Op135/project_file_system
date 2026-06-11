import asyncio
import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DB_STORAGE_PATH = ROOT_DIR / "src" / "db_storage.py"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def load_isolated_db_storage(module_name: str, db_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, DB_STORAGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载数据库模块：{DB_STORAGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "DB_PATH", str(db_path))
    return module


class ErrorManagementConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_deep_update_preserves_cross_instance_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cross_instance.db"
            left = load_isolated_db_storage("test_db_storage_left", db_path)
            right = load_isolated_db_storage("test_db_storage_right", db_path)
            await left.init_db()
            await right.init_db()
            try:
                await left.set_item("shared", {"count": 0, "kept": "yes"})

                results = await asyncio.gather(
                    *[
                        (left if index % 2 else right).atomic_deep_update(
                            ["shared", "count"],
                            lambda value: (value or 0) + 1,
                        )
                        for index in range(80)
                    ]
                )
                self.assertTrue(all(results))

                await right.atomic_deep_update(
                    ["shared", "missing"],
                    lambda _: right.ATOMIC_NO_UPDATE,
                )
                connection = sqlite3.connect(db_path)
                try:
                    value_json = connection.execute(
                        "SELECT value FROM general_storage WHERE key = 'shared'"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(json.loads(value_json), {"count": 80, "kept": "yes"})
            finally:
                await left.close_db()
                await right.close_db()

    async def test_stale_record_save_and_duplicate_create_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_error_management_db_storage",
                Path(temp_dir) / "error_management.db",
            )
            try:
                await isolated_db.init_db()
                from src.pages import error_management

                original_db_storage = error_management.db_storage
                error_management.db_storage = isolated_db
                try:
                    missing = await error_management.atomic_error_update(
                        "DOES-NOT-EXIST",
                        lambda record: ("updated", record),
                    )
                    self.assertEqual(missing.code, "not_found")
                    self.assertNotIn(
                        "DOES-NOT-EXIST",
                        isolated_db.get_item(error_management.ERROR_DATA_KEY, {}),
                    )

                    draft = error_management.generate_initial_error_data("editor-a", "admin")
                    draft["error_id"] = "ERR-RACE"
                    draft["basic_info"]["product_name"] = "original"
                    draft["descriptions"] = [{"id": "desc-1", "content": "issue", "speaker": "editor-a"}]
                    draft["preventive_actions"] = [
                        {
                            "id": "action-1",
                            "content": "measure",
                            "owner": "owner-a",
                            "due_date": "2026-06-20",
                            "status": "待执行",
                            "extension_requests": [],
                        }
                    ]

                    created = await error_management.save_error_record(draft, "editor-a", "admin", is_new=True)
                    created_record = created.record
                    self.assertIsNotNone(created_record)
                    assert created_record is not None
                    stale_copy = copy.deepcopy(created_record)

                    def change_due_date(record):
                        record["preventive_actions"][0]["due_date"] = "2026-07-01"
                        return "updated", record

                    def add_reminder_marker(record):
                        record["reminder_log"]["marker-1"] = {"state": "sent"}
                        return "updated", record

                    updates = await asyncio.gather(
                        error_management.atomic_error_update("ERR-RACE", change_due_date),
                        error_management.atomic_error_update("ERR-RACE", add_reminder_marker),
                    )
                    self.assertTrue(all(result.changed for result in updates))

                    stale_copy["basic_info"]["product_name"] = "must-not-overwrite"
                    rejected = await error_management.save_error_record(
                        stale_copy,
                        "editor-a",
                        "admin",
                        is_new=False,
                    )
                    self.assertEqual(rejected.code, "revision_conflict")

                    stored = isolated_db.get_deep_item([error_management.ERROR_DATA_KEY, "ERR-RACE"])
                    self.assertEqual(stored["basic_info"]["product_name"], "original")
                    self.assertEqual(stored["preventive_actions"][0]["due_date"], "2026-07-01")
                    self.assertEqual(stored["reminder_log"]["marker-1"]["state"], "sent")

                    duplicate_a = copy.deepcopy(draft)
                    duplicate_b = copy.deepcopy(draft)
                    duplicate_a["error_id"] = duplicate_b["error_id"] = "ERR-DUPLICATE"
                    duplicate_results = await asyncio.gather(
                        error_management.save_error_record(duplicate_a, "editor-a", "admin", is_new=True),
                        error_management.save_error_record(duplicate_b, "editor-b", "admin", is_new=True),
                    )
                    self.assertEqual(
                        sorted(result.code for result in duplicate_results),
                        ["already_exists", "updated"],
                    )
                finally:
                    error_management.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()


if __name__ == "__main__":
    unittest.main()
