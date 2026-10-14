import unittest
from types import SimpleNamespace
from src import notification_recipients


class NotificationRecipientTests(unittest.IsolatedAsyncioTestCase):
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
            legacy_targets=[{"position": "研发经理"}],
            user_service=service,
        )

        self.assertEqual(recipients, "wecom-zhangsan")

    async def test_database_mode_does_not_fall_back_to_legacy_role_targets(self):
        """数据库模式无订阅人时保持为空，避免旧职位规则重新参与路由。"""
        service = SimpleNamespace(
            list_usernames_with_permission=lambda *_args, **_kwargs: [],
            list_wecom_bindings=lambda: {},
        )
        recipients = await notification_recipients.resolve_permission_wecom_recipients(
            "notifications.error.extension.request.receive",
            legacy_targets=[{"position": "研发助理"}],
            user_service=service,
        )

        self.assertEqual(recipients, "")


if __name__ == "__main__":
    unittest.main()
