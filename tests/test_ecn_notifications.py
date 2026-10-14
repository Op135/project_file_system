"""ECN微信路由、去重和失败重试测试；所有外部发送均使用模拟接口。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.ecn_management_config import ECN_LEVEL_COMPLEX, ECNState, load_ecn_config
from src.modules.ecn import notifications
from src.modules.ecn.models import get_ecn_template
from tests.test_error_management_concurrency import load_isolated_db_storage


class Users:
    def load_users(self):
        return {
            "admin": {"role": "admin", "status": "active"},
            "writer": {"role": "研发硬件", "status": "active"},
            "manager": {"role": "研发经理", "status": "active"},
            "inactive": {"role": "研发硬件", "status": "inactive"},
        }

    def has_permission(self, username, permission_code, **kwargs):
        return True

    def list_wecom_bindings(self):
        return {
            "admin": {"external_userid": "admin_wx"},
            "writer": {"external_userid": "writer_wx"},
            "manager": {"external_userid": "manager_wx"},
        }

    def list_primary_memberships(self):
        return {
            "admin": {"manager_username": "", "position_name": "系统管理员"},
            "writer": {"manager_username": "manager", "position_name": "研发硬件"},
            "inactive": {"manager_username": "manager", "position_name": "研发硬件"},
            "manager": {"manager_username": "", "position_name": "研发经理"},
        }


class ECNNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = load_isolated_db_storage("ecn_notification_test_db", Path(self.temp.name) / "ecn.db")
        await self.storage.init_db()
        self.settings = load_ecn_config()["wecom"]
        self.settings["cc_manager_enabled"] = False
        self.settings["public_base_url"] = "https://example.test"
        self.service = Users()
        self.record = get_ecn_template()
        self.record["ecn_id"] = "ECN-test"
        self.record["basic_info"].update(applicant="writer", title="测试变更")
        self.record["workflow"].update(
            current_state=ECNState.ECN_SCHEMING,
            current_phase="ECN_SCHEME_PHASE",
            scheme_participants={"writer": "editing", "inactive": "editing"},
        )
        await self.save_record()
        self.sender = AsyncMock(return_value=(True, "ok"))
        self.resolver = AsyncMock(return_value="manager_wx")
        self.sender_patch = patch.object(notifications, "send_wecom_textcard_message", self.sender)
        self.resolver_patch = patch.object(notifications, "resolve_wecom_recipients", self.resolver)
        self.sender_patch.start()
        self.resolver_patch.start()

    async def asyncTearDown(self):
        self.sender_patch.stop()
        self.resolver_patch.stop()
        await self.storage.close_db()
        self.temp.cleanup()

    async def save_record(self):
        await self.storage.set_item("ecn_management_data", {self.record["ecn_id"]: self.record})

    async def check(self):
        return await notifications.check_and_send_ecn_reminders(
            config=self.settings,
            user_service=self.service,
            storage=self.storage,
        )

    async def test_system_admin_full_permissions_do_not_create_business_notifications(self):
        pending = notifications.collect_pending_users(self.record, self.service)
        self.assertEqual(pending, {"writer": "研发硬件"})
        self.assertNotIn("admin", pending)

        self.settings["cc_manager_enabled"] = False
        self.assertEqual(await self.check(), (1, 0))
        self.assertEqual(self.sender.call_args.args[1], "writer_wx")
        self.assertNotIn("admin", self.sender.call_args.args[0])

    async def test_complex_validation_report_notifies_author_then_reviewer(self):
        self.record["workflow"].update(
            ecn_level=ECN_LEVEL_COMPLEX,
            scheme_participants={"writer": "confirmed"},
        )
        self.record["change_items"] = [
            {
                "item_id": "S1",
                "author": "writer",
                "change_type": "物料更改",
                "validation_report": {
                    "required": True,
                    "status": "pending_upload",
                    "attachments": [],
                },
            }
        ]
        pending = notifications.collect_pending_users(self.record, self.service)
        self.assertEqual(pending, {"writer": "研发硬件"})
        tasks = notifications.pending_task_details(self.record, pending, self.service)
        self.assertIn("上传验证报告", tasks["writer"][0])

        report = self.record["change_items"][0]["validation_report"]
        report["status"] = "pending_review"
        report["attachments"] = [{"id": "R1", "name": "验证报告.pdf"}]
        pending = notifications.collect_pending_users(self.record, self.service)
        self.assertEqual(pending, {"manager": "研发经理"})
        tasks = notifications.pending_task_details(self.record, pending, self.service)
        self.assertIn("审批验证报告", tasks["manager"][0])

    async def test_execution_review_revocation_escalates_notice_to_inactive_users_manager(self):
        self.settings["cc_manager_enabled"] = False
        self.record["workflow"].update(
            current_state=ECNState.ECN_EXECUTING,
            current_phase="ECN_EXECUTION_PHASE",
            scheme_participants={},
        )
        self.record["execution_info"] = {
            "stage": "material_confirmation",
            "ordinary_confirmations": {},
            "erp_confirmation": {"confirmed": True},
            "material_confirmations": {},
            "verification": {
                "status": "correction_required",
                "notices": {
                    "review-event-1": {
                        "event_id": "review-event-1",
                        "recipient": "inactive",
                        "subject": "特定事项 #01 测试报告",
                        "reason": "现场文件没有更新",
                        "reviewer": "manager",
                        "time": "2026-09-22 10:00:00",
                    }
                },
            },
        }
        await self.save_record()

        self.assertEqual(await self.check(), (1, 0))
        self.assertEqual(self.sender.call_args.args[1], "manager_wx")
        description = self.sender.call_args.args[0]
        self.assertIn("已沿直属上级传导", description)
        self.assertIn("现场文件没有更新", description)
        self.assertEqual(await self.check(), (0, 0))

    async def test_missing_url_uses_text_with_business_owned_retry(self):
        self.settings["public_base_url"] = ""
        with patch.object(notifications, "send_wecom_text_message", new_callable=AsyncMock) as sender:
            sender.return_value = (True, "ok")
            self.assertEqual(await self.check(), (1, 0))
            self.assertFalse(sender.call_args.kwargs["retry_tracking"])
            self.assertFalse(sender.call_args.kwargs["alert_on_max_failure"])
        self.sender.assert_not_awaited()

    async def test_card_escapes_user_content_and_limits_utf8_bytes(self):
        self.record["basic_info"]["title"] = '<script>&"测试' * 100
        title, description = notifications.build_notification_card(
            self.record,
            {"writer": ["待办" * 200]},
            ["writer"],
            self.settings,
        )
        self.assertLessEqual(len(title.encode("utf-8")), 128)
        self.assertLessEqual(len(description.encode("utf-8")), 512)
        self.assertNotIn("<script>", description)
        self.assertIn("&lt;script&gt;", description)
        self.assertTrue(description.endswith("</div>"))
        self.assertIn("进入系统查看完整待办", description)

    async def test_card_uses_separate_blocks_and_combines_identical_tasks(self):
        _, description = notifications.build_notification_card(
            self.record,
            {"writer": ["请发起评审"], "manager": ["请发起评审"]},
            ["writer", "manager"],
            self.settings,
        )
        self.assertNotIn("<br", description)
        self.assertIn('</div><div class="normal">主题：', description)
        self.assertIn('</div><div class="normal">待处理人员：writer、manager</div>', description)
        self.assertEqual(description.count("请发起评审"), 1)

    async def test_normal_and_cc_cards_keep_every_intended_person_name(self):
        names = [f"处理人{index}" for index in range(1, 9)]
        tasks = {name: ["请处理待办"] for name in names}
        expected = "、".join(names)

        _, normal_description = notifications.build_notification_card(
            self.record, tasks, names, self.settings
        )
        self.assertIn(f"待处理人员：{expected}", normal_description)
        self.assertNotIn("等8人", normal_description)

        _, cc_description = notifications.build_notification_card(
            self.record, tasks, names, self.settings, is_cc=True
        )
        self.assertIn(f"待处理人员：{expected}", cc_description)
        self.assertNotIn("等8人", cc_description)

    async def test_production_copies_manager_without_duplicate_scans(self):
        self.settings["cc_manager_enabled"] = True
        self.resolver.return_value = "manager_wx|manager_wx|@all"
        self.assertEqual(await self.check(), (2, 0))
        messages = {call.args[1]: call.args[0] for call in self.sender.await_args_list}
        self.assertEqual(set(messages), {"writer_wx", "manager_wx"})
        self.assertIn("研发经理抄送", messages["manager_wx"])
        self.assertIn("待处理人员：writer", messages["manager_wx"])
        self.assertNotIn("抄送", messages["writer_wx"])
        self.assertEqual(await self.check(), (0, 0))

    async def test_manager_observes_each_person_as_a_separate_notification(self):
        self.settings["cc_manager_enabled"] = True
        self.resolver.return_value = "manager_wx"
        self.record["workflow"]["scheme_participants"] = {"writer": "editing", "manager": "editing"}
        await self.save_record()
        self.assertEqual(await self.check(), (3, 0))
        copies = [call.args[0] for call in self.sender.await_args_list if call.args[1] == "manager_wx"]
        self.assertEqual(len(copies), 2)
        self.assertTrue(any("待处理人员：writer" in content for content in copies))
        self.assertTrue(any("待处理人员：manager" in content for content in copies))
        self.assertFalse(any("writer、manager" in content for content in copies))

    async def test_one_person_completing_work_does_not_remind_unchanged_people(self):
        self.record["workflow"]["scheme_participants"] = {
            "writer": "editing",
            "manager": "editing",
        }
        await self.save_record()
        self.assertEqual(await self.check(), (2, 0))

        self.sender.reset_mock()
        self.record["workflow"]["scheme_participants"] = {"manager": "editing"}
        await self.save_record()
        self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_not_awaited()

        self.record["workflow"]["scheme_participants"] = {
            "writer": "editing",
            "manager": "editing",
        }
        await self.save_record()
        self.assertEqual(await self.check(), (1, 0))
        self.assertIn("待处理人员：writer", self.sender.call_args.args[0])

    async def test_cc_switch_does_not_resend_actual_notification(self):
        self.settings["cc_manager_enabled"] = False
        self.resolver.return_value = "manager_wx"
        self.assertEqual(await self.check(), (1, 0))
        self.sender.reset_mock()
        self.settings["cc_manager_enabled"] = True
        self.assertEqual(await self.check(), (1, 0))
        self.assertEqual(self.sender.call_args.args[1], "manager_wx")
        self.settings["cc_manager_enabled"] = False
        self.assertEqual(await self.check(), (0, 0))

    async def test_cc_failure_retries_only_manager_and_disable_stops_retry(self):
        self.settings["cc_manager_enabled"] = True
        self.resolver.return_value = "manager_wx"
        self.sender.side_effect = [(True, "ok"), (False, "模拟失败")]
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (1, 1))
        await self.storage.atomic_deep_update(
            [
                notifications.NOTIFICATION_STATE_KEY,
                "ECN-test",
                "users",
                "writer",
                "recipients",
                "cc:manager_wx",
            ],
            lambda entry: {**entry, "attempted_at": 0},
        )
        self.sender.reset_mock(side_effect=True)
        self.sender.return_value = (True, "ok")
        self.settings["cc_manager_enabled"] = False
        self.assertEqual(await self.check(), (0, 0))
        self.settings["cc_manager_enabled"] = True
        self.assertEqual(await self.check(), (1, 0))
        self.sender.assert_awaited_once()
        self.assertEqual(self.sender.call_args.args[1], "manager_wx")

    async def test_missing_cc_recipient_does_not_block_actual_notification(self):
        self.settings["cc_manager_enabled"] = True
        self.resolver.return_value = ""
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (1, 0))
        self.assertEqual(self.sender.call_args.args[1], "writer_wx")

    async def test_cc_resolution_error_does_not_block_actual_notification(self):
        self.settings["cc_manager_enabled"] = True
        self.resolver.side_effect = RuntimeError("模拟通讯录失败")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.check(), (1, 0))
        self.assertEqual(self.sender.call_args.args[1], "writer_wx")

    async def test_unbound_real_recipient_is_skipped(self):
        with patch.object(self.service, "list_wecom_bindings", return_value={}), self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_not_awaited()

    async def test_disable_stops_sending_without_affecting_badge_candidates(self):
        self.settings["enabled"] = False
        self.assertEqual(await self.check(), (0, 0))
        self.assertEqual(notifications.collect_pending_users(self.record, self.service), {"writer": "研发硬件"})
        self.sender.assert_not_awaited()

    async def test_repeat_scan_and_text_only_autosave_do_not_duplicate(self):
        self.assertEqual(await self.check(), (1, 0))
        self.record["basic_info"]["title"] = "输入过程中的修改"
        await self.save_record()
        self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_awaited_once()

    async def test_changed_participant_routes_new_pending_notification(self):
        await self.check()
        self.sender.reset_mock()
        self.record["workflow"]["scheme_participants"] = {"manager": "editing"}
        await self.save_record()
        self.assertEqual(await self.check(), (1, 0))
        self.assertIn("待处理人员：manager", self.sender.call_args.args[0])

    async def test_closed_record_does_not_retry_failed_message(self):
        self.sender.return_value = (False, "模拟网络失败")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (0, 1))
        self.record["workflow"]["current_state"] = ECNState.CLOSED
        await self.save_record()
        self.sender.reset_mock()
        self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_not_awaited()

    async def test_enforced_time_window_stops_before_actual_send(self):
        with patch.object(
            notifications,
            "is_scheduled_wecom_reminder_time",
            return_value=False,
        ):
            result = await notifications.check_and_send_ecn_reminders(
                config=self.settings,
                user_service=self.service,
                storage=self.storage,
                enforce_time_window=True,
            )

        self.assertEqual(result, (0, 0))
        self.sender.assert_not_awaited()

    async def test_failure_retries_after_configured_interval_only(self):
        self.sender.return_value = (False, "模拟网络失败")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (0, 1))
        self.sender.return_value = (True, "ok")
        self.assertEqual(await self.check(), (0, 0))
        await self.storage.atomic_deep_update(
            [
                notifications.NOTIFICATION_STATE_KEY,
                "ECN-test",
                "users",
                "writer",
                "recipients",
                "primary:writer_wx",
            ],
            lambda entry: {**entry, "attempted_at": 0},
        )
        self.assertEqual(await self.check(), (1, 0))

    async def test_final_pending_check_skips_record_closed_during_resolution(self):
        async def resolve(*_args, **_kwargs):
            self.record["workflow"]["current_state"] = ECNState.CLOSED
            await self.save_record()
            return {"writer_wx": ["writer"]}

        with patch.object(notifications, "resolve_delivery_targets", side_effect=resolve):
            self.assertEqual(await self.check(), (0, 0))
            self.sender.assert_not_awaited()


class ECNWecomConfigTests(unittest.TestCase):
    def test_default_uses_bound_recipients_and_invalid_interval_keeps_default(self):
        config = load_ecn_config({"wecom": {"enabled": False, "check_interval_seconds": -1}})[
            "wecom"
        ]
        self.assertTrue(config["cc_manager_enabled"])
        self.assertFalse(config["enabled"])
        self.assertEqual(config["check_interval_seconds"], 60)

    def test_cc_switch_can_be_disabled_independently(self):
        config = load_ecn_config({"wecom": {"cc_manager_enabled": False}})["wecom"]
        self.assertFalse(config["cc_manager_enabled"])


class ECNReminderScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_timer_callback_skips_scan_outside_global_window(self):
        callbacks = []

        def capture_timer(_interval, callback, **_kwargs):
            callbacks.append(callback)

        scan = AsyncMock(return_value=(1, 0))
        with (
            patch.dict(notifications.ECN_WECOM_CONFIG, {"enabled": True}),
            patch.object(notifications.app, "timer", side_effect=capture_timer),
            patch.object(notifications, "is_scheduled_wecom_reminder_time", return_value=False),
            patch.object(notifications, "check_and_send_ecn_reminders", scan),
        ):
            notifications.init_ecn_reminder_task()
            self.assertEqual(len(callbacks), 2)
            await callbacks[0]()
            await callbacks[1]()

        scan.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
