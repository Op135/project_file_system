import asyncio
import copy
import unittest
from datetime import datetime
from unittest.mock import patch

from src import db_storage
from src.overview_batch_operations import (
    apply_related_overview_impacts,
    build_batch_result_lines,
    build_new_overview_chip,
    build_project_category_map,
    build_project_model_range_options,
    collect_editable_overview_configs,
    filter_batch_projects,
    find_projects_without_row_anchors,
    get_chip_state_visuals,
    insert_overview_chip,
    update_overview_chip_state,
)
from src.utils import format_overview_timestamp, parse_overview_timestamp


class OverviewBatchOperationTests(unittest.TestCase):
    def test_batch_result_lines_include_every_skipped_and_failed_item(self):
        skipped = [f"P{index}：相同概述内容已存在" for index in range(1, 6)]
        failed = ["P6：校验失败", "P7：写入失败"]
        lines = build_batch_result_lines(2, skipped, failed)
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[0], "批量处理完成：成功 2 项，跳过 5 项，失败 2 项。")
        self.assertEqual(lines[5], "跳过｜P5：相同概述内容已存在")
        self.assertEqual(lines[-1], "失败｜P7：写入失败")

    def test_all_selected_projects_must_have_table_row_anchors(self):
        projects = ["P1", "P2", "P3"]
        anchors = {"P1": "row-1", "P2": None}
        self.assertEqual(find_projects_without_row_anchors(projects, anchors), ["P2", "P3"])

    def test_overview_timestamp_accepts_legacy_microseconds_and_displays_seconds(self):
        value = "2026-08-14 18:13:45.171106"
        self.assertEqual(parse_overview_timestamp(value).microsecond, 171106)
        self.assertEqual(format_overview_timestamp(value), "2026-08-14 18:13:45")

    def test_project_filter_uses_status_major_and_subcategory(self):
        summary = {
            "a": {"sub_project": "内部型号-A", "project": "RFFM-1519-A", "state": "研发"},
            "a2": {"sub_project": "内部型号-A2", "project": "RFFM-1519-B", "state": "转产"},
            "b": {"sub_project": "内部型号-B", "project": "RFFM-1699-A", "state": "转产"},
            "c": {"sub_project": "内部型号-C", "project": "RFFM-1518-B", "state": "量产"},
            "d": {"sub_project": "内部型号-D", "project": "RM3000", "state": "待定"},
        }
        categories = build_project_category_map(item["project"] for item in summary.values())
        self.assertEqual(categories["RFFM"], ["所有", "16", "15"])
        self.assertEqual(
            filter_batch_projects(summary, ["研发", "转产"], "RFFM", "15"),
            ["内部型号-A", "内部型号-A2"],
        )
        self.assertEqual(
            build_project_model_range_options(
                (item["project"] for item in summary.values()),
                "RFFM",
                "15",
            ),
            ["所有", "RFFM-1519", "RFFM-1518"],
        )
        self.assertEqual(
            filter_batch_projects(summary, ["研发", "转产"], "RFFM", "15", "RFFM-1519"),
            ["内部型号-A", "内部型号-A2"],
        )
        self.assertEqual(filter_batch_projects(summary, ["待定"], "其它", "所有"), ["内部型号-D"])

    def test_only_configs_editable_by_current_role_are_exposed(self):
        config = {
            "结构": {
                "图纸": [
                    {"label": "a", "permission": {"edit_role": ["研发结构"]}},
                    {"label": "b", "permission": {"edit_role": ["研发经理"]}},
                ]
            }
        }
        result = collect_editable_overview_configs(config, "研发结构", {"图纸": "OverviewTableGroup"})
        self.assertEqual([item["label"] for item in result], ["a"])
        self.assertTrue(result[0]["is_table_group"])
        self.assertEqual(result[0]["first_col_label"], "a")

    def test_new_chip_and_state_visuals_match_single_item_rules(self):
        config = {"role": "结构", "processing_type": "image", "is_table_group": True}
        chip = build_new_overview_chip(
            project="P1",
            config=config,
            content="a.png",
            notes="批量添加",
            creator="张三",
            req_max_ver="2.0",
            row_id="row-1",
            extra_data={"file_type": "image/png", "url_path": "/files/a.png"},
        )
        self.assertEqual(chip["icon"], "image")
        self.assertEqual(chip["row_id"], "row-1")
        self.assertEqual(chip["select_activ_dic"], {"0.0": False, "1.0": False, "2.0": True})
        datetime.strptime(next(iter(chip["timestamp"])), "%Y-%m-%d %H:%M:%S")
        self.assertEqual(get_chip_state_visuals("image", None), ("question_mark", None, "bg-amber-5"))
        self.assertEqual(get_chip_state_visuals("image", False), ("block", False, "bg-grey-5"))

    def test_atomic_insert_rejects_duplicate_content(self):
        stored = {"old": {"id": "old", "type": "text", "content": "相同内容"}}

        async def fake_atomic(_path, updater, *args, **kwargs):
            result = updater(copy.deepcopy(stored), *args, **kwargs)
            if result is not db_storage.ATOMIC_NO_UPDATE:
                stored.clear()
                stored.update(result)
            return True

        candidate = {"id": "new", "type": "text", "content": "相同内容"}
        with patch("src.overview_batch_operations.db_storage.atomic_deep_update", new=fake_atomic):
            inserted, message = asyncio.run(insert_overview_chip("P1", "label", candidate))
        self.assertFalse(inserted)
        self.assertIn("已存在", message)
        self.assertNotIn("new", stored)

    def test_table_text_allows_same_content_in_a_different_row(self):
        stored = {
            "old": {"id": "old", "type": "text", "content": "相同内容", "row_id": "row-1"}
        }

        async def fake_atomic(_path, updater, *args, **kwargs):
            result = updater(copy.deepcopy(stored), *args, **kwargs)
            if result is not db_storage.ATOMIC_NO_UPDATE:
                stored.clear()
                stored.update(result)
            return True

        candidate = {"id": "new", "type": "text", "content": "相同内容", "row_id": "row-2"}
        with patch("src.overview_batch_operations.db_storage.atomic_deep_update", new=fake_atomic):
            inserted, _ = asyncio.run(insert_overview_chip("P1", "label", candidate))
        self.assertTrue(inserted)
        self.assertIn("new", stored)

    def test_state_update_preserves_versions_and_adds_current_version(self):
        stored = {
            "id": "chip-1",
            "type": "image",
            "content": "a.png",
            "enabled": True,
            "select_activ_dic": {"1.0": True},
            "timestamp": {},
        }

        async def fake_atomic(_path, updater, *args, **kwargs):
            nonlocal stored
            result = updater(copy.deepcopy(stored), *args, **kwargs)
            if result is not db_storage.ATOMIC_NO_UPDATE:
                stored = result
            return True

        with patch("src.overview_batch_operations.db_storage.atomic_deep_update", new=fake_atomic):
            changed, _, updated = asyncio.run(
                update_overview_chip_state("P1", "label", "chip-1", "2.0", None, "张三")
            )
        self.assertTrue(changed)
        self.assertEqual(updated["select_activ_dic"], {"1.0": True, "2.0": None})
        self.assertEqual(updated["icon"], "question_mark")
        self.assertIsNone(updated["enabled"])
        datetime.strptime(next(reversed(updated["timestamp"])), "%Y-%m-%d %H:%M:%S")

    def test_related_impact_records_active_and_already_pending_targets(self):
        overview = {
            "target": {
                "active": {"select_activ_dic": {"2.0": True}, "enabled": True},
                "pending": {"select_activ_dic": {"2.0": None}, "enabled": None},
                "inactive": {"select_activ_dic": {"2.0": False}, "enabled": False},
            }
        }
        records = {}

        def fake_get_item(_key, _default=None):
            return copy.deepcopy(overview)

        async def fake_atomic(path, updater, *args, **kwargs):
            if path[0].endswith("_over_data"):
                chip = overview[path[1]][path[2]]
                result = updater(copy.deepcopy(chip), *args, **kwargs)
                if result is not db_storage.ATOMIC_NO_UPDATE:
                    overview[path[1]][path[2]] = result
                return True
            key = tuple(path[1:4])
            result = updater(copy.deepcopy(records.get(key)), *args, **kwargs)
            if result is not db_storage.ATOMIC_NO_UPDATE:
                records[key] = result
            return True

        with (
            patch("src.overview_batch_operations.db_storage.get_item", new=fake_get_item),
            patch("src.overview_batch_operations.db_storage.atomic_deep_update", new=fake_atomic),
        ):
            changed_labels = asyncio.run(
                apply_related_overview_impacts(
                    project="P1",
                    related_labels=["target"],
                    source_content="来源项",
                    source_state=True,
                    operation_type="add_chip",
                    creator="张三",
                    config_flat={"target": {"role": "结构"}},
                    overview_role={"P1": {"结构": {"latest_user": "结构：李四"}}},
                )
            )

        self.assertEqual(changed_labels, {"target"})
        self.assertIsNone(overview["target"]["active"]["select_activ_dic"]["2.0"])
        self.assertIn(("target", "active", "open"), records)
        self.assertIn(("target", "pending", "open"), records)
        self.assertNotIn(("target", "inactive", "open"), records)


if __name__ == "__main__":
    unittest.main()
