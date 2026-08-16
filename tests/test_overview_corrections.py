import asyncio
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from src import db_storage
from src.overview_corrections import (
    build_correction_changes,
    build_media_file_audit,
    build_test_field_changes,
    can_review_correction_request,
    chip_snapshot_fingerprint,
    execute_correction_request,
    get_correction_pending_count,
    get_correction_reviewer_roles,
    validate_test_correction,
)
from src.utils import build_overview_page_url


class OverviewCorrectionTests(unittest.TestCase):
    def test_test_diff_keeps_unchanged_fields_visible(self):
        config = {
            "test_nature_options": ["功能"],
            "state_options": ["常温", "高温"],
            "node_options": ["成品"],
            "instrument_options": ["万用表", "示波器"],
        }
        before = {
            "test_nature_select": "功能",
            "state_select": "常温",
            "node_select": "成品",
            "instrument_select": "万用表",
        }
        after = {**before, "state_select": "高温"}

        changes = build_test_field_changes(before, after, config)

        self.assertEqual([change["title"] for change in changes], ["测试性质", "条件/状态", "节点/位置", "工具/仪器/治具"])
        self.assertEqual([change["changed"] for change in changes], [False, True, False, False])
        self.assertEqual(changes[-1]["before_select"], "万用表")
        self.assertEqual(changes[-1]["after_select"], "万用表")

    def test_test_validation_requires_other_text(self):
        config = {"instrument_options": ["其它"]}
        valid, message = validate_test_correction(
            {"instrument_select": "其它", "instrument_other_text": ""},
            config,
        )
        self.assertFalse(valid)
        self.assertIn("特殊要求", message)

    def test_reviewer_mapping_disallows_self_review_and_counts_tasks(self):
        request = {
            "submitter": "结构甲",
            "submitter_role": "研发结构",
            "reviewer_roles": ["研发经理"],
            "status": "pending",
        }
        self.assertEqual(get_correction_reviewer_roles("研发结构"), ["研发经理"])
        self.assertTrue(can_review_correction_request(request, "经理乙", "研发经理"))
        self.assertFalse(can_review_correction_request(request, "结构甲", "研发经理"))
        self.assertEqual(get_correction_pending_count({"r1": request}, "经理乙", "研发经理"), 1)

    def test_fingerprint_and_content_diff_detect_actual_change(self):
        before = {"id": "c1", "type": "text", "content": "旧内容"}
        after = {**before, "content": "新内容"}
        self.assertNotEqual(chip_snapshot_fingerprint(before), chip_snapshot_fingerprint(after))
        changes = build_correction_changes(before, after, {}, "correct")
        self.assertTrue(changes[0]["changed"])

    def test_overview_return_url_keeps_correction_target(self):
        url = build_overview_page_url(
            review=False,
            overview_file_path=r"F:\项目资料\RFFM-1007-A.json",
            correction_label="结构/材料",
            correction_chip_id="chip 01",
        )

        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["type"], ["overview"])
        self.assertEqual(query["json_path"], [r"F:\项目资料\RFFM-1007-A.json"])
        self.assertEqual(query["correction_label"], ["结构/材料"])
        self.assertEqual(query["correction_chip_id"], ["chip 01"])

    def test_media_audit_records_original_and_staged_file_hashes(self):
        with tempfile.TemporaryDirectory() as temp_value:
            temp_root = Path(temp_value)
            upload_dir = temp_root / "uploads"
            staging_dir = temp_root / "staging"
            request_dir = staging_dir / "request-1"
            upload_dir.mkdir()
            request_dir.mkdir(parents=True)
            original_path = upload_dir / "old.png"
            staged_path = request_dir / "new.png"
            original_path.write_bytes(b"old-image")
            staged_path.write_bytes(b"new-image")

            with patch("src.overview_corrections.OVERVIEW_CORRECTION_STAGING_DIR", staging_dir):
                valid, message, audit = build_media_file_audit(
                    {"content": "old.png"},
                    {"upload_path": str(upload_dir)},
                    str(staged_path),
                )

            self.assertTrue(valid, message)
            self.assertEqual(audit["original_file_sha256"], hashlib.sha256(b"old-image").hexdigest())
            self.assertEqual(audit["staged_file_sha256"], hashlib.sha256(b"new-image").hexdigest())

    def test_text_correction_preserves_normal_record_fields(self):
        before = {
            "id": "c1",
            "type": "text",
            "content": "错误文本",
            "notes": "原录入注释",
            "creator": "原操作人",
            "req_ver": "2.0",
            "enabled": True,
            "select_activ_dic": {"2.0": True},
            "timestamp": {"2026-01-01 10:00:00": {"creator": "原操作人"}},
        }
        after = {**before, "content": "正确文本"}
        stored = {"label-1": {"c1": copy.deepcopy(before)}}
        config = {
            "label": "label-1",
            "processing_type": "text",
            "content_regular": [],
            "permission": {"edit_role": ["研发结构"]},
        }
        request = {
            "project": "P1",
            "label": "label-1",
            "chip_id": "c1",
            "action": "correct",
            "submitter_role": "研发结构",
            "payload": {
                "before_snapshot": before,
                "before_fingerprint": chip_snapshot_fingerprint(before),
                "after_snapshot": after,
                "config": config,
            },
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(
                general={
                    "over_config_data_flat": {"label-1": config},
                    "project_summary": {"P1": {"state": "研发"}},
                }
            )
        )

        async def fake_atomic(path, updater, *args, **kwargs):
            nonlocal stored
            self.assertEqual(path, ["P1_over_data"])
            result = updater(copy.deepcopy(stored), *args, **kwargs)
            if result is db_storage.ATOMIC_NO_UPDATE:
                return True
            stored = result
            return True

        with (
            patch("src.overview_corrections.app", fake_app),
            patch("src.overview_corrections.db_storage.atomic_deep_update", new=fake_atomic),
            patch("src.components.OverviewVersionManager.bump"),
        ):
            result = asyncio.run(execute_correction_request(request))

        self.assertTrue(result["ok"])
        corrected = stored["label-1"]["c1"]
        self.assertEqual(corrected["content"], "正确文本")
        for key in ("notes", "creator", "req_ver", "enabled", "select_activ_dic", "timestamp"):
            self.assertEqual(corrected[key], before[key])

    def test_table_first_column_delete_can_remove_the_snapshotted_whole_row_atomically(self):
        first = {"id": "first", "type": "text", "content": "基准", "row_id": "row-1"}
        child = {"id": "child", "type": "text", "content": "子项", "row_id": "row-1"}
        stored = {"first-label": {"first": copy.deepcopy(first)}, "child-label": {"child": copy.deepcopy(child)}}
        config = {
            "label": "first-label",
            "processing_type": "text",
            "permission": {"edit_role": ["研发结构"]},
            "is_table_group": True,
            "first_col_label": "first-label",
        }
        request = {
            "project": "P1",
            "label": "first-label",
            "chip_id": "first",
            "action": "delete",
            "submitter_role": "研发结构",
            "payload": {
                "before_snapshot": first,
                "before_fingerprint": chip_snapshot_fingerprint(first),
                "after_snapshot": None,
                "config": config,
                "delete_targets": [
                    {"label": "first-label", "chip_id": "first", "snapshot": first},
                    {"label": "child-label", "chip_id": "child", "snapshot": child},
                ],
            },
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(general={"over_config_data_flat": {"first-label": config}})
        )

        async def fake_atomic(_path, updater, *args, **kwargs):
            nonlocal stored
            result = updater(copy.deepcopy(stored), *args, **kwargs)
            if result is db_storage.ATOMIC_NO_UPDATE:
                return True
            stored = result
            return True

        with (
            patch("src.overview_corrections.app", fake_app),
            patch("src.overview_corrections.db_storage.atomic_deep_update", new=fake_atomic),
            patch("src.components.OverviewVersionManager.bump"),
        ):
            result = asyncio.run(execute_correction_request(request))

        self.assertTrue(result["ok"])
        self.assertEqual(stored["first-label"], {})
        self.assertEqual(stored["child-label"], {})
        self.assertEqual(len(result["deleted_snapshots"]), 2)


if __name__ == "__main__":
    unittest.main()
