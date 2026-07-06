"""生产异常模块的并发写入回归测试。

这里刻意模拟两个数据库模块实例和多个同时执行的业务动作，用来防止未来维护时重新引入
“后保存的旧快照覆盖先保存的新数据”问题。该文件应长期保留，并在修改原子更新逻辑后运行。
"""

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
    """加载一份拥有独立内存缓存和连接的 db_storage，用于模拟另一个服务进程。"""
    spec = importlib.util.spec_from_file_location(module_name, DB_STORAGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载数据库模块：{DB_STORAGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "DB_PATH", str(db_path))
    return module


class ErrorManagementConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_deep_update_preserves_cross_instance_writes(self):
        """两个实例同时累加同一字段时，80 次更新应全部保留，旁边字段也不能丢失。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cross_instance.db"
            left = load_isolated_db_storage("test_db_storage_left", db_path)
            right = load_isolated_db_storage("test_db_storage_right", db_path)
            await left.init_db()
            await right.init_db()
            try:
                await left.set_item("shared", {"count": 0, "kept": "yes"})

                results = await asyncio.gather(
                    # 左右实例共享同一个 SQLite 文件，但各自拥有缓存，接近多进程部署时的竞争场景。
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
        """验证旧表单不能覆盖后台更新，并发创建相同异常单号时只能有一方成功。"""
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

                    # 模拟负责人修改日期与后台提醒同时更新同一异常单的不同字段。
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
                    # stale_copy 的 _revision 落后于数据库，整单保存必须被乐观锁拒绝。
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
                    # create=True 在事务内检查存在性，避免两个请求都通过页面侧的“未找到”检查。
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

    async def test_preventive_close_request_requires_closure_nature_on_approval(self):
        """责任人申请关闭后，审批通过必须写入措施性质。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_error_close_request_db_storage",
                Path(temp_dir) / "error_close_request.db",
            )
            try:
                await isolated_db.init_db()
                from src.pages import error_management

                original_db_storage = error_management.db_storage
                error_management.db_storage = isolated_db
                try:
                    draft = error_management.generate_initial_error_data("editor-a", "admin")
                    draft["error_id"] = "ERR-CLOSE"
                    draft["basic_info"]["product_name"] = "close-product"
                    draft["descriptions"] = [{"id": "desc-1", "content": "issue", "speaker": "editor-a"}]
                    draft["preventive_actions"] = [
                        {
                            "id": "action-1",
                            "content": "update fixture",
                            "owner": "owner-a",
                            "due_date": "2026-07-20",
                            "status": "待执行",
                            "extension_requests": [],
                        }
                    ]

                    created = await error_management.save_error_record(draft, "editor-a", "admin", is_new=True)
                    self.assertTrue(created.changed)

                    missing_note = await error_management.submit_error_preventive_close_request(
                        "ERR-CLOSE",
                        "action-1",
                        "owner-a",
                        "测试工程师",
                        "",
                    )
                    self.assertEqual(missing_note.code, "missing_close_note")

                    requested = await error_management.submit_error_preventive_close_request(
                        "ERR-CLOSE",
                        "action-1",
                        "owner-a",
                        "测试工程师",
                        "验证完成",
                    )
                    self.assertTrue(requested.changed)
                    assert requested.record is not None
                    action = error_management.find_preventive_action(requested.record, "action-1")
                    assert action is not None
                    close_request = error_management.get_pending_close_request(action)
                    self.assertIsNotNone(close_request)
                    self.assertEqual(action["status"], "待执行")
                    self.assertEqual(
                        error_management.get_error_dashboard_pending_count(
                            {"ERR-CLOSE": requested.record},
                            "manager",
                            "研发经理",
                        ),
                        1,
                    )

                    assert close_request is not None
                    missing_nature = await error_management.approve_error_preventive_close_request(
                        "ERR-CLOSE",
                        "action-1",
                        close_request["id"],
                        True,
                        "manager",
                        "研发经理",
                    )
                    self.assertEqual(missing_nature.code, "missing_closure_nature")

                    approved = await error_management.approve_error_preventive_close_request(
                        "ERR-CLOSE",
                        "action-1",
                        close_request["id"],
                        True,
                        "manager",
                        "研发经理",
                        "设计问题",
                    )
                    self.assertTrue(approved.changed)
                    assert approved.record is not None
                    action = error_management.find_preventive_action(approved.record, "action-1")
                    assert action is not None
                    self.assertEqual(action["status"], "已关闭")
                    self.assertEqual(action["close_note"], "验证完成")
                    self.assertEqual(action["closed_by"], "manager")
                    self.assertEqual(action["closure_nature"], "设计问题")
                    self.assertEqual(
                        error_management.get_error_closure_nature_options({"ERR-CLOSE": approved.record}),
                        ["设计问题"],
                    )
                finally:
                    error_management.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

    async def test_admin_delete_preserves_concurrent_record_and_rejects_other_roles(self):
        """admin 删除单张异常单时应保留其它实例的并发新增，非 admin 不能删除。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "error_management_delete.db"
            admin_instance = load_isolated_db_storage("test_error_delete_admin", db_path)
            other_instance = load_isolated_db_storage("test_error_delete_other", db_path)
            await admin_instance.init_db()
            await other_instance.init_db()
            try:
                from src.pages import error_management

                await admin_instance.set_item(
                    error_management.ERROR_DATA_KEY,
                    {
                        "DELETE-ME": {"error_id": "DELETE-ME"},
                        "KEEP-ME": {"error_id": "KEEP-ME"},
                    },
                )
                original_db_storage = error_management.db_storage
                error_management.db_storage = admin_instance
                try:
                    forbidden = await error_management.delete_error_record("DELETE-ME", "研发经理")
                    self.assertEqual(forbidden.code, "forbidden")

                    deleted, added = await asyncio.gather(
                        error_management.delete_error_record("DELETE-ME", "admin"),
                        other_instance.atomic_deep_update(
                            [error_management.ERROR_DATA_KEY, "ADDED-CONCURRENTLY"],
                            lambda _: {"error_id": "ADDED-CONCURRENTLY"},
                        ),
                    )
                    self.assertTrue(deleted.changed)
                    self.assertEqual(deleted.code, "deleted")
                    self.assertTrue(added)

                    stored = other_instance.get_item(error_management.ERROR_DATA_KEY, {})
                    self.assertNotIn("DELETE-ME", stored)
                    self.assertIn("KEEP-ME", stored)
                    self.assertIn("ADDED-CONCURRENTLY", stored)
                finally:
                    error_management.db_storage = original_db_storage
            finally:
                await admin_instance.close_db()
                await other_instance.close_db()


if __name__ == "__main__":
    unittest.main()
