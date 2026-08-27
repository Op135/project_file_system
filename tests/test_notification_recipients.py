import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import notification_recipients


class NotificationRecipientTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_mode_resolves_permission_users_from_wecom_bindings(self):
        """数据库模式只返回拥有权限且已绑定企业微信的用户。"""
        service = SimpleNamespace(
            storage_mode="database",
            list_usernames_with_permission=lambda *_args, **_kwargs: ["张三", "李四"],
            list_wecom_bindings=lambda: {
                "张三": {"external_userid": "wecom-zhangsan"},
            },
        )
        legacy_resolver = AsyncMock(return_value="legacy-userid")
        with patch.object(
            notification_recipients,
            "resolve_wecom_recipients",
            legacy_resolver,
        ):
            recipients = await notification_recipients.resolve_permission_wecom_recipients(
                "notifications.sample_order.extension.receive",
                legacy_targets=[{"position": "研发经理"}],
                user_service=service,
            )

        self.assertEqual(recipients, "wecom-zhangsan")
        legacy_resolver.assert_not_awaited()

    async def test_database_mode_does_not_fall_back_to_legacy_role_targets(self):
        """数据库模式无订阅人时保持为空，避免旧职位规则重新参与路由。"""
        service = SimpleNamespace(
            storage_mode="database",
            list_usernames_with_permission=lambda *_args, **_kwargs: [],
            list_wecom_bindings=lambda: {},
        )
        legacy_resolver = AsyncMock(return_value="legacy-userid")
        with patch.object(
            notification_recipients,
            "resolve_wecom_recipients",
            legacy_resolver,
        ):
            recipients = await notification_recipients.resolve_permission_wecom_recipients(
                "notifications.error.extension.request.receive",
                legacy_targets=[{"position": "研发助理"}],
                user_service=service,
            )

        self.assertEqual(recipients, "")
        legacy_resolver.assert_not_awaited()

    async def test_excel_mode_keeps_legacy_recipient_rules(self):
        """服务器未迁移用户时仍按原 JSON 目标解析，部署新代码不会改变通知。"""
        service = SimpleNamespace(storage_mode="legacy_excel")
        legacy_targets = [{"position": "研发经理"}]
        legacy_resolver = AsyncMock(return_value="legacy-userid")
        with patch.object(
            notification_recipients,
            "resolve_wecom_recipients",
            legacy_resolver,
        ):
            recipients = await notification_recipients.resolve_permission_wecom_recipients(
                "notifications.sample_order.extension.receive",
                legacy_targets=legacy_targets,
                user_service=service,
            )

        self.assertEqual(recipients, "legacy-userid")
        legacy_resolver.assert_awaited_once_with(legacy_targets, fallback_touser="")


if __name__ == "__main__":
    unittest.main()
