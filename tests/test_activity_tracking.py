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
            patch.object(utils.Client, "instances", self.instances),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        utils.setup_global_activity_tracking()
        self.heartbeat = self.ui.timer.call_args.args[1]
        self.cleanup = self.client.on_delete.call_args.args[0]

    async def test_connected_client_updates_activity(self):
        await self.heartbeat()
        self.client.run_javascript.assert_awaited_once()
        self.assertEqual(utils.online_users[self.client.id]["last_activity_ts"], 123.0)

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
        self.assertEqual(utils.online_users[self.client.id]["last_activity_ts"], 123.0)

    async def test_delete_cancels_current_invocation_and_removes_online_record(self):
        self.cleanup()
        self.timer.cancel.assert_called_once_with(with_current_invocation=True)
        self.assertNotIn(self.client.id, utils.online_users)

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
        self.assertEqual(utils.online_users[self.client.id]["last_activity_ts"], 123.0)


if __name__ == "__main__":
    unittest.main()
