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
    def test_grid_row_and_columns_keep_dashboard_information(self):
        from src.pages import sample_issue_collection as sample_issue

        issue = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
        issue["issue_id"] = "SPI20260814001"
        issue["basic_info"].update(
            {
                "product_model": "MODEL-A",
                "sample_order_no": "Y26081401",
                "issue_description": "测试问题描述",
            }
        )
        issue["countermeasure"]["owner"] = "张三"

        row = sample_issue.build_sample_issue_grid_row(issue, "张三", "测试工程师")
        columns = sample_issue.get_sample_issue_grid_columns()

        self.assertEqual(row["record_id"], "SPI20260814001")
        self.assertEqual(row["detail_action"], "详情")
        self.assertEqual(row["issue_description"], "测试问题描述")
        self.assertEqual(row["row_tone"], "pending")
        self.assertEqual(columns[0]["field"], "detail_action")
        self.assertFalse(columns[0]["filter"])
        self.assertTrue(all("width" in column for column in columns))
        self.assertTrue(
            all(
                isinstance(cell_style := column.get("cellStyle"), dict)
                and cell_style.get("textAlign") == "center"
                for column in columns
            )
        )

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
        self.assertFalse(sample_issue.sample_issue_matches_filter(issue, "未关闭"))
        self.assertTrue(sample_issue.sample_issue_matches_filter(issue, "已关闭"))

    def test_my_pending_filter_uses_current_user(self):
        """主页角标进入模块后，“我的待办”只保留当前责任人的记录。"""
        from src.pages import sample_issue_collection as sample_issue

        issue = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
        issue["countermeasure"]["owner"] = "李四"

        self.assertTrue(
            sample_issue.sample_issue_matches_filter(
                issue,
                sample_issue.SAMPLE_FILTER_MY_PENDING_STATE,
                "李四",
                "测试工程师",
            )
        )
        self.assertFalse(
            sample_issue.sample_issue_matches_filter(
                issue,
                sample_issue.SAMPLE_FILTER_MY_PENDING_STATE,
                "王五",
                "测试工程师",
            )
        )

    def test_template_merge_backfills_evidence_files(self):
        """旧样品问题数据缺少附件字段时应安全补齐。"""
        from src.pages import sample_issue_collection as sample_issue

        merged = sample_issue.merge_with_sample_issue_template(
            {"issue_id": "SPI-OLD", "basic_info": {"record_date": "2026-07-01"}, "countermeasure": {}}
        )

        self.assertEqual(merged["basic_info"]["assembly_date"], "2026-07-01")
        self.assertEqual(merged["basic_info"]["evidence_files"], [])
        self.assertEqual(merged["countermeasure"]["evidence_files"], [])
        self.assertEqual(merged["countermeasure"]["extension_requests"], [])
        self.assertEqual(merged["countermeasure"]["close_requests"], [])
        self.assertEqual(merged["special_preparation"]["owner_name"], "杨铁华")
        self.assertEqual(merged["special_preparation"]["owner_userid"], "YangTieHua")

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

    def test_attachment_thumbnail_key_allows_independent_section_numbers(self):
        """问题附件和对策附件可以各自显示 1 号，但内部缩略图 key 不能冲突。"""
        from src.pages import sample_issue_collection as sample_issue

        self.assertEqual(sample_issue.get_sample_attachment_thumbnail_key("basic", "1"), "basic:1")
        self.assertEqual(sample_issue.get_sample_attachment_thumbnail_key("countermeasure", "1"), "countermeasure:1")
        self.assertNotEqual(
            sample_issue.get_sample_attachment_thumbnail_key("basic", "1"),
            sample_issue.get_sample_attachment_thumbnail_key("countermeasure", "1"),
        )

    def test_active_attachment_hashes_ignore_deleted_thumbnails(self):
        """已删除但未保存的缩略图不应阻止重新上传同一个文件。"""
        from src.pages import sample_issue_collection as sample_issue

        thumbnail_dic = {
            "basic:1": {"file_information": {"file_name_hash": "same_hash.jpg", "file_del_bool": True}},
            "basic:2": {"file_information": {"file_name_hash": "active_hash.jpg", "file_del_bool": False}},
            "basic:3": {"file_information": {"file_name_hash": "%E9%97%AE%E9%A2%98.jpg", "file_del_bool": False}},
        }

        active_hashes = sample_issue.get_active_attachment_hashes_from_thumbnail_state(thumbnail_dic)

        self.assertNotIn("same_hash.jpg", active_hashes)
        self.assertIn("active_hash.jpg", active_hashes)
        self.assertIn("问题.jpg", active_hashes)

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
        self.assertTrue(sample_issue.is_sample_issue_pending_for_user(all_issues["SPI-1"], "李四", "测试工程师"))
        self.assertFalse(sample_issue.is_sample_issue_pending_for_user(all_issues["SPI-3"], "李四", "测试工程师"))
        self.assertTrue(sample_issue.is_sample_issue_pending_for_user(all_issues["SPI-3"], "经理", "研发经理"))
        self.assertFalse(sample_issue.is_sample_issue_pending_for_user(all_issues["SPI-1"], "经理", "研发经理"))

        pending = {**all_issues["SPI-1"], "updated_at": "2026-01-01 00:00:00"}
        normal = {**all_issues["SPI-3"], "updated_at": "2026-12-31 00:00:00"}
        records = sorted(
            [normal, pending],
            key=lambda item: sample_issue.get_sample_issue_card_sort_key(item, "李四", "测试工程师"),
            reverse=True,
        )
        self.assertEqual(records[0]["issue_id"], "SPI-1")

    def test_reviewer_sees_overdue_without_request_as_second_priority(self):
        """评审角色应凸显逾期未申请事项，但仍排在本人待审批事项之后。"""
        from src.pages import sample_issue_collection as sample_issue

        reference_date = datetime(2026, 7, 17).date()
        pending = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
        pending.update({"issue_id": "SPI-PENDING", "updated_at": "2026-01-01 00:00:00"})
        pending["countermeasure"].update(
            {
                "owner": "张三",
                "due_date": "2026-07-10",
                "extension_requests": [{"id": "ext-1", "status": "待审批"}],
            }
        )

        overdue = sample_issue.generate_initial_sample_issue_data("李四", "测试工程师")
        overdue.update({"issue_id": "SPI-OVERDUE", "updated_at": "2026-02-01 00:00:00"})
        overdue["countermeasure"].update({"owner": "李四", "due_date": "2026-07-10"})

        normal = sample_issue.generate_initial_sample_issue_data("王五", "测试工程师")
        normal.update({"issue_id": "SPI-NORMAL", "updated_at": "2026-12-31 00:00:00"})
        normal["countermeasure"].update({"owner": "王五", "due_date": "2026-07-20"})

        self.assertTrue(
            sample_issue.is_sample_issue_overdue_without_request_for_reviewer(
                overdue, "研发经理", reference_date
            )
        )
        self.assertFalse(
            sample_issue.is_sample_issue_overdue_without_request_for_reviewer(
                overdue, "测试工程师", reference_date
            )
        )
        self.assertFalse(
            sample_issue.is_sample_issue_overdue_without_request_for_reviewer(
                pending, "研发经理", reference_date
            )
        )
        close_pending = copy.deepcopy(overdue)
        close_pending["countermeasure"]["close_requests"] = [{"id": "close-1", "status": "待审批"}]
        self.assertFalse(
            sample_issue.is_sample_issue_overdue_without_request_for_reviewer(
                close_pending, "研发经理", reference_date
            )
        )

        records = sorted(
            [normal, overdue, pending],
            key=lambda item: sample_issue.get_sample_issue_card_sort_key(
                item, "经理", "研发经理", reference_date
            ),
            reverse=True,
        )
        self.assertEqual(
            [item["issue_id"] for item in records],
            ["SPI-PENDING", "SPI-OVERDUE", "SPI-NORMAL"],
        )

    def test_reviewer_sees_missing_due_date_as_second_priority(self):
        """评审角色应凸显负责人未填预计日期的问题，并排在普通记录之前。"""
        from src.pages import sample_issue_collection as sample_issue

        reference_date = datetime(2026, 7, 17).date()
        pending = sample_issue.generate_initial_sample_issue_data("张三", "测试工程师")
        pending.update({"issue_id": "SPI-PENDING", "updated_at": "2026-01-01 00:00:00"})
        pending["countermeasure"].update(
            {
                "owner": "张三",
                "due_date": "2026-07-20",
                "extension_requests": [{"id": "ext-1", "status": "待审批"}],
            }
        )

        missing_due_date = sample_issue.generate_initial_sample_issue_data("李四", "测试工程师")
        missing_due_date.update({"issue_id": "SPI-MISSING-DUE", "updated_at": "2026-02-01 00:00:00"})
        missing_due_date["countermeasure"].update({"owner": "李四", "due_date": ""})

        normal = sample_issue.generate_initial_sample_issue_data("王五", "测试工程师")
        normal.update({"issue_id": "SPI-NORMAL", "updated_at": "2026-12-31 00:00:00"})
        normal["countermeasure"].update({"owner": "王五", "due_date": "2026-07-20"})

        self.assertTrue(
            sample_issue.is_sample_issue_missing_due_date_for_reviewer(
                missing_due_date, "研发经理"
            )
        )
        self.assertFalse(
            sample_issue.is_sample_issue_missing_due_date_for_reviewer(
                missing_due_date, "测试工程师"
            )
        )
        unassigned = copy.deepcopy(missing_due_date)
        unassigned["countermeasure"]["owner"] = ""
        self.assertFalse(
            sample_issue.is_sample_issue_missing_due_date_for_reviewer(
                unassigned, "研发经理"
            )
        )

        records = sorted(
            [normal, missing_due_date, pending],
            key=lambda item: sample_issue.get_sample_issue_card_sort_key(
                item, "经理", "研发经理", reference_date
            ),
            reverse=True,
        )
        self.assertEqual(
            [item["issue_id"] for item in records],
            ["SPI-PENDING", "SPI-MISSING-DUE", "SPI-NORMAL"],
        )

    def test_closure_nature_catalog_options_are_ranked_and_deduplicated(self):
        """问题性质应优先按独立词库使用次数排序，并用历史数据补齐。"""
        from src.pages import sample_issue_collection as sample_issue

        catalog = {
            "设计问题": {"name": "设计问题", "use_count": 2},
            "process": {"name": "工艺问题", "use_count": 5},
        }
        issues = {
            "SPI-1": {
                "issue_id": "SPI-1",
                "countermeasure": {"closure_nature": "设计问题", "close_requests": []},
            },
            "SPI-2": {
                "issue_id": "SPI-2",
                "countermeasure": {"closure_nature": "物料问题", "close_requests": []},
            },
        }

        self.assertEqual(
            sample_issue.get_sample_closure_nature_options(issues, catalog),
            ["工艺问题", "设计问题", "物料问题"],
        )

    def test_special_owner_candidates_use_wecom_position_and_default_owner(self):
        """特殊准备候选人应来自企业微信 NPI 职位，并把默认负责人排在首位。"""
        from src.pages import sample_issue_collection as sample_issue

        candidates = sample_issue.get_sample_special_owner_candidates(
            {
                "contacts": [
                    {"userid": "Other", "name": "其他人", "position": "测试工程师", "is_active": True},
                    {"userid": "NpiB", "name": "NPI乙", "position": "NPI工程师", "is_active": True},
                    {"userid": "YangTieHua", "name": "杨铁华", "position": "NPI工程师", "is_active": True},
                ]
            }
        )

        self.assertEqual(candidates[0]["userid"], "YangTieHua")
        self.assertEqual([candidate["name"] for candidate in candidates], ["杨铁华", "NPI乙"])

    def test_close_approval_notification_people_include_special_owner_only_after_approval(self):
        """关闭审批通过并转特殊准备时，应同时通知申请人和实际 NPI 负责人。"""
        from src.pages import sample_issue_collection as sample_issue

        issue = {"special_preparation": {"owner_name": "杨铁华"}}
        request = {"requester": "李四", "follow_up_required": True}
        route = {"notify_requester_on_approval": True}

        self.assertEqual(
            sample_issue.get_sample_close_approval_additional_people(issue, request, route, True),
            "李四|杨铁华",
        )
        self.assertEqual(
            sample_issue.get_sample_close_approval_additional_people(issue, request, route, False),
            "李四",
        )

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


class SampleIssueNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_approved_notification_uses_two_stable_permissions(self):
        """延期通过通知应分别解析审批结果和通过追加接收权限。"""
        from src.pages import sample_issue_collection as sample_issue

        resolver = AsyncMock(side_effect=["result_user", "approved_user"])
        sender = AsyncMock(return_value=(True, "ok"))
        with patch.object(sample_issue, "resolve_permission_wecom_recipients", resolver), patch.object(
            sample_issue,
            "send_wecom_text_message",
            sender,
        ):
            result = await sample_issue.send_sample_extension_wecom_message(
                "延期通过",
                issue_id="SPI001",
                business_key="SPI001:extension",
                message_type="extension_approval",
                include_approved_recipients=True,
            )

        self.assertEqual(result, (True, "ok"))
        self.assertEqual(
            [call.args[0] for call in resolver.await_args_list],
            [
                sample_issue.SAMPLE_ISSUE_EXTENSION_RESULT_NOTIFY_PERMISSION,
                sample_issue.SAMPLE_ISSUE_EXTENSION_APPROVED_NOTIFY_PERMISSION,
            ],
        )
        self.assertEqual(sender.await_args.args[1], "result_user|approved_user")

    async def test_electron_close_request_uses_route_specific_notification_permission(self):
        """研发电子类关闭申请不能落入默认关闭通知权限。"""
        from src.pages import sample_issue_collection as sample_issue

        resolver = AsyncMock(return_value="electron_approver")
        sender = AsyncMock(return_value=(True, "ok"))
        with patch.object(sample_issue, "resolve_permission_wecom_recipients", resolver), patch.object(
            sample_issue,
            "send_wecom_text_message",
            sender,
        ):
            await sample_issue.send_sample_extension_wecom_message(
                "电子类关闭申请",
                issue_id="SPI002",
                business_key="SPI002:close",
                message_type="close_request",
                route_key="electron_to_electron",
            )

        self.assertEqual(
            resolver.await_args.args[0],
            sample_issue.SAMPLE_ISSUE_CLOSE_ELECTRON_REQUEST_NOTIFY_PERMISSION,
        )


class SampleIssueCollectionConfigTests(unittest.TestCase):
    def test_project_config_has_required_business_values(self):
        """项目实际使用的样品问题 JSON 应能生成一份可运行的配置。"""
        from src import sample_issue_config

        config = sample_issue_config.SAMPLE_ISSUE_CONFIG

        self.assertTrue(config["public_base_url"].startswith("http"))
        self.assertTrue(config["editor_roles"])
        self.assertEqual(config["filter_states"][0], "全部")
        self.assertEqual(config["filter_states"][1], "未关闭")
        self.assertIn("延期申请中", config["filter_states"])
        self.assertIn("关闭申请中", config["filter_states"])
        self.assertIn("试产前特殊准备", config["filter_states"])
        self.assertIn("已关闭", config["filter_states"])
        self.assertEqual(config["special_preparation"]["owner_role"], "NPI工程师")
        self.assertIn("NPI工程", config["special_preparation"]["owner_role_keywords"])
        self.assertEqual(config["special_preparation"]["default_owner_name"], "杨铁华")
        self.assertEqual(config["special_preparation"]["default_owner_userid"], "YangTieHua")
        self.assertTrue(config["special_preparation"]["default_actions"])
        self.assertTrue(config["wecom"]["default_notify_targets"])
        self.assertTrue(config["wecom"]["extension"]["approver_roles"])
        self.assertTrue(config["wecom"]["extension"]["approval_notify_targets"])
        self.assertTrue(config["wecom"]["extension"]["notify_requester_on_approval"])
        self.assertTrue(config["wecom"]["close"]["approver_roles"])
        self.assertIn("routing_rules", config["wecom"]["close"])
        self.assertIsInstance(config["reminders"]["background_enabled"], bool)
        self.assertEqual(config["reminders"]["check_window"], {"enabled": True, "start": "08:30", "end": "18:30"})
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
                "close": {
                    "approver_roles": ["默认关闭审批"],
                    "routing_rules": [
                        {
                            "key": "pie_to_quality",
                            "requester_role_keywords": ["PIE"],
                            "approver_roles": ["品质经理"],
                            "notify_requester_on_approval": False,
                        },
                        {"key": "bad_rule", "requester_role_keywords": ["QE"]},
                    ],
                },
            },
            "reminders": {
                "background_enabled": "true",
                "initial_delay_seconds": 0,
                "check_interval_seconds": "3600",
                "check_window": {"enabled": True, "start": "bad", "end": "17:30"},
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
            [
                "全部",
                "未关闭",
                "纠正预防措施填写完毕",
                "试产前特殊准备",
                "延期申请中",
                "关闭申请中",
                "已关闭",
            ],
        )
        self.assertEqual(loaded["wecom"]["default_notify_targets"], [{"position": "研发经理"}])
        self.assertEqual(loaded["wecom"]["extension"]["approver_roles"], ["样品经理"])
        self.assertTrue(loaded["wecom"]["extension"]["notify_requester_on_approval"])
        self.assertEqual(loaded["wecom"]["close"]["approver_roles"], ["默认关闭审批"])
        self.assertEqual(loaded["wecom"]["close"]["routing_rules"][0]["key"], "pie_to_quality")
        self.assertEqual(loaded["wecom"]["close"]["routing_rules"][0]["approver_roles"], ["品质经理"])
        self.assertEqual(loaded["wecom"]["close"]["routing_rules"][0]["notify_targets"], [{"position": "品质经理"}])
        self.assertFalse(loaded["wecom"]["close"]["routing_rules"][0]["notify_requester_on_approval"])
        self.assertTrue(loaded["reminders"]["background_enabled"])
        self.assertEqual(loaded["reminders"]["initial_delay_seconds"], 60)
        self.assertEqual(loaded["reminders"]["check_interval_seconds"], 3600)
        self.assertEqual(loaded["reminders"]["check_window"], {"enabled": True, "start": "08:30", "end": "18:30"})
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
    async def asyncSetUp(self):
        """并发测试只验证旧 Excel 兼容路径，隔离主程序已经初始化的数据库用户服务。"""
        from nicegui import app
        from src.pages import sample_issue_collection as sample_issue

        self.original_user_service = getattr(app.state, "user_service", None)
        self.original_can_create_sample_issue = sample_issue.can_create_sample_issue
        self.original_can_view_sample_issue_collection = sample_issue.can_view_sample_issue_collection
        app.state.user_service = None
        sample_issue.can_create_sample_issue = lambda role, username="": True
        sample_issue.can_view_sample_issue_collection = lambda role, username="": True

    async def asyncTearDown(self):
        """恢复主程序用户服务，避免影响后续权限测试。"""
        from nicegui import app
        from src.pages import sample_issue_collection as sample_issue

        app.state.user_service = self.original_user_service
        sample_issue.can_create_sample_issue = self.original_can_create_sample_issue
        sample_issue.can_view_sample_issue_collection = self.original_can_view_sample_issue_collection

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
                        "设计问题",
                    )
                    self.assertEqual(no_permission.code, "forbidden")

                    missing_nature = await sample_issue.approve_sample_close_request(
                        issue_id,
                        close_request["id"],
                        True,
                        "经理",
                        "研发经理",
                    )
                    self.assertEqual(missing_nature.code, "missing_closure_nature")

                    approved = await sample_issue.approve_sample_close_request(
                        issue_id,
                        close_request["id"],
                        True,
                        "经理",
                        "研发经理",
                        "设计问题",
                    )
                    self.assertTrue(approved.changed)
                    assert approved.record is not None
                    self.assertEqual(sample_issue.calculate_sample_issue_status(approved.record), "已关闭")
                    self.assertEqual(approved.record["countermeasure"]["close_note"], "")
                    self.assertEqual(approved.record["countermeasure"]["closed_by"], "经理")
                    self.assertEqual(approved.record["countermeasure"]["closure_nature"], "设计问题")
                    self.assertEqual(
                        sample_issue.get_sample_closure_nature_options({issue_id: approved.record}),
                        ["设计问题"],
                    )
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

    async def test_close_approval_can_transfer_to_npi_special_preparation(self):
        """首次审批可转 NPI 特殊准备，逐项完成后再申请并最终关闭。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_special_preparation_db_storage",
                Path(temp_dir) / "sample_issue_special_preparation.db",
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
                            "product_model": "MODEL-NPI",
                            "issue_description": "试产前需补充准备",
                            "sample_order_no": "SAMPLE-NPI",
                            "record_date": "2026-07-16",
                            "assembled_qty": "3",
                            "issue_qty": "1",
                            "recorder_name": "张三",
                        }
                    )
                    draft["countermeasure"].update(
                        {
                            "owner": "李四",
                            "reason_analysis": "工装和文件尚未固化",
                            "temporary_action": "样品阶段人工确认",
                            "corrective_preventive_action": "试产前完成准备",
                            "due_date": "2026-07-20",
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

                    requested = await sample_issue.submit_sample_close_request(
                        issue_id,
                        "李四",
                        "测试工程师",
                    )
                    assert requested.record is not None
                    first_request = sample_issue.get_pending_close_request(requested.record["countermeasure"])
                    assert first_request is not None

                    missing_actions = await sample_issue.approve_sample_close_request(
                        issue_id,
                        first_request["id"],
                        True,
                        "经理",
                        "研发经理",
                        "流程问题",
                        True,
                        [],
                    )
                    self.assertEqual(missing_actions.code, "missing_follow_up_actions")

                    missing_owner = await sample_issue.approve_sample_close_request(
                        issue_id,
                        first_request["id"],
                        True,
                        "经理",
                        "研发经理",
                        "流程问题",
                        True,
                        ["试产前落实工装治具"],
                    )
                    self.assertEqual(missing_owner.code, "missing_special_owner")

                    transferred = await sample_issue.approve_sample_close_request(
                        issue_id,
                        first_request["id"],
                        True,
                        "经理",
                        "研发经理",
                        "流程问题",
                        True,
                        ["试产前落实工装治具", "试产前落实到SOP"],
                        {"name": "杨铁华", "userid": "YangTieHua", "position": "NPI工程师"},
                    )
                    self.assertTrue(transferred.changed)
                    assert transferred.record is not None
                    self.assertEqual(
                        sample_issue.calculate_sample_issue_status(transferred.record),
                        "试产前特殊准备",
                    )
                    self.assertFalse(sample_issue.is_sample_issue_closed(transferred.record))
                    self.assertEqual(transferred.record["special_preparation"]["owner_name"], "杨铁华")
                    self.assertEqual(transferred.record["special_preparation"]["owner_userid"], "YangTieHua")
                    self.assertTrue(
                        sample_issue.can_manage_sample_special_preparation(
                            transferred.record,
                            "杨铁华",
                            "NPI工程",
                        )
                    )
                    self.assertFalse(
                        sample_issue.can_manage_sample_special_preparation(
                            transferred.record,
                            "NPI乙",
                            "NPI工程师",
                        )
                    )
                    self.assertEqual(
                        sample_issue.get_sample_dashboard_pending_count(
                            {issue_id: transferred.record},
                            "NPI甲",
                            "NPI工程师",
                        ),
                        0,
                    )

                    actions = transferred.record["special_preparation"]["actions"]
                    forbidden = await sample_issue.set_sample_special_action_completed(
                        issue_id,
                        actions[0]["id"],
                        True,
                        "NPI乙",
                        "NPI工程师",
                    )
                    self.assertEqual(forbidden.code, "forbidden")

                    latest_record = transferred.record
                    for action in actions:
                        completed = await sample_issue.set_sample_special_action_completed(
                            issue_id,
                            action["id"],
                            True,
                            "杨铁华",
                            "NPI工程",
                        )
                        self.assertTrue(completed.changed)
                        assert completed.record is not None
                        latest_record = completed.record
                    self.assertTrue(sample_issue.are_sample_special_actions_complete(latest_record))

                    final_requested = await sample_issue.submit_sample_close_request(
                        issue_id,
                        "杨铁华",
                        "NPI工程",
                    )
                    self.assertTrue(final_requested.changed)
                    assert final_requested.record is not None
                    final_request = sample_issue.get_pending_close_request(final_requested.record["countermeasure"])
                    assert final_request is not None
                    self.assertEqual(final_request["stage"], "special_preparation")
                    self.assertEqual(
                        sample_issue.calculate_sample_issue_status(final_requested.record),
                        "试产前特殊准备",
                    )
                    self.assertEqual(
                        sample_issue.get_sample_dashboard_pending_count(
                            {issue_id: final_requested.record},
                            "经理",
                            "研发经理",
                        ),
                        1,
                    )
                    self.assertEqual(
                        sample_issue.get_sample_dashboard_pending_count(
                            {issue_id: final_requested.record},
                            "NPI甲",
                            "NPI工程师",
                        ),
                        0,
                    )

                    closed = await sample_issue.approve_sample_close_request(
                        issue_id,
                        final_request["id"],
                        True,
                        "经理",
                        "研发经理",
                    )
                    self.assertTrue(closed.changed)
                    assert closed.record is not None
                    self.assertEqual(sample_issue.calculate_sample_issue_status(closed.record), "已关闭")
                    self.assertEqual(closed.record["countermeasure"]["closure_nature"], "流程问题")
                    catalog = isolated_db.get_item(sample_issue.SAMPLE_CLOSURE_NATURE_CATALOG_KEY, {})
                    self.assertEqual(catalog["流程问题"]["use_count"], 1)
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()

    async def test_close_request_approval_uses_requester_role_route(self):
        """关闭申请应按申请人角色关键词固定到配置的审批角色。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated_db = load_isolated_db_storage(
                "test_sample_issue_close_route_db_storage",
                Path(temp_dir) / "sample_issue_close_route.db",
            )
            try:
                await isolated_db.init_db()
                from src.pages import sample_issue_collection as sample_issue

                original_db_storage = sample_issue.db_storage
                original_route_values = {
                    "SAMPLE_CLOSE_APPROVER_ROLES": sample_issue.SAMPLE_CLOSE_APPROVER_ROLES,
                    "SAMPLE_CLOSE_NOTIFY_TARGETS": sample_issue.SAMPLE_CLOSE_NOTIFY_TARGETS,
                    "SAMPLE_CLOSE_APPROVAL_NOTIFY_TARGETS": sample_issue.SAMPLE_CLOSE_APPROVAL_NOTIFY_TARGETS,
                    "SAMPLE_CLOSE_NOTIFY_REQUESTER_ON_APPROVAL": sample_issue.SAMPLE_CLOSE_NOTIFY_REQUESTER_ON_APPROVAL,
                    "SAMPLE_CLOSE_ROUTING_RULES": sample_issue.SAMPLE_CLOSE_ROUTING_RULES,
                }
                sample_issue.db_storage = isolated_db
                sample_issue.SAMPLE_CLOSE_APPROVER_ROLES = ["研发经理", "admin"]
                sample_issue.SAMPLE_CLOSE_NOTIFY_TARGETS = [{"position": "研发经理"}]
                sample_issue.SAMPLE_CLOSE_APPROVAL_NOTIFY_TARGETS = [{"position": "PIE工程师"}]
                sample_issue.SAMPLE_CLOSE_NOTIFY_REQUESTER_ON_APPROVAL = True
                sample_issue.SAMPLE_CLOSE_ROUTING_RULES = [
                    {
                        "key": "pie_to_quality",
                        "label": "PIE关闭审批",
                        "requester_role_keywords": ["PIE"],
                        "approver_roles": ["品质经理"],
                        "notify_targets": [{"position": "品质经理"}],
                        "approval_notify_targets": [{"position": "PIE工程师"}],
                        "notify_requester_on_approval": True,
                    }
                ]
                try:
                    draft = sample_issue.generate_initial_sample_issue_data("张三", "PIE工程师")
                    draft["basic_info"].update(
                        {
                            "product_model": "MODEL-ROUTE",
                            "issue_description": "样机外观划伤",
                            "sample_order_no": "SAMPLE-ROUTE",
                            "record_date": "2026-07-10",
                            "assembled_qty": "3",
                            "issue_qty": "1",
                            "recorder_name": "张三",
                        }
                    )
                    draft["countermeasure"].update(
                        {
                            "owner": "PIE工程师",
                            "reason_analysis": "周转防护不足",
                            "temporary_action": "增加隔离袋",
                            "corrective_preventive_action": "更新周转规范",
                            "due_date": "2026-07-15",
                        }
                    )
                    created = await sample_issue.save_sample_issue_record(
                        draft,
                        "张三",
                        "PIE工程师",
                        is_new=True,
                    )
                    self.assertTrue(created.changed)
                    assert created.record is not None
                    issue_id = created.record["issue_id"]

                    requested = await sample_issue.submit_sample_close_request(issue_id, "李四", "PIE工程师")

                    self.assertTrue(requested.changed)
                    assert requested.record is not None
                    close_request = sample_issue.get_pending_close_request(requested.record["countermeasure"])
                    assert close_request is not None
                    self.assertEqual(close_request["key"], "pie_to_quality")
                    self.assertEqual(close_request["approver_roles"], ["品质经理"])

                    old_default_approver = await sample_issue.approve_sample_close_request(
                        issue_id,
                        close_request["id"],
                        True,
                        "经理",
                        "研发经理",
                        "流程问题",
                    )
                    self.assertEqual(old_default_approver.code, "forbidden")

                    routed_approver = await sample_issue.approve_sample_close_request(
                        issue_id,
                        close_request["id"],
                        True,
                        "品质经理A",
                        "品质经理",
                        "流程问题",
                    )
                    self.assertTrue(routed_approver.changed)
                    assert routed_approver.record is not None
                    self.assertEqual(routed_approver.record["countermeasure"]["closed_by"], "品质经理A")
                finally:
                    sample_issue.db_storage = original_db_storage
                    for name, value in original_route_values.items():
                        setattr(sample_issue, name, value)
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
                    draft["basic_info"]["evidence_files"] = [
                        {
                            "attachment_scope": "basic",
                            "file_del_bool": False,
                            "file_name": "问题件",
                            "file_url": "/uploads/sample_issue/张三/problem.hash.jpg",
                            "file_name_hash": "sample_issue_SPI-RACE_problem.hash.jpg",
                            "file_name_suffix": "problem.jpg",
                            "file_type": "image/jpeg",
                            "file_lab": "1",
                            "parents_h": 12,
                        }
                    ]
                    draft["countermeasure"]["owner"] = "李四"
                    draft["countermeasure"]["evidence_files"] = [
                        {
                            "attachment_scope": "countermeasure",
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
                    self.assertEqual(stored["basic_info"]["evidence_files"][0]["file_name_suffix"], "problem.jpg")
                    self.assertEqual(stored["countermeasure"]["reason_analysis"], "连接器接触不良")
                    self.assertEqual(stored["countermeasure"]["evidence_files"][0]["file_name_suffix"], "photo.jpg")
                finally:
                    sample_issue.db_storage = original_db_storage
            finally:
                await isolated_db.close_db()


if __name__ == "__main__":
    unittest.main()
