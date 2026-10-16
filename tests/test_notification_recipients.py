import unittest
from types import SimpleNamespace
from src import notification_recipients


class NotificationRecipientTests(unittest.IsolatedAsyncioTestCase):
    def test_wecom_recipient_display_names_use_bound_real_names(self):
        service = SimpleNamespace(
            list_wecom_bindings=lambda: {
                "zhangsan": {
                    "external_userid": "wecom-zhangsan",
                    "external_display_name": "张三",
                },
                "lisi": {
                    "external_userid": "wecom-lisi",
                    "external_display_name": "",
                },
            },
            load_users=lambda: {
                "zhangsan": {"display_name": "张三（系统）"},
                "lisi": {"display_name": "李四"},
            },
        )

        labels = notification_recipients.resolve_wecom_recipient_display_names(
            ["wecom-zhangsan", "wecom-lisi", "unknown-user"],
            user_service=service,
        )

        self.assertEqual(labels, ("张三", "李四", "unknown-user"))

    def test_position_recipients_exclude_system_admin(self):
        service = SimpleNamespace(
            load_users=lambda: {
                "admin": {"status": "active"},
                "业务人员": {"status": "active"},
            },
            list_primary_memberships=lambda: {
                "admin": {"position_id": "position-observer"},
                "业务人员": {"position_id": "position-observer"},
            },
        )
        usernames, missing = notification_recipients.resolve_position_usernames(
            ["position-observer"],
            user_service=service,
        )
        self.assertEqual(usernames, ["业务人员"])
        self.assertEqual(missing, [])

    async def test_database_mode_resolves_permission_users_from_wecom_bindings(self):
        """数据库模式只返回拥有权限且已绑定企业微信的用户。"""
        service = SimpleNamespace(
            list_usernames_with_permission=lambda *_args, **_kwargs: ["张三", "李四"],
            list_wecom_bindings=lambda: {
                "张三": {"external_userid": "wecom-zhangsan"},
            },
        )
        recipients = await notification_recipients.resolve_permission_wecom_recipients(
            "notifications.sample_order.extension.receive",
            user_service=service,
        )

        self.assertEqual(recipients, "wecom-zhangsan")

    async def test_database_mode_without_subscribers_returns_empty(self):
        service = SimpleNamespace(
            list_usernames_with_permission=lambda *_args, **_kwargs: [],
            list_wecom_bindings=lambda: {},
        )
        recipients = await notification_recipients.resolve_permission_wecom_recipients(
            "notifications.error.extension.request.receive",
            user_service=service,
        )

        self.assertEqual(recipients, "")


if __name__ == "__main__":
    unittest.main()
