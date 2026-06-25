"""样品问题收集模块的数据和并发写入回归测试。"""

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
DB_STORAGE_PATH = ROOT_DIR / "src" / "db_storage.py"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def load_isolated_db_storage(module_name: str, db_path: Path) -> Any:
    """加载一份拥有独立连接和缓存的 db_storage。"""
    spec = importlib.util.spec_from_file_location(module_name, DB_STORAGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载数据库模块：{DB_STORAGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setattr(module, "DB_PATH", str(db_path))
    return module


class SampleIssueCollectionDataTests(unittest.TestCase):
    def test_status_and_pending_extension_filter(self):
        """状态由对策区块填写进度推导，延期申请中作为跨状态筛选项。"""
        from src.pages import sample_issue_collection as sample_issue

        issue = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
        issue["basic_info"]["product_model"] = "MODEL-A"
        issue["countermeasure"]["owner"] = "李四"

        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "问题录入")

        issue["countermeasure"]["reason_analysis"] = "装配定位偏差"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "对策填写中")

        issue["countermeasure"]["temporary_action"] = "临时加检"
        issue["countermeasure"]["corrective_preventive_action"] = "优化治具定位"
        issue["countermeasure"]["due_date"] = "2026-07-01"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "措施执行中")

        issue["countermeasure"]["extension_requests"] = [{"id": "ext-1", "status": "待审批"}]
        self.assertTrue(sample_issue.sample_issue_matches_filter(issue, "延期申请中"))

    def test_dashboard_pending_count(self):
        """对策责任人看未填完/待延期问题，审批角色看待审批延期申请。"""
        from src.pages import sample_issue_collection as sample_issue

        all_issues = {
            "SPI-1": {
                "issue_id": "SPI-1",
                "countermeasure": {
                    "owner": "李四",
                    "reason_analysis": "",
                    "temporary_action": "",
                    "corrective_preventive_action": "",
                    "due_date": "",
                    "extension_requests": [],
                },
            },
            "SPI-2": {
                "issue_id": "SPI-2",
                "countermeasure": {
                    "owner": "李四",
                    "reason_analysis": "原因",
                    "temporary_action": "临时",
                    "corrective_preventive_action": "纠正",
                    "due_date": "2026-07-01",
                    "extension_requests": [],
                },
            },
            "SPI-3": {
                "issue_id": "SPI-3",
                "countermeasure": {
                    "owner": "QE工程师",
                    "reason_analysis": "原因",
                    "temporary_action": "临时",
                    "corrective_preventive_action": "纠正",
                    "due_date": "2026-07-01",
                    "extension_requests": [{"id": "ext-1", "status": "待审批"}],
                },
            },
        }

        self.assertEqual(sample_issue.get_sample_dashboard_pending_count(all_issues, "李四", "测试工程师"), 1)
        self.assertEqual(sample_issue.get_sample_dashboard_pending_count(all_issues, "王五", "QE工程师"), 1)
        self.assertEqual(sample_issue.get_sample_dashboard_pending_count(all_issues, "经理", "研发经理"), 1)


class SampleIssueCollectionConfigTests(unittest.TestCase):
    def test_project_config_has_required_business_values(self):
        """项目实际使用的样品问题 JSON 应能生成一份可运行的配置。"""
        from src import sample_issue_config

        config = sample_issue_config.SAMPLE_ISSUE_CONFIG

        self.assertTrue(config["public_base_url"].startswith("http"))
        self.assertTrue(config["editor_roles"])
        self.assertEqual(config["filter_states"][0], "全部")
        self.assertIn("延期申请中", config["filter_states"])
        self.assertTrue(config["wecom"]["default_notify_targets"])
        self.assertTrue(config["wecom"]["extension"]["approver_roles"])
        self.assertTrue(config["wecom"]["extension"]["approval_notify_targets"])
        self.assertTrue(config["wecom"]["extension"]["notify_requester_on_approval"])
        self.assertTrue(sample_issue_config.SAMPLE_ISSUE_CONFIG_PATH.exists())

    def test_invalid_fields_fall_back_independently(self):
        """坏字段应逐项回退；合法字段继续生效。"""
        from src import sample_issue_config

        test_config = {
            "public_base_url": "",
            "editor_roles": "invalid",
            "filter_states": ["措施执行中"],
            "wecom": {
                "default_notify_targets": "invalid",
                "extension": {
                    "approver_roles": ["样品经理"],
                    "notify_requester_on_approval": "false",
                },
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sample_issue_collection_config.json"
            config_path.write_text(json.dumps(test_config, ensure_ascii=False), encoding="utf-8")
            with patch.object(sample_issue_config, "SAMPLE_ISSUE_CONFIG_PATH", config_path):
                loaded = sample_issue_config.load_sample_issue_config()

        self.assertEqual(loaded["public_base_url"], "http://192.168.1.102:8080")
        self.assertEqual(loaded["editor_roles"], ["研发经理", "admin", "研发助理"])
        self.assertEqual(loaded["filter_states"], ["全部", "措施执行中", "延期申请中"])
        self.assertEqual(loaded["wecom"]["default_notify_targets"], [{"position": "研发经理"}])
        self.assertEqual(loaded["wecom"]["extension"]["approver_roles"], ["样品经理"])
        self.assertTrue(loaded["wecom"]["extension"]["notify_requester_on_approval"])


class SampleIssueCollectionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_save_is_rejected(self):
        """旧表单保存不能覆盖其他用户已经写入的新数据。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_db_storage",
                Path(temp_dir) / "sample_issue.db",
            )
            try:
                await isolated_db.init_db()
                from src.pages import sample_issue_collection as sample_issue

                original_db_storage = sample_issue.db_storage
                sample_issue.db_storage = isolated_db
                try:
                    draft = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
                    draft["issue_id"] = "SPI-RACE"
                    draft["basic_info"].update(
                        {
                            "product_model": "MODEL-A",
                            "issue_description": "样机点亮异常",
                            "sample_order_no": "SAMPLE-001",
                            "record_date": "2026-06-23",
                            "assembled_qty": "10",
                            "issue_qty": "2",
                            "recorder_name": "张三",
                        }
                    )
                    draft["countermeasure"]["owner"] = "李四"

                    created = await sample_issue.save_sample_issue_record(
                        draft,
                        "张三",
                        "测试工程师",
                        is_new=True,
                    )
                    self.assertTrue(created.changed)
                    self.assertIsNotNone(created.record)
                    stale_copy = copy.deepcopy(created.record)

                    def fill_countermeasure(record):
                        record["countermeasure"]["reason_analysis"] = "连接器接触不良"
                        return "updated", record

                    updated = await sample_issue.atomic_sample_issue_update("SPI-RACE", fill_countermeasure)
                    self.assertTrue(updated.changed)

                    assert stale_copy is not None
                    stale_copy["basic_info"]["product_model"] = "STALE-MODEL"
                    rejected = await sample_issue.save_sample_issue_record(
                        stale_copy,
                        "张三",
                        "测试工程师",
                        is_new=False,
                    )
                    self.assertEqual(rejected.code, "revision_conflict")

                    stored = isolated_db.get_deep_item([sample_issue.SAMPLE_ISSUE_DATA_KEY, "SPI-RACE"])
                    self.assertEqual(stored["basic_info"]["product_model"], "MODEL-A")
                    self.assertEqual(stored["countermeasure"]["reason_analysis"], "连接器接触不良")
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()


if __name__ == "__main__":
    unittest.main()
