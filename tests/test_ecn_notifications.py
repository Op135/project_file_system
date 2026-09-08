"""ECN微信路由、去重和失败重试测试；所有外部发送均使用模拟接口。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.ecn_management_config import ECNState, load_ecn_config
from src.modules.ecn import notifications
from src.modules.ecn.models import get_ecn_template
from tests.test_error_management_concurrency import load_isolated_db_storage


class Users:
    storage_mode = "database"

    def load_users(self):
        return {
            "writer": {"role": "研发硬件", "status": "active"},
            "manager": {"role": "研发经理", "status": "active"},
            "inactive": {"role": "研发硬件", "status": "inactive"},
        }

    def has_permission(self, username, permission_code, **kwargs):
        return True

    def list_wecom_bindings(self):
        return {"writer": {"external_userid": "writer_wx"}, "manager": {"external_userid": "manager_wx"}}


class ECNNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = load_isolated_db_storage("ecn_notification_test_db", Path(self.temp.name) / "ecn.db")
        await self.storage.init_db()
        self.settings = load_ecn_config()["wecom"]
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
        self.resolver = AsyncMock(return_value="debug_manager_wx")
        self.sender_patch = patch.object(notifications, "send_wecom_text_message", self.sender)
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

    async def test_test_mode_routes_only_to_manager_and_shows_intended_people(self):
        self.assertEqual(await self.check(), (1, 0))
        args, kwargs = self.sender.call_args
        self.assertEqual(args[1], "debug_manager_wx")
        self.assertIn("原应通知人员：writer", args[0])
        self.assertNotIn("inactive", args[0])
        self.assertFalse(kwargs["retry_tracking"])
        self.assertFalse(kwargs["alert_on_max_failure"])
        self.resolver.assert_awaited_once_with([{"position": "研发经理"}], fallback_touser="")

    async def test_missing_debug_recipient_does_not_fallback_to_real_people(self):
        self.resolver.return_value = ""
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_not_awaited()

    async def test_switch_to_production_notifies_real_binding_without_debug_route(self):
        await self.check()
        self.sender.reset_mock()
        self.resolver.reset_mock()
        self.settings["test_mode"] = False
        self.assertEqual(await self.check(), (1, 0))
        self.assertEqual(self.sender.call_args.args[1], "writer_wx")
        self.assertNotIn("调试转发", self.sender.call_args.args[0])
        self.resolver.assert_not_awaited()

    async def test_unbound_real_recipient_is_skipped(self):
        self.settings["test_mode"] = False
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
        self.assertIn("原应通知人员：manager", self.sender.call_args.args[0])

    async def test_closed_record_does_not_retry_failed_message(self):
        self.sender.return_value = (False, "模拟网络失败")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (0, 1))
        self.record["workflow"]["current_state"] = ECNState.CLOSED
        await self.save_record()
        self.sender.reset_mock()
        self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_not_awaited()

    async def test_failure_retries_after_configured_interval_only(self):
        self.sender.return_value = (False, "模拟网络失败")
        with self.assertLogs(level="WARNING"):
            self.assertEqual(await self.check(), (0, 1))
        self.sender.return_value = (True, "ok")
        self.assertEqual(await self.check(), (0, 0))
        await self.storage.atomic_deep_update(
            [notifications.NOTIFICATION_STATE_KEY, "ECN-test"],
            lambda entry: {
                **entry,
                "recipients": {key: {**value, "attempted_at": 0} for key, value in entry["recipients"].items()},
            },
        )
        self.assertEqual(await self.check(), (1, 0))

    async def test_final_pending_check_skips_record_closed_during_resolution(self):
        async def resolve(*args, **kwargs):
            self.record["workflow"]["current_state"] = ECNState.CLOSED
            await self.save_record()
            return "debug_manager_wx"

        self.resolver.side_effect = resolve
        self.assertEqual(await self.check(), (0, 0))
        self.sender.assert_not_awaited()


class ECNWecomConfigTests(unittest.TestCase):
    def test_default_is_debug_and_invalid_switch_does_not_enable_production(self):
        config = load_ecn_config({"wecom": {"test_mode": "false", "enabled": False, "check_interval_seconds": -1}})[
            "wecom"
        ]
        self.assertTrue(config["test_mode"])
        self.assertFalse(config["enabled"])
        self.assertEqual(config["check_interval_seconds"], 60)
        self.assertEqual(config["test_notify_targets"], [{"position": "研发经理"}])


if __name__ == "__main__":
    unittest.main()
