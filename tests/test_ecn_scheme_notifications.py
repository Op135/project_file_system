import unittest
from unittest.mock import AsyncMock, patch

from src.modules.ecn import scheme_notifications


class SchemeSalesUsers:
    storage_mode = "database"

    def __init__(self):
        self.users = {
            "admin": {"role": "admin", "status": "active"},
            "sales": {"role": "销售", "status": "active", "display_name": "销售甲"},
            "supervisor": {"role": "销售主管", "status": "active"},
            "director": {"role": "销售总监", "status": "active"},
        }
        self.memberships = {
            "admin": {"position_name": "系统管理员", "manager_username": ""},
            "sales": {"position_name": "项目销售", "manager_username": "supervisor"},
            "supervisor": {"position_name": "销售主管", "manager_username": "director"},
            "director": {"position_name": "销售总监", "manager_username": ""},
        }
        self.bindings = {
            "admin": {"external_userid": "wx-admin"},
            "sales": {"external_userid": "wx-sales"},
            "supervisor": {"external_userid": "wx-supervisor"},
            "director": {"external_userid": "wx-director"},
        }

    def load_users(self):
        return self.users

    def list_primary_memberships(self):
        return self.memberships

    def list_wecom_bindings(self):
        return self.bindings


class SchemeSalesNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SchemeSalesUsers()
        self.record = {
            "ecn_id": "ECN-SALES-001",
            "basic_info": {"title": "销售通知测试"},
            "target_projects": ["P1", "P2"],
            "review_info": {
                "expanded_projects_mass": [],
                "expanded_projects_non_mass": [],
            },
        }

    def routes(self, mapping):
        return scheme_notifications.resolve_scheme_sales_notification_routes(
            self.record,
            mapping,
            user_service=self.service,
        )

    def test_bound_project_sales_receives_directly(self):
        routes = self.routes({"P1": "sales", "P2": "销售甲"})
        self.assertEqual([route["recipient_usernames"] for route in routes], [["sales"], ["sales"]])
        self.assertFalse(any(route["escalated"] for route in routes))

    def test_unbound_sales_escalates_to_first_deliverable_manager(self):
        self.service.bindings.pop("sales")
        routes = self.routes({"P1": "sales", "P2": "sales"})
        self.assertEqual([route["recipient_usernames"] for route in routes], [["supervisor"], ["supervisor"]])
        self.assertTrue(all(route["escalated"] for route in routes))

    def test_unbound_supervisor_continues_to_director(self):
        self.service.bindings.pop("sales")
        self.service.bindings.pop("supervisor")
        routes = self.routes({"P1": "sales", "P2": "未指定"})
        self.assertEqual([route["recipient_usernames"] for route in routes], [["director"], ["director"]])

    def test_system_admin_is_not_used_as_project_sales_recipient(self):
        routes = self.routes({"P1": "admin", "P2": "admin"})
        self.assertEqual(
            [route["recipient_usernames"] for route in routes],
            [["supervisor"], ["supervisor"]],
        )
        self.assertNotIn("admin", {
            username
            for route in routes
            for username in route["recipient_usernames"]
        })

    async def test_test_mode_only_sends_manager_and_lists_intended_people(self):
        config = {
            "enabled": True,
            "test_mode": True,
            "test_notify_targets": [{"position": "研发经理"}],
            "cc_manager_enabled": True,
            "public_base_url": "https://example.test",
        }
        with (
            patch.object(
                scheme_notifications,
                "resolve_wecom_recipients",
                AsyncMock(return_value="wx-rd-manager"),
            ),
            patch.object(
                scheme_notifications,
                "send_wecom_textcard_message",
                AsyncMock(return_value=(True, "ok")),
            ) as sender,
        ):
            result = await scheme_notifications.send_scheme_sales_completion_notifications(
                self.record,
                {"P1": "sales", "P2": "sales"},
                approval_round="round-1",
                config=config,
                user_service=self.service,
            )

        self.assertEqual(result, (1, 0))
        await_args = sender.await_args
        assert await_args is not None
        self.assertEqual(await_args.args[1], "wx-rd-manager")
        self.assertIn("未通知实际项目销售", await_args.args[0])
        self.assertIn("通知人员：sales", await_args.args[0])

    async def test_production_merges_projects_for_same_sales_person(self):
        config = {
            "enabled": True,
            "test_mode": False,
            "test_notify_targets": [],
            "cc_manager_enabled": False,
            "public_base_url": "https://example.test",
        }
        with patch.object(
            scheme_notifications,
            "send_wecom_textcard_message",
            AsyncMock(return_value=(True, "ok")),
        ) as sender:
            result = await scheme_notifications.send_scheme_sales_completion_notifications(
                self.record,
                {"P1": "sales", "P2": "sales"},
                approval_round="round-1",
                config=config,
                user_service=self.service,
            )

        self.assertEqual(result, (1, 0))
        sender.assert_awaited_once()
        await_args = sender.await_args
        assert await_args is not None
        self.assertIn("涉及项目：P1、P2", await_args.args[0])


if __name__ == "__main__":
    unittest.main()
