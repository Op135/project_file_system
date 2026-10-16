import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from src import wecom_service
from src.modules.dev_identity_switcher.guard import assert_local_dev_environment
from src.modules.dev_identity_switcher.state import LocalDevRuntimePolicy
from src.runtime_controls import (
    allows_background_task,
    clear_runtime_effect_policy,
    decide_wecom_delivery,
    has_runtime_effect_policy,
    install_runtime_effect_policy,
)

PROJECT_ROOT = Path(__file__).parents[1]


def _load_business_config(filename: str) -> dict[str, Any]:
    raw = json.loads((PROJECT_ROOT / filename).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AssertionError(f"{filename} 顶层必须是 JSON 对象")
    return cast(dict[str, Any], raw)


class CheckedInBusinessConfigTests(unittest.TestCase):
    def test_external_effect_settings_match_server_operation(self):
        ecn_wecom = _load_business_config("ecn_management_config.json").get("wecom")
        error_reminders = _load_business_config("error_management_config.json").get("reminders")
        sample_issue_reminders = _load_business_config("sample_issue_collection_config.json").get("reminders")
        sample_order_wecom = _load_business_config("sample_order_dashboard_config.json").get("wecom")

        if not all(
            isinstance(section, dict)
            for section in (ecn_wecom, error_reminders, sample_issue_reminders, sample_order_wecom)
        ):
            self.fail("四份业务配置缺少外部副作用配置段")
        assert isinstance(ecn_wecom, dict)
        assert isinstance(error_reminders, dict)
        assert isinstance(sample_issue_reminders, dict)
        assert isinstance(sample_order_wecom, dict)
        self.assertNotIn("test_mode", ecn_wecom)
        self.assertTrue(error_reminders.get("background_enabled"))
        self.assertTrue(sample_issue_reminders.get("background_enabled"))
        self.assertNotIn("redirect_applicant_notifications_to_manager", sample_order_wecom)


class RuntimeControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_runtime_effect_policy()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(clear_runtime_effect_policy)

    def test_production_default_has_no_behavior_change(self):
        self.assertFalse(has_runtime_effect_policy())
        decision = decide_wecom_delivery(
            ("user-a",),
            module="test",
            business_key="record-1",
            message_type="event",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.recipients, ("user-a",))
        self.assertTrue(allows_background_task("ecn.reminders"))

    async def test_local_default_blocks_without_calling_wecom(self):
        policy = LocalDevRuntimePolicy(Path(self.temp_dir.name) / "state.json")
        install_runtime_effect_policy(policy)
        self.assertTrue(has_runtime_effect_policy())
        with (
            patch.object(wecom_service, "_send_one_text_message", AsyncMock()) as sender,
            patch.object(wecom_service, "_append_log", AsyncMock()) as append_log,
        ):
            success, message = await wecom_service.send_wecom_text_message(
                "提醒内容",
                "real-user",
                module="test",
                business_key="record-1",
            )
        self.assertTrue(success)
        self.assertIn("已拦截", message)
        sender.assert_not_awaited()
        append_log.assert_awaited_once()
        append_log_call = append_log.await_args
        assert append_log_call is not None
        self.assertTrue(append_log_call.args[0]["suppressed"])
        self.assertFalse(allows_background_task("wecom.retry"))

    async def test_redirect_replaces_recipient_and_marks_content(self):
        state_path = Path(self.temp_dir.name) / "state.json"
        with patch.dict("os.environ", {"DEV_WECOM_RECIPIENT_ALLOWLIST": "developer-a|developer-b"}):
            policy = LocalDevRuntimePolicy(state_path)
            policy.update(
                wecom_mode="redirect",
                redirect_recipients=("developer-a",),
                background_tasks_enabled=True,
            )
            install_runtime_effect_policy(policy)
            with (
                patch.object(
                    wecom_service,
                    "_send_one_text_message",
                    AsyncMock(return_value=(True, "sent")),
                ) as sender,
                patch.object(wecom_service, "_append_log", AsyncMock()),
                patch.object(wecom_service, "_clear_retry_failure", AsyncMock()),
            ):
                success, _ = await wecom_service.send_wecom_text_message(
                    "提醒内容",
                    "real-user",
                    module="test",
                    business_key="record-1",
                )
        self.assertTrue(success)
        sender_call = sender.await_args
        assert sender_call is not None
        sent_content, sent_recipient = sender_call.args
        self.assertEqual(sent_recipient, "developer-a")
        self.assertIn("原收件人：real-user", sent_content)
        self.assertTrue(allows_background_task("ecn.reminders"))

    async def test_redirect_uses_resolved_real_name_in_content(self):
        state_path = Path(self.temp_dir.name) / "state.json"
        with patch.dict("os.environ", {"DEV_WECOM_RECIPIENT_ALLOWLIST": "developer-a"}):
            policy = LocalDevRuntimePolicy(
                state_path,
                recipient_label_resolver=lambda recipients: tuple(
                    "王铁华" if value == "YangTieHua" else value
                    for value in recipients
                ),
            )
            policy.update(
                wecom_mode="redirect",
                redirect_recipients=("developer-a",),
                background_tasks_enabled=False,
            )
            install_runtime_effect_policy(policy)
            with (
                patch.object(
                    wecom_service,
                    "_send_one_text_message",
                    AsyncMock(return_value=(True, "sent")),
                ) as sender,
                patch.object(wecom_service, "_append_log", AsyncMock()),
                patch.object(wecom_service, "_clear_retry_failure", AsyncMock()),
            ):
                success, _ = await wecom_service.send_wecom_text_message(
                    "提醒内容",
                    "YangTieHua|coo",
                    module="test",
                    business_key="record-1",
                )

        self.assertTrue(success)
        sender_call = sender.await_args
        assert sender_call is not None
        sent_content, _sent_recipient = sender_call.args
        self.assertIn("原收件人：王铁华、coo", sent_content)

    async def test_redirect_rewrites_card_link_away_from_production(self):
        state_path = Path(self.temp_dir.name) / "state.json"
        environment = {
            "DEV_WECOM_RECIPIENT_ALLOWLIST": "developer-a",
            "DEV_CARD_BASE_URL": "http://127.0.0.1:9090",
        }
        with patch.dict("os.environ", environment):
            policy = LocalDevRuntimePolicy(state_path)
            policy.update(
                wecom_mode="redirect",
                redirect_recipients=("developer-a",),
                background_tasks_enabled=False,
            )
            install_runtime_effect_policy(policy)
            with (
                patch.object(
                    wecom_service,
                    "_send_one_application_message",
                    AsyncMock(return_value=(True, "sent")),
                ) as sender,
                patch.object(wecom_service, "_append_log", AsyncMock()),
            ):
                success, _ = await wecom_service.send_wecom_textcard_message(
                    "卡片内容",
                    "real-user",
                    title="卡片标题",
                    link_url="https://production.example/ecn_management?id=1",
                    module="test",
                    business_key="record-1",
                )
        self.assertTrue(success)
        sender_call = sender.await_args
        assert sender_call is not None
        _, body, recipient = sender_call.args
        self.assertEqual(recipient, "developer-a")
        self.assertEqual(body["url"], "http://127.0.0.1:9090/ecn_management?id=1")
        self.assertTrue(body["title"].startswith("【本地调试】"))

    def test_guard_rejects_non_loopback_host(self):
        environment = {
            "APP_ENV": "development",
            "ENABLE_DEV_IDENTITY_SWITCHER": "1",
            "DEV_TOOLS_PASSWORD": "local-secret",
        }
        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(RuntimeError, "只能监听"):
                assert_local_dev_environment("0.0.0.0")
            assert_local_dev_environment("127.0.0.1")

    def test_new_process_policy_always_restores_safe_defaults(self):
        state_path = Path(self.temp_dir.name) / "state.json"
        with patch.dict("os.environ", {"DEV_WECOM_RECIPIENT_ALLOWLIST": "developer-a"}):
            first = LocalDevRuntimePolicy(state_path)
            first.update(
                wecom_mode="redirect",
                redirect_recipients=("developer-a",),
                background_tasks_enabled=True,
            )
            restarted = LocalDevRuntimePolicy(state_path).snapshot()
        self.assertEqual(restarted.wecom_mode, "blocked")
        self.assertFalse(restarted.background_tasks_enabled)
        self.assertEqual(restarted.redirect_recipients, ("developer-a",))


if __name__ == "__main__":
    unittest.main()
