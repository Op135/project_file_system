# -*- encoding: utf-8 -*-
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.pages import statistics


class OverviewCompletionClassificationTests(unittest.TestCase):
    def test_unassigned_owner_has_the_highest_priority(self):
        self.assertEqual(
            statistics.classify_overview_completion({"缺必填", "有待定"}, has_unassigned_owner=True),
            "存在概述无负责人",
        )

    def test_existing_categories_keep_their_priority(self):
        self.assertEqual(statistics.classify_overview_completion({"缺必填", "有待定"}), "存在缺必填")
        self.assertEqual(statistics.classify_overview_completion({"有待定", "缺需填"}), "无缺必填有待定")
        self.assertEqual(statistics.classify_overview_completion({"缺需填"}), "仅缺需填")
        self.assertEqual(statistics.classify_overview_completion(set()), "概述已完成")


class OverviewManagementSnapshotTests(unittest.TestCase):
    def test_projects_are_deduplicated_and_completion_uses_the_users_scope(self):
        overview_role = {
            "P1": {
                "光学": {"latest_user": "最近：张三"},
                "结构": {"latest_user": "最近指定：张三"},
            },
            "P2": {"光学": {"latest_user": "张三"}},
            "P3": {"光学": {"latest_user": "李四"}},
            "P4": {"光学": {"latest_user": "——"}},
        }
        pending_data = {
            "张三": {"P1": {"optional": "缺需填"}},
            "王五": {"P2": {"required": "有待定"}},
            "待定负责人": {"P3": {"required": "缺必填"}},
        }

        snapshot = statistics.build_overview_management_snapshot(overview_role, pending_data)

        self.assertEqual(snapshot["张三"]["managed_projects"], ["P1", "P2"])
        self.assertEqual(snapshot["张三"]["completed_projects"], ["P1", "P2"])
        self.assertEqual(snapshot["张三"]["incomplete_projects"], [])
        self.assertEqual(snapshot["李四"]["completed_projects"], ["P3"])
        self.assertEqual(snapshot["李四"]["incomplete_projects"], [])


class DailyStatisticsPersistenceTests(unittest.TestCase):
    def test_management_counts_are_written_once_per_day(self):
        project_summary = {
            "P1": {"state": "研发"},
            "P2": {"state": "研发"},
            "P3": {"state": "转产"},
        }
        overview_role = {
            "P1": {"光学": {"latest_user": "最近：张三"}},
            "P2": {"光学": {"latest_user": "张三"}},
            "P3": {"光学": {"latest_user": "李四"}},
        }
        pending_data = {
            "张三": {"P1": {"optional": "缺需填"}, "P2": {"required": "有待定"}},
            "待定负责人": {"P3": {"required": "缺必填"}},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            stats_file = Path(temp_dir) / "daily_project_stats.xlsx"
            old_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            pd.DataFrame(
                [
                    {
                        "日期": old_date,
                        "用户": "历史用户",
                        "项目状态": "研发",
                        "缺必填数": 1,
                        "有待定数": 0,
                        "缺需填数": 0,
                    }
                ]
            ).to_excel(stats_file, index=False)

            with patch.object(statistics, "STATS_FILE", str(stats_file)):
                statistics.record_daily_stats(project_summary, pending_data, overview_role)
                statistics.record_daily_stats(project_summary, pending_data, overview_role)

            result = pd.read_excel(stats_file)
            today = datetime.now().strftime("%Y-%m-%d")
            today_rows = result[pd.to_datetime(result["日期"]).dt.strftime("%Y-%m-%d") == today]
            zhang_rows = today_rows[today_rows["用户"] == "张三"]
            li_rows = today_rows[today_rows["用户"] == "李四"]

            self.assertEqual(len(result[result["用户"] == "历史用户"]), 1)
            self.assertEqual(zhang_rows["负责项目数"].sum(), 2)
            self.assertEqual(zhang_rows["填写完成项目数"].sum(), 1)
            self.assertEqual(li_rows["负责项目数"].sum(), 1)
            self.assertEqual(li_rows["填写完成项目数"].sum(), 1)


if __name__ == "__main__":
    unittest.main()
