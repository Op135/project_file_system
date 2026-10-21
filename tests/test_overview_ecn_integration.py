"""ECN 占用与普通概述操作交叉执行的数据库、文件回归测试。"""

import ast
import copy
import hashlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import components, overview_batch_operations, overview_corrections, overview_storage, overview_file_storage
from src.ecn_management_config import ECN_OVERVIEW_ACTION_UPDATE, ECNState
from tests.test_db_storage_entities import load_isolated_db_storage


class OverviewECNIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = load_isolated_db_storage("overview_ecn_audit", self.root / "test.db")
        await self.storage.init_db()
        self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self.storage.close_db)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module in (components, overview_batch_operations, overview_corrections, overview_storage):
            self.stack.enter_context(patch.object(module, "db_storage", self.storage))
        self.app = SimpleNamespace(storage=SimpleNamespace(user={"current_user": "测试人员"}, general={}))
        self.stack.enter_context(patch.object(components, "app", self.app))
        self.stack.enter_context(patch.object(overview_corrections, "app", self.app))
        self.notify = self.stack.enter_context(patch.object(components.ui, "notify"))
        self.bump = self.stack.enter_context(patch.object(components.OverviewVersionManager, "bump"))

    async def lock(self, label="child", chip_id="b"):
        await self.storage.set_item("ecn_management_data", {"ECN-TEST": {
            "ecn_id": "ECN-TEST", "workflow": {"current_state": ECNState.ECN_SCHEMING},
            "change_items": [{"item_id": "item", "type": "overview_update", "label": label,
                              "first_col_label": "base",
                              "project_states": {"P": {"action": ECN_OVERVIEW_ACTION_UPDATE, "chip_id": chip_id}}}],
        }})

    async def seed_row(self, state: bool | None = True):
        base = {"id": "a", "row_id": "row", "type": "text", "content": "首列", "creator": "原录入人",
                "select_activ_dic": {"1.0": state}, "enabled": state, "timestamp": {}}
        data = {"base": {"a": base}, "child": {"b": {**copy.deepcopy(base), "id": "b", "content": "子项"}}}
        await self.storage.set_item("P_over_data", data)
        await self.storage.set_item("P_over_related_record", {"base": {"a": {"open": {"reason": "待确认"}}}})
        return data

    async def change_row(self, state: bool | None = False):
        table = components.OverviewTableGroup.__new__(components.OverviewTableGroup)
        table.project = "P"
        table.configs = [{"label": "base"}, {"label": "child"}]
        spinner = Mock()
        with (
            patch.object(table, "_check_version_updated", return_value=False),
            patch.object(table, "cancel_checkbox_change"),
            patch.object(table, "_update_display", new=AsyncMock()),
            patch.object(table, "_show_related_chip_select_dialog"),
        ):
            await table.handle_checkbox_change(spinner, "a", "首列", {"label": "base"},
                                               SimpleNamespace(value="状态调整"), {"1.0": state})
        return spinner

    async def test_locked_child_rolls_back_entire_row_and_releases_spinner(self):
        before = await self.seed_row()
        await self.lock()
        spinner = await self.change_row()
        self.assertEqual(self.storage.get_item("P_over_data"), before)
        self.assertIn("open", self.storage.get_item("P_over_related_record")["base"]["a"])
        self.bump.assert_not_called()
        spinner.set_visibility.assert_called_with(False)

    async def test_unlocked_row_changes_together_and_preserves_creator(self):
        await self.seed_row()
        await self.change_row()
        saved = self.storage.get_item("P_over_data")
        for label, chip_id in (("base", "a"), ("child", "b")):
            self.assertFalse(saved[label][chip_id]["select_activ_dic"]["1.0"])
            self.assertEqual(saved[label][chip_id]["creator"], "原录入人")
            self.assertTrue(saved[label][chip_id]["timestamp"])

    async def test_locked_child_also_blocks_row_promotion(self):
        before = await self.seed_row(False)
        await self.lock()
        await self.change_row(True)
        self.assertEqual(self.storage.get_item("P_over_data"), before)

    async def test_batch_state_api_rolls_back_locked_row(self):
        before = await self.seed_row()
        await self.lock()
        changed_targets: set[tuple[str, str]] = set()
        changed, message, _ = await overview_batch_operations.update_overview_chip_state(
            "P", "base", "a", "1.0", False, "测试人员",
            group_configs=[{"label": "base"}, {"label": "child"}], changed_targets=changed_targets,
        )
        self.assertFalse(changed)
        self.assertIn("ECN", message)
        self.assertEqual(self.storage.get_item("P_over_data"), before)
        self.assertEqual(changed_targets, set())

    async def test_old_version_cascade_preserves_latest_visual_and_other_rows(self):
        data = await self.seed_row()
        child = data["child"]["b"]
        child.update(select_activ_dic={"1.0": True, "2.0": True}, enabled=True, icon="image", bg_color="bg-light-blue-1")
        data["child"]["other"] = {**copy.deepcopy(child), "row_id": "other-row"}
        await self.storage.set_item("P_over_data", data)
        await self.change_row()
        saved = self.storage.get_item("P_over_data")
        self.assertEqual(saved["child"]["b"]["select_activ_dic"], {"1.0": False, "2.0": True})
        for key in ("enabled", "icon", "bg_color"):
            self.assertEqual(saved["child"]["b"][key], child[key])
        self.assertEqual(saved["child"]["other"], data["child"]["other"])

    async def test_promotion_exclusions_do_not_bypass_ecn_anchor_dependency_lock(self):
        before = await self.seed_row(False)
        await self.lock()
        changed, message, _ = await overview_batch_operations.update_overview_chip_state(
            "P", "base", "a", "1.0", True, "测试人员", group_configs=[
                {"label": "base", "row_state_follow_excluded_labels": ["child"]}, {"label": "child"}],
        )
        self.assertFalse(changed)
        self.assertIn("ECN", message)
        self.assertEqual(self.storage.get_item("P_over_data"), before)

    async def test_pending_row_promotion_keeps_child_pending_record_open(self):
        await self.seed_row(False)
        await self.storage.set_item("P_over_related_record", {"child": {"b": {"open": {"reason": "仍需确认"}}}})
        await self.change_row(None)
        self.assertIsNone(self.storage.get_item("P_over_data")["child"]["b"]["enabled"])
        self.assertIn("open", self.storage.get_item("P_over_related_record")["child"]["b"])

    async def test_second_connection_new_lock_is_respected(self):
        before = await self.seed_row()
        await self.lock()
        records = self.storage.get_item("ecn_management_data")
        await self.storage.set_item("ecn_management_data", {})
        other = load_isolated_db_storage("overview_ecn_other", self.root / "test.db")
        await other.init_db()
        try:
            await other.set_item("ecn_management_data", records)
            await self.change_row()
            self.assertEqual(await other.get_fresh_item("P_over_data"), before)
        finally:
            await other.close_db()

    async def test_bulk_pending_ui_does_not_split_locked_row(self):
        before = await self.seed_row(None)
        await self.lock()
        table = components.OverviewTableGroup.__new__(components.OverviewTableGroup)
        table.project = "P"
        table.configs = [{"label": "base"}, {"label": "child"}]
        with patch.object(table, "_continue_after_column_bulk_pending_decisions"):
            await table._apply_column_bulk_pending_decisions([("a", "首列", "1.0", False)], "停用", {"label": "base"})
        self.assertEqual(self.storage.get_item("P_over_data"), before)
        self.bump.assert_not_called()

    async def media_request(self, chip_type="file", suffix="pdf", isolated=True):
        old = self.root / ("ecn-unique." + suffix if isolated else "original." + suffix)
        old.write_bytes(b"original")
        new = self.root / ("corrected." + suffix)
        new.write_bytes(b"corrected")
        before = {"id": "f", "type": chip_type, "content": "original." + suffix, "enabled": True}
        if isolated:
            before["local_file_path"] = str(old)
        config = {"label": "files", "processing_type": chip_type, "upload_path": str(self.root),
                  "permission": {"edit_role": ["editor"]}, "content_regular": []}
        await self.storage.set_item("P_over_data", {"files": {"f": before}})
        self.app.storage.general = {"over_config_data_flat": {"files": config}}
        request = {"project": "P", "label": "files", "chip_id": "f", "action": "correct",
                   "submitter_role": "editor", "payload": {"before_snapshot": before,
                   "before_fingerprint": overview_corrections.chip_snapshot_fingerprint(before),
                   "after_snapshot": {**before, "content": new.name}, "config": config,
                   "uploaded_file_type": chip_type + "/test", "target_url_path": "/files/" + new.name}}
        ok, message, audit = overview_corrections.build_media_file_audit(
            before, config, "", after=request["payload"]["after_snapshot"])
        self.assertTrue(ok, message)
        request["payload"].update(audit)
        return request, old, new

    async def test_correction_repoints_ecn_file_image_and_video(self):
        for chip_type, suffix in (("file", "pdf"), ("image", "png"), ("video", "mp4")):
            with self.subTest(chip_type=chip_type):
                request, old, new = await self.media_request(chip_type, suffix)
                result = await overview_corrections.execute_correction_request(request)
                self.assertTrue(result["ok"], result)
                saved = self.storage.get_item("P_over_data")["files"]["f"]
                rendered = Path(saved.get("local_file_path") or self.root / saved["content"])
                self.assertEqual(rendered.read_bytes(), new.read_bytes())
                self.assertEqual(old.read_bytes(), b"original")

    async def test_regular_filename_correction_remains_supported(self):
        request, old, new = await self.media_request(isolated=False)
        result = await overview_corrections.execute_correction_request(request)
        self.assertTrue(result["ok"], result)
        saved = self.storage.get_item("P_over_data")["files"]["f"]
        self.assertEqual(Path(saved.get("local_file_path") or self.root / saved["content"]).read_bytes(), new.read_bytes())
        self.assertTrue(old.exists())

    async def test_media_audit_and_execution_check_actual_ecn_path(self):
        request, old, _ = await self.media_request()
        payload = request["payload"]
        valid, message, audit = overview_corrections.build_media_file_audit(payload["before_snapshot"], payload["config"], "")
        self.assertTrue(valid, message)
        self.assertEqual(audit["original_file_sha256"], hashlib.sha256(b"original").hexdigest())
        payload.update(audit)
        old.write_bytes(b"changed while awaiting approval")
        result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertIn("原文件", result["message"])

    async def test_locked_media_correction_preserves_staging_and_formal_file(self):
        request, old, target = await self.media_request()
        target.unlink()
        staging = self.root / "staging"
        staging.mkdir()
        staged = staging / "corrected.pdf"
        staged.write_bytes(b"new upload")
        request["payload"]["staged_file_path"] = str(staged)
        await self.lock("files", "f")
        before = self.storage.get_item("P_over_data")
        with patch.object(overview_corrections, "OVERVIEW_CORRECTION_STAGING_DIR", staging):
            result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertEqual(self.storage.get_item("P_over_data"), before)
        self.assertTrue(staged.exists())
        self.assertEqual(target.read_bytes(), b"new upload")
        self.assertEqual(old.read_bytes(), b"original")

    async def test_identical_preexisting_copy_survives_failed_correction(self):
        request, _, target = await self.media_request()
        staging = self.root / "staging"
        staging.mkdir()
        staged = staging / "corrected.pdf"
        staged.write_bytes(target.read_bytes())
        request["payload"]["staged_file_path"] = str(staged)
        await self.lock("files", "f")
        with patch.object(overview_corrections, "OVERVIEW_CORRECTION_STAGING_DIR", staging):
            result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertEqual(target.read_bytes(), b"corrected")
        self.assertTrue(staged.exists())

    async def test_existing_file_keeps_original_path_and_url(self):
        import httpx
        from nicegui.app.app import App

        request, _, source = await self.media_request()
        result = await overview_corrections.execute_correction_request(request)
        self.assertTrue(result["ok"], result)
        saved = self.storage.get_item("P_over_data")["files"]["f"]
        target = Path(saved["local_file_path"])
        self.assertEqual(target, source)
        self.assertEqual(saved["content"], "corrected.pdf")
        self.assertEqual(saved["url_path"], "/files/corrected.pdf")
        file_app = App()
        file_app.add_static_file(local_file=target, url_path=saved["url_path"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=file_app), base_url="http://test") as client:
            response = await client.get(saved["url_path"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"corrected")

    async def test_parallel_same_name_same_bytes_failure_cannot_delete_successful_file(self):
        first, _, source = await self.media_request()
        second = copy.deepcopy(first)
        second["chip_id"] = "g"
        second["payload"]["before_snapshot"]["id"] = "g"
        second["payload"]["before_fingerprint"] = overview_corrections.chip_snapshot_fingerprint(second["payload"]["before_snapshot"])
        await self.storage.set_deep_item(["P_over_data", "files", "g"], second["payload"]["before_snapshot"])
        await self.lock("files", "f")
        real_atomic = overview_storage.atomic_deep_update
        entered = False
        second_result: dict = {}

        async def interleave(path, updater, *args, **kwargs):
            nonlocal entered, second_result
            if not entered:
                entered = True
                second_result = await overview_corrections.execute_correction_request(second)
            return await real_atomic(path, updater, *args, **kwargs)

        with patch.object(overview_storage, "atomic_deep_update", new=interleave):
            first_result = await overview_corrections.execute_correction_request(first)
        self.assertFalse(first_result["ok"])
        self.assertTrue(second_result["ok"], second_result)
        saved = self.storage.get_item("P_over_data")["files"]["g"]
        self.assertEqual(Path(saved["local_file_path"]).read_bytes(), b"corrected")
        self.assertEqual(Path(saved["local_file_path"]), source)
        self.assertEqual(source.read_bytes(), b"corrected")

    async def test_uploaded_same_name_different_bytes_never_overwrites_existing_file(self):
        request, _, source = await self.media_request()
        staging = self.root / "staging"
        staging.mkdir()
        staged = staging / source.name
        staged.write_bytes(b"different newly approved content")
        request["payload"]["staged_file_path"] = str(staged)
        with patch.object(overview_corrections, "OVERVIEW_CORRECTION_STAGING_DIR", staging):
            result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"], result)
        self.assertIn("更换文件名", result["message"])
        self.assertEqual(source.read_bytes(), b"corrected")
        self.assertTrue(staged.exists())

    async def test_failed_saved_request_reuses_original_path_after_retry(self):
        request, _, source = await self.media_request()
        saved, request_id = await overview_corrections.create_correction_request(request)
        self.assertTrue(saved)
        request = self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        self.assertNotIn("media_file_id", request)
        await self.lock("files", "f")
        result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertEqual(source.read_bytes(), b"corrected")
        await self.storage.set_item("ecn_management_data", {})
        reloaded = self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        result = await overview_corrections.execute_correction_request(reloaded)
        self.assertTrue(result["ok"], result)
        self.assertEqual(Path(result["after_snapshot"]["local_file_path"]), source)

    async def test_late_same_name_collision_is_rejected_without_touching_existing_bytes(self):
        occupied = self.root / "target.pdf"
        occupied.write_bytes(b"belongs to another file")
        source = self.root / "source.pdf"
        source.write_bytes(b"new upload")
        with self.assertRaisesRegex(ValueError, "同名文件"):
            await overview_corrections.publish_correction_media({}, source, self.root, occupied.name, hashlib.sha256(b"new upload").hexdigest())
        self.assertEqual(occupied.read_bytes(), b"belongs to another file")

    async def test_deleted_request_cannot_publish_or_recreate_itself(self):
        request, _, _ = await self.media_request()
        saved, request_id = await overview_corrections.create_correction_request(request)
        self.assertTrue(saved)
        request = self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        await self.storage.del_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertFalse(self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id]))
        self.assertEqual(self.storage.get_item("P_over_data")["files"]["f"]["content"], "original.pdf")

    async def test_archive_retains_actual_published_path_instead_of_staging(self):
        request, _, _ = await self.media_request()
        saved, request_id = await overview_corrections.create_correction_request(request)
        self.assertTrue(saved)
        request = self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        result = await overview_corrections.execute_correction_request(request)
        self.assertTrue(result["ok"], result)
        await self.storage.set_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id, "status"], "approved")
        archived = await overview_corrections.archive_correction_request(request_id, request, "approved", {"result": result})
        self.assertTrue(archived)
        payload = self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_ARCHIVES_KEY, request_id, "payload"])
        self.assertEqual(payload["after_snapshot"]["local_file_path"], result["after_snapshot"]["local_file_path"])
        self.assertEqual(payload["staged_file_path"], "")

    async def test_preview_uses_actual_old_file_and_published_original_name_file(self):
        from src.pages import information

        request, old, _ = await self.media_request()
        payload = request["payload"]
        result = await overview_corrections.execute_correction_request(request)
        self.assertTrue(result["ok"], result)
        archived_payload = {**payload, "after_snapshot": result["after_snapshot"], "staged_file_path": ""}
        with (
            patch.object(information, "app", SimpleNamespace(add_static_file=Mock())),
            patch.object(information, "_correction_preview_routes", set()),
        ):
            before_preview = information._get_correction_media_preview("preview", payload, payload["before_snapshot"], "before")
            after_preview = information._get_correction_media_preview("preview", archived_payload, result["after_snapshot"], "after")
        assert before_preview is not None
        assert after_preview is not None
        self.assertEqual(Path(before_preview[2]), old)
        self.assertEqual(Path(after_preview[2]).read_bytes(), b"corrected")
        self.assertEqual(Path(after_preview[2]), Path(result["after_snapshot"]["local_file_path"]))

    async def test_changed_saved_request_cannot_publish_stale_file(self):
        request, _, _ = await self.media_request()
        saved, request_id = await overview_corrections.create_correction_request(request)
        self.assertTrue(saved)
        request = self.storage.get_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id])
        await self.storage.set_deep_item([overview_corrections.OVERVIEW_CORRECTION_REQUESTS_KEY, request_id, "payload", "staged_file_path"], "changed")
        result = await overview_corrections.execute_correction_request(request)
        self.assertFalse(result["ok"])
        self.assertEqual(self.storage.get_item("P_over_data")["files"]["f"]["content"], "original.pdf")

    async def test_copy_collision_and_io_failure_do_not_damage_existing_files(self):
        source, target = self.root / "source.pdf", self.root / "target.pdf"
        source.write_bytes(b"new upload")
        target.write_bytes(b"existing file")
        new_target = self.root / "new.pdf"
        with patch.object(overview_file_storage.shutil, "copyfileobj", side_effect=OSError("模拟磁盘故障")):
            with self.assertRaises(OSError):
                await overview_corrections.publish_correction_media({}, source, self.root, new_target.name, hashlib.sha256(b"new upload").hexdigest())
        self.assertFalse(new_target.exists())
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b"existing file")

    async def legacy_approval(self, *, existing=False, locked=True):
        formal = self.root / "existing.pdf"
        if existing:
            formal.write_bytes(b"existing file must survive")
        staging = self.root / "legacy-staging"
        staging.mkdir()
        self.stack.enter_context(patch.object(overview_corrections, "OVERVIEW_CORRECTION_STAGING_DIR", staging))
        staged = staging / "staged.pdf"
        staged.write_bytes(b"new upload")
        request = {"config": {"upload_path": str(self.root)}, "project_name": "P", "new_content": formal.name,
                   "chip_type": "file", "temp_file_path": str(staged), "label": "files", "chip_id": "f", "action": "modify"}
        await self.storage.set_item("P_over_data", {"files": {"f": {"content": "original.pdf"}}})
        if locked:
            await self.lock("files", "f")
        self.app.storage.general = {"overview_change_requests": {"request": request}}
        source = Path("src/pages/information.py").read_text(encoding="utf-8")
        handler = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_approve")
        archive = AsyncMock()
        namespace = {"__name__": "src.pages.information", "__package__": "src.pages", "Path": Path, "copy": copy, "app": self.app,
                     "current_role": "reviewer", "current_user": "reviewer", "can_review_overview_correction": lambda *args: True,
                     "db_storage": self.storage, "overview_storage": overview_storage, "ui": SimpleNamespace(notify=self.notify),
                     "handle_archive": archive, "FILES_URL_DIR": "/files"}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "src/pages/information.py", "exec"), namespace)
        await namespace["handle_approve"]("request", request)
        published_path = self.storage.get_deep_item(["P_over_data", "files", "f", "local_file_path"])
        if published_path:
            formal = Path(published_path)
        return formal, staged, archive

    async def test_legacy_failure_does_not_delete_preexisting_file(self):
        formal, staged, archive = await self.legacy_approval(existing=True)
        self.assertTrue(formal.exists())
        self.assertEqual(formal.read_bytes(), b"existing file must survive")
        self.assertTrue(staged.exists())
        archive.assert_not_awaited()

    async def test_legacy_failure_keeps_formal_file(self):
        formal, staged, archive = await self.legacy_approval()
        self.assertEqual(formal.read_bytes(), b"new upload")
        self.assertTrue(staged.exists())
        archive.assert_not_awaited()

    async def test_legacy_success_preserves_normal_workflow(self):
        formal, staged, archive = await self.legacy_approval(locked=False)
        self.assertEqual(formal.read_bytes(), b"new upload")
        self.assertFalse(staged.exists())
        archive.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
