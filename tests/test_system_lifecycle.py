import asyncio
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src import system_lifecycle
from src.system_lifecycle import SystemAction, request_system_action


ROOT_DIR = Path(__file__).resolve().parents[1]
DB_STORAGE_PATH = ROOT_DIR / "src" / "db_storage.py"


def load_isolated_db_storage(module_name: str, db_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, DB_STORAGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载数据库模块：{DB_STORAGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "DB_PATH", str(db_path))
    return module


class FakeBackupManager:
    def __init__(self, *, backup_path: str = "C:/backups/verified.db", error: Exception | None = None):
        self.backup_path = backup_path
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    async def run_safe_backup(self, trigger_type: str, *, strict: bool = False) -> str:
        self.calls.append((trigger_type, strict))
        if self.error is not None:
            raise self.error
        return self.backup_path


class SystemLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        system_lifecycle.reset_action_state_for_tests()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.restart_path = Path(self.temporary_directory.name) / "restart-request.json"
        self.path_patch = patch.object(system_lifecycle, "RESTART_REQUEST_PATH", self.restart_path)
        self.path_patch.start()

    def tearDown(self) -> None:
        system_lifecycle.reset_action_state_for_tests()
        self.path_patch.stop()
        self.temporary_directory.cleanup()

    async def test_shutdown_requires_strict_successful_backup(self) -> None:
        manager = FakeBackupManager()
        shutdown_calls: list[str] = []

        receipt = await request_system_action(
            SystemAction.SHUTDOWN,
            actor="admin",
            backup_manager=manager,
            delay_seconds=0,
            shutdown_callback=lambda: shutdown_calls.append("shutdown"),
        )
        await asyncio.sleep(0.01)

        self.assertEqual(receipt.backup_path, manager.backup_path)
        self.assertEqual(manager.calls, [("SYSTEM_SHUTDOWN_PRECHECK", True)])
        self.assertEqual(shutdown_calls, ["shutdown"])
        self.assertFalse(self.restart_path.exists())

    async def test_backup_failure_never_calls_shutdown(self) -> None:
        manager = FakeBackupManager(error=RuntimeError("backup failed"))
        shutdown_calls: list[str] = []

        with self.assertRaisesRegex(RuntimeError, "backup failed"):
            await request_system_action(
                SystemAction.SHUTDOWN,
                actor="admin",
                backup_manager=manager,
                delay_seconds=0,
                shutdown_callback=lambda: shutdown_calls.append("shutdown"),
            )
        await asyncio.sleep(0.01)

        self.assertEqual(shutdown_calls, [])
        self.assertFalse(self.restart_path.exists())

    async def test_restart_marker_is_created_and_consumed_once(self) -> None:
        manager = FakeBackupManager()
        shutdown_calls: list[str] = []

        await request_system_action(
            SystemAction.RESTART,
            actor="admin",
            backup_manager=manager,
            delay_seconds=0,
            shutdown_callback=lambda: shutdown_calls.append("shutdown"),
        )
        await asyncio.sleep(0.01)

        self.assertEqual(shutdown_calls, ["shutdown"])
        self.assertTrue(self.restart_path.exists())
        self.assertTrue(system_lifecycle.consume_restart_request())
        self.assertFalse(system_lifecycle.consume_restart_request())

    async def test_duplicate_action_is_rejected(self) -> None:
        manager = FakeBackupManager()

        await request_system_action(
            SystemAction.SHUTDOWN,
            actor="admin",
            backup_manager=manager,
            delay_seconds=60,
            shutdown_callback=lambda: None,
        )

        with self.assertRaisesRegex(RuntimeError, "系统已在执行关闭"):
            await request_system_action(
                SystemAction.RESTART,
                actor="admin",
                backup_manager=manager,
                delay_seconds=0,
                shutdown_callback=lambda: None,
            )


class VerifiedBackupTests(unittest.IsolatedAsyncioTestCase):
    async def test_online_backups_are_distinct_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            database_path = temporary_path / "source.db"
            backup_directory = temporary_path / "backups"
            storage = load_isolated_db_storage("test_verified_system_backup", database_path)
            await storage.init_db()
            try:
                self.assertTrue(await storage.set_item("safety_probe", {"revision": 1}))
                first_backup = Path(await storage.backup_db(str(backup_directory), retention_days=0))
                second_backup = Path(await storage.backup_db(str(backup_directory), retention_days=0))

                self.assertTrue(first_backup.is_file())
                self.assertTrue(second_backup.is_file())
                self.assertNotEqual(first_backup, second_backup)

                connection = sqlite3.connect(first_backup)
                try:
                    check_result = connection.execute("PRAGMA quick_check").fetchall()
                    stored_json = connection.execute(
                        "SELECT value FROM general_storage WHERE key = ?",
                        ("safety_probe",),
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual(check_result, [("ok",)])
                self.assertIsNotNone(stored_json)
                assert stored_json is not None
                self.assertIn('"revision": 1', stored_json[0])
            finally:
                await storage.close_db()


if __name__ == "__main__":
    unittest.main()
