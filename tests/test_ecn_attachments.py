"""ECN 附件的归档路径、实时权限与数据库保存回归。"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from src.ecn_management_config import ECNState
from src.modules.ecn import actions, attachment_preview, attachments, repository
from src.modules.ecn.models import get_ecn_template
from tests.test_error_management_concurrency import load_isolated_db_storage


class Uploaded:
    name = "测试报告.pdf"

    def size(self):
        return len(b"test-pdf-content")

    async def save(self, path):
        Path(path).write_bytes(b"test-pdf-content")


class Users:
    storage_mode = "database"

    def __init__(self, grants):
        self.grants = grants

    def get_user(self, username):
        return {"status": "active", "role": "测试岗位"} if username in self.grants else None

    def get_primary_membership(self, username):
        return {"position_name": "测试岗位"} if username in self.grants else None

    def has_permission(self, username, code, **kwargs):
        del kwargs
        return code in self.grants.get(username, set())


class ECNAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.files = root / "files"
        self.storage = load_isolated_db_storage("attachment_db", root / "ecn.db")
        await self.storage.init_db()
        self.db_patch = patch.object(repository, "db_storage", self.storage)
        self.path_patch = patch.object(attachments, "attachment_root", return_value=self.files)
        self.db_patch.start()
        self.path_patch.start()
        self.service = Users({
            "owner": {"ecn.view", "ecn.request.create", "ecn.scheme.edit"},
            "other": {"ecn.view"},
        })

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.db_patch.stop()
        await self.storage.close_db()
        self.temp.cleanup()

    async def test_new_ecr_attachment_moves_under_assignee_and_allocated_number(self):
        draft = get_ecn_template()
        draft["basic_info"]["applicant"] = "owner"
        staged = await attachments.stage_upload(Uploaded(), "owner")
        draft["basic_info"]["attachments"] = [staged]
        legacy_service = Users({"owner": {"ecn.request.create"}})
        legacy_service.storage_mode = "legacy_excel"
        result = await actions.execute_action(
            copy.deepcopy(draft), copy.deepcopy(draft), "save_draft", "owner", "测试岗位",
            is_new=True, user_service=legacy_service, storage=self.storage,
        )
        self.assertTrue(result.ok, result.message)
        assert result.record is not None
        saved = result.record["basic_info"]["attachments"][0]
        self.assertIn(f"owner/{result.record['ecn_id']}/ecr/", saved["relative_path"])
        self.assertEqual(attachments.resolve_attachment(saved).read_bytes(), b"test-pdf-content")
        self.assertFalse((self.files / staged["pending_path"]).exists())

    async def test_existing_ecr_upload_checks_latest_applicant_and_permissions(self):
        record = get_ecn_template()
        record["ecn_id"] = "ECN26091601"
        record["basic_info"]["applicant"] = "owner"
        await self.storage.set_item("ecn_management_data", {record["ecn_id"]: record})
        good = await attachments.stage_upload(Uploaded(), "owner")
        result = await attachments.attach_existing(
            record["ecn_id"], "ecr", "", "", good, "owner", "测试岗位", self.service
        )
        self.assertTrue(result.ok, result.message)
        saved = await self.storage.get_fresh_item("ecn_management_data", {})
        self.assertEqual(len(saved[record["ecn_id"]]["basic_info"]["attachments"]), 1)

        denied = await attachments.stage_upload(Uploaded(), "other")
        result = await attachments.attach_existing(
            record["ecn_id"], "ecr", "", "", denied, "other", "测试岗位", self.service
        )
        self.assertFalse(result.ok)
        self.assertEqual(len((await self.storage.get_fresh_item("ecn_management_data", {}))[record["ecn_id"]]["basic_info"]["attachments"]), 1)
        self.assertEqual(len(list((self.files / "other" / record["ecn_id"]).rglob("*.pdf"))), 0)
        attachments.cleanup_staged([denied])

        attached = saved[record["ecn_id"]]["basic_info"]["attachments"][0]
        path = attachments.resolve_attachment(attached)
        removed = await attachments.remove_existing(
            record["ecn_id"], "ecr", "", "", attached["id"],
            "owner", "测试岗位", self.service,
        )
        self.assertTrue(removed.ok, removed.message)
        self.assertFalse(path.exists())
        current = await self.storage.get_fresh_item("ecn_management_data", {})
        self.assertEqual(current[record["ecn_id"]]["basic_info"]["attachments"], [])

    async def test_delegated_execution_node_accepts_only_its_assignee(self):
        record = get_ecn_template()
        record["ecn_id"] = "ECN26091602"
        record["workflow"]["current_state"] = ECNState.ECN_EXECUTING
        record["execution_info"] = {"ordinary_confirmations": {"item-1": {"assignee": "owner", "confirmed": False}}}
        await self.storage.set_item("ecn_management_data", {record["ecn_id"]: record})
        staged = await attachments.stage_upload(Uploaded(), "owner")
        result = await attachments.attach_existing(
            record["ecn_id"], "ordinary", "item-1", "", staged,
            "owner", "测试岗位", self.service,
        )
        self.assertTrue(result.ok, result.message)
        saved = await self.storage.get_fresh_item("ecn_management_data", {})
        self.assertEqual(len(saved[record["ecn_id"]]["execution_info"]["attachments"]["ordinary|item-1|"]), 1)

        other = await attachments.stage_upload(Uploaded(), "other")
        result = await attachments.attach_existing(
            record["ecn_id"], "ordinary", "item-1", "", other,
            "other", "测试岗位", self.service,
        )
        self.assertFalse(result.ok)
        attachments.cleanup_staged([other])

    async def test_scheme_attachment_is_bound_to_author_and_item(self):
        record = get_ecn_template()
        record["ecn_id"] = "ECN26091603"
        record["workflow"].update(current_state=ECNState.ECN_SCHEMING, current_phase="ECN_SCHEME_PHASE")
        item = {
            "item_id": "item-a", "author": "owner", "scheme_category": "ordinary_document",
            "type": "text_desc", "old_content": "旧", "new_content": "新", "attachments": [],
        }
        record["change_items"] = [item]
        await self.storage.set_item("ecn_management_data", {record["ecn_id"]: record})
        staged = await attachments.stage_upload(Uploaded(), "owner")
        published, _ = attachments.publish_pending(staged, record["ecn_id"], "owner", "scheme_item-a")
        updated = copy.deepcopy(item)
        updated["attachments"] = [published]
        result = await actions.edit_scheme(
            record["ecn_id"], copy.deepcopy(record), updated, copy.deepcopy(item),
            "owner", "测试岗位", user_service=self.service, storage=self.storage,
        )
        self.assertTrue(result.ok, result.message)
        attachments.cleanup_staged([staged])
        assert result.record is not None
        self.assertEqual(result.record["change_items"][0]["attachments"], [published])

        forged = copy.deepcopy(updated)
        forged["attachments"].append({**published, "id": "0" * 32})
        result = await actions.edit_scheme(
            record["ecn_id"], copy.deepcopy(result.record), forged, copy.deepcopy(updated),
            "owner", "测试岗位", user_service=self.service, storage=self.storage,
        )
        self.assertFalse(result.ok)


class ECNAttachmentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        attachment_preview._preview_tokens.clear()
        self.addCleanup(attachment_preview._preview_tokens.clear)
        attachment_preview._pending_preview_tokens.clear()
        self.addCleanup(attachment_preview._pending_preview_tokens.clear)

    def test_pdf_and_image_use_inline_response(self):
        for name, media_type in (("测试报告.pdf", "application/pdf"), ("现场照片.png", "image/png")):
            with self.subTest(name=name):
                path = Path(self.temp.name) / name
                path.write_bytes(b"file-content")
                metadata = {"id": "file-1", "name": name}
                with patch.object(attachment_preview, "visible_attachments", return_value=[metadata]), patch.object(
                    attachment_preview, "resolve_attachment", return_value=path,
                ):
                    url = attachment_preview.issue_attachment_preview_url(
                        "ECN26091601", "ecr", "", "", "file-1", "owner", "测试岗位",
                    )
                    response = attachment_preview.serve_ecn_attachment_preview(url.rsplit("/", 1)[-1])
                self.assertEqual(response.media_type, media_type)
                self.assertTrue(response.headers["content-disposition"].startswith("inline;"))
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_preview_rechecks_access_and_rejects_non_preview_files(self):
        url = attachment_preview.issue_attachment_preview_url(
            "ECN26091601", "ecr", "", "", "file-1", "owner", "测试岗位",
        )
        token = url.rsplit("/", 1)[-1]
        with patch.object(attachment_preview, "visible_attachments", return_value=[]):
            with self.assertRaises(HTTPException) as denied:
                attachment_preview.serve_ecn_attachment_preview(token)
        self.assertEqual(denied.exception.status_code, 404)

        with patch.object(attachment_preview, "visible_attachments", return_value=[
            {"id": "file-1", "name": "资料.docx"},
        ]):
            with self.assertRaises(HTTPException) as unsupported:
                attachment_preview.serve_ecn_attachment_preview(token)
        self.assertEqual(unsupported.exception.status_code, 404)

    def test_staged_image_preview_requires_existing_file_and_active_uploader(self):
        path = Path(self.temp.name) / "pending-image"
        path.write_bytes(b"image-content")
        staged = {"id": "a" * 32, "name": "现场照片.png", "uploaded_by": "owner", "pending_path": "_pending/" + "a" * 32}
        with patch.object(attachment_preview, "resolve_staged_attachment", return_value=path), patch.object(
            attachment_preview, "get_active_ecn_actor_role", return_value="测试岗位",
        ):
            url = attachment_preview.issue_staged_attachment_preview_url(staged, "owner", "测试岗位")
            token = url.rsplit("/", 1)[-1]
            response = attachment_preview.serve_ecn_pending_attachment_preview(token)
            self.assertEqual(response.media_type, "image/png")
        with patch.object(attachment_preview, "get_active_ecn_actor_role", return_value=None):
            with self.assertRaises(HTTPException) as denied:
                attachment_preview.serve_ecn_pending_attachment_preview(token)
        self.assertEqual(denied.exception.status_code, 404)
