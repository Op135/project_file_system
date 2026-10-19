"""正式原名、明确同名选择、审批快照与执行复核。"""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src import overview_batch_operations
from src.overview_file_storage import checked_filename, file_digest, make_file_selection, validate_file_selection
from src.modules.ecn import attachments, overview_files
from src.modules.ecn.editing import ECNConflict


class Upload:
    name = "报告.pdf"
    def size(self):
        return len(b"new upload")
    async def save(self, path):
        Path(path).write_bytes(b"new upload")


class FileChoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.formal = self.root / "formal"
        self.formal.mkdir()
        self.config = {"upload_path": str(self.formal)}
        archive_patch = patch.object(attachments, "attachment_root", return_value=self.root / "archive")
        archive_patch.start()
        self.addCleanup(archive_patch.stop)
        app_patch = patch.object(overview_files, "app", SimpleNamespace(storage=SimpleNamespace(general={"over_config_data_flat": {"docs": self.config}})))
        app_patch.start()
        self.addCleanup(app_patch.stop)
        self.upload = await attachments.stage_upload(Upload(), "owner")
        self.item = {"item_id": "s1", "type": "overview_update", "label": "docs", "config_processing_type": "file",
                     "project_states": {"P1": {"action": "add"}, "P2": {"action": "add"}},
                     "new_data": {"overview_upload": self.upload}}

    def prepare(self, selection):
        item = copy.deepcopy(self.item)
        item["new_data"]["file_selection"] = selection
        return overview_files.prepare_overview_upload(item, None, "ECN1", "owner")[0]

    async def test_checkpoint_recovery_validates_formal_file_without_restoring_it(self):
        selection = make_file_selection(self.formal, "recovery.pdf", "new", hashlib.sha256(b"new upload").hexdigest())
        item = self.prepare(selection)
        chip, _ = overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        overview_files.verify_published_overview_upload(item, chip, self.config)
        target = Path(chip["local_file_path"])
        target.write_bytes(b"changed outside system")
        with self.assertRaises(ValueError):
            overview_files.verify_published_overview_upload(item, chip, self.config)
        target.unlink()
        with self.assertRaises(ValueError):
            overview_files.verify_published_overview_upload(item, chip, self.config)
        self.assertFalse(target.exists())

    async def test_new_original_name_shared_by_projects_and_idempotent(self):
        selection = make_file_selection(self.formal, "报告-新版本.pdf", "new", hashlib.sha256(b"new upload").hexdigest())
        item = self.prepare(selection)
        self.assertEqual(list(self.formal.iterdir()), [])
        one, _ = overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        two, _ = overview_files.publish_overview_upload(item, "P2", "ECN1", self.config)
        self.assertEqual(one, two)
        self.assertEqual(one["content"], "报告-新版本.pdf")
        self.assertEqual(Path(one["local_file_path"]).name, "报告-新版本.pdf")
        self.assertEqual(len(list(self.formal.iterdir())), 1)

    async def test_existing_choice_reviews_existing_not_uploaded_bytes(self):
        target = self.formal / Upload.name
        target.write_bytes(b"existing selected bytes")
        item = self.prepare(make_file_selection(self.formal, Upload.name, "existing", file_digest(target)))
        archived = attachments.resolve_attachment(item["new_data"]["overview_upload"])
        self.assertEqual(archived.read_bytes(), b"existing selected bytes")
        self.assertEqual(attachments.resolve_staged_attachment(self.upload).read_bytes(), b"new upload")
        result, installed = overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        self.assertIsNone(installed)
        self.assertEqual(Path(result["local_file_path"]), target)
        self.assertEqual(item["new_data"]["overview_upload"]["size"], target.stat().st_size)

    async def test_retry_cannot_publish_bytes_different_from_selection(self):
        target = self.formal / Upload.name
        target.write_bytes(b"existing selected bytes")
        first = self.prepare(make_file_selection(self.formal, Upload.name, "existing", file_digest(target)))
        old_archive = attachments.resolve_attachment(first["new_data"]["overview_upload"])
        # 同名但内容不匹配时仍不能把上传文件冒充所选内容。
        with self.assertRaises(ValueError):
            self.prepare(make_file_selection(self.formal, Upload.name, "new", file_digest(target)))
        self.assertEqual(old_archive.read_bytes(), b"existing selected bytes")

    async def test_failed_save_retry_new_content_preserves_previous_archive(self):
        target = self.formal / Upload.name
        target.write_bytes(b"existing selected bytes")
        first = self.prepare(make_file_selection(self.formal, Upload.name, "existing", file_digest(target)))
        old_archive = attachments.resolve_attachment(first["new_data"]["overview_upload"])
        target.unlink()
        second = self.prepare(make_file_selection(self.formal, Upload.name, "new", hashlib.sha256(b"new upload").hexdigest()))
        new_archive = attachments.resolve_attachment(second["new_data"]["overview_upload"])
        self.assertNotEqual(old_archive, new_archive)
        self.assertEqual(old_archive.read_bytes(), b"existing selected bytes")
        self.assertEqual(new_archive.read_bytes(), b"new upload")

    async def test_renamed_archive_metadata_matches_confirmed_name(self):
        item = self.prepare(make_file_selection(self.formal, "renamed.pdf", "new", hashlib.sha256(b"new upload").hexdigest()))
        self.assertEqual(item["new_data"]["overview_upload"]["name"], "renamed.pdf")

    async def test_existing_changed_after_review_is_rejected(self):
        target = self.formal / Upload.name
        target.write_bytes(b"existing")
        item = self.prepare(make_file_selection(self.formal, Upload.name, "existing", file_digest(target)))
        target.write_bytes(b"changed after review")
        with self.assertRaisesRegex(ValueError, "内容已变化"):
            overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        self.assertEqual(target.read_bytes(), b"changed after review")

    async def test_existing_deleted_is_not_silently_recreated(self):
        target = self.formal / Upload.name
        target.write_bytes(b"existing")
        item = self.prepare(make_file_selection(self.formal, Upload.name, "existing", file_digest(target)))
        target.unlink()
        with self.assertRaisesRegex(ValueError, "已不存在"):
            overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        self.assertFalse(target.exists())

    async def test_new_late_collision_never_overwrites(self):
        item = self.prepare(make_file_selection(self.formal, Upload.name, "new", hashlib.sha256(b"new upload").hexdigest()))
        target = self.formal / Upload.name
        target.write_bytes(b"other writer")
        with self.assertRaises(ValueError):
            overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        self.assertEqual(target.read_bytes(), b"other writer")

    async def test_missing_selection_cannot_bypass_backend(self):
        with self.assertRaises(ECNConflict):
            overview_files.prepare_overview_upload(self.item, None, "ECN1", "owner")

    async def test_changed_directory_is_rejected(self):
        selection = make_file_selection(self.formal, Upload.name, "new", hashlib.sha256(b"new upload").hexdigest())
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(ValueError, "目录已变化"):
            validate_file_selection(selection, other)

    async def test_tampered_review_archive_is_rejected(self):
        item = self.prepare(make_file_selection(self.formal, Upload.name, "new", hashlib.sha256(b"new upload").hexdigest()))
        attachments.resolve_attachment(item["new_data"]["overview_upload"]).write_bytes(b"tampered")
        with self.assertRaises(ECNConflict):
            overview_files.publish_overview_upload(item, "P1", "ECN1", self.config)
        self.assertEqual(list(self.formal.iterdir()), [])

    async def test_filename_traversal_and_reserved_names_rejected(self):
        for name in ("../x.pdf", "sub/x.pdf", "sub\\x.pdf", "CON.pdf", "file.pdf ", "file.pdf.", "a:b.pdf"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                checked_filename(name)

    async def test_batch_existing_snapshot_checked_before_any_project_write(self):
        target = self.formal / Upload.name
        target.write_bytes(b"chosen server file")
        staging = self.root / "staging"
        staging.mkdir()
        snapshot = staging / Upload.name
        snapshot.write_bytes(target.read_bytes())
        selection = make_file_selection(self.formal, Upload.name, "existing", file_digest(target))
        payload = {"staged_file_path": str(snapshot), "content": Upload.name, "extra_data": {"file_selection": selection}}
        with patch.object(overview_batch_operations, "BATCH_OVERVIEW_STAGING_DIR", staging):
            ok, message = overview_batch_operations._install_staged_media(payload, self.config)
            self.assertTrue(ok, message)
            target.write_bytes(b"changed")
            ok, message = overview_batch_operations._install_staged_media(payload, self.config)
            self.assertFalse(ok)
        self.assertEqual(snapshot.read_bytes(), b"chosen server file")
        self.assertEqual(target.read_bytes(), b"changed")
