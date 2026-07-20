"""生产异常 JSON 配置加载器的回归测试。

这组测试不关心页面样式，只保证根目录配置具有运行所需的关键字段，并验证错误配置不会阻止
系统启动。新增可维护字段或新的校验规则时，应在这里补充相应断言。
"""

import json
import importlib
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

error_management_config = importlib.import_module("src.error_management_config")
error_management = importlib.import_module("src.pages.error_management")


class ErrorManagementConfigTests(unittest.TestCase):
    def test_project_config_has_required_business_values(self):
        """项目实际使用的根目录 JSON 应能生成一份可运行的配置。"""
        config = error_management_config.ERROR_MANAGEMENT_CONFIG

        self.assertTrue(config["public_base_url"].startswith("http"))
        self.assertTrue(config["editor_roles"])
        self.assertTrue(config["product_states"])
        self.assertEqual(config["filter_states"][0], "全部")
        self.assertEqual(config["filter_states"][1], "未关闭")
        self.assertIn("延期申请中", config["filter_states"])
        self.assertIn("关闭申请中", config["filter_states"])
        self.assertEqual(config["wecom"]["default_notify_targets"], [{"position": "研发经理"}])
        self.assertTrue(config["wecom"]["extension"]["approver_roles"])
        self.assertTrue(config["wecom"]["extension"]["approval_notify_targets"])
        self.assertTrue(config["wecom"]["extension"]["notify_requester_on_approval"])
        self.assertEqual(config["reminders"]["check_window"], {"enabled": True, "start": "08:30", "end": "18:30"})
        self.assertTrue(config["reminders"]["rules"])
        self.assertNotIn("YueYeXiaoSheng", error_management_config.ERROR_MANAGEMENT_CONFIG_PATH.read_text(encoding="utf-8"))

    def test_invalid_fields_fall_back_and_disabled_rules_are_removed(self):
        """坏字段应逐项回退；合法字段继续生效；禁用提醒规则不进入运行时列表。"""
        test_config = {
            "public_base_url": "",
            "editor_roles": "invalid",
            "filter_states": ["已关闭"],
            "reminders": {
                "initial_delay_seconds": -1,
                "check_interval_seconds": 120,
                "check_window": {"enabled": True, "start": "09:00", "end": "17:30"},
                "rules": [
                    {
                        "key": "disabled",
                        "label": "禁用规则",
                        "days_until_due": 1,
                        "enabled": False,
                    },
                    {
                        "key": "valid",
                        "label": "有效规则",
                        "days_until_due": 2,
                        "enabled": True,
                    },
                ],
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            # 临时替换配置文件路径，不读取或修改项目真实配置。
            config_path = Path(temp_dir) / "error_management_config.json"
            config_path.write_text(json.dumps(test_config, ensure_ascii=False), encoding="utf-8")
            with patch.object(error_management_config, "ERROR_MANAGEMENT_CONFIG_PATH", config_path):
                loaded = error_management_config.load_error_management_config()

        self.assertEqual(loaded["public_base_url"], "http://192.168.1.102:8080")
        self.assertEqual(loaded["editor_roles"], ["研发经理", "admin", "研发助理"])
        self.assertEqual(loaded["filter_states"], ["全部", "未关闭", "已关闭"])
        self.assertEqual(loaded["wecom"]["default_notify_targets"], [{"position": "研发经理"}])
        self.assertEqual(
            loaded["wecom"]["extension"]["approval_notify_targets"],
            error_management_config._DEFAULT_CONFIG["wecom"]["extension"]["approval_notify_targets"],
        )
        self.assertEqual(loaded["reminders"]["initial_delay_seconds"], 60)
        self.assertEqual(loaded["reminders"]["check_interval_seconds"], 120)
        self.assertEqual(loaded["reminders"]["check_window"], {"enabled": True, "start": "09:00", "end": "17:30"})
        self.assertEqual(loaded["reminders"]["rules"], [{"key": "valid", "label": "有效规则", "days_until_due": 2}])

    def test_time_window_matches_daytime_and_overnight_ranges(self):
        """提醒时间窗口支持普通工作时段、跨午夜时段和关闭限制。"""
        from src.issue_workflow_utils import is_time_in_window

        daytime_window = {"enabled": True, "start": "08:30", "end": "18:30"}
        overnight_window = {"enabled": True, "start": "22:00", "end": "06:00"}

        self.assertTrue(is_time_in_window(daytime_window, datetime(2026, 7, 10, 9, 0)))
        self.assertFalse(is_time_in_window(daytime_window, datetime(2026, 7, 10, 7, 0)))
        self.assertTrue(is_time_in_window(overnight_window, datetime(2026, 7, 10, 23, 0)))
        self.assertTrue(is_time_in_window(overnight_window, datetime(2026, 7, 11, 2, 0)))
        self.assertFalse(is_time_in_window(overnight_window, datetime(2026, 7, 10, 12, 0)))
        self.assertTrue(is_time_in_window({"enabled": False, "start": "08:30", "end": "18:30"}))
        self.assertTrue(is_time_in_window({"enabled": False, "start": "", "end": ""}, datetime(2026, 7, 10, 3, 0)))


class ErrorManagementNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_extension_approval_notification_combines_role_and_requester(self):
        """审批结果通知应同时包含配置角色和申请人，并由发送层统一发送。"""
        resolve_mock = AsyncMock(side_effect=["rd-manager-userid", "quality-manager-userid|qe-userid", "requester-userid"])
        send_mock = AsyncMock(return_value=(True, "发送成功"))

        with (
            patch.object(error_management, "resolve_wecom_recipients", resolve_mock),
            patch.object(error_management, "send_wecom_text_message", send_mock),
        ):
            success, _ = await error_management.send_error_extension_wecom_message(
                "审批结果",
                error_id="ERR-001",
                business_key="ERR-001:approval",
                message_type="extension_approval",
                additional_people="申请人",
                additional_targets=[{"position": "品质经理"}, {"position": "QE工程师"}],
            )

        self.assertTrue(success)
        await_args = send_mock.await_args
        if await_args is None:
            self.fail("send_wecom_text_message was not awaited")
        self.assertEqual(
            await_args.args[1],
            "rd-manager-userid|quality-manager-userid|qe-userid|requester-userid",
        )
        self.assertEqual(resolve_mock.await_count, 3)


class ErrorManagementDashboardPendingTests(unittest.TestCase):
    def setUp(self):
        self.all_errors = {
            "ERR-001": {
                "error_id": "ERR-001",
                "preventive_actions": [
                    {
                        "owner": "张三",
                        "status": "待执行",
                        "extension_requests": [{"status": "待审批"}, {"status": "已通过"}],
                    },
                    {
                        "owner": "张三、品质主管",
                        "status": "待执行",
                        "extension_requests": [{"status": "待审批"}],
                    },
                ],
            },
            "ERR-002": {
                "error_id": "ERR-002",
                "preventive_actions": [
                    {"owner": "张三", "status": "已关闭", "extension_requests": []},
                    {"owner": "李四", "status": "待执行", "extension_requests": []},
                ],
            },
            "ERR-003": {
                "error_id": "ERR-003",
                "preventive_actions": [
                    {"owner": "品质主管", "status": "待执行", "extension_requests": []},
                ],
            },
        }

    def test_responsible_user_counts_distinct_error_records(self):
        """普通负责人按异常单去重计数，而不是按负责措施条数计数。"""
        count = error_management.get_error_dashboard_pending_count(self.all_errors, "张三", "QE工程师")
        self.assertEqual(count, 1)

    def test_responsible_role_can_match_owner_field(self):
        """负责人字段填写角色时，该角色下用户也应看到异常待办。"""
        count = error_management.get_error_dashboard_pending_count(self.all_errors, "王五", "品质主管")
        self.assertEqual(count, 2)

    def test_rd_manager_counts_pending_extension_requests(self):
        """研发经理角标按待审批延期申请条数统计，不统计普通负责人待办。"""
        count = error_management.get_error_dashboard_pending_count(self.all_errors, "经理", "研发经理")
        self.assertEqual(count, 2)

    def test_card_pending_marker_uses_dashboard_rules(self):
        """列表待办标识应与主页角标对当前角色采用相同判定口径。"""
        self.assertTrue(error_management.is_error_pending_for_user(self.all_errors["ERR-001"], "张三", "QE工程师"))
        self.assertFalse(error_management.is_error_pending_for_user(self.all_errors["ERR-002"], "张三", "QE工程师"))
        self.assertTrue(error_management.is_error_pending_for_user(self.all_errors["ERR-001"], "经理", "研发经理"))
        self.assertFalse(error_management.is_error_pending_for_user(self.all_errors["ERR-003"], "经理", "研发经理"))

    def test_card_sort_key_prioritizes_current_user_pending(self):
        """即使待办更新时间更早，也应排在非待办卡片之前。"""
        pending = {**self.all_errors["ERR-001"], "updated_at": "2026-01-01 00:00:00"}
        normal = {**self.all_errors["ERR-002"], "updated_at": "2026-12-31 00:00:00"}
        records = sorted(
            [normal, pending],
            key=lambda item: error_management.get_error_card_sort_key(item, "张三", "QE工程师"),
            reverse=True,
        )
        self.assertEqual(records[0]["error_id"], "ERR-001")

    def test_reviewer_sees_overdue_without_request_as_second_priority(self):
        """评审角色应凸显逾期未申请措施，但仍排在本人待审批事项之后。"""
        reference_date = datetime(2026, 7, 17).date()
        pending = {
            "error_id": "ERR-PENDING",
            "updated_at": "2026-01-01 00:00:00",
            "preventive_actions": [
                {
                    "owner": "张三",
                    "status": "待执行",
                    "due_date": "2026-07-10",
                    "extension_requests": [{"status": "待审批"}],
                    "close_requests": [],
                }
            ],
        }
        overdue = {
            "error_id": "ERR-OVERDUE",
            "updated_at": "2026-02-01 00:00:00",
            "preventive_actions": [
                {
                    "owner": "李四",
                    "status": "待执行",
                    "due_date": "2026-07-10",
                    "extension_requests": [],
                    "close_requests": [],
                }
            ],
        }
        normal = {
            "error_id": "ERR-NORMAL",
            "updated_at": "2026-12-31 00:00:00",
            "preventive_actions": [
                {
                    "owner": "王五",
                    "status": "待执行",
                    "due_date": "2026-07-20",
                    "extension_requests": [],
                    "close_requests": [],
                }
            ],
        }

        self.assertTrue(
            error_management.has_error_overdue_without_request_for_reviewer(
                overdue, "研发经理", reference_date
            )
        )
        self.assertFalse(
            error_management.has_error_overdue_without_request_for_reviewer(
                overdue, "测试工程师", reference_date
            )
        )
        self.assertFalse(
            error_management.has_error_overdue_without_request_for_reviewer(
                pending, "研发经理", reference_date
            )
        )
        close_pending = {
            "error_id": "ERR-CLOSE-PENDING",
            "preventive_actions": [
                {
                    "owner": "李四",
                    "status": "待执行",
                    "due_date": "2026-07-10",
                    "extension_requests": [],
                    "close_requests": [{"status": "待审批"}],
                }
            ],
        }
        self.assertFalse(
            error_management.has_error_overdue_without_request_for_reviewer(
                close_pending, "研发经理", reference_date
            )
        )

        records = sorted(
            [normal, overdue, pending],
            key=lambda item: error_management.get_error_card_sort_key(
                item, "经理", "研发经理", reference_date
            ),
            reverse=True,
        )
        self.assertEqual(
            [item["error_id"] for item in records],
            ["ERR-PENDING", "ERR-OVERDUE", "ERR-NORMAL"],
        )

    def test_pending_extension_filter_matches_across_main_statuses(self):
        """延期申请中筛选应跨越异常单主状态，并排除没有待审批申请的异常单。"""
        self.assertTrue(error_management.error_matches_filter(self.all_errors["ERR-001"], "延期申请中"))
        self.assertFalse(error_management.error_matches_filter(self.all_errors["ERR-002"], "延期申请中"))

    def test_open_filter_hides_only_fully_closed_errors(self):
        """默认未关闭筛选应保留处理中记录，并隐藏全部措施均关闭的异常单。"""
        closed_error = {
            "error_id": "ERR-CLOSED",
            "preventive_actions": [{"status": "已关闭"}],
        }
        self.assertTrue(
            error_management.error_matches_filter(self.all_errors["ERR-001"], "未关闭")
        )
        self.assertFalse(error_management.error_matches_filter(closed_error, "未关闭"))
        self.assertTrue(error_management.error_matches_filter(closed_error, "已关闭"))

    def test_card_status_prioritizes_pending_extension(self):
        """总览卡片应优先显示延期申请中，而不是底层流程主状态。"""
        self.assertEqual(error_management.calculate_error_status(self.all_errors["ERR-001"]), "纠正预防执行中")
        self.assertEqual(error_management.get_error_card_status(self.all_errors["ERR-001"]), "延期申请中")
        self.assertEqual(error_management.get_error_card_status(self.all_errors["ERR-002"]), "纠正预防执行中")

    def test_pending_close_filter_and_card_status(self):
        """待审批关闭申请应支持筛选，并优先显示为关闭申请中。"""
        close_pending_error = {
            "error_id": "ERR-CLOSE",
            "preventive_actions": [
                {
                    "owner": "李四",
                    "status": "待执行",
                    "extension_requests": [{"status": "待审批"}],
                    "close_requests": [{"status": "待审批"}],
                }
            ],
        }
        self.assertTrue(error_management.error_matches_filter(close_pending_error, "关闭申请中"))
        self.assertEqual(error_management.get_error_card_status(close_pending_error), "关闭申请中")

    def test_manual_reminder_check_is_rd_manager_only(self):
        """人工检查提醒入口只对研发经理角色开放。"""
        self.assertTrue(error_management.is_error_rd_manager("研发经理"))
        self.assertTrue(error_management.is_error_rd_manager("研发经理兼项目负责人"))
        self.assertFalse(error_management.is_error_rd_manager("admin"))
        self.assertFalse(error_management.is_error_rd_manager("研发助理"))


if __name__ == "__main__":
    unittest.main()
