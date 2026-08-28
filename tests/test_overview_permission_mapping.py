import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.overview_permission_mapping import (
    build_overview_position_permission_plan,
    collect_legacy_overview_role_usage,
    normalize_overview_role_position_mapping,
)
from src.permission_catalog import PROJECT_VIEW_PERMISSION, project_overview_item_permission
from src.user_service import UserService


OVERVIEW_CONFIG = {
    "硬件": {
        "电路": [
            {
                "label": "alpha_item",
                "title": "甲项目",
                "permission": {
                    "read_role": ["质量", "研发"],
                    "edit_role": ["研发"],
                },
            },
            {
                "label": "beta_item",
                "title": "乙项目",
                "permission": {
                    "read_role": ["质量"],
                    "edit_role": ["质量"],
                },
            },
        ]
    }
}


class OverviewPermissionMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        data_dir = root / "data"
        data_dir.mkdir()
        excel_path = data_dir / "users.xlsx"
        pd.DataFrame(
            [
                {"用户名": "admin", "密码": "123456", "角色": "admin"},
                {"用户名": "张三", "密码": "123456", "角色": "质量"},
            ]
        ).to_excel(excel_path, index=False, engine="openpyxl")
        self.service = UserService(
            excel_path=excel_path,
            db_path=root / "identity.db",
            backup_dir=root / "backups",
            password_iterations=1_000,
        )
        self.service.migrate_legacy_users()
        self.service.sync_permission_catalog(strict_overview=True, overview_config=OVERVIEW_CONFIG)
        self.quality_position = self.service.save_position(code="quality.engineer", name="质量工程师")
        self.manager_position = self.service.save_position(code="quality.manager", name="质量主管")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_collect_usage_and_build_many_position_plan(self):
        usage = {item["role"]: item for item in collect_legacy_overview_role_usage(OVERVIEW_CONFIG)}
        self.assertEqual(usage["质量"]["view_count"], 2)
        self.assertEqual(usage["质量"]["edit_count"], 1)

        mapping = normalize_overview_role_position_mapping(
            {
                "质量": [self.quality_position, self.manager_position, self.quality_position],
                "已删除角色": [self.quality_position],
            },
            valid_roles={"质量", "研发"},
            valid_position_ids={self.quality_position, self.manager_position},
        )
        plan = build_overview_position_permission_plan(OVERVIEW_CONFIG, mapping)
        expected = {
            project_overview_item_permission("alpha_item", "view"),
            project_overview_item_permission("beta_item", "view"),
            project_overview_item_permission("beta_item", "edit"),
        }
        self.assertEqual(plan[self.quality_position], expected)
        self.assertEqual(plan[self.manager_position], expected)

    def test_apply_replaces_only_overview_items_and_persists_mapping(self):
        old_manual_code = project_overview_item_permission("alpha_item", "edit")
        self.service.set_position_permissions(
            self.quality_position,
            [PROJECT_VIEW_PERMISSION, old_manual_code],
            actor_username="admin",
        )
        mapping = {"质量": [self.quality_position, self.manager_position]}
        plan = build_overview_position_permission_plan(OVERVIEW_CONFIG, mapping)
        count = self.service.apply_overview_role_position_mapping(
            mapping,
            plan,
            affected_position_ids={self.quality_position, self.manager_position},
            actor_username="admin",
        )

        self.assertEqual(count, 2)
        quality_codes = self.service.get_position_permission_codes(self.quality_position)
        self.assertIn(PROJECT_VIEW_PERMISSION, quality_codes)
        self.assertIn(old_manual_code, quality_codes)
        self.assertIn(project_overview_item_permission("beta_item", "edit"), quality_codes)
        self.assertEqual(self.service.get_overview_role_position_mapping(), mapping)

        # 取消质量工程师映射后再次整理，会清空其概述项权限但保留其他模块权限。
        updated_mapping = {"质量": [self.manager_position]}
        updated_plan = build_overview_position_permission_plan(OVERVIEW_CONFIG, updated_mapping)
        self.service.apply_overview_role_position_mapping(
            updated_mapping,
            updated_plan,
            affected_position_ids={self.quality_position, self.manager_position},
            actor_username="admin",
        )
        quality_codes = self.service.get_position_permission_codes(self.quality_position)
        self.assertEqual(
            {code for code in quality_codes if code.startswith("project_overview.item.")},
            {old_manual_code},
        )
        self.assertIn(PROJECT_VIEW_PERMISSION, quality_codes)


if __name__ == "__main__":
    unittest.main()
