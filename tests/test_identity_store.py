import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.identity_store import verify_password
from src.user_service import UserService


class IdentityStoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.excel_path = root / "users.xlsx"
        self.db_path = root / "identity.db"
        self.backup_dir = root / "backups"
        self._write_users(
            [
                {"用户名": "admin", "密码": "server-admin-pass", "角色": "admin"},
                {"用户名": "张三", "密码": "123456", "角色": "研发硬件"},
            ]
        )
        self.service = UserService(
            excel_path=self.excel_path,
            db_path=self.db_path,
            backup_dir=self.backup_dir,
            password_iterations=1_000,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_users(self, rows):
        pd.DataFrame(rows).to_excel(self.excel_path, index=False, engine="openpyxl")

    def test_legacy_mode_remains_available_until_explicit_migration(self):
        self.assertEqual(self.service.storage_mode, "legacy_excel")
        self.assertTrue(self.service.authenticate("张三", "123456"))
        self.assertFalse(self.service.authenticate("张三", "bad-password"))

    def test_migration_hashes_the_passwords_from_the_current_workbook(self):
        result = self.service.migrate_legacy_users()

        self.assertEqual(result.imported, 2)
        self.assertEqual(result.total, 2)
        self.assertTrue(Path(result.backup_path).exists())
        self.assertEqual(self.service.storage_mode, "database")
        self.assertTrue(self.service.authenticate("admin", "server-admin-pass"))
        self.assertTrue(self.service.authenticate("张三", "123456"))
        self.assertIsNone(self.service.get_user("张三")["password"])

        connection = sqlite3.connect(self.db_path)
        try:
            encoded = connection.execute(
                "SELECT password_hash FROM iam_users WHERE username='张三'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotEqual(encoded, "123456")
        self.assertTrue(verify_password("123456", encoded))

    def test_safe_repeat_does_not_replace_a_migrated_server_password(self):
        self.service.migrate_legacy_users()
        self._write_users(
            [
                {"用户名": "admin", "密码": "local-accidental-value", "角色": "admin"},
                {"用户名": "张三", "密码": "changed-in-excel", "角色": "研发软件"},
            ]
        )

        result = self.service.migrate_legacy_users()

        self.assertEqual(result.password_refreshed, 0)
        self.assertTrue(self.service.authenticate("admin", "server-admin-pass"))
        self.assertFalse(self.service.authenticate("admin", "local-accidental-value"))
        self.assertEqual(self.service.get_user("张三")["role"], "研发软件")

    def test_explicit_refresh_can_make_the_workbook_authoritative_again(self):
        self.service.migrate_legacy_users()
        self._write_users(
            [
                {"用户名": "admin", "密码": "new-server-password", "角色": "admin"},
                {"用户名": "张三", "密码": "new-user-password", "角色": "研发硬件"},
            ]
        )

        result = self.service.migrate_legacy_users(refresh_existing_passwords=True)

        self.assertEqual(result.password_refreshed, 2)
        self.assertTrue(self.service.authenticate("admin", "new-server-password"))
        self.assertFalse(self.service.authenticate("admin", "server-admin-pass"))

    def test_manual_wecom_binding_is_unique_and_user_can_be_disabled(self):
        self.service.migrate_legacy_users()
        self.service.bind_wecom_user(
            "张三",
            {
                "userid": "wecom-zhangsan",
                "name": "张三",
                "departments": ["研发部"],
                "department_ids": ["2"],
                "position": "工程师",
                "is_active": True,
            },
        )

        binding = self.service.get_wecom_binding("张三")
        self.assertEqual(binding["external_userid"], "wecom-zhangsan")
        with self.assertRaisesRegex(ValueError, "已绑定"):
            self.service.bind_wecom_user(
                "admin",
                {"userid": "wecom-zhangsan", "name": "管理员"},
            )

        self.service.modify_user("deactivate", "张三")
        self.assertFalse(self.service.authenticate("张三", "123456"))
        self.assertEqual(self.service.get_user("张三")["status"], "disabled")

    def test_org_import_position_and_direct_manager_membership(self):
        self.service.migrate_legacy_users()
        inserted, updated = self.service.import_wecom_departments(
            [
                {"id": "1", "name": "公司", "parentid": "0", "order": 1},
                {"id": "2", "name": "研发部", "parentid": "1", "order": 2},
            ]
        )
        self.assertEqual((inserted, updated), (2, 0))
        inserted, updated = self.service.import_wecom_departments(
            [
                {"id": "1", "name": "公司", "parentid": "0", "order": 1},
                {"id": "2", "name": "研发中心", "parentid": "1", "order": 2},
            ]
        )
        self.assertEqual((inserted, updated), (0, 2))

        units = self.service.list_org_units()
        rd_unit = next(item for item in units if item["wecom_department_id"] == "2")
        root_unit = next(item for item in units if item["wecom_department_id"] == "1")
        self.service.save_org_unit(
            code=rd_unit["code"],
            name="内部研发中心",
            parent_org_unit_id=root_unit["org_unit_id"],
            wecom_department_id="2",
            sort_order=20,
        )
        self.service.import_wecom_departments(
            [
                {"id": "1", "name": "公司", "parentid": "0", "order": 1},
                {"id": "2", "name": "企微再次改名", "parentid": "1", "order": 2},
            ]
        )
        rd_unit = next(
            item for item in self.service.list_org_units() if item["wecom_department_id"] == "2"
        )
        self.assertEqual(rd_unit["name"], "内部研发中心")
        self.assertEqual(rd_unit["manual_override"], 1)

        position_id = self.service.save_position(code="rd.engineer", name="研发工程师", level=10)
        self.service.set_primary_membership(
            "张三",
            org_unit_id=rd_unit["org_unit_id"],
            position_id=position_id,
            manager_username="admin",
        )

        membership = self.service.get_primary_membership("张三")
        self.assertEqual(membership["org_name"], "内部研发中心")
        self.assertEqual(membership["position_name"], "研发工程师")
        self.assertEqual(membership["manager_username"], "admin")
        position = next(
            item for item in self.service.list_positions() if item["position_id"] == position_id
        )
        self.assertEqual(position["org_unit_ids"], [rd_unit["org_unit_id"]])

    def test_positions_are_scoped_and_filtered_by_department(self):
        """岗位支持多部门归属，任职时不能选择其他部门的岗位。"""
        self.service.migrate_legacy_users()
        rd_unit_id = self.service.save_org_unit(code="org.rd.scope", name="研发部")
        engineering_unit_id = self.service.save_org_unit(
            code="org.engineering.scope",
            name="工程部",
        )
        quality_unit_id = self.service.save_org_unit(code="org.quality.scope", name="质量部")
        position_id = self.service.save_position(
            code="position.shared.engineer",
            name="共用工程师",
            org_unit_ids=[rd_unit_id, engineering_unit_id],
        )

        self.assertEqual(
            {item["position_id"] for item in self.service.list_positions(rd_unit_id)},
            {position_id},
        )
        self.assertEqual(self.service.list_positions(quality_unit_id), [])
        with self.assertRaisesRegex(ValueError, "不属于当前部门"):
            self.service.set_primary_membership(
                "张三",
                org_unit_id=quality_unit_id,
                position_id=position_id,
            )

        self.service.set_primary_membership(
            "张三",
            org_unit_id=rd_unit_id,
            position_id=position_id,
        )
        with self.assertRaisesRegex(ValueError, "仍有在职员工"):
            self.service.save_position(
                code="position.shared.engineer",
                name="共用工程师",
                org_unit_ids=[engineering_unit_id],
            )

    def test_wecom_position_import_is_sorted_deduplicated_and_preserves_manual_edit(self):
        self.service.migrate_legacy_users()
        contacts = [
            {"userid": "2", "position": "软件工程师", "is_active": True},
            {"userid": "1", "position": "硬件工程师", "is_active": True},
            {"userid": "3", "position": "软件工程师", "is_active": True},
            {"userid": "4", "position": "停用岗位", "is_active": False},
        ]
        self.assertEqual(self.service.import_wecom_positions(contacts), (2, 0))
        imported = [item for item in self.service.list_positions() if item["source"] == "wecom"]
        self.assertEqual({item["name"] for item in imported}, {"软件工程师", "硬件工程师"})

        software = next(item for item in imported if item["external_name_snapshot"] == "软件工程师")
        self.service.save_position(
            code=software["code"],
            name="内部软件研发岗",
            level=20,
        )
        self.assertEqual(self.service.import_wecom_positions(contacts), (0, 2))
        software = next(
            item
            for item in self.service.list_positions()
            if item["external_name_snapshot"] == "软件工程师"
        )
        self.assertEqual(software["name"], "内部软件研发岗")
        self.assertEqual(software["manual_override"], 1)

    def test_manual_codes_are_normalized_validated_and_checked_for_duplicates(self):
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(
            code="ORG.Sales",
            name="销售部",
            reject_existing=True,
        )
        org_unit = next(
            item for item in self.service.list_org_units() if item["org_unit_id"] == org_unit_id
        )
        self.assertEqual(org_unit["code"], "org.sales")
        with self.assertRaisesRegex(ValueError, "部门编码已存在"):
            self.service.save_org_unit(
                code="org.sales",
                name="重复销售部",
                reject_existing=True,
            )
        with self.assertRaisesRegex(ValueError, "编码格式不正确"):
            self.service.save_position(
                code="中文岗位",
                name="无效岗位",
                reject_existing=True,
            )

        position_id = self.service.save_position(
            code="POSITION.Sales.Manager",
            name="销售经理",
            reject_existing=True,
        )
        position = next(
            item for item in self.service.list_positions() if item["position_id"] == position_id
        )
        self.assertEqual(position["code"], "position.sales.manager")
        with self.assertRaisesRegex(ValueError, "岗位编码已存在"):
            self.service.save_position(
                code="position.sales.manager",
                name="重复销售经理",
                reject_existing=True,
            )

        self.service.create_security_role(code="ECN.Reviewer", name="ECN审核员")
        with self.assertRaisesRegex(ValueError, "附加权限组编码已存在"):
            self.service.create_security_role(code="ecn.reviewer", name="重复审核员")

    def test_safe_match_plan_binds_and_fills_only_missing_org_membership(self):
        self.service.migrate_legacy_users()
        departments = [
            {"id": "1", "name": "公司", "parentid": "0", "order": 1},
            {"id": "2", "name": "研发部", "parentid": "1", "order": 2},
        ]
        contacts = [
            {
                "userid": "admin",
                "name": "管理员",
                "department_ids": ["1"],
                "main_department_id": "1",
                "departments": ["公司"],
                "position": "管理员",
                "is_active": True,
            },
            {
                "userid": "wecom-zhangsan",
                "name": "张三",
                "department_ids": ["2"],
                "main_department_id": "2",
                "departments": ["研发部"],
                "position": "研发工程师",
                "is_active": True,
            },
        ]
        self.service.import_wecom_departments(departments)
        self.service.import_wecom_positions(contacts)

        rd_unit = next(
            item for item in self.service.list_org_units() if item["wecom_department_id"] == "2"
        )
        rd_position = next(
            item
            for item in self.service.list_positions()
            if item["external_name_snapshot"] == "研发工程师"
        )
        self.assertEqual(rd_position["org_unit_ids"], [rd_unit["org_unit_id"]])

        plan = self.service.build_wecom_match_plan(contacts)
        self.assertEqual(sum(item["status"] == "matched" for item in plan), 2)
        self.assertEqual(self.service.apply_wecom_match_plan(plan), (2, 2))
        self.assertEqual(
            self.service.get_wecom_binding("张三")["external_userid"],
            "wecom-zhangsan",
        )
        membership = self.service.get_primary_membership("张三")
        self.assertEqual(membership["org_name"], "研发部")
        self.assertEqual(membership["position_name"], "研发工程师")

        # 后续企业微信建议不能替换已经手工建立的系统任职。
        root = next(item for item in self.service.list_org_units() if item["wecom_department_id"] == "1")
        self.service.set_primary_membership("张三", org_unit_id=root["org_unit_id"])
        self.assertFalse(self.service.apply_suggested_org_membership("张三", contacts[1]))
        self.assertEqual(self.service.get_primary_membership("张三")["org_name"], "公司")


if __name__ == "__main__":
    unittest.main()
