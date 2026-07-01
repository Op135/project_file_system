"""样品问题收集模块的数据和并发写入回归测试。"""

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
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
        self.assertEqual(issue["issue_id"], "")
        issue["basic_info"]["product_model"] = "MODEL-A"
        issue["countermeasure"]["owner"] = "李四"

        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "问题录入完毕")

        issue["countermeasure"]["reason_analysis"] = "装配定位偏差"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "问题录入完毕")

        issue["countermeasure"]["temporary_action"] = "临时加检"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "临时对策填写完毕")

        issue["countermeasure"]["corrective_preventive_action"] = "优化治具定位"
        issue["countermeasure"]["due_date"] = "2026-07-01"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "纠正预防措施填写完毕")

        issue["countermeasure"]["corrective_preventive_action"] = ""
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "临时对策填写完毕")

        issue["countermeasure"]["corrective_preventive_action"] = "优化治具定位"
        issue["countermeasure"]["temporary_action"] = ""
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "问题录入完毕")

        issue["countermeasure"]["temporary_action"] = "临时加检"
        issue["countermeasure"]["reason_analysis"] = ""
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "问题录入完毕")

        issue["countermeasure"]["reason_analysis"] = "装配定位偏差"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "纠正预防措施填写完毕")

        issue["countermeasure"]["extension_requests"] = [{"id": "ext-1", "status": "待审批"}]
        self.assertTrue(sample_issue.sample_issue_matches_filter(issue, "延期申请中"))

        issue["countermeasure"]["close_requests"] = [{"id": "close-1", "status": "待审批"}]
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "关闭申请中")
        self.assertTrue(sample_issue.sample_issue_matches_filter(issue, "关闭申请中"))

        issue["countermeasure"]["close_requests"][0]["status"] = "已通过"
        issue["countermeasure"]["closed_at"] = "2026-07-02 09:00:00"
        self.assertEqual(sample_issue.calculate_sample_issue_status(issue), "已关闭")

    def test_template_merge_backfills_evidence_files(self):
        """旧样品问题数据缺少附件字段时应安全补齐。"""
        from src.pages import sample_issue_collection as sample_issue

        merged = sample_issue.merge_with_sample_issue_template({"issue_id": "SPI-OLD", "countermeasure": {}})

        self.assertEqual(merged["countermeasure"]["evidence_files"], [])
        self.assertEqual(merged["countermeasure"]["extension_requests"], [])
        self.assertEqual(merged["countermeasure"]["close_requests"], [])

    def test_sample_attachment_path_uses_uploader_folder(self):
        """样品附件应保存到 uploads/sample_issue/上传人 文件夹中。"""
        from src.components import get_upload_local_path
        from src.pages import sample_issue_collection as sample_issue

        target_path, url_path = sample_issue.get_sample_attachment_storage_paths("李/四", "evidence.pdf")
        target = Path(target_path)

        self.assertEqual(target.name, "evidence.pdf")
        self.assertEqual(target.parent.name, "李_四")
        self.assertEqual(target.parent.parent.name, "sample_issue")
        self.assertIn("/uploads/sample_issue/", url_path)
        self.assertEqual(Path(get_upload_local_path(url_path)).parent.name, "李_四")

    def test_next_sample_issue_id_uses_today_sequence(self):
        """样品问题编号按 SPI年月日三位序列号生成。"""
        from src.pages import sample_issue_collection as sample_issue

        all_issues = {
            "SPI20260701001": {"issue_id": "SPI20260701001"},
            "LEGACY": {"issue_id": "SPI-OLD"},
            "OTHER-DAY": {"issue_id": "SPI20260630009"},
            "MIRRORED": {"issue_id": "SPI20260701003"},
        }

        self.assertEqual(
            sample_issue.get_next_sample_issue_id(all_issues, datetime(2026, 7, 1)),
            "SPI20260701004",
        )

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
            "SPI-4": {
                "issue_id": "SPI-4",
                "countermeasure": {
                    "owner": "王五",
                    "reason_analysis": "原因",
                    "temporary_action": "临时",
                    "corrective_preventive_action": "纠正",
                    "due_date": "2026-07-01",
                    "extension_requests": [],
                    "close_requests": [{"id": "close-1", "status": "待审批"}],
                },
            },
        }

        self.assertEqual(sample_issue.get_sample_dashboard_pending_count(all_issues, "李四", "测试工程师"), 2)
        self.assertEqual(sample_issue.get_sample_dashboard_pending_count(all_issues, "王五", "QE工程师"), 1)
        self.assertEqual(sample_issue.get_sample_dashboard_pending_count(all_issues, "经理", "研发经理"), 2)


class SampleIssueCollectionConfigTests(unittest.TestCase):
    def test_project_config_has_required_business_values(self):
        """项目实际使用的样品问题 JSON 应能生成一份可运行的配置。"""
        from src import sample_issue_config

        config = sample_issue_config.SAMPLE_ISSUE_CONFIG

        self.assertTrue(config["public_base_url"].startswith("http"))
        self.assertTrue(config["editor_roles"])
        self.assertEqual(config["filter_states"][0], "全部")
        self.assertIn("延期申请中", config["filter_states"])
        self.assertIn("关闭申请中", config["filter_states"])
        self.assertIn("已关闭", config["filter_states"])
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
        self.assertEqual(
            loaded["filter_states"],
            ["全部", "纠正预防措施填写完毕", "延期申请中", "关闭申请中", "已关闭"],
        )
        self.assertEqual(loaded["wecom"]["default_notify_targets"], [{"position": "研发经理"}])
        self.assertEqual(loaded["wecom"]["extension"]["approver_roles"], ["样品经理"])
        self.assertTrue(loaded["wecom"]["extension"]["notify_requester_on_approval"])


class SampleIssueCollectionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_delete_preserves_concurrent_record_and_rejects_other_roles(self):
        """admin 删除单张样品问题时应保留其它实例的并发新增，非 admin 不能删除。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "sample_issue_delete.db"
            admin_instance = load_isolated_db_storage("test_sample_issue_delete_admin", db_path)
            other_instance = load_isolated_db_storage("test_sample_issue_delete_other", db_path)
            await admin_instance.init_db()
            await other_instance.init_db()
            try:
                from src.pages import sample_issue_collection as sample_issue

                await admin_instance.set_item(
                    sample_issue.SAMPLE_ISSUE_DATA_KEY,
                    {
                        "DELETE-ME": {"issue_id": "DELETE-ME"},
                        "KEEP-ME": {"issue_id": "KEEP-ME"},
                    },
                )
                original_db_storage = sample_issue.db_storage
                sample_issue.db_storage = admin_instance
                try:
                    forbidden = await sample_issue.delete_sample_issue_record("DELETE-ME", "研发经理")
                    self.assertEqual(forbidden.code, "forbidden")

                    deleted, added = await asyncio.gather(
                        sample_issue.delete_sample_issue_record("DELETE-ME", "admin"),
                        other_instance.atomic_deep_update(
                            [sample_issue.SAMPLE_ISSUE_DATA_KEY, "ADDED-CONCURRENTLY"],
                            lambda _: {"issue_id": "ADDED-CONCURRENTLY"},
                        ),
                    )
                    self.assertTrue(deleted.changed)
                    self.assertEqual(deleted.code, "deleted")
                    self.assertTrue(added)

                    stored = other_instance.get_item(sample_issue.SAMPLE_ISSUE_DATA_KEY, {})
                    self.assertNotIn("DELETE-ME", stored)
                    self.assertIn("KEEP-ME", stored)
                    self.assertIn("ADDED-CONCURRENTLY", stored)
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await admin_instance.close_db()
                await other_instance.close_db()

    async def test_concurrent_create_allocates_unique_daily_ids(self):
        """并发录入样品问题时应分配不重复的当天序列号。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_auto_id_db_storage",
                Path(temp_dir) / "sample_issue_auto_id.db",
            )
            try:
                await isolated_db.init_db()
                from src.pages import sample_issue_collection as sample_issue

                original_db_storage = sample_issue.db_storage
                sample_issue.db_storage = isolated_db
                try:

                    def make_draft(index: int) -> dict:
                        draft = sample_issue.generate_initial_sample_issue_data(f"张三{index}", "测试工程师")
                        draft["basic_info"].update(
                            {
                                "product_model": f"MODEL-{index}",
                                "issue_description": "样机点亮异常",
                                "sample_order_no": f"SAMPLE-{index:03d}",
                                "record_date": "2026-06-23",
                                "assembled_qty": "10",
                                "issue_qty": "2",
                                "recorder_name": f"张三{index}",
                            }
                        )
                        draft["countermeasure"]["owner"] = "李四"
                        return draft

                    left, right = await asyncio.gather(
                        sample_issue.save_sample_issue_record(make_draft(1), "张三1", "测试工程师", is_new=True),
                        sample_issue.save_sample_issue_record(make_draft(2), "张三2", "测试工程师", is_new=True),
                    )

                    self.assertTrue(left.changed)
                    self.assertTrue(right.changed)
                    assert left.record is not None
                    assert right.record is not None
                    issue_ids = sorted([left.record["issue_id"], right.record["issue_id"]])
                    today_prefix = sample_issue.get_sample_issue_id_prefix()
                    self.assertEqual(issue_ids, [f"{today_prefix}001", f"{today_prefix}002"])
                    stored = isolated_db.get_item(sample_issue.SAMPLE_ISSUE_DATA_KEY, {})
                    self.assertEqual(sorted(stored.keys()), issue_ids)
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

    async def test_close_request_and_approval_workflow(self):
        """对策责任人提交关闭申请，审批角色通过后样品问题进入已关闭。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_close_db_storage",
                Path(temp_dir) / "sample_issue_close.db",
            )
            try:
                await isolated_db.init_db()
                from src.pages import sample_issue_collection as sample_issue

                original_db_storage = sample_issue.db_storage
                sample_issue.db_storage = isolated_db
                try:
                    draft = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
                    draft["basic_info"].update(
                        {
                            "product_model": "MODEL-C",
                            "issue_description": "样机装配干涉",
                            "sample_order_no": "SAMPLE-C",
                            "record_date": "2026-06-24",
                            "assembled_qty": "6",
                            "issue_qty": "1",
                            "recorder_name": "张三",
                        }
                    )
                    draft["countermeasure"].update(
                        {
                            "owner": "李四",
                            "reason_analysis": "定位尺寸偏小",
                            "temporary_action": "临时修磨",
                            "corrective_preventive_action": "调整结构间隙",
                            "due_date": "2026-07-02",
                        }
                    )
                    created = await sample_issue.save_sample_issue_record(
                        draft,
                        "张三",
                        "测试工程师",
                        is_new=True,
                    )
                    self.assertTrue(created.changed)
                    self.assertIsNotNone(created.record)
                    assert created.record is not None
                    issue_id = created.record["issue_id"]

                    forbidden = await sample_issue.submit_sample_close_request(
                        issue_id,
                        "王五",
                        "测试工程师",
                    )
                    self.assertEqual(forbidden.code, "permission_changed")

                    requested = await sample_issue.submit_sample_close_request(
                        issue_id,
                        "李四",
                        "测试工程师",
                    )
                    self.assertTrue(requested.changed)
                    assert requested.record is not None
                    self.assertEqual(sample_issue.calculate_sample_issue_status(requested.record), "关闭申请中")
                    close_request = sample_issue.get_pending_close_request(requested.record["countermeasure"])
                    self.assertIsNotNone(close_request)

                    duplicate = await sample_issue.submit_sample_close_request(
                        issue_id,
                        "李四",
                        "测试工程师",
                    )
                    self.assertEqual(duplicate.code, "pending_close")

                    assert close_request is not None
                    no_permission = await sample_issue.approve_sample_close_request(
                        issue_id,
                        close_request["id"],
                        True,
                        "赵六",
                        "测试工程师",
                    )
                    self.assertEqual(no_permission.code, "forbidden")

                    approved = await sample_issue.approve_sample_close_request(
                        issue_id,
                        close_request["id"],
                        True,
                        "经理",
                        "研发经理",
                    )
                    self.assertTrue(approved.changed)
                    assert approved.record is not None
                    self.assertEqual(sample_issue.calculate_sample_issue_status(approved.record), "已关闭")
                    self.assertEqual(approved.record["countermeasure"]["close_note"], "")
                    self.assertEqual(approved.record["countermeasure"]["closed_by"], "经理")
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

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
                    draft["countermeasure"]["evidence_files"] = [
                        {
                            "file_del_bool": False,
                            "file_name": "问题照片",
                            "file_url": "/uploads/sample_issue_SPI-RACE_photo.hash.jpg",
                            "file_name_hash": "sample_issue_SPI-RACE_photo.hash.jpg",
                            "file_name_suffix": "photo.jpg",
                            "file_type": "image/jpeg",
                            "file_lab": "1",
                            "parents_h": 12,
                        }
                    ]

                    created = await sample_issue.save_sample_issue_record(
                        draft,
                        "张三",
                        "测试工程师",
                        is_new=True,
                    )
                    self.assertTrue(created.changed)
                    self.assertIsNotNone(created.record)
                    assert created.record is not None
                    issue_id = created.record["issue_id"]
                    stale_copy = copy.deepcopy(created.record)

                    def fill_countermeasure(record):
                        record["countermeasure"]["reason_analysis"] = "连接器接触不良"
                        return "updated", record

                    updated = await sample_issue.atomic_sample_issue_update(issue_id, fill_countermeasure)
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

                    stored = isolated_db.get_deep_item([sample_issue.SAMPLE_ISSUE_DATA_KEY, issue_id])
                    self.assertEqual(stored["basic_info"]["product_model"], "MODEL-A")
                    self.assertEqual(stored["countermeasure"]["reason_analysis"], "连接器接触不良")
                    self.assertEqual(stored["countermeasure"]["evidence_files"][0]["file_name_suffix"], "photo.jpg")
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()


if __name__ == "__main__":
    unittest.main()
