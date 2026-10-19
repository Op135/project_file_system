"""ECN 附件的归档路径、实时权限与数据库保存回归。"""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.ecn_management_config import ECNState, ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
from src.modules.ecn import (
    actions,
    attachment_preview,
    attachment_ui,
    attachments,
    repository,
    overview_execution,
    overview_files,
)
from src.overview_file_storage import file_digest, make_file_selection
from src.modules.ecn.models import get_ecn_template
from src.modules.ecn.scheme_panel import get_overview_file_selection_note
from src.config import PDF_PREVIEW_CACHE
from src.pdf_preview import get_cached_pdf_response
from src.user_service import UserService
from tests.test_error_management_concurrency import load_isolated_db_storage


class Uploaded:
    name = "测试报告.pdf"

    def size(self):
        return len(b"test-pdf-content")

    async def save(self, path):
        Path(path).write_bytes(b"test-pdf-content")


class ECNAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.files = root / "files"
        self.formal = root / "formal"
        self.formal.mkdir()
        self.file_config = {"upload_path": str(self.formal)}
        self.overview_app_patch = patch.object(overview_files, "app", SimpleNamespace(storage=SimpleNamespace(general={"over_config_data_flat": {"file-param": self.file_config}})))
        self.overview_app_patch.start()
        self.addCleanup(self.overview_app_patch.stop)
        self.storage = load_isolated_db_storage("attachment_db", root / "ecn.db")
        await self.storage.init_db()
        self.db_patch = patch.object(repository, "db_storage", self.storage)
        self.path_patch = patch.object(attachments, "attachment_root", return_value=self.files)
        self.db_patch.start()
        self.path_patch.start()
        self.service = UserService(db_path=root / "ecn.db", password_iterations=1_000)
        self.service.modify_user("add", "owner", "123456", "测试岗位")
        self.service.modify_user("add", "other", "123456", "测试岗位")
        owner_role = self.service.create_security_role(
            code="test.ecn.attachment.owner",
            name="附件所有者测试权限",
            permission_codes=["ecn.view", "ecn.request.create", "ecn.scheme.edit"],
        )
        viewer_role = self.service.create_security_role(
            code="test.ecn.attachment.viewer",
            name="附件查看测试权限",
            permission_codes=["ecn.view"],
        )
        self.service.set_user_security_roles("owner", [owner_role])
        self.service.set_user_security_roles("other", [viewer_role])

    async def asyncTearDown(self):
        self.path_patch.stop()
        self.db_patch.stop()
        await self.storage.close_db()
        self.temp.cleanup()

    async def test_overview_upload_is_isolated_and_execution_never_overwrites_same_name(self):
        formal_dir = Path(self.temp.name) / "formal"
        original_file = formal_dir / Uploaded.name
        original_file.write_bytes(b"approved-original")
        staged = await attachments.stage_upload(Uploaded(), "owner")
        selection = make_file_selection(formal_dir, "测试报告-新版.pdf", "new", file_digest(attachments.resolve_staged_attachment(staged)))
        value = get_ecn_template()
        value["ecn_id"] = "ECN26101799"
        value["workflow"].update(current_state=ECNState.ECN_SCHEMING, current_phase="ECN_SCHEME_PHASE")
        value["target_projects"] = ["P1"]
        item = {
            "item_id": "upload-scheme", "type": "overview_update", "scheme_category": "overview_document",
            "author": "owner", "role": "研发", "label": "file-param", "first_col_label": "file-param",
            "config_processing_type": "file", "projects": ["P1"], "req_idxs": [], "linked_docs": [], "linked_materials": [],
            "project_states": {"P1": {"action": "add"}}, "new_data": {"content": selection["name"], "overview_upload": staged, "file_selection": selection},
        }
        await self.storage.set_item("ecn_management_data", {value["ecn_id"]: value})
        saved = await actions.edit_scheme(value["ecn_id"], value, item, None, "owner", "测试岗位", user_service=self.service, storage=self.storage)
        self.assertTrue(saved.ok, saved.message)
        assert saved.record is not None
        self.assertEqual(original_file.read_bytes(), b"approved-original")
        self.assertEqual(list(formal_dir.iterdir()), [original_file])
        value = saved.record
        value["workflow"]["current_state"] = ECNState.ECN_EXECUTING
        value["execution_info"].update(stage=ECN_EXECUTION_STAGE_OVERVIEW_RUNNING, overview_run_id="run-1")
        await self.storage.set_item("ecn_management_data", {value["ecn_id"]: value})
        fake_app = SimpleNamespace(storage=SimpleNamespace(general={
            "project_req_max_ver": {"P1": "1.0"}, "over_config_data_flat": {"file-param": {"upload_path": str(formal_dir)}},
        }))
        retry = copy.deepcopy(value)
        with patch.object(overview_execution, "db_storage", self.storage), patch.object(overview_execution, "app", fake_app):
            real_update = overview_execution.overview_storage.atomic_deep_update

            async def rollback_after_publish(path, updater, **kwargs):
                async def fail(data):
                    await updater(data)
                    raise OSError("模拟文件发布后概述事务失败")
                return await real_update(path, fail, **kwargs)

            with patch.object(overview_execution.overview_storage, "atomic_deep_update", new=rollback_after_publish):
                failed = await overview_execution.execute_ecn_overview_schemes(copy.deepcopy(value), "2026-10-17 11:59:00")
            self.assertEqual(failed["upload-scheme"]["status"], "failed")
            self.assertEqual(await self.storage.get_fresh_item("P1_over_data", {}), {})
            retained = [path for path in formal_dir.iterdir() if path != original_file]
            self.assertEqual(len(retained), 1)
            self.assertEqual(retained[0].read_bytes(), b"test-pdf-content")
            results = await overview_execution.execute_ecn_overview_schemes(value, "2026-10-17 12:00:00")
            repeated = await overview_execution.execute_ecn_overview_schemes(retry, "2026-10-17 12:01:00")
        self.assertEqual(results["upload-scheme"]["status"], "success")
        self.assertEqual(repeated["upload-scheme"]["status"], "success")
        stored = await self.storage.get_fresh_item("P1_over_data", {})
        chip = next(iter(stored["file-param"].values()))
        self.assertEqual(chip["content"], "测试报告-新版.pdf")
        final_path = Path(chip["local_file_path"])
        self.assertNotEqual(final_path, original_file)
        self.assertEqual(final_path, retained[0])
        self.assertEqual(final_path.name, "测试报告-新版.pdf")
        self.assertEqual(final_path.read_bytes(), b"test-pdf-content")
        self.assertEqual(original_file.read_bytes(), b"approved-original")
        self.assertEqual(len(list(formal_dir.iterdir())), 2)
        await attachments.cleanup_published_attachments(saved.record)
        self.assertTrue(final_path.is_file())
        archive = saved.record["change_items"][0]["new_data"]["overview_upload"]
        self.assertTrue(attachments.resolve_attachment(archive).is_file())

    async def test_running_ecn_cannot_be_deleted(self):
        value = get_ecn_template()
        value["ecn_id"] = "ECN26101798"
        value["execution_info"]["stage"] = ECN_EXECUTION_STAGE_OVERVIEW_RUNNING
        await self.storage.set_item("ecn_management_data", {value["ecn_id"]: value})
        with patch("src.ecn_access.can_delete_ecn", return_value=True):
            deleted, message = await repository.delete_record(value["ecn_id"], username="owner", user_service=self.service, storage=self.storage)
        self.assertFalse(deleted)
        self.assertIn("正在执行", message)
        self.assertIn(value["ecn_id"], await self.storage.get_fresh_item("ecn_management_data", {}))

    async def test_rejected_overview_save_retains_archive_and_retries_same_path(self):
        staged = await attachments.stage_upload(Uploaded(), "owner")
        selection = make_file_selection(self.formal, Uploaded.name, "new", file_digest(attachments.resolve_staged_attachment(staged)))
        value = get_ecn_template()
        value["ecn_id"] = "ECN26101797"
        value["workflow"].update(current_state=ECNState.ECN_SCHEMING, current_phase="ECN_SCHEME_PHASE")
        value["target_projects"] = ["P1"]
        item = {
            "item_id": "upload-scheme", "type": "overview_update", "scheme_category": "overview_document",
            "author": "owner", "label": "file-param", "first_col_label": "file-param",
            "config_processing_type": "file", "projects": ["P1"], "req_idxs": [], "linked_docs": [], "linked_materials": [],
            "project_states": {"P1": {"action": "add"}}, "new_data": {"content": Uploaded.name, "overview_upload": staged, "file_selection": selection},
        }
        changed = copy.deepcopy(value)
        changed["workflow"]["current_state"] = ECNState.ECN_REVIEWING
        await self.storage.set_item("ecn_management_data", {value["ecn_id"]: changed})
        saved = await actions.edit_scheme(value["ecn_id"], value, item, None, "owner", "测试岗位", user_service=self.service, storage=self.storage)
        self.assertFalse(saved.ok)
        self.assertTrue(attachments.resolve_staged_attachment(staged).is_file())
        archived_files = [file for file in self.files.rglob("*") if file.is_file() and file != attachments.resolve_staged_attachment(staged)]
        self.assertEqual(len(archived_files), 1)
        archive_path = archived_files[0]
        self.assertEqual(archive_path.read_bytes(), b"test-pdf-content")
        await self.storage.set_item("ecn_management_data", {value["ecn_id"]: value})
        retried = await actions.edit_scheme(value["ecn_id"], value, item, None, "owner", "测试岗位", user_service=self.service, storage=self.storage)
        self.assertTrue(retried.ok, retried.message)
        assert retried.record is not None
        self.assertEqual(attachments.resolve_attachment(retried.record["change_items"][0]["new_data"]["overview_upload"]), archive_path)
        self.assertFalse(attachments.resolve_staged_attachment(staged).exists())

    async def test_new_ecr_attachment_moves_under_assignee_and_allocated_number(self):
        draft = get_ecn_template()
        draft["basic_info"]["applicant"] = "owner"
        staged = await attachments.stage_upload(Uploaded(), "owner")
        draft["basic_info"]["attachments"] = [staged]
        result = await actions.execute_action(
            copy.deepcopy(draft), copy.deepcopy(draft), "save_draft", "owner", "测试岗位",
            is_new=True, user_service=self.service, storage=self.storage,
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
        record["target_projects"] = ["P1"]
        record["workflow"].update(current_state=ECNState.ECN_SCHEMING, current_phase="ECN_SCHEME_PHASE")
        item = {
            "item_id": "item-a", "author": "owner", "scheme_category": "ordinary_document",
            "type": "text_desc", "old_content": "旧", "new_content": "新", "attachments": [],
            "projects": ["P1"], "req_idxs": [], "linked_docs": [], "linked_materials": [],
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

    async def test_deleting_scheme_removes_its_uuid_named_attachment(self):
        record = get_ecn_template()
        record["ecn_id"] = "ECN26091604"
        record["workflow"].update(current_state=ECNState.ECN_SCHEMING, current_phase="ECN_SCHEME_PHASE")
        file_id = "b" * 32
        relative = f"owner/{record['ecn_id']}/scheme_item-a/{file_id}.pdf"
        metadata = {
            "id": file_id,
            "name": "测试报告.pdf",
            "uploaded_by": "owner",
            "relative_path": relative,
        }
        path = self.files / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"scheme-file")
        item = {
            "item_id": "item-a",
            "author": "owner",
            "scheme_category": "ordinary_document",
            "type": "text_desc",
            "old_content": "旧",
            "new_content": "新",
            "attachments": [metadata],
        }
        record["change_items"] = [item]
        await self.storage.set_item("ecn_management_data", {record["ecn_id"]: record})

        result = await actions.edit_scheme(
            record["ecn_id"],
            copy.deepcopy(record),
            None,
            copy.deepcopy(item),
            "owner",
            "测试岗位",
            delete=True,
            user_service=self.service,
            storage=self.storage,
        )

        self.assertTrue(result.ok, result.message)
        self.assertFalse(path.exists())

    async def test_deleting_record_removes_all_uuid_named_attachments(self):
        record = get_ecn_template()
        record["ecn_id"] = "ECN26091605"
        record["basic_info"]["applicant"] = "owner"
        paths = []
        metadata = []
        for index, file_id in enumerate(("c" * 32, "d" * 32), start=1):
            relative = f"owner/{record['ecn_id']}/ecr/{file_id}.pdf"
            path = self.files / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"file-{index}".encode())
            paths.append(path)
            metadata.append(
                {
                    "id": file_id,
                    "name": f"附件{index}.pdf",
                    "uploaded_by": "owner",
                    "relative_path": relative,
                }
            )
        record["basic_info"]["attachments"] = [metadata[0]]
        record["execution_info"] = {"attachments": {"ordinary|item-1|": [metadata[1]]}}
        await self.storage.set_item("ecn_management_data", {record["ecn_id"]: record})

        with patch("src.ecn_access.can_delete_ecn", return_value=True):
            deleted, message = await repository.delete_record(
                record["ecn_id"],
                username="owner",
                user_service=self.service,
                storage=self.storage,
            )

        self.assertTrue(deleted, message)
        self.assertTrue(all(not path.exists() for path in paths))


class ECNAttachmentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        attachment_preview._preview_tokens.clear()
        self.addCleanup(attachment_preview._preview_tokens.clear)
        attachment_preview._pending_preview_tokens.clear()
        self.addCleanup(attachment_preview._pending_preview_tokens.clear)
        PDF_PREVIEW_CACHE.clear()
        self.addCleanup(PDF_PREVIEW_CACHE.clear)

    def test_cached_pdf_can_be_read_repeatedly_by_browser_viewer(self):
        PDF_PREVIEW_CACHE["client-id-preview-id"] = b"%PDF-1.7\ncontent"

        first = get_cached_pdf_response("client-id-preview-id")
        second = get_cached_pdf_response("client-id-preview-id")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.media_type, "application/pdf")
        self.assertIn("client-id-preview-id", PDF_PREVIEW_CACHE)

    def test_overview_file_selection_note_is_file_type_independent(self):
        for name in ("示意图.png", "执行文件.hex", "规格书.pdf"):
            with self.subTest(name=name):
                self.assertEqual(
                    get_overview_file_selection_note(
                        {"file_selection": {"mode": "new", "name": name}}
                    ),
                    f"保存本次上传文件 · 正式文件名：{name}",
                )
        self.assertEqual(
            get_overview_file_selection_note(
                {"file_selection": {"mode": "existing", "name": "已有文件.pdf"}}
            ),
            "使用服务器已有文件 · 正式文件名：已有文件.pdf",
        )
        self.assertEqual(get_overview_file_selection_note({"content": "普通文字"}), "")

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

    def test_pdf_open_uses_same_browser_window_method_as_overview_page(self):
        path = Path(self.temp.name) / "测试报告.pdf"
        path.write_bytes(b"pdf-content")
        metadata = {"id": "file-1", "name": "测试报告.pdf"}
        with (
            patch.object(attachment_ui, "visible_attachments", return_value=[metadata]),
            patch.object(attachment_ui, "resolve_attachment", return_value=path),
            patch.object(
                attachment_ui,
                "issue_attachment_preview_url",
                return_value="/ecn_attachment_preview/test-token",
            ),
            patch.object(attachment_ui.ui, "run_javascript") as run_javascript,
            patch.object(attachment_ui.ui.navigate, "to") as navigate_to,
        ):
            attachment_ui.open_ecn_attachment_file(
                "ECN26091601", "scheme", "item-1", "", "file-1", "owner", "测试岗位"
            )

        run_javascript.assert_called_once_with(
            'window.open("/ecn_attachment_preview/test-token", \'_blank\');'
        )
        navigate_to.assert_not_called()

    def test_execution_review_attachment_is_visible_to_reviewer_and_original_owner(self):
        attachment = {
            "id": "review-file",
            "name": "整改现场照片.png",
            "uploaded_by": "reviewer",
        }
        record = {
            "execution_info": {
                "verification": {
                    "history": [
                        {
                            "event_id": "review-event",
                            "action": "revoked",
                            "reviewer": "reviewer",
                            "original_user": "owner",
                            "attachments": [attachment],
                        }
                    ]
                }
            }
        }
        with patch.object(
            attachments.db_storage,
            "get_deep_item",
            return_value=record,
        ), patch.object(attachments, "can_view_ecn", return_value=True):
            for username in ("reviewer", "owner"):
                with self.subTest(username=username):
                    visible = attachments.visible_attachments(
                        "ECN26092201",
                        "execution_review",
                        "review-event",
                        "",
                        username,
                        "测试岗位",
                        object(),
                    )
                    self.assertEqual(visible, [attachment])

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
