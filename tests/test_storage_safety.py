from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from nicegui.persistence.file_persistent_dict import FilePersistentDict

from src import config_service
from src.components import StorageBackupManager
from src.config_service import ConfigService
from src.storage_safety import (
    GeneralStorageSafetyError,
    assert_no_suspicious_storage_shrink,
    atomic_write_bytes,
    install_safe_nicegui_general_storage,
    read_general_storage_snapshot,
    validate_general_storage_against_latest_backup,
)


def _write_json(path: Path, data: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _representative_storage(*, collapsed: bool = False) -> dict[str, object]:
    if collapsed:
        return {
            "overview_pending_history": {},
            "overview_charge_pending": {},
            "project_summary": {},
            "overview_role": {},
            "wait_review": {},
        }
    repeated = "业务数据" * 10_000
    return {
        "overview_pending_history": {"2026-10-19": repeated},
        "overview_charge_pending": {"P-001": repeated},
        "project_summary": {"P-001": repeated},
        "overview_role": {"P-001": repeated},
        "wait_review": {"P-001": repeated},
    }


class GeneralStorageSnapshotTests(unittest.TestCase):
    def test_rejects_invalid_or_non_mapping_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-general.json"
            path.write_text('{"unfinished":', encoding="utf-8")
            with self.assertRaises(GeneralStorageSafetyError):
                read_general_storage_snapshot(path)

            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(GeneralStorageSafetyError):
                read_general_storage_snapshot(path)

    def test_rejects_multi_key_collapse_against_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            reference_path = root / "reference.json"
            current_path = root / "current.json"
            _write_json(reference_path, _representative_storage())
            _write_json(current_path, _representative_storage(collapsed=True))

            reference = read_general_storage_snapshot(reference_path)
            current = read_general_storage_snapshot(current_path)
            with self.assertRaisesRegex(GeneralStorageSafetyError, "异常缩水"):
                assert_no_suspicious_storage_shrink(current, reference)

    def test_startup_validation_uses_latest_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage_path = root / ".nicegui" / "storage-general.json"
            backup_path = root / "backups" / "storage-general_SHUTDOWN_NORMAL_20261019_143312.json"
            _write_json(backup_path, _representative_storage())
            _write_json(storage_path, _representative_storage(collapsed=True))

            with self.assertRaisesRegex(GeneralStorageSafetyError, "拒绝继续运行"):
                validate_general_storage_against_latest_backup(storage_path, backup_path.parent)


class AtomicGeneralStorageTests(unittest.TestCase):
    def test_atomic_write_leaves_only_complete_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-general.json"
            content = json.dumps(_representative_storage(), ensure_ascii=False).encode("utf-8")

            atomic_write_bytes(path, content)

            snapshot = read_general_storage_snapshot(path)
            self.assertEqual(snapshot.raw, content)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_safe_backend_rejects_stale_process_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-general.json"
            _write_json(path, {"project_summary": {"P-001": {"state": "研发"}}})
            storage = FilePersistentDict(path, encoding="utf-8")
            storage.initialize_sync()
            self.assertTrue(install_safe_nicegui_general_storage(storage))

            external = {"project_summary": {"P-001": {"state": "转产"}}}
            atomic_write_bytes(path, json.dumps(external, ensure_ascii=False).encode("utf-8"))
            dict.__setitem__(storage, "project_summary", {"P-001": {"state": "旧进程"}})
            handler = storage._change_handlers[0]

            with self.assertRaisesRegex(GeneralStorageSafetyError, "陈旧快照"):
                handler()
            self.assertEqual(read_general_storage_snapshot(path).data, external)

    def test_safe_backend_rejects_direct_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "storage-general.json"
            original = {"project_summary": {"P-001": {"state": "研发"}}}
            _write_json(path, original)
            storage = FilePersistentDict(path, encoding="utf-8")
            storage.initialize_sync()
            self.assertTrue(install_safe_nicegui_general_storage(storage))

            with self.assertRaisesRegex(GeneralStorageSafetyError, "禁止直接清空"):
                storage.clear()
            self.assertEqual(dict(storage), original)
            self.assertEqual(read_general_storage_snapshot(path).data, original)

    def test_safe_backend_rejects_runtime_multi_key_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / ".nicegui" / "storage-general.json"
            backup = root / "backups" / "storage-general_TEST_20261019_150000.json"
            original = _representative_storage()
            _write_json(path, original)
            _write_json(backup, original)
            storage = FilePersistentDict(path, encoding="utf-8")
            storage.initialize_sync()
            self.assertTrue(install_safe_nicegui_general_storage(storage))

            for key, value in _representative_storage(collapsed=True).items():
                dict.__setitem__(storage, key, value)
            handler = storage._change_handlers[0]

            with self.assertRaisesRegex(GeneralStorageSafetyError, "异常缩水"):
                handler()
            self.assertEqual(read_general_storage_snapshot(path).data, original)


class StorageBackupManagerTests(unittest.TestCase):
    def _manager(self, storage_path: Path, backup_dir: Path) -> StorageBackupManager:
        manager = StorageBackupManager.__new__(StorageBackupManager)
        manager.json_file = storage_path
        manager.backup_dir_path = backup_dir
        return manager

    def test_backup_rejects_collapsed_current_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage_path = root / ".nicegui" / "storage-general.json"
            backup_dir = root / "backups"
            previous = backup_dir / "storage-general_SHUTDOWN_NORMAL_20261019_143312.json"
            _write_json(previous, _representative_storage())
            _write_json(storage_path, _representative_storage(collapsed=True))
            manager = self._manager(storage_path, backup_dir)

            with self.assertLogs("src.components", level="ERROR"):
                self.assertFalse(manager._backup_json_file("TEST", "20261019_150000"))
            self.assertFalse((backup_dir / "storage-general_TEST_20261019_150000.json").exists())

    def test_backup_is_verified_and_does_not_overwrite_same_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            storage_path = root / ".nicegui" / "storage-general.json"
            backup_dir = root / "backups"
            data = _representative_storage()
            _write_json(storage_path, data)
            manager = self._manager(storage_path, backup_dir)

            self.assertTrue(manager._backup_json_file("TEST", "20261019_150000"))
            self.assertTrue(manager._backup_json_file("TEST", "20261019_150000"))

            created = list(backup_dir.glob("storage-general_TEST_*.json"))
            self.assertEqual(len(created), 2)
            for path in created:
                self.assertEqual(read_general_storage_snapshot(path).data, data)


class ConfigServicePersistenceTests(unittest.TestCase):
    def test_custom_labels_are_published_with_one_top_level_write(self) -> None:
        class CountingStorage(dict[str, object]):
            writes = 0

            def __setitem__(self, key: str, value: object) -> None:
                self.writes += 1
                super().__setitem__(key, value)

        processed = {
            "1": {
                "node_id": "node-1",
                "options": [
                    {"option_id": "opt-1", "option_label": "标签一"},
                    {"option_id": "opt-2", "option_label": "标签二"},
                ],
            },
            "2": {
                "node_id": "node-2",
                "options": [{"option_id": "opt-3", "option_label": "标签三"}],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ConfigService()
            service.base_dir = Path(temp_dir)
            service.excel_path = service.base_dir / "config.xlsx"
            service.excel_path.write_bytes(b"test-config")
            storage = CountingStorage()

            with (
                patch.object(config_service.pd, "read_excel", return_value=object()),
                patch.object(service, "_process_data", return_value=processed),
                patch.object(config_service.app.storage, "_general", storage),
            ):
                result = service.load_config(force_reload=True)

            self.assertIsNotNone(result)
            self.assertEqual(storage.writes, 1)
            self.assertEqual(
                storage["config_service_custom_labels"],
                {
                    "node-1": {"opt-1": "标签一", "opt-2": "标签二"},
                    "node-2": {"opt-3": "标签三"},
                },
            )


if __name__ == "__main__":
    unittest.main()
