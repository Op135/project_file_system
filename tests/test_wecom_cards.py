"""卡片发送协议回归；模拟 HTTP、令牌和日志，不触达企业微信。"""

import unittest
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
