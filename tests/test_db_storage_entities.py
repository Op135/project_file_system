# -*- encoding: utf-8 -*-
"""逐实体JSON存储的迁移、原子更新与批量写入测试。"""

import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DB_STORAGE_PATH = ROOT_DIR / "src" / "db_storage.py"


def load_isolated_db_storage(module_name: str, db_path: Path) -> Any:
    """加载独立数据库模块，避免测试污染正式连接和缓存。"""
    spec = importlib.util.spec_from_file_location(module_name, DB_STORAGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载数据库模块：{DB_STORAGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "DB_PATH", str(db_path))
    return module


class JsonEntityStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_deep_reads_copy_only_the_requested_branch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_deep_read_copy",
                Path(temp_dir) / "deep-read.db",
            )
            await storage.init_db()
            try:
                await storage.set_item(
                    "overview",
                    {
                        "large_unrelated_branch": [
                            {"index": index, "payload": "x" * 100}
                            for index in range(1000)
                        ],
                        "target": {"status": "进行中"},
                    },
                )
                target = storage.get_deep_item(["overview", "target"])
                target["status"] = "已修改返回值"
                self.assertEqual(
                    storage.get_deep_item(["overview", "target", "status"]),
                    "进行中",
                )
                self.assertIs(
                    storage.get_deep_item(["overview", "target"], return_ref=True),
                    storage.get_item("overview", return_ref=True)["target"],
                )
            finally:
                await storage.close_db()

    async def test_cross_instance_deep_set_and_delete_use_latest_database_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "deep-concurrency.db"
            left = load_isolated_db_storage("test_deep_left", db_path)
            right = load_isolated_db_storage("test_deep_right", db_path)
            await left.init_db()
            await left.set_item(
                "overview",
                {"left": {"value": 0}, "right": {"value": 0}, "obsolete": True},
            )
            await right.init_db()
            try:
                await left.set_deep_item(["overview", "left", "value"], 1)
                await right.set_deep_item(["overview", "right", "value"], 2)
                await left.set_deep_item(["overview", "created_later"], "保留")
                self.assertTrue(await right.del_deep_item(["overview", "obsolete"]))

                connection = sqlite3.connect(db_path)
                try:
                    stored_json = connection.execute(
                        "SELECT value FROM general_storage WHERE key = ?",
                        ("overview",),
                    ).fetchone()[0]
                finally:
                    connection.close()
                stored = json.loads(stored_json)
                self.assertEqual(stored["left"]["value"], 1)
                self.assertEqual(stored["right"]["value"], 2)
                self.assertEqual(stored["created_later"], "保留")
                self.assertNotIn("obsolete", stored)
            finally:
                await left.close_db()
                await right.close_db()

    async def test_resource_locks_are_reused_only_for_the_same_resource(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_resource_locks",
                Path(temp_dir) / "locks.db",
            )
            same_left = storage._get_resource_lock("general:module-a")
            same_right = storage._get_resource_lock("general:module-a")
            different = storage._get_resource_lock("general:module-b")
            self.assertIs(same_left, same_right)
            self.assertIsNot(same_left, different)

    async def test_legacy_dictionary_is_migrated_once_and_kept_as_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_json_entity_migration",
                Path(temp_dir) / "migration.db",
            )
            await storage.init_db()
            try:
                legacy = {
                    "record-1": {"record_id": "record-1", "value": 1},
                    "record-2": {"record_id": "record-2", "value": 2},
                }
                await storage.set_item("legacy_records", legacy)
                migrated = await storage.migrate_json_dict_to_entities(
                    "sample_orders",
                    "legacy_records",
                )
                self.assertEqual(migrated, 2)
                self.assertEqual(storage.get_json_entities("sample_orders"), legacy)
                self.assertEqual(storage.get_item("legacy_records"), legacy)

                await storage.set_item(
                    "legacy_records",
                    {**legacy, "record-3": {"record_id": "record-3", "value": 3}},
                )
                self.assertEqual(
                    await storage.migrate_json_dict_to_entities("sample_orders", "legacy_records"),
                    0,
                )
                self.assertNotIn("record-3", storage.get_json_entities("sample_orders"))
            finally:
                await storage.close_db()

    async def test_single_entity_update_and_delete_do_not_rewrite_other_entities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_json_entity_update",
                Path(temp_dir) / "update.db",
            )
            await storage.init_db()
            try:
                self.assertTrue(
                    await storage.insert_json_entities(
                        "sample_orders",
                        {
                            "record-1": {"record_id": "record-1", "revision": 1},
                            "record-2": {"record_id": "record-2", "revision": 1},
                        },
                    )
                )

                def increment(current):
                    current["revision"] += 1
                    return current

                self.assertTrue(
                    await storage.atomic_json_entity_update(
                        "sample_orders",
                        "record-1",
                        increment,
                    )
                )
                self.assertEqual(storage.get_json_entity("sample_orders", "record-1")["revision"], 2)
                self.assertEqual(storage.get_json_entity("sample_orders", "record-2")["revision"], 1)

                self.assertTrue(
                    await storage.atomic_json_entity_update(
                        "sample_orders",
                        "record-1",
                        lambda _current: storage.ATOMIC_DELETE,
                    )
                )
                self.assertIsNone(storage.get_json_entity("sample_orders", "record-1"))
                self.assertIsNotNone(storage.get_json_entity("sample_orders", "record-2"))
            finally:
                await storage.close_db()

    async def test_cross_instance_updates_preserve_every_revision_increment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "concurrency.db"
            left = load_isolated_db_storage("test_json_entity_left", db_path)
            right = load_isolated_db_storage("test_json_entity_right", db_path)
            await left.init_db()
            await right.init_db()
            try:
                await left.insert_json_entities(
                    "sample_orders",
                    {"record-1": {"record_id": "record-1", "revision": 0}},
                )

                def increment(current):
                    current["revision"] += 1
                    return current

                results = await asyncio.gather(
                    *[
                        (left if index % 2 == 0 else right).atomic_json_entity_update(
                            "sample_orders",
                            "record-1",
                            increment,
                        )
                        for index in range(40)
                    ]
                )
                self.assertTrue(all(results))
                connection = sqlite3.connect(db_path)
                try:
                    stored_json = connection.execute(
                        "SELECT value FROM json_entity_storage WHERE namespace = ? AND entity_id = ?",
                        ("sample_orders", "record-1"),
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(json.loads(stored_json)["revision"], 40)

                await left.set_item("sample_order_version", 123.0)
                self.assertEqual(await right.get_fresh_item("sample_order_version", 0), 123.0)
                self.assertEqual(await right.refresh_json_entities("sample_orders"), 1)
                self.assertEqual(
                    right.get_json_entity("sample_orders", "record-1")["revision"],
                    40,
                )
            finally:
                await left.close_db()
                await right.close_db()
