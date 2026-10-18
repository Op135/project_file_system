"""概述保留旧命名与正式文件不回收的文件系统回归。"""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import components, overview_add_approval, overview_batch_operations, overview_file_storage


class NamedFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_existing_different_content_is_never_overwritten(self):
        target = self.root / "原始名称.pdf"
        target.write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "同名文件"):
            overview_file_storage.save_named_bytes(b"new", target)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_concurrent_identical_uploads_keep_original_name_and_one_file(self):
        target = self.root / "原始名称.pdf"
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: overview_file_storage.save_named_bytes(b"same", target), range(8)))
        self.assertEqual(paths, [target] * 8)
        self.assertEqual(list(self.root.iterdir()), [target])
        self.assertEqual(target.read_bytes(), b"same")

    def test_concurrent_different_content_has_one_winner(self):
        target = self.root / "same.pdf"

        def save(content):
            try:
                overview_file_storage.save_named_bytes(content, target)
                return content
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [b"one", b"two"]))
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(target.read_bytes(), winners[0])
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_partial_copy_failure_cleans_only_unpublished_temporary_file(self):
        target = self.root / "new.pdf"

        def fail(source, output):
            output.write(b"partial")
            raise OSError("disk full")

        with patch.object(overview_file_storage.shutil, "copyfileobj", side_effect=fail):
            with self.assertRaises(OSError):
                overview_file_storage.save_named_bytes(b"whole", target)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_batch_refuses_unconfirmed_different_same_name_file(self):
        staging = self.root / "staging"
        staging.mkdir()
        staged = staging / "same.pdf"
        staged.write_bytes(b"new")
        target = self.root / "same.pdf"
        target.write_bytes(b"old")
        with patch.object(overview_batch_operations, "BATCH_OVERVIEW_STAGING_DIR", staging):
            ok, message = overview_batch_operations._install_staged_media(
                {"staged_file_path": str(staged), "content": "same.pdf"}, {"upload_path": str(self.root)},
            )
        self.assertFalse(ok, message)
        self.assertTrue(staged.is_file())
        self.assertEqual(target.read_bytes(), b"old")


class ApprovalFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    async def test_existing_name_still_prompts_before_reading_upload(self):
        target = self.root / "same.pdf"
        target.write_bytes(b"old")
        table = components.OverviewTableGroup.__new__(components.OverviewTableGroup)
        table.project = "P"
        table.current_config = {"label": "files", "processing_type": "file", "upload_path": str(self.root)}
        uploaded = SimpleNamespace(name="same.pdf", content_type="application/pdf", read=AsyncMock(return_value=b"new"))
        with patch.object(components.db_storage, "get_deep_item", return_value={}), patch.object(table, "_select_file_show") as choice:
            await table._handle_file_upload(SimpleNamespace(file=uploaded))
        choice.assert_called_once()
        uploaded.read.assert_not_awaited()
        self.assertEqual(target.read_bytes(), b"old")

    async def test_queue_does_not_move_existing_formal_file(self):
        formal = self.root / "same.pdf"
        formal.write_bytes(b"shared")
        insert = AsyncMock(return_value=True)
        with (
            patch.object(overview_add_approval, "OVERVIEW_ADD_STAGING_DIR", self.root / "staging"),
            patch.object(overview_add_approval, "create_approval_sequence_assignments", return_value={"status": "matched", "assignment": {"current_task_key": "task"}}),
            patch.object(overview_add_approval, "_insert_request", new=insert),
        ):
            result = await overview_add_approval.save_or_submit_overview_addition(
                project="P", label="files", chip={"id": "f", "content": "same.pdf", "type": "file"},
                config={"add_approval_required": True}, requester_username="owner", requester_role="editor",
                uploaded_file_path=formal, user_service=SimpleNamespace(supersede_pending_work_assignments=Mock()),
            )
            self.assertTrue(result.pending, result.message)
            self.assertEqual(formal.read_bytes(), b"shared")
            record = insert.call_args.args[0]
            self.assertEqual(Path(record["payload"]["staged_file_path"]).read_bytes(), b"shared")
            overview_add_approval._cleanup_staged_file(record)
            self.assertEqual(formal.read_bytes(), b"shared")

    async def test_approval_failure_retains_new_formal_file_and_retry_source(self):
        staging = self.root / "staging"
        staging.mkdir()
        source = staging / "same.pdf"
        source.write_bytes(b"approved")
        target = self.root / "same.pdf"
        config = {"label": "files", "upload_path": str(self.root)}
        request = {"project": "P", "label": "files", "submitter": "owner", "payload": {
            "chip": {"id": "f", "type": "file", "content": "same.pdf"}, "config": config,
            "staged_file_path": str(source), "final_file_path": str(target),
        }}
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={"over_config_data_flat": {"files": config}}))
        with (
            patch.object(overview_add_approval, "OVERVIEW_ADD_STAGING_DIR", staging),
            patch.object(overview_add_approval, "app", fake_app),
            patch.object(overview_add_approval, "can_edit_overview_item", return_value=True),
            patch.object(overview_batch_operations, "insert_overview_chip", new=AsyncMock(return_value=(False, "locked"))),
        ):
            ok, _ = await overview_add_approval._execute_approved_add(request)
        self.assertFalse(ok)
        self.assertEqual(target.read_bytes(), b"approved")
        self.assertEqual(source.read_bytes(), b"approved")
