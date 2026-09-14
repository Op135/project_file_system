"""移交提醒业务字段、摘要预算和收件人隔离回归。"""

import unittest

from src.ecn_management_config import ECNState
from src.modules.ecn.notifications import build_notification_card
from src.modules.ecn.special_task_messages import (
    get_special_message_items,
    get_special_message_item,
    build_special_card,
)


class SpecialMessageTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "ecn_id": "ECN-test",
            "workflow": {"current_state": ECNState.ECN_EXECUTING},
            "basic_info": {"title": "不应占据摘要的通用主题"},
            "change_items": [
                {
                    "item_id": "internal-uuid",
                    "change_type": "测试报告内容格式",
                    "projects": ["RFFM-1009-A"],
                    "new_content": "测试报告模板1",
                }
            ],
            "execution_info": {"ordinary_confirmations": {"internal-uuid": {"assignee": "接收人", "confirmed": False}}},
        }
        self.config = {"test_mode": True}

    def test_card_prioritizes_execution_table_fields_over_generic_tasks(self):
        title, body = build_notification_card(
            self.record, {"助理": ["启动系统内资料执行"]}, ["助理", "接收人"], self.config
        )
        self.assertIn("特定事项待确认", title)
        for text in ("事项/方案：测试报告内容格式", "项目：RFFM-1009-A", "应执行内容：测试报告模板1"):
            self.assertIn(text, body)
        self.assertLess(body.index("应执行内容"), body.index("单号"))
        self.assertNotIn("internal-uuid", body)
        self.assertNotIn("启动系统内资料执行", body)
        self.assertNotIn("通用主题", body)

    def test_long_values_keep_all_three_labels_and_valid_entities_within_limit(self):
        item = self.record["change_items"][0]
        item.update(change_type="<特别事项>&" * 100, projects=["项目" * 100], new_content="<内容>&" * 100)
        _, body = build_notification_card(self.record, {}, ["接收人"], self.config)
        self.assertLessEqual(len(body.encode("utf-8")), 512)
        for label in ("事项/方案：", "项目：", "应执行内容："):
            self.assertIn(label, body)
        self.assertNotIn("<内容>", body)
        self.assertEqual(body.count("<div "), body.count("</div>"))

    def test_filters_other_peoples_tasks_and_marks_additional_items(self):
        self.record["execution_info"]["erp_confirmation"] = {"assignee": "接收人", "confirmed": False}
        self.assertEqual(get_special_message_items(self.record, ["其他人"]), [])
        _, body = build_notification_card(self.record, {}, ["接收人"], self.config)
        self.assertIn("另有1项", body)
        self.record["execution_info"]["ordinary_confirmations"]["internal-uuid"]["confirmed"] = True
        _, body = build_notification_card(self.record, {}, ["接收人"], self.config)
        self.assertIn("事项/方案：ERP相关变更", body)
        self.assertIn("项目：—", body)

    def test_cancellation_can_disable_pending_task_layout(self):
        title, body = build_notification_card(
            self.record, {"接收人": ["旧任务取消"]}, ["接收人"], self.config, include_special=False
        )
        self.assertNotIn("事项/方案：", body)
        self.assertIn("旧任务取消", body)

    def test_cancel_card_uses_business_fields_instead_of_internal_id(self):
        item = get_special_message_item(self.record, "internal-uuid", "原负责人")
        for debug, cc in ((True, False), (False, True), (False, False)):
            title, body = build_special_card(self.record, [item], test_mode=debug, is_cc=cc, cancelled=True)
            self.assertIn("取消", title)
            self.assertIn("已取消，无需继续处理", body)
            self.assertIn("事项/方案：", body)
            self.assertIn("项目：RFFM-1009-A", body)
            self.assertIn("应执行内容：测试报告模板1", body)
            self.assertNotIn("internal-uuid", body)
            self.assertLessEqual(len(body.encode("utf-8")), 512)
        item.update(subject="<事项>" * 100, projects="项目" * 100, content="&内容" * 100)
        _, body = build_special_card(self.record, [item], test_mode=True, is_cc=False, cancelled=True)
        self.assertLessEqual(len(body.encode("utf-8")), 512)
        self.assertIn("应执行内容：", body)
        self.assertEqual(body.count("<div "), body.count("</div>"))
