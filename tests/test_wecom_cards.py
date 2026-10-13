"""卡片发送协议回归；模拟 HTTP、令牌和日志，不触达企业微信。"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src import wecom_service as wecom


class WecomCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncMock()
        self.response = MagicMock()
        self.response.json.return_value = {"errcode": 0}
        self.client.post.return_value = self.response
        self.client.__aenter__.return_value = self.client
        self.patches = [
            patch.object(wecom, "_get_wecom_access_token", AsyncMock(return_value=(True, "fake-token"))),
            patch.object(wecom.httpx, "AsyncClient", return_value=self.client),
            patch.object(wecom, "_append_log", AsyncMock()),
            patch.object(wecom, "WECOM_AGENT_ID", "123"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    async def send(self, **kwargs):
        args = dict(
            title="🔧【ECN工程变更】待办提醒",
            link_url="https://example.test/ecn_management",
            module="ecn_management",
            business_key="ECN-test",
        )
        args.update(kwargs)
        return await wecom.send_wecom_textcard_message('<div class="gray">调试转发</div>', "writer", **args)

    async def test_payload_is_textcard_with_button_and_no_public_retry(self):
        with patch.object(wecom, "_set_retry_failure", AsyncMock()) as retry:
            success, _ = await self.send()
            self.assertTrue(success)
            body = self.client.post.call_args.kwargs["json"]
            self.assertEqual(body["msgtype"], "textcard")
            self.assertEqual(body["agentid"], 123)
            self.assertEqual(body["touser"], "writer")
            self.assertEqual(body["textcard"]["btntxt"], "查看详情")
            self.assertEqual(body["textcard"]["url"], "https://example.test/ecn_management")
            self.response.json.return_value = {"errcode": 40014, "errmsg": "模拟失败"}
            success, _ = await self.send()
            self.assertFalse(success)
            retry.assert_not_awaited()

    async def test_invalid_user_and_network_failure_are_not_success(self):
        self.response.json.return_value = {"errcode": 0, "invaliduser": "writer"}
        self.assertFalse((await self.send())[0])
        self.client.post.side_effect = httpx.ConnectError("模拟断网")
        with self.assertLogs(level="ERROR"):
            self.assertFalse((await self.send())[0])

    async def test_invalid_card_is_rejected_before_network(self):
        self.assertFalse((await self.send(link_url=""))[0])
        self.assertFalse((await self.send(title="测" * 100))[0])
        self.client.post.assert_not_awaited()

    async def test_existing_text_protocol_is_unchanged(self):
        self.assertTrue((await wecom._send_one_text_message("样品问题提醒", "writer"))[0])
        body = self.client.post.call_args.kwargs["json"]
        self.assertEqual(body["msgtype"], "text")
        self.assertEqual(body["text"], {"content": "样品问题提醒"})
        self.assertNotIn("textcard", body)

    async def test_product_issue_reminder_retry_recognizes_new_and_legacy_titles(self):
        for title in ("产品问题纠正预防措施提醒", "生产异常纠正预防措施提醒"):
            self.assertTrue(
                wecom._is_state_dependent_reminder_retry(
                    {
                        "module": "error_management",
                        "content": f"{title}\n单据：ISSUE-001",
                    }
                )
            )

    async def test_public_retry_discards_state_dependent_reminders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            retry_path = Path(temp_dir) / "wecom_retry_state.json"
            reminder_content = "【样品单逾期未填新交期提醒】\n样品单号：Y001"
            event_content = "样品单延期通知\n样品单号：Y002"
            reminder_key = wecom._retry_key(
                "sample_order_dashboard",
                "record-1:overdue:2026-10-13:2026-10-01:",
                "leader",
                reminder_content,
            )
            event_key = wecom._retry_key(
                "sample_order_dashboard",
                "record-2:extension:extension-1",
                "leader",
                event_content,
            )
            state = {
                reminder_key: {
                    "module": "sample_order_dashboard",
                    "business_key": "record-1:overdue:2026-10-13:2026-10-01:",
                    "recipient": "leader",
                    "content": reminder_content,
                    "attempts": wecom.WECOM_MAX_RETRY_COUNT,
                    "message_type": "overdue_target_missing",
                },
                event_key: {
                    "module": "sample_order_dashboard",
                    "business_key": "record-2:extension:extension-1",
                    "recipient": "leader",
                    "content": event_content,
                    "attempts": 1,
                    "message_type": "extension_created",
                },
            }
            with patch.object(wecom, "WECOM_RETRY_STATE_PATH", retry_path):
                await wecom._write_retry_state(state)
                success_count, fail_count = await wecom.retry_failed_wecom_messages()
                remaining = await wecom._read_retry_state()

            self.assertEqual((success_count, fail_count), (1, 0))
            self.assertNotIn(reminder_key, remaining)
            self.assertNotIn(event_key, remaining)
