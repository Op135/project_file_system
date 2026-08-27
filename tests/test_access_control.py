import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.access_control import can_use_tool
from src.permission_catalog import (
    ERROR_NOTIFICATION_MODULE,
    ERROR_RECORD_EDIT_PERMISSION,
    ERROR_VIEW_PERMISSION,
    SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
    SAMPLE_ORDER_NOTIFICATION_MODULE,
    SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION,
    SAMPLE_ORDER_BASE_EDIT_PERMISSION,
    SAMPLE_ORDER_VIEW_PERMISSION,
    tool_permission_code,
)
from src.user_service import UserService


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        data_dir = root / "data"
        data_dir.mkdir()
        self.excel_path = data_dir / "users.xlsx"
        self.db_path = root / "identity.db"
        (root / "tools_permission.json").write_text(
            json.dumps(
                {
                    "mode_calc": ["admin", "研发硬件"],
                    "pixel_statistics": ["admin"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pd.DataFrame(
            [
                {"用户名": "admin", "密码": "admin-pass", "角色": "admin"},
                {"用户名": "张三", "密码": "123456", "角色": "研发硬件"},
            ]
        ).to_excel(self.excel_path, index=False, engine="openpyxl")
        self.service = UserService(
            excel_path=self.excel_path,
            db_path=self.db_path,
            password_iterations=1_000,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_excel_mode_uses_exact_legacy_fallback(self):
        self.assertTrue(
            self.service.has_permission(
                "admin",
                "system.manage",
                legacy_role="普通用户",
                legacy_allowed_roles=[],
            )
        )
        self.assertTrue(
            can_use_tool(
                self.service,
                "张三",
                "mode_calc",
                legacy_role="研发硬件",
                legacy_allowed_roles=["研发硬件"],
            )
        )
        self.assertFalse(
            can_use_tool(
                self.service,
                "张三",
                "mode_calc",
                legacy_role="研发硬件",
                legacy_allowed_roles=["研发经理"],
            )
        )

    def test_migration_registers_stable_permissions(self):
        self.service.migrate_legacy_users()

        codes = {item["code"] for item in self.service.list_permissions()}
        self.assertIn(tool_permission_code("mode_calc"), codes)
        legacy_role = next(
            role
            for role in self.service.get_user_security_roles("张三")
            if role["code"].startswith("legacy.")
        )
        self.assertIn(tool_permission_code("mode_calc"), legacy_role["permission_codes"])
        self.assertFalse(self.service.has_permission("张三", tool_permission_code("mode_calc")))
        self.assertTrue(self.service.has_permission("admin", "system.manage"))
        self.assertFalse(self.service.has_permission("张三", "system.manage"))

    def test_database_mode_does_not_fall_back_to_legacy_role_lists(self):
        self.service.migrate_legacy_users()

        self.assertFalse(
            self.service.has_permission(
                "张三",
                "system.manage",
                legacy_role="admin",
                legacy_allowed_roles=["admin"],
            )
        )

    def test_admin_automatically_has_every_registered_permission(self):
        self.service.migrate_legacy_users()
        legacy_admin_role = next(
            role
            for role in self.service.get_user_security_roles("admin")
            if role["code"].startswith("legacy.")
        )
        self.service.update_security_role(
            legacy_admin_role["role_id"],
            name=legacy_admin_role["name"],
            permission_codes=[],
        )

        registered_codes = {item["code"] for item in self.service.list_permissions()}
        self.assertEqual(self.service.get_user_permission_codes("admin"), registered_codes)
        self.assertTrue(self.service.has_permission("admin", "system.manage"))
        self.assertFalse(self.service.has_permission("admin", "permission.code.does_not_exist"))

    def test_custom_role_can_be_assigned_and_combined(self):
        self.service.migrate_legacy_users()
        permission_code = tool_permission_code("mode_calc")
        role_id = self.service.create_security_role(
            code="tools.mode.reviewer",
            name="模式分析专员",
            permission_codes=[permission_code],
            actor_username="admin",
        )

        compatibility_roles = self.service.get_user_security_roles("张三")
        self.assertTrue(any(role["code"].startswith("legacy.") for role in compatibility_roles))

        self.service.set_user_security_roles(
            "张三",
            [role_id],
            actor_username="admin",
        )

        assigned = self.service.get_user_security_roles(
            "张三",
            include_compatibility=False,
        )
        self.assertEqual([role["code"] for role in assigned], ["tools.mode.reviewer"])
        self.assertIn(permission_code, self.service.get_user_permission_codes("张三"))
        connection = sqlite3.connect(self.db_path)
        try:
            actions = {
                row[0]
                for row in connection.execute(
                    "SELECT action FROM iam_audit_logs WHERE action LIKE '%security_role%'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertIn("security_role_created", actions)
        self.assertIn("user_security_roles_updated", actions)

    def test_manual_permission_removal_is_not_reversed_by_resync(self):
        self.service.migrate_legacy_users()
        legacy_role = next(
            role
            for role in self.service.get_user_security_roles("张三")
            if role["code"].startswith("legacy.")
        )
        permission_code = tool_permission_code("mode_calc")
        self.assertIn(permission_code, legacy_role["permission_codes"])

        self.service.update_security_role(
            legacy_role["role_id"],
            name=legacy_role["name"],
            permission_codes=[],
            actor_username="admin",
        )
        self.service.sync_permission_catalog()

        refreshed = next(
            role
            for role in self.service.get_user_security_roles("张三")
            if role["role_id"] == legacy_role["role_id"]
        )
        self.assertNotIn(permission_code, refreshed["permission_codes"])
        self.assertFalse(self.service.has_permission("张三", permission_code))

    def test_disabled_role_no_longer_grants_permission(self):
        self.service.migrate_legacy_users()
        permission_code = tool_permission_code("mode_calc")
        role_id = self.service.create_security_role(
            code="tools.temporary",
            name="临时工具权限",
            permission_codes=[permission_code],
        )
        legacy_role = next(
            role
            for role in self.service.get_user_security_roles("张三")
            if role["code"].startswith("legacy.")
        )
        self.service.update_security_role(
            legacy_role["role_id"],
            name=legacy_role["name"],
            permission_codes=[],
        )
        self.service.set_user_security_roles("张三", [role_id])
        self.assertTrue(self.service.has_permission("张三", permission_code))

        self.service.update_security_role(
            role_id,
            name="临时工具权限",
            status="disabled",
            permission_codes=[permission_code],
        )
        self.assertFalse(self.service.has_permission("张三", permission_code))

    def test_primary_position_grants_default_permission(self):
        self.service.migrate_legacy_users()
        permission_code = tool_permission_code("pixel_statistics")
        org_unit_id = self.service.save_org_unit(code="org.rd", name="研发部")
        position_id = self.service.save_position(code="rd.engineer", name="研发工程师")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )

        self.assertFalse(self.service.has_permission("张三", permission_code))
        self.service.set_position_permissions(
            position_id,
            [permission_code],
            actor_username="admin",
        )

        self.assertTrue(self.service.has_permission("张三", permission_code))
        self.assertIn(permission_code, self.service.get_user_permission_codes("张三"))
        position = next(
            item for item in self.service.list_positions() if item["position_id"] == position_id
        )
        self.assertEqual(position["permission_codes"], [permission_code])
        self.assertEqual(position["member_count"], 1)

        self.service.set_position_permissions(position_id, [], actor_username="admin")
        self.assertFalse(self.service.has_permission("张三", permission_code))

    def test_notification_subscription_uses_stable_permission_and_excludes_admin(self):
        """通知订阅按岗位权限找在职用户，系统管理员不会因全权限被自动订阅。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.notice", name="通知测试部")
        position_id = self.service.save_position(code="notice.receiver", name="通知接收岗")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(
            position_id,
            [SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION],
            actor_username="admin",
        )

        self.assertEqual(
            self.service.list_usernames_with_permission(SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION),
            ["张三"],
        )
        self.assertEqual(
            set(
                self.service.list_usernames_with_permission(
                    SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
                    include_system_admin=True,
                )
            ),
            {"admin", "张三"},
        )

    def test_notification_permissions_are_grouped_by_business_module(self):
        """样品单和异常单通知应在权限界面形成两个独立分组。"""
        self.service.migrate_legacy_users()
        notification_modules = {
            item["module"]
            for item in self.service.list_permissions()
            if str(item["code"]).startswith("notifications.")
        }
        self.assertEqual(
            notification_modules,
            {SAMPLE_ORDER_NOTIFICATION_MODULE, ERROR_NOTIFICATION_MODULE},
        )

    def test_broad_notification_permission_is_split_without_losing_assignments(self):
        """上一版宽权限应迁移到全部对应事件权限，并从管理目录移除。"""
        self.service.migrate_legacy_users()
        old_code = "notifications.sample_order.attention.receive"
        self.service.identity_store.seed_permission_catalog(
            [
                {
                    "code": old_code,
                    "name": "旧样品单关注通知",
                    "module": "通知接收",
                    "description": "测试旧权限迁移",
                }
            ]
        )
        org_unit_id = self.service.save_org_unit(code="org.notice.old", name="旧通知测试部")
        position_id = self.service.save_position(code="notice.old", name="旧通知接收岗")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(position_id, [old_code])

        self.service.sync_permission_catalog()

        permission_codes = {item["code"] for item in self.service.list_permissions()}
        self.assertNotIn(old_code, permission_codes)
        self.assertEqual(
            self.service.get_position_permission_codes(position_id),
            {
                SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
                SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION,
            },
        )

    def test_sample_order_permission_requires_new_position_or_additional_group(self):
        """样品单完成迁移后不得继续从兼容角色继承操作权限。"""
        self.service.migrate_legacy_users()
        registered_codes = {item["code"] for item in self.service.list_permissions()}
        self.assertIn(SAMPLE_ORDER_BASE_EDIT_PERMISSION, registered_codes)
        self.assertIn(SAMPLE_ORDER_VIEW_PERMISSION, registered_codes)
        legacy_role = next(
            role
            for role in self.service.get_user_security_roles("张三")
            if role["code"].startswith("legacy.")
        )
        self.service.update_security_role(
            legacy_role["role_id"],
            name=legacy_role["name"],
            permission_codes=[SAMPLE_ORDER_BASE_EDIT_PERMISSION],
        )
        self.assertFalse(
            self.service.has_permission("张三", SAMPLE_ORDER_BASE_EDIT_PERMISSION)
        )

        org_unit_id = self.service.save_org_unit(code="org.samples", name="样品组")
        position_id = self.service.save_position(code="sample.assistant", name="样品助理")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(
            position_id,
            [SAMPLE_ORDER_BASE_EDIT_PERMISSION],
            actor_username="admin",
        )

        self.assertTrue(
            self.service.has_permission("张三", SAMPLE_ORDER_BASE_EDIT_PERMISSION)
        )

    def test_error_permission_requires_new_position_or_additional_group(self):
        """异常模块迁移后不得继续从兼容角色继承操作权限。"""
        self.service.migrate_legacy_users()
        registered_codes = {item["code"] for item in self.service.list_permissions()}
        self.assertIn(ERROR_VIEW_PERMISSION, registered_codes)
        self.assertIn(ERROR_RECORD_EDIT_PERMISSION, registered_codes)
        legacy_role = next(
            role
            for role in self.service.get_user_security_roles("张三")
            if role["code"].startswith("legacy.")
        )
        self.service.update_security_role(
            legacy_role["role_id"],
            name=legacy_role["name"],
            permission_codes=[ERROR_RECORD_EDIT_PERMISSION],
        )
        self.assertFalse(
            self.service.has_permission("张三", ERROR_RECORD_EDIT_PERMISSION)
        )

        org_unit_id = self.service.save_org_unit(code="org.quality", name="质量部")
        position_id = self.service.save_position(code="quality.engineer", name="质量工程师")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(
            position_id,
            [ERROR_VIEW_PERMISSION, ERROR_RECORD_EDIT_PERMISSION],
            actor_username="admin",
        )

        self.assertTrue(self.service.has_permission("张三", ERROR_VIEW_PERMISSION))
        self.assertTrue(
            self.service.has_permission("张三", ERROR_RECORD_EDIT_PERMISSION)
        )


if __name__ == "__main__":
    unittest.main()
