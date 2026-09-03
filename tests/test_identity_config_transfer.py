import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.identity_config_transfer import PACKAGE_KIND
from src.user_service import UserService


class IdentityConfigurationTransferTests(unittest.TestCase):
    """验证配置可以跨数据库合并，同时隔离密码和业务数据。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self._create_service("source", "local-password")
        self.target = self._create_service("target", "server-password", include_extra_user=True)
        self.source.migrate_legacy_users()
        self.target.migrate_legacy_users()
        self._configure_source()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_service(
        self, name: str, user_password: str, *, include_extra_user: bool = False
    ) -> UserService:
        directory = self.root / name
        directory.mkdir()
        rows = [
            {"用户名": "admin", "密码": f"{name}-admin", "角色": "admin"},
            {"用户名": "张三", "密码": user_password, "角色": "研发硬件"},
            {"用户名": "李经理", "密码": user_password, "角色": "研发经理"},
        ]
        if include_extra_user:
            rows.append({"用户名": "服务器用户", "密码": "server-only", "角色": "普通用户"})
        excel_path = directory / "users.xlsx"
        pd.DataFrame(rows).to_excel(excel_path, index=False, engine="openpyxl")
        return UserService(
            excel_path=excel_path,
            db_path=directory / "identity.db",
            backup_dir=directory / "migration_backups",
            password_iterations=1_000,
        )

    def _configure_source(self) -> None:
        root_id = self.source.save_org_unit(code="company", name="公司")
        rd_id = self.source.save_org_unit(
            code="department.rd", name="研发部", parent_org_unit_id=root_id
        )
        position_id = self.source.save_position(
            code="position.engineer",
            name="研发工程师",
            level=5,
            org_unit_ids=[rd_id],
        )
        self.source.set_position_permissions(
            position_id,
            ["sample_issue.view", "sample_issue.record.create"],
            actor_username="admin",
        )
        role_id = self.source.create_security_role(
            code="special.reviewer",
            name="专项复核组",
            permission_codes=["sample_issue.close.approve"],
            actor_username="admin",
        )
        self.source.set_user_security_roles("张三", [role_id], actor_username="admin")
        self.source.set_primary_membership(
            "李经理", org_unit_id=rd_id, position_id=position_id
        )
        self.source.set_primary_membership(
            "张三",
            org_unit_id=rd_id,
            position_id=position_id,
            manager_username="李经理",
        )
        self.source.bind_wecom_user(
            "张三",
            {
                "userid": "wecom-zhangsan",
                "name": "张三",
                "departments": ["研发部"],
                "department_ids": ["2"],
                "position": "研发工程师",
            },
        )
        workflow_id, _ = self.source.save_approval_workflow_draft(
            code="sample_issue.close.rd",
            module="sample_issue",
            event="close",
            name="研发问题关闭审批",
            priority=10,
            condition={
                "requester_org_unit_ids": [rd_id],
                "requester_position_ids": [position_id],
                "include_child_org_units": True,
            },
            approver={"strategy": "users", "user_ids": [self.source.get_user("李经理")["user_id"]]},
            required_permission_code="sample_issue.close.approve",
            actor_username="admin",
        )
        self.source.publish_approval_workflow(workflow_id, actor_username="admin")

    def test_export_does_not_contain_passwords_or_runtime_tables(self):
        package = self.source.export_identity_configuration()
        serialized = json.dumps(package, ensure_ascii=False)

        self.assertEqual(package["package_kind"], PACKAGE_KIND)
        self.assertNotIn("local-password", serialized)
        self.assertNotIn("password_hash", serialized)
        self.assertNotIn("work_assignments", serialized)
        self.assertNotIn("iam_audit_logs", serialized)

    def test_preview_and_import_merge_by_codes_and_preserve_server_password(self):
        package = self.source.export_identity_configuration()
        preview = self.target.preview_identity_configuration(package)

        self.assertTrue(preview.can_import, preview.errors)
        self.assertEqual(preview.summary["users_matched"], 3)
        connection = sqlite3.connect(self.target.identity_store.db_path)
        try:
            connection.execute("CREATE TABLE business_sentinel(id TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO business_sentinel VALUES('record-1', '服务器业务数据')")
            connection.commit()
        finally:
            connection.close()
        result = self.target.import_identity_configuration(
            package,
            actor_username="admin",
            backup_dir=self.root / "import_backups",
        )

        self.assertTrue(Path(result.backup_path).exists())
        self.assertTrue(self.target.authenticate("张三", "server-password"))
        self.assertFalse(self.target.authenticate("张三", "local-password"))
        self.assertTrue(self.target.authenticate("服务器用户", "server-only"))
        connection = sqlite3.connect(self.target.identity_store.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM business_sentinel WHERE id='record-1'"
                ).fetchone()[0],
                "服务器业务数据",
            )
        finally:
            connection.close()

        departments = {item["code"]: item for item in self.target.list_org_units()}
        positions = {item["code"]: item for item in self.target.list_positions()}
        self.assertEqual(departments["department.rd"]["parent_name"], "公司")
        self.assertEqual(positions["position.engineer"]["org_names"], ["研发部"])
        self.assertIn("sample_issue.view", positions["position.engineer"]["permission_codes"])

        membership = self.target.get_primary_membership("张三")
        self.assertEqual(membership["org_name"], "研发部")
        self.assertEqual(membership["position_name"], "研发工程师")
        self.assertEqual(membership["manager_username"], "李经理")
        self.assertEqual(
            self.target.get_wecom_binding("张三")["external_userid"], "wecom-zhangsan"
        )
        role_codes = {
            item["code"]
            for item in self.target.get_user_security_roles(
                "张三", include_compatibility=False
            )
        }
        self.assertEqual(role_codes, {"special.reviewer"})

        workflow = next(
            item
            for item in self.target.list_approval_workflows(module="sample_issue")
            if item["code"] == "sample_issue.close.rd"
        )
        target_position_id = positions["position.engineer"]["position_id"]
        self.assertEqual(
            workflow["active_version"]["condition"]["requester_position_ids"],
            [target_position_id],
        )
        self.assertEqual(
            workflow["active_version"]["approver"]["user_ids"],
            [self.target.get_user("李经理")["user_id"]],
        )

        versions_before = len(workflow["versions"])
        self.target.import_identity_configuration(
            package,
            actor_username="admin",
            backup_dir=self.root / "repeat_backups",
        )
        workflow_after_repeat = next(
            item
            for item in self.target.list_approval_workflows(module="sample_issue")
            if item["code"] == "sample_issue.close.rd"
        )
        self.assertEqual(len(workflow_after_repeat["versions"]), versions_before)

    def test_tampered_package_is_rejected_without_writing(self):
        package = self.source.export_identity_configuration()
        package["configuration"]["positions"][0]["name"] = "被篡改"
        preview = self.target.preview_identity_configuration(package)
        before = len(self.target.list_positions())

        self.assertFalse(preview.can_import)
        self.assertTrue(any("校验值" in error for error in preview.errors))
        with self.assertRaisesRegex(ValueError, "预检失败"):
            self.target.import_identity_configuration(package)
        self.assertEqual(len(self.target.list_positions()), before)

    def test_import_failure_rolls_back_all_configuration_changes(self):
        package = self.source.export_identity_configuration()
        # 用数据库触发器模拟预检结束后才发生的写入故障，验证整个事务回滚。
        connection = sqlite3.connect(self.target.identity_store.db_path)
        try:
            connection.execute(
                "CREATE TRIGGER force_position_import_failure BEFORE INSERT ON iam_positions "
                "WHEN NEW.code='position.engineer' BEGIN "
                "SELECT RAISE(ABORT, 'forced import failure'); END"
            )
            connection.commit()
        finally:
            connection.close()
        before = len(self.target.list_org_units())

        with self.assertRaises(sqlite3.IntegrityError):
            self.target.import_identity_configuration(
                package, backup_dir=self.root / "rollback_backups"
            )
        self.assertEqual(len(self.target.list_org_units()), before)


if __name__ == "__main__":
    unittest.main()
