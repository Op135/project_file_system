import unittest
from unittest.mock import AsyncMock, patch

from src import workflow_notifications


class WorkflowCompletionNotificationTests(unittest.IsolatedAsyncioTestCase):
    def test_completion_cc_is_additive_and_disabled_by_default(self):
        self.assertEqual(workflow_notifications.completion_cc_position_ids({}), [])
        self.assertEqual(
            workflow_notifications.completion_cc_position_ids(
                {
                    "notification": {
                        "notify_assignees": True,
                        "notify_requester_on_result": True,
                        "completion_cc": {
                            "enabled": True,
                            "position_ids": ["position-a", "position-a", "position-b"],
                        },
                    }
                }
            ),
            ["position-a", "position-b"],
        )

    def test_original_and_cc_recipients_are_deduplicated(self):
        self.assertEqual(
            workflow_notifications.merge_wecom_userids(
                "approver|requester",
                "requester|observer",
            ),
            "approver|requester|observer",
        )

    async def test_completion_cc_sends_only_configured_position_recipients(self):
        assignment = {
            "notification": {
                "completion_cc": {
                    "enabled": True,
                    "position_ids": ["position-observer"],
                }
            }
        }
        with (
            patch.object(
                workflow_notifications,
                "resolve_position_wecom_recipients",
                AsyncMock(return_value="observer-userid"),
            ) as resolver,
            patch.object(
                workflow_notifications,
                "send_wecom_textcard_message",
                AsyncMock(return_value=(True, "发送成功")),
            ) as sender,
        ):
            success, _message = await workflow_notifications.send_workflow_completion_cc(
                assignment,
                title="流程已通过",
                lines=("单号：A001", "结果：全部通过"),
                link_url="http://system.local/detail",
                module="test_module",
                business_key="A001:completion_cc",
            )

        self.assertTrue(success)
        resolver.assert_awaited_once_with(["position-observer"], user_service=None)
        sender.assert_awaited_once()
        await_args = sender.await_args
        assert await_args is not None
        self.assertEqual(await_args.args[1], "observer-userid")


if __name__ == "__main__":
    unittest.main()
