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
    async def test_empty_namespace_does_not_restore_deleted_legacy_entities(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "empty-namespace.db"
            first = load_isolated_db_storage("test_empty_namespace_first", db_path)
            await first.init_db()
            try:
                await first.set_item(
                    "legacy_records",
                    {"record-1": {"record_id": "record-1", "value": 1}},
                )
                self.assertEqual(
                    await first.migrate_json_dict_to_entities("sample_orders", "legacy_records"),
                    1,
                )
                self.assertTrue(
                    await first.atomic_json_entity_update(
                        "sample_orders",
                        "record-1",
                        lambda _current: first.ATOMIC_DELETE,
                    )
                )
                self.assertEqual(first.get_json_entities("sample_orders"), {})
            finally:
                await first.close_db()

            second = load_isolated_db_storage("test_empty_namespace_second", db_path)
            await second.init_db()
            try:
                self.assertTrue(second.is_json_entity_namespace_initialized("sample_orders"))
                self.assertEqual(
                    await second.migrate_json_dict_to_entities("sample_orders", "legacy_records"),
                    0,
                )
                self.assertEqual(second.get_json_entities("sample_orders"), {})
            finally:
                await second.close_db()

    async def test_failed_commit_is_rolled_back_before_the_next_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "commit-failure.db"
            storage = load_isolated_db_storage("test_commit_failure", db_path)
            await storage.init_db()
            connection = storage._db
            assert connection is not None
            original_commit = connection.commit
            try:
                async def fail_commit():
                    raise OSError("simulated commit failure")

                connection.commit = fail_commit
                with self.assertRaises(OSError):
                    await storage.set_item("reported_failed", {"value": 1})
                self.assertFalse(connection.in_transaction)
                self.assertNotIn("reported_failed", storage._data_cache)

                connection.commit = original_commit
                await storage.set_item("next_write", {"value": 2})

                sqlite_connection = sqlite3.connect(db_path)
                try:
                    keys = {
                        row[0]
                        for row in sqlite_connection.execute("SELECT key FROM general_storage")
                    }
                finally:
                    sqlite_connection.close()
                self.assertEqual(keys, {"next_write"})
            finally:
                connection.commit = original_commit
                await storage.close_db()

    async def test_cancelled_atomic_update_rolls_back_the_open_transaction(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cancelled-transaction.db"
            storage = load_isolated_db_storage("test_cancelled_transaction", db_path)
            await storage.init_db()
            connection = storage._db
            assert connection is not None
            original_commit = connection.commit
            commit_started = asyncio.Event()
            release_failed_commit = asyncio.Event()

            async def blocked_commit():
                commit_started.set()
                await release_failed_commit.wait()
                raise OSError("simulated cancelled commit failure")

            try:
                connection.commit = blocked_commit
                update_task = asyncio.create_task(
                    storage.atomic_deep_update(["cancelled"], lambda _current: {"value": 1})
                )
                await commit_started.wait()
                update_task.cancel()
                # 安全提交会等待后台提交得出确定结果；让模拟提交失败后再检查回滚状态。
                await asyncio.sleep(0)
                self.assertFalse(update_task.done())
                release_failed_commit.set()
                with self.assertRaises(asyncio.CancelledError):
                    await update_task
                self.assertFalse(connection.in_transaction)

                connection.commit = original_commit
                await storage.set_item("next_write", {"value": 2})
                self.assertEqual(storage.get_item("next_write"), {"value": 2})
                self.assertIsNone(storage.get_item("cancelled"))
            finally:
                release_failed_commit.set()
                connection.commit = original_commit
                await storage.close_db()

    async def test_cancel_after_commit_updates_cache_before_propagating_cancellation(self):
        """提交已经成功时，应先发布缓存，再把任务取消继续抛给调用方。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cancel-after-commit.db"
            storage = load_isolated_db_storage("test_cancel_after_commit", db_path)
            await storage.init_db()
            connection = storage._db
            assert connection is not None
            original_commit = connection.commit
            committed = asyncio.Event()
            allow_commit_return = asyncio.Event()

            async def commit_then_suspend():
                # 先完成真实 SQLite 提交，再制造“协程尚未返回到缓存更新代码”的取消窗口。
                await original_commit()
                committed.set()
                await allow_commit_return.wait()

            try:
                connection.commit = commit_then_suspend
                update_task = asyncio.create_task(
                    storage.set_item("committed_key", {"value": 1})
                )
                await committed.wait()
                update_task.cancel()
                # 提交任务受保护时，外层写入任务会继续等待确定结果，而不是立即丢弃缓存发布。
                await asyncio.sleep(0)
                self.assertFalse(update_task.done())
                allow_commit_return.set()

                with self.assertRaises(asyncio.CancelledError):
                    await update_task
                self.assertEqual(storage.get_item("committed_key"), {"value": 1})
                self.assertFalse(connection.in_transaction)

                sqlite_connection = sqlite3.connect(db_path)
                try:
                    stored_json = sqlite_connection.execute(
                        "SELECT value FROM general_storage WHERE key = ?",
                        ("committed_key",),
                    ).fetchone()[0]
                finally:
                    sqlite_connection.close()
                self.assertEqual(json.loads(stored_json), {"value": 1})
            finally:
                allow_commit_return.set()
                connection.commit = original_commit
                await storage.close_db()

    async def test_cancelled_lock_wait_queues_rollback_after_pending_begin(self):
        """取消等待 SQLite 写锁的任务后，不应留下延迟开启的事务。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "cancelled-lock-wait.db"
            storage = load_isolated_db_storage("test_cancelled_lock_wait", db_path)
            await storage.init_db()
            connection = storage._db
            assert connection is not None

            # 外部同步连接先占住写锁，使 aiosqlite 工作线程阻塞在 BEGIN IMMEDIATE。
            blocker = sqlite3.connect(db_path)
            blocker.execute("BEGIN IMMEDIATE")
            update_task = None
            try:
                update_task = asyncio.create_task(
                    storage.atomic_deep_update(["blocked"], lambda _current: {"value": 1})
                )
                # db.execute 在等待前已经把 BEGIN 放入工作线程队列；短暂让出事件循环即可进入锁等待。
                await asyncio.sleep(0.1)
                self.assertFalse(update_task.done())

                update_task.cancel()
                # 让取消处理分支把 rollback 排到仍在等待的 BEGIN 后面。
                await asyncio.sleep(0)
                blocker.rollback()
                blocker.close()
                blocker = None

                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(update_task, timeout=3)
                self.assertFalse(connection.in_transaction)

                # 后续事务能正常开启，证明没有遗留延迟生效的写事务。
                self.assertTrue(
                    await storage.atomic_deep_update(
                        ["after_cancel"],
                        lambda _current: {"value": 2},
                    )
                )
                self.assertEqual(storage.get_item("after_cancel"), {"value": 2})
            finally:
                if blocker is not None:
                    blocker.rollback()
                    blocker.close()
                if update_task is not None and not update_task.done():
                    update_task.cancel()
                    try:
                        await asyncio.wait_for(update_task, timeout=3)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                await storage.close_db()

    async def test_close_and_reinitialize_reset_connection_and_caches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "lifecycle.db"
            storage = load_isolated_db_storage("test_lifecycle", db_path)
            await storage.init_db()
            first_connection = storage._db
            await storage.init_db()
            self.assertIs(storage._db, first_connection)
            await storage.set_item("removed_while_closed", {"value": 1})
            await storage.close_db()

            self.assertIsNone(storage._db)
            self.assertIsNone(storage.get_item("removed_while_closed"))
            sqlite_connection = sqlite3.connect(db_path)
            try:
                sqlite_connection.execute(
                    "DELETE FROM general_storage WHERE key = ?",
                    ("removed_while_closed",),
                )
                sqlite_connection.commit()
            finally:
                sqlite_connection.close()

            await storage.init_db()
            try:
                self.assertIsNone(storage.get_item("removed_while_closed"))
                self.assertIsNot(storage._db, first_connection)
            finally:
                await storage.close_db()

    async def test_managed_resource_locks_are_released_after_use(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_resource_lock_cleanup",
                Path(temp_dir) / "lock-cleanup.db",
            )
            await storage.init_db()
            try:
                await asyncio.gather(
                    *[
                        storage.set_item(f"key-{index}", {"value": index})
                        for index in range(30)
                    ]
                )
                self.assertEqual(storage._resource_locks, {})
                self.assertEqual(storage._resource_lock_users, {})
                self.assertFalse(
                    any(key.startswith("业务资源锁:") for key in storage._last_lock_wait_warnings)
                )
            finally:
                await storage.close_db()

    async def test_bulk_insert_handles_empty_and_unserializable_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "bulk-boundaries.db"
            storage = load_isolated_db_storage("test_bulk_boundaries", db_path)
            await storage.close_db()
            self.assertFalse(await storage.insert_json_entities("before_init", {}))

            await storage.init_db()
            try:
                self.assertTrue(await storage.insert_json_entities("empty_namespace", {}))
                self.assertTrue(storage.is_json_entity_namespace_initialized("empty_namespace"))
                self.assertFalse(
                    await storage.insert_json_entities(
                        "invalid_namespace",
                        {"record-1": {"invalid": object()}},
                    )
                )
                self.assertFalse(storage.is_json_entity_namespace_initialized("invalid_namespace"))
            finally:
                await storage.close_db()

            reloaded = load_isolated_db_storage("test_bulk_boundaries_reloaded", db_path)
            await reloaded.init_db()
            try:
                self.assertTrue(reloaded.is_json_entity_namespace_initialized("empty_namespace"))
                self.assertEqual(reloaded.get_json_entities("empty_namespace"), {})
            finally:
                await reloaded.close_db()

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

    async def test_invalid_legacy_entity_aborts_migration_and_can_be_retried(self):
        """旧数据中任一非法实体都应阻止标记完成，并允许修正后重新迁移。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_invalid_json_entity_migration",
                Path(temp_dir) / "invalid-migration.db",
            )
            await storage.init_db()
            try:
                await storage.set_item(
                    "legacy_records",
                    {
                        "valid": {"record_id": "valid", "value": 1},
                        "invalid": "not-an-entity-dict",
                    },
                )

                self.assertEqual(
                    await storage.migrate_json_dict_to_entities(
                        "sample_orders",
                        "legacy_records",
                    ),
                    0,
                )
                self.assertFalse(storage.is_json_entity_namespace_initialized("sample_orders"))
                self.assertEqual(storage.get_json_entities("sample_orders"), {})

                # 修正旧数据后再次调用，全部实体都应进入同一批事务并建立完成标记。
                corrected = {
                    "valid": {"record_id": "valid", "value": 1},
                    "invalid": {"record_id": "invalid", "value": 2},
                }
                await storage.set_item("legacy_records", corrected)
                self.assertEqual(
                    await storage.migrate_json_dict_to_entities(
                        "sample_orders",
                        "legacy_records",
                    ),
                    2,
                )
                self.assertTrue(storage.is_json_entity_namespace_initialized("sample_orders"))
                self.assertEqual(storage.get_json_entities("sample_orders"), corrected)
            finally:
                await storage.close_db()

    async def test_partial_legacy_migration_is_completed_before_marking_namespace(self):
        """已有部分实体但缺少完成标记时，应补齐缺失记录并保留现有实体值。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "partial-migration.db"
            storage = load_isolated_db_storage("test_partial_json_entity_migration", db_path)
            await storage.init_db()
            try:
                legacy = {
                    "record-1": {"record_id": "record-1", "value": 1},
                    "record-2": {"record_id": "record-2", "value": 2},
                }
                await storage.set_item("legacy_records", legacy)

                # 模拟旧版本已经迁移第一条且发生过业务更新，但尚未写入命名空间完成标记。
                sqlite_connection = sqlite3.connect(db_path)
                try:
                    sqlite_connection.execute(
                        "INSERT INTO json_entity_storage(namespace, entity_id, value) VALUES (?, ?, ?)",
                        (
                            "sample_orders",
                            "record-1",
                            json.dumps({"record_id": "record-1", "value": 10}),
                        ),
                    )
                    sqlite_connection.commit()
                finally:
                    sqlite_connection.close()

                self.assertEqual(
                    await storage.migrate_json_dict_to_entities(
                        "sample_orders",
                        "legacy_records",
                    ),
                    1,
                )
                self.assertTrue(storage.is_json_entity_namespace_initialized("sample_orders"))
                self.assertEqual(
                    storage.get_json_entities("sample_orders"),
                    {
                        "record-1": {"record_id": "record-1", "value": 10},
                        "record-2": {"record_id": "record-2", "value": 2},
                    },
                )
            finally:
                await storage.close_db()

    async def test_set_deep_item_reports_success_and_failure(self):
        """深层设置不能再用相同的 None 同时表示成功和失败。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_set_deep_item_result",
                Path(temp_dir) / "set-deep-result.db",
            )
            await storage.close_db()
            self.assertFalse(await storage.set_item("top_before_init", 1))
            self.assertFalse(await storage.set_deep_item(["before_init", "value"], 1))

            await storage.init_db()
            try:
                self.assertTrue(await storage.set_deep_item(["top_level"], 1))
                self.assertEqual(storage.get_item("top_level"), 1)
                self.assertTrue(await storage.set_deep_item(["tree", "leaf"], 1))
                self.assertEqual(storage.get_deep_item(["tree", "leaf"]), 1)

                # object 无法序列化，原子更新会失败并通过 False 明确反馈给调用方。
                self.assertFalse(
                    await storage.set_deep_item(["tree", "invalid"], object())
                )
                self.assertEqual(
                    storage.get_deep_item(["tree"], {}),
                    {"leaf": 1},
                )
            finally:
                await storage.close_db()

    async def test_json_null_is_distinct_from_a_missing_key_or_entity(self):
        """已存储的 JSON null 应返回 None，而不是被调用方默认值覆盖。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = load_isolated_db_storage(
                "test_json_null_semantics",
                Path(temp_dir) / "json-null.db",
            )
            await storage.init_db()
            try:
                await storage.set_item("null_top", None)
                self.assertIsNone(storage.get_item("null_top", "missing"))
                self.assertIsNone(await storage.get_fresh_item("null_top", "missing"))
                self.assertEqual(storage.get_item("absent_top", "missing"), "missing")

                await storage.set_deep_item(["tree", "leaf"], None)
                self.assertIsNone(storage.get_deep_item(["tree", "leaf"], "missing"))
                self.assertEqual(storage.get_deep_item(["tree", "absent"], "missing"), "missing")

                self.assertTrue(
                    await storage.insert_json_entities(
                        "nullable_entities",
                        {"null-entity": None},
                    )
                )
                self.assertIsNone(
                    storage.get_json_entity("nullable_entities", "null-entity", "missing")
                )
                self.assertEqual(
                    storage.get_json_entity("nullable_entities", "absent", "missing"),
                    "missing",
                )

                # 不更新分支必须按“查询到行”刷新缓存，不能因值为 None 而删除缓存键。
                self.assertTrue(
                    await storage.atomic_deep_update(
                        ["null_top"],
                        lambda _current: storage.ATOMIC_NO_UPDATE,
                    )
                )
                self.assertIsNone(storage.get_item("null_top", "missing"))
                self.assertTrue(
                    await storage.atomic_json_entity_update(
                        "nullable_entities",
                        "null-entity",
                        lambda _current: storage.ATOMIC_NO_UPDATE,
                    )
                )
                self.assertIn("null-entity", storage.get_json_entities("nullable_entities"))
                self.assertIsNone(
                    storage.get_json_entity("nullable_entities", "null-entity", "missing")
                )
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
