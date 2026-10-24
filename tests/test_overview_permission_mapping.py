import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.overview_permission_mapping import (
    build_overview_position_permission_plan,
    collect_legacy_overview_role_usage,
    normalize_overview_role_position_mapping,
)
from src.overview_operation import append_overview_timestamp
from src.permission_catalog import PROJECT_VIEW_PERMISSION, project_overview_item_permission
from src.user_service import UserService
from src.utils import overview_role_update


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
        self.service = UserService(
            db_path=root / "identity.db",
            password_iterations=1_000,
        )
        self.service.modify_user("add", "admin", "123456", "admin")
        self.service.modify_user("add", "张三", "123456", "质量")
        self.service.sync_permission_catalog(strict_overview=True, overview_config=OVERVIEW_CONFIG)
        self.quality_position = self.service.save_position(code="quality.engineer", name="质量工程师")
        self.manager_position = self.service.save_position(code="quality.manager", name="质量主管")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mapping_rejects_config_without_legacy_rules(self):
        config = {"硬件": {"电路": [{"label": "alpha_item", "title": "甲项目"}]}}
        with self.assertRaisesRegex(ValueError, "不含旧角色权限规则"):
            build_overview_position_permission_plan(config, {"质量": [self.quality_position]})

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



class OverviewOwnerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = UserService(
            db_path=Path(self.temp.name) / "identity.db", password_iterations=1_000,
        )
        self.service.modify_user("add", "张三", "123456", "研发结构")
        self.service.modify_user("add", "李四", "123456", "研发经理")
        unit = self.service.save_org_unit(code="test.owner", name="负责人测试部")
        self.editor_position = self.service.save_position(code="test.owner.editor", name="结构工程师")
        self.helper_position = self.service.save_position(code="test.owner.helper", name="协助岗位")
        self.service.set_primary_membership("张三", org_unit_id=unit, position_id=self.editor_position)
        self.service.set_primary_membership("李四", org_unit_id=unit, position_id=self.helper_position)

    def test_policy_uses_primary_position_and_persists(self):
        self.service.set_overview_owner_position_policy("结构", [self.editor_position])
        policy = self.service.get_overview_owner_position_policy()["结构"]
        self.assertEqual(policy["position_ids"], [self.editor_position])
        self.assertTrue(self.service.is_overview_owner_eligible("张三", {self.editor_position}))
        self.assertFalse(self.service.is_overview_owner_eligible("李四", {self.editor_position}))
        self.assertFalse(self.service.is_overview_owner_eligible("admin", {self.editor_position}))
        reloaded = UserService(db_path=Path(self.temp.name) / "identity.db", password_iterations=1_000)
        self.assertEqual(reloaded.get_overview_owner_position_policy()["结构"], policy)
        chip: dict = {}
        event_time = append_overview_timestamp(chip, creator="张三", reason="维护")
        self.assertGreater(
            datetime.fromisoformat(event_time), datetime.fromisoformat(policy["effective_from"]),
        )

    def test_restricted_owner_ignores_assisting_edits_and_keeps_history(self):
        self.service.set_overview_owner_position_policy("结构", [self.editor_position])
        base = datetime.now() + timedelta(days=1)

        def record(operator: str, offset: int) -> dict:
            time_text = (base + timedelta(seconds=offset)).isoformat(sep=" ", timespec="seconds")
            return {"creator": operator, "timestamp": {time_text: {"creator": operator}}}

        data = {"drawing": {
            "old": record("李四", -100_000),
            "editor": record("张三", 1),
            "helper": record("李四", 2),
        }}
        general = {
            "overview_role": {"P1": {"结构": {"latest_user": "最近指定：李四", "most_user": ""}}},
            "overview_charge_pending": {},
            "over_config_data": {"结构": {"图纸": [{"label": "drawing"}]}},
        }
        fake_app = SimpleNamespace(state=SimpleNamespace(user_service=self.service),
                                   storage=SimpleNamespace(general=general))
        with (
            patch("src.utils.app", fake_app),
            patch("src.utils.db_storage.get_item", return_value=data),
            patch("src.utils.update_overview_charge_pending_dic"),
        ):
            overview_role_update("P1", "结构")
            self.assertEqual(general["overview_role"]["P1"]["结构"]["latest_user"], "最近：张三")
            overview_role_update("P1", "结构")
            self.assertEqual(general["overview_role"]["P1"]["结构"]["latest_user"], "最近：张三")
            data["drawing"]["helper2"] = record("李四", 3)
            overview_role_update("P1", "结构")
            self.assertEqual(general["overview_role"]["P1"]["结构"]["latest_user"], "最近：张三")
            self.assertEqual(data["drawing"]["helper2"]["creator"], "李四")
            role_state = general["overview_role"]["P1"]["结构"]
            role_state["latest_user"] = "最近指定：李四"
            role_state["latest_designation_time"] = (base + timedelta(seconds=10)).isoformat(
                sep=" ", timespec="seconds",
            )
            overview_role_update("P1", "结构")
            self.assertEqual(role_state["latest_user"], "最近指定：李四")
            data["drawing"]["editor2"] = record("张三", 11)
            overview_role_update("P1", "结构")
            self.assertEqual(role_state["latest_user"], "最近：张三")

    def test_empty_policy_keeps_existing_owner(self):
        self.service.set_overview_owner_position_policy("结构", [])
        self.assertFalse(self.service.is_overview_owner_eligible("张三", set()))

    def test_invalid_policy_does_not_replace_existing_rules(self):
        self.service.set_overview_owner_position_policy("结构", [self.editor_position])
        before = self.service.get_overview_owner_position_policy()
        with self.assertRaisesRegex(ValueError, "无效的概述职能"):
            self.service.set_overview_owner_position_policy("不存在", [self.helper_position])
        with self.assertRaisesRegex(ValueError, "不存在或已停用"):
            self.service.set_overview_owner_position_policy("结构", ["missing-position"])
        self.assertEqual(self.service.get_overview_owner_position_policy(), before)

if __name__ == "__main__":
    unittest.main()
