"""样品问题收集模块的数据和并发写入回归测试。"""

import asyncio
import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch


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

        merged = sample_issue.merge_with_sample_issue_template(
            {"issue_id": "SPI-OLD", "basic_info": {"record_date": "2026-07-01"}, "countermeasure": {}}
        )

        self.assertEqual(merged["basic_info"]["assembly_date"], "2026-07-01")
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

    def test_chinese_date_locale_helper(self):
        """日期控件应能复用中文月份和星期配置。"""
        from src.utils import apply_chinese_date_locale

        class FakeDateElement:
            def __init__(self):
                self.props = {}

        fake_date = FakeDateElement()

        self.assertIs(apply_chinese_date_locale(fake_date), fake_date)
        self.assertEqual(fake_date.props["locale"]["months"][0], "一月")
        self.assertEqual(fake_date.props["locale"]["daysShort"], ["日", "一", "二", "三", "四", "五", "六"])

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

    def test_find_unknown_wecom_names_uses_contacts_cache(self):
        """人员输入校验应识别企业微信通讯录姓名，并跳过允许填写的角色名。"""
        from src import wecom_service

        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "wecom_contacts.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "contacts": [
                            {"userid": "zhangsan", "name": "张三", "is_active": True},
                            {"userid": "disabled", "name": "停用人员", "is_active": False},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.object(wecom_service, "WECOM_CONTACTS_CACHE_PATH", cache_path):
                unknown = asyncio.run(
                    wecom_service.find_unknown_wecom_names(
                        "张三、李四, 李四；停用人员；研发经理",
                        allowed_values=["研发经理"],
                        refresh_if_stale=False,
                    )
                )
                unknown_with_inactive = asyncio.run(
                    wecom_service.find_unknown_wecom_names(
                        "张三、李四；停用人员",
                        refresh_if_stale=False,
                        include_inactive=True,
                    )
                )

        self.assertEqual(unknown, ["李四", "停用人员"])
        self.assertEqual(unknown_with_inactive, ["李四"])


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
        self.assertTrue(config["reminders"]["background_enabled"])
        self.assertTrue(config["reminders"]["rules"])
        self.assertTrue(config["reminders"]["incomplete_rules"])
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
            "reminders": {
                "background_enabled": "true",
                "initial_delay_seconds": 0,
                "check_interval_seconds": "3600",
                "rules": [
                    {"key": "invalid"},
                    {"key": "custom_due_today", "label": "当天提醒", "days_until_due": 0, "enabled": True},
                    {"key": "disabled", "label": "禁用规则", "days_until_due": 1, "enabled": False},
                ],
                "incomplete_rules": [
                    {"key": "bad"},
                    {
                        "key": "custom_incomplete",
                        "label": "录入后2天未完善",
                        "days_since_record": 2,
                        "enabled": True,
                    },
                    {
                        "key": "custom_daily",
                        "label": "超过4天每日提醒",
                        "min_days_since_record": 4,
                        "enabled": True,
                    },
                    {
                        "key": "disabled_incomplete",
                        "label": "停用",
                        "days_since_record": 1,
                        "enabled": False,
                    },
                ],
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
        self.assertTrue(loaded["reminders"]["background_enabled"])
        self.assertEqual(loaded["reminders"]["initial_delay_seconds"], 60)
        self.assertEqual(loaded["reminders"]["check_interval_seconds"], 3600)
        self.assertEqual(
            loaded["reminders"]["rules"],
            [{"key": "custom_due_today", "label": "当天提醒", "days_until_due": 0}],
        )
        self.assertEqual(
            loaded["reminders"]["incomplete_rules"],
            [
                {"key": "custom_incomplete", "label": "录入后2天未完善", "days_since_record": 2},
                {"key": "custom_daily", "label": "超过4天每日提醒", "min_days_since_record": 4},
            ],
        )


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

    async def test_record_date_is_auto_preserved_and_assembly_date_is_editable(self):
        """记录日期由系统维护不可改，组装日期可由录入区块维护。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_assembly_date_db_storage",
                Path(temp_dir) / "sample_issue_assembly_date.db",
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
                            "product_model": "MODEL-DATE",
                            "issue_description": "样机外观异常",
                            "sample_order_no": "SAMPLE-DATE",
                            "record_date": "2020-01-01",
                            "assembly_date": "2026-07-01",
                            "assembled_qty": "4",
                            "issue_qty": "1",
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
                    assert created.record is not None
                    issue_id = created.record["issue_id"]
                    self.assertEqual(created.record["basic_info"]["record_date"], datetime.now().strftime("%Y-%m-%d"))
                    self.assertEqual(created.record["basic_info"]["assembly_date"], "2026-07-01")

                    edited = copy.deepcopy(created.record)
                    edited["basic_info"]["record_date"] = "2020-02-02"
                    edited["basic_info"]["assembly_date"] = "2026-07-02"
                    saved = await sample_issue.save_sample_issue_record(
                        edited,
                        "张三",
                        "测试工程师",
                        is_new=False,
                    )

                    self.assertTrue(saved.changed)
                    assert saved.record is not None
                    self.assertEqual(saved.record["basic_info"]["record_date"], datetime.now().strftime("%Y-%m-%d"))
                    self.assertEqual(saved.record["basic_info"]["assembly_date"], "2026-07-02")
                    stored = isolated_db.get_deep_item([sample_issue.SAMPLE_ISSUE_DATA_KEY, issue_id])
                    self.assertEqual(stored["basic_info"]["record_date"], datetime.now().strftime("%Y-%m-%d"))
                    self.assertEqual(stored["basic_info"]["assembly_date"], "2026-07-02")
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

    async def test_close_request_auto_saves_countermeasure_changes(self):
        """申请关闭时应先保存当前对策表单，再发起关闭申请。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_close_auto_save_db_storage",
                Path(temp_dir) / "sample_issue_close_auto_save.db",
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
                            "product_model": "MODEL-AUTO",
                            "issue_description": "样机电流偏高",
                            "sample_order_no": "SAMPLE-AUTO",
                            "record_date": "2026-07-02",
                            "assembled_qty": "5",
                            "issue_qty": "1",
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
                    assert created.record is not None

                    local_copy = copy.deepcopy(created.record)
                    local_copy["countermeasure"].update(
                        {
                            "reason_analysis": "器件参数偏差",
                            "temporary_action": "临时筛选",
                            "corrective_preventive_action": "调整来料检验标准",
                            "due_date": "2026-07-08",
                        }
                    )

                    requested = await sample_issue.save_and_submit_sample_close_request(
                        local_copy,
                        "李四",
                        "测试工程师",
                    )

                    self.assertTrue(requested.changed)
                    assert requested.record is not None
                    self.assertEqual(sample_issue.calculate_sample_issue_status(requested.record), "关闭申请中")
                    self.assertEqual(requested.record["countermeasure"]["reason_analysis"], "器件参数偏差")
                    self.assertEqual(requested.record["countermeasure"]["due_date"], "2026-07-08")
                    self.assertIsNotNone(sample_issue.get_pending_close_request(requested.record["countermeasure"]))
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

    async def test_sample_issue_reminder_sends_once_and_writes_log(self):
        """样品问题预计完成日期命中规则时应提醒责任人，并按日期去重。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_reminder_db_storage",
                Path(temp_dir) / "sample_issue_reminder.db",
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
                            "product_model": "MODEL-R",
                            "issue_description": "样机温升偏高",
                            "sample_order_no": "SAMPLE-R",
                            "record_date": datetime.now().strftime("%Y-%m-%d"),
                            "assembled_qty": "8",
                            "issue_qty": "2",
                            "recorder_name": "张三",
                        }
                    )
                    draft["countermeasure"].update(
                        {
                            "owner": "李四",
                            "reason_analysis": "散热间隙不足",
                            "temporary_action": "增加温升复测",
                            "corrective_preventive_action": "调整散热结构",
                            "due_date": datetime.now().strftime("%Y-%m-%d"),
                        }
                    )
                    created = await sample_issue.save_sample_issue_record(
                        draft,
                        "张三",
                        "测试工程师",
                        is_new=True,
                    )
                    self.assertTrue(created.changed)
                    assert created.record is not None
                    issue_id = created.record["issue_id"]

                    send_mock = AsyncMock(return_value=(True, "ok"))
                    with (
                        patch.object(
                            sample_issue,
                            "SAMPLE_REMINDER_RULES",
                            [{"key": "due_today", "label": "预计完成日期当天", "days_until_due": 0}],
                        ),
                        patch.object(sample_issue, "retry_failed_wecom_messages", AsyncMock(return_value=(0, 0))),
                        patch.object(sample_issue, "format_people_for_wecom", AsyncMock(return_value="lisi")),
                        patch.object(sample_issue, "send_wecom_text_message", send_mock),
                    ):
                        sent_count, fail_count = await sample_issue.check_and_send_sample_issue_reminders()
                        repeated_sent_count, repeated_fail_count = await sample_issue.check_and_send_sample_issue_reminders()

                    self.assertEqual((sent_count, fail_count), (1, 0))
                    self.assertEqual((repeated_sent_count, repeated_fail_count), (0, 0))
                    self.assertEqual(send_mock.await_count, 1)
                    stored = isolated_db.get_deep_item([sample_issue.SAMPLE_ISSUE_DATA_KEY, issue_id])
                    self.assertEqual(len(stored["reminder_log"]), 1)
                    reminder_entry = next(iter(stored["reminder_log"].values()))
                    self.assertEqual(reminder_entry["state"], "sent")
                    self.assertTrue(reminder_entry["success"])
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

    async def test_incomplete_countermeasure_reminder_is_configurable_and_deduplicated(self):
        """未完善对策时应按记录日期提醒责任人，并按规则和日期去重。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_incomplete_reminder_db_storage",
                Path(temp_dir) / "sample_issue_incomplete_reminder.db",
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
                            "product_model": "MODEL-I",
                            "issue_description": "样机按键失灵",
                            "sample_order_no": "SAMPLE-I",
                            "record_date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                            "assembled_qty": "5",
                            "issue_qty": "1",
                            "recorder_name": "张三",
                        }
                    )
                    draft["countermeasure"].update(
                        {
                            "owner": "李四",
                            "reason_analysis": "按键间隙偏小",
                            "temporary_action": "",
                            "corrective_preventive_action": "",
                            "due_date": "",
                        }
                    )
                    created = await sample_issue.save_sample_issue_record(
                        draft,
                        "张三",
                        "测试工程师",
                        is_new=True,
                    )
                    self.assertTrue(created.changed)
                    assert created.record is not None
                    issue_id = created.record["issue_id"]

                    send_mock = AsyncMock(return_value=(True, "ok"))
                    with (
                        patch.object(
                            sample_issue,
                            "SAMPLE_REMINDER_RULES",
                            [{"key": "due_today", "label": "预计完成日期当天", "days_until_due": 0}],
                        ),
                        patch.object(
                            sample_issue,
                            "SAMPLE_INCOMPLETE_REMINDER_RULES",
                            [{"key": "record_day", "label": "问题录入当天未完善对策", "days_since_record": 0}],
                        ),
                        patch.object(sample_issue, "retry_failed_wecom_messages", AsyncMock(return_value=(0, 0))),
                        patch.object(sample_issue, "format_people_for_wecom", AsyncMock(return_value="lisi")),
                        patch.object(sample_issue, "send_wecom_text_message", send_mock),
                    ):
                        sent_count, fail_count = await sample_issue.check_and_send_sample_issue_reminders()
                        repeated_sent_count, repeated_fail_count = await sample_issue.check_and_send_sample_issue_reminders()

                    self.assertEqual((sent_count, fail_count), (1, 0))
                    self.assertEqual((repeated_sent_count, repeated_fail_count), (0, 0))
                    self.assertEqual(send_mock.await_count, 1)
                    _, _, kwargs = send_mock.mock_calls[0]
                    self.assertEqual(kwargs["message_type"], "sample_issue_incomplete_reminder")
                    self.assertIn("待完善字段：样品临时对策、纠正预防措施、纠正预防措施预计完成日期", send_mock.call_args.args[0])
                    stored = isolated_db.get_deep_item([sample_issue.SAMPLE_ISSUE_DATA_KEY, issue_id])
                    self.assertEqual(len(stored["reminder_log"]), 1)
                    reminder_key = next(iter(stored["reminder_log"]))
                    self.assertIn(":incomplete:record_day:", reminder_key)
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
