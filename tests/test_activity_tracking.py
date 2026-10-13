import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src import utils


class ActivityTrackingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            id="heartbeat-test",
            ip="127.0.0.1",
            has_socket_connection=True,
            run_javascript=AsyncMock(return_value=123000.0),
            on_delete=Mock(),
        )
        self.timer = Mock()
        self.ui = SimpleNamespace(
            context=SimpleNamespace(client=self.client),
            add_head_html=Mock(),
            timer=Mock(return_value=self.timer),
        )
        self.instances = {self.client.id: self.client}
        for patcher in (
            patch.object(utils, "ui", self.ui),
            patch.object(utils, "app", SimpleNamespace(storage=SimpleNamespace(user={"current_user": "tester"}))),
            patch.object(utils, "online_users", {}),
            patch.object(utils, "_client_page_modules", {}),
            patch.object(utils.Client, "instances", self.instances),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        utils.setup_global_activity_tracking("ecn_management")
        self.heartbeat = self.ui.timer.call_args.args[1]
        self.cleanup = self.client.on_delete.call_args.args[0]

    def test_records_page_module_without_url_details(self):
        record = utils.online_users[self.client.id]
        self.assertEqual(record.get("page_key"), "ecn_management")
        self.assertEqual(record.get("page_label"), "工程变更")
        self.assertIn("page_entered_ts", record)

    def test_socket_reconnect_preserves_page_module(self):
        original_entered_ts = utils.online_users[self.client.id].get("page_entered_ts")

        utils.handle_disconnect(self.client)
        utils.handle_connect(self.client)

        record = utils.online_users[self.client.id]
        self.assertEqual(record.get("page_key"), "ecn_management")
        self.assertEqual(record.get("page_label"), "工程变更")
        self.assertEqual(record.get("page_entered_ts"), original_entered_ts)

    async def test_connected_client_updates_activity(self):
        await self.heartbeat()
        self.client.run_javascript.assert_awaited_once()
        self.assertEqual(utils.online_users[self.client.id].get("last_activity_ts"), 123.0)

    async def test_queued_callback_after_deletion_does_not_send_javascript(self):
        self.instances.clear()
        await self.heartbeat()
        self.client.run_javascript.assert_not_called()
        self.timer.cancel.assert_called_once()

    async def test_disconnect_skips_heartbeat_and_reconnect_resumes(self):
        self.client.has_socket_connection = False
        await self.heartbeat()
        self.client.run_javascript.assert_not_called()
        self.timer.cancel.assert_not_called()
        self.client.has_socket_connection = True
        await self.heartbeat()
        self.assertEqual(utils.online_users[self.client.id].get("last_activity_ts"), 123.0)

    async def test_delete_cancels_current_invocation_and_removes_online_record(self):
        self.cleanup()
        self.timer.cancel.assert_called_once_with(with_current_invocation=True)
        self.assertNotIn(self.client.id, utils.online_users)
        self.assertNotIn(self.client.id, utils._client_page_modules)

    async def test_deletion_while_waiting_does_not_update_activity(self):
        original = utils.online_users[self.client.id].copy()
        started = asyncio.Event()
        result = asyncio.get_running_loop().create_future()

        async def wait_for_response(*args, **kwargs):
            started.set()
            return await result

        self.client.run_javascript.side_effect = wait_for_response
        task = asyncio.create_task(self.heartbeat())
        await asyncio.wait_for(started.wait(), timeout=1)
        self.instances.clear()
        result.set_result(123000.0)
        await task
        self.assertEqual(utils.online_users[self.client.id], original)

    async def test_timeout_allows_next_heartbeat(self):
        self.client.run_javascript.side_effect = [TimeoutError(), 123000.0]
        await self.heartbeat()
        self.timer.cancel.assert_not_called()
        await self.heartbeat()
        self.assertEqual(utils.online_users[self.client.id].get("last_activity_ts"), 123.0)

    def test_summarizes_multiple_tabs_using_latest_activity(self):
        utils.online_users.clear()
        utils.online_users.update(
            {
                "main-tab": {
                    "username": "tester",
                    "last_seen_ts": 195.0,
                    "last_activity_ts": 180.0,
                    "page_label": "首页",
                    "page_entered_ts": 100.0,
                },
                "ecn-tab": {
                    "username": "tester",
                    "last_seen_ts": 198.0,
                    "last_activity_ts": 190.0,
                    "page_label": "工程变更",
                    "page_entered_ts": 150.0,
                },
                "expired-tab": {
                    "username": "expired",
                    "last_seen_ts": 100.0,
                    "last_activity_ts": 199.0,
                    "page_label": "统计信息",
                    "page_entered_ts": 90.0,
                },
            }
        )

        summaries = utils.summarize_online_users(current_time=200.0, threshold_seconds=60.0)

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary["username"], "tester")
        self.assertEqual(summary["current_page"], "工程变更")
        self.assertEqual(summary["open_pages"], ["工程变更", "首页"])
        self.assertEqual(summary["last_seen_ts"], 198.0)
        self.assertEqual(summary["last_activity_ts"], 190.0)

    def test_latest_page_entry_breaks_equal_activity_timestamps(self):
        utils.online_users.clear()
        utils.online_users.update(
            {
                "old-tab": {
                    "username": "tester",
                    "last_seen_ts": 200.0,
                    "last_activity_ts": 190.0,
                    "page_label": "首页",
                    "page_entered_ts": 100.0,
                },
                "new-tab": {
                    "username": "tester",
                    "last_seen_ts": 200.0,
                    "last_activity_ts": 190.0,
                    "page_label": "工程变更",
                    "page_entered_ts": 180.0,
                },
            }
        )

        summaries = utils.summarize_online_users(current_time=200.0)

        self.assertEqual(summaries[0]["current_page"], "工程变更")

    def test_tooltip_lists_pages_and_escapes_dynamic_text(self):
        summaries: list[utils.OnlineUserSummary] = [
            {
                "username": "<tester>",
                "last_seen_ts": 200.0,
                "last_activity_ts": 190.0,
                "current_page": "工程变更",
                "open_pages": ["工程变更", "首页<script>"],
            }
        ]

        tooltip = utils.build_online_users_tooltip(summaries)

        self.assertIn("&lt;tester&gt; — 工程变更 — 最后操作:", tooltip)
        self.assertIn("同时打开: 首页&lt;script&gt;", tooltip)
        self.assertNotIn("同时打开: 工程变更", tooltip)
        self.assertNotIn("<script>", tooltip)
        self.assertEqual(tooltip.count("<br>"), 1)

    def test_tooltip_omits_open_pages_when_only_current_page_is_open(self):
        summaries: list[utils.OnlineUserSummary] = [
            {
                "username": "tester",
                "last_seen_ts": 200.0,
                "last_activity_ts": 190.0,
                "current_page": "首页",
                "open_pages": ["首页"],
            }
        ]

        tooltip = utils.build_online_users_tooltip(summaries)

        self.assertNotIn("同时打开", tooltip)


if __name__ == "__main__":
    unittest.main()
