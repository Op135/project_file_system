import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from src.access_control import can_use_tool
from src.permission_catalog import (
    DESIGN_KNOWLEDGE_CREATE_PERMISSION,
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_VIEW_PERMISSION,
    ERROR_NOTIFICATION_MODULE,
    ERROR_RECORD_EDIT_PERMISSION,
    ERROR_VIEW_PERMISSION,
    PROJECT_ALL_STATES_VIEW_PERMISSION,
    PROJECT_BASE_EDIT_PERMISSION,
    PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
    PROJECT_LEGACY_RECORD_MANAGE_PERMISSION,
    PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_BATCH_SUBMIT_PERMISSION,
    PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
    PROJECT_STATUS_EDIT_PERMISSION,
    PROJECT_REQUIREMENT_DRAFT_MANAGE_ALL_PERMISSION,
    PROJECT_REQUIREMENT_EDIT_PERMISSION,
    PROJECT_REQUIREMENT_REVIEW_ALL_PERMISSION,
    PROJECT_REQUIREMENT_REVIEW_ASSIGNED_PERMISSION,
    PROJECT_REQUIREMENT_REVOKE_PERMISSION,
    PROJECT_REQUIREMENT_VIEW_PERMISSION,
    PROJECT_TODO_VIEW_PERMISSION,
    PROJECT_VIEW_PERMISSION,
    ProjectOverviewPermissionCatalogError,
    SAMPLE_ISSUE_CREATE_PERMISSION,
    SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
    SAMPLE_ISSUE_LEGACY_CLOSE_DEFAULT_APPROVE_PERMISSION,
    SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION,
    SAMPLE_ISSUE_NOTIFICATION_MODULE,
    SAMPLE_ISSUE_VIEW_PERMISSION,
    SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
    SAMPLE_ORDER_NOTIFICATION_MODULE,
    SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION,
    SAMPLE_ORDER_BASE_EDIT_PERMISSION,
    SAMPLE_ORDER_VIEW_PERMISSION,
    STATISTICS_OVERVIEW_OWNER_MANAGE_PERMISSION,
    STATISTICS_OVERVIEW_VIEW_PERMISSION,
    STATISTICS_VIEW_PERMISSION,
    tool_permission_code,
    project_overview_item_permission,
    project_overview_dimension_permission,
    project_overview_permission_definitions,
)
from src.pages.project_table import (
    can_manage_project_records,
    can_view_all_project_states,
    can_view_project_table,
)
from src.project_access import can_assign_all_project_engineers, can_edit_project_status
from src.project_requirement_access import (
    can_edit_project_requirement,
    can_manage_all_project_requirement_drafts,
    can_review_all_project_requirements,
    can_review_project_requirement,
    can_revoke_project_requirement_approval,
    can_view_project_requirement,
)
from src.project_todo_access import (
    can_view_project_todo,
    filter_actionable_overview_pending,
)
from src.project_overview_access import (
    can_edit_overview_item,
    can_review_batch_overview,
    can_review_overview_correction,
    can_submit_batch_overview,
    can_view_overview_item,
)
from src.user_service import UserService
from src.statistics_access import (
    can_manage_overview_owners,
    can_view_overview_statistics,
    can_view_statistics,
)


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

    def test_statistics_legacy_mode_keeps_entry_and_overview_role_rules(self):
        """旧 Excel 模式保留统计入口关键词及概述统计精确角色。"""
        self.assertTrue(can_view_statistics("质量经理", "张三", user_service=self.service))
        self.assertFalse(can_view_statistics("研发光学", "张三", user_service=self.service))
        self.assertTrue(
            can_view_overview_statistics("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_view_overview_statistics("质量经理", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_manage_overview_owners("研发电子主管", "张三", user_service=self.service)
        )

    def test_statistics_database_mode_uses_three_independent_permissions(self):
        """数据库模式下统计入口、全员概述统计和负责人配置互不隐含。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.statistics", name="统计测试部")
        position_id = self.service.save_position(code="statistics.viewer", name="统计查看岗位")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )

        self.assertFalse(can_view_statistics("研发经理", "张三", user_service=self.service))
        self.service.set_position_permissions(
            position_id,
            [STATISTICS_VIEW_PERMISSION],
            actor_username="admin",
        )
        self.assertTrue(can_view_statistics("普通岗位", "张三", user_service=self.service))
        self.assertFalse(
            can_view_overview_statistics("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_manage_overview_owners("研发电子主管", "张三", user_service=self.service)
        )

        self.service.set_position_permissions(
            position_id,
            [
                STATISTICS_VIEW_PERMISSION,
                STATISTICS_OVERVIEW_VIEW_PERMISSION,
                STATISTICS_OVERVIEW_OWNER_MANAGE_PERMISSION,
            ],
            actor_username="admin",
        )
        self.assertTrue(
            can_view_overview_statistics("普通岗位", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_manage_overview_owners("普通岗位", "张三", user_service=self.service)
        )

    def test_project_todo_legacy_mode_keeps_original_entry_keywords(self):
        """旧 Excel 模式继续按原角色关键词开放项目待办入口。"""
        self.assertTrue(can_view_project_todo("质量工程师", "张三", user_service=self.service))
        self.assertTrue(can_view_project_todo("研发硬件", "张三", user_service=self.service))
        self.assertFalse(can_view_project_todo("行政专员", "张三", user_service=self.service))

    def test_project_todo_database_mode_requires_stable_entry_permission(self):
        """数据库模式不得因岗位名称碰巧包含旧关键词而开放项目待办。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.todo", name="待办测试部")
        position_id = self.service.save_position(code="todo.viewer", name="研发待办查看岗位")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )

        self.assertFalse(can_view_project_todo("研发经理", "张三", user_service=self.service))
        self.service.set_position_permissions(
            position_id,
            [PROJECT_TODO_VIEW_PERMISSION],
            actor_username="admin",
        )
        self.assertTrue(can_view_project_todo("普通岗位", "张三", user_service=self.service))

    def test_project_todo_only_keeps_actionable_overview_labels(self):
        """个人概述待办只显示当前角色真正可以维护的 label。"""
        pending = {
            "P100": {
                "hardware_summary": "缺必填",
                "optical_summary": "有待定",
                "removed_summary": "缺需填",
            }
        }
        overview_config = {
            "hardware_summary": {
                "label": "hardware_summary",
                "permission": {"edit_role": ["研发硬件"]},
            },
            "optical_summary": {
                "label": "optical_summary",
                "permission": {"edit_role": ["研发光学"]},
            },
        }

        self.assertEqual(
            filter_actionable_overview_pending(
                pending,
                overview_config,
                "研发硬件",
                "张三",
                user_service=self.service,
            ),
            {"P100": {"hardware_summary": "缺必填"}},
        )

    def test_project_table_legacy_mode_keeps_original_role_behavior(self):
        """旧 Excel 模式仍允许登录用户查看，并保留原项目维护和状态范围规则。"""
        self.assertTrue(
            can_view_project_table("普通用户", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_manage_project_records("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_manage_project_records("研发硬件", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_edit_project_status("研发助理", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_assign_all_project_engineers("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_view_all_project_states("工程IE", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_view_all_project_states("研发硬件", "张三", user_service=self.service)
        )

    def test_project_table_database_mode_uses_only_stable_permissions(self):
        """项目资料迁移后不得从旧角色名称获得查看或维护能力。"""
        self.service.migrate_legacy_users()

        self.assertFalse(
            can_view_project_table("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_manage_project_records("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_view_all_project_states("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_edit_project_status("研发经理", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_assign_all_project_engineers("研发经理", "张三", user_service=self.service)
        )

        org_unit_id = self.service.save_org_unit(code="org.project", name="项目测试部")
        position_id = self.service.save_position(code="project.manager", name="项目管理员")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(
            position_id,
            [PROJECT_VIEW_PERMISSION],
            actor_username="admin",
        )
        self.assertTrue(
            can_view_project_table("普通岗位", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_view_all_project_states("普通岗位", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_manage_project_records("普通岗位", "张三", user_service=self.service)
        )

        self.service.set_position_permissions(
            position_id,
            [
                PROJECT_VIEW_PERMISSION,
                PROJECT_ALL_STATES_VIEW_PERMISSION,
                PROJECT_BASE_EDIT_PERMISSION,
            ],
            actor_username="admin",
        )
        self.assertTrue(
            can_view_all_project_states("普通岗位", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_manage_project_records("普通岗位", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_edit_project_status("普通岗位", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_assign_all_project_engineers("普通岗位", "张三", user_service=self.service)
        )

        self.service.set_position_permissions(
            position_id,
            [
                PROJECT_VIEW_PERMISSION,
                PROJECT_STATUS_EDIT_PERMISSION,
                PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
            ],
            actor_username="admin",
        )
        self.assertFalse(
            can_manage_project_records("普通岗位", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_edit_project_status("普通岗位", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_assign_all_project_engineers("普通岗位", "张三", user_service=self.service)
        )

    def test_legacy_project_manage_grant_expands_to_three_independent_permissions(self):
        """上一版项目维护授权只迁移一次，拆为基础资料、状态和工程师指定。"""
        self.service.migrate_legacy_users()
        position_id = self.service.save_position(code="project.legacy.manager", name="旧项目管理员")
        self.service.identity_store.seed_permission_catalog(
            [
                {
                    "code": PROJECT_LEGACY_RECORD_MANAGE_PERMISSION,
                    "name": "旧项目维护",
                    "module": "项目资料",
                    "description": "",
                }
            ],
            {},
        )
        self.service.set_position_permissions(
            position_id,
            [PROJECT_LEGACY_RECORD_MANAGE_PERMISSION],
            actor_username="admin",
        )
        self.service.sync_permission_catalog(strict_overview=True)
        permission_codes = self.service.get_position_permission_codes(position_id)
        self.assertNotIn(PROJECT_LEGACY_RECORD_MANAGE_PERMISSION, permission_codes)
        self.assertTrue(
            {
                PROJECT_BASE_EDIT_PERMISSION,
                PROJECT_STATUS_EDIT_PERMISSION,
                PROJECT_ENGINEER_ASSIGN_ALL_PERMISSION,
            }.issubset(permission_codes)
        )

    def test_project_requirement_legacy_mode_keeps_editor_and_assignee_rules(self):
        """旧模式继续按销售编辑、研发经理全审和项目工程师具体分配运行。"""
        engineers = {"P1": "张三", "P2": "李四"}
        self.assertTrue(
            can_view_project_requirement("普通用户", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_edit_project_requirement("销售", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_review_project_requirement(
                "研发硬件",
                "张三",
                "P1",
                engineers,
                user_service=self.service,
            )
        )
        self.assertFalse(
            can_review_project_requirement(
                "研发硬件",
                "张三",
                "P2",
                engineers,
                user_service=self.service,
            )
        )
        self.assertTrue(
            can_review_all_project_requirements("研发经理", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_revoke_project_requirement_approval(
                "研发经理",
                "张三",
                user_service=self.service,
            )
        )

    def test_project_requirement_database_mode_uses_stable_and_assigned_permissions(self):
        """数据库模式同时校验稳定资格和项目工程师的具体责任。"""
        self.service.migrate_legacy_users()
        engineers = {"P1": "张三", "P2": "李四"}
        self.assertFalse(
            can_edit_project_requirement("销售", "张三", user_service=self.service)
        )
        self.assertFalse(
            can_review_all_project_requirements("研发经理", "张三", user_service=self.service)
        )

        org_unit_id = self.service.save_org_unit(code="org.requirement", name="需求测试部")
        position_id = self.service.save_position(code="requirement.owner", name="需求维护岗位")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(
            position_id,
            [
                PROJECT_REQUIREMENT_VIEW_PERMISSION,
                PROJECT_REQUIREMENT_EDIT_PERMISSION,
                PROJECT_REQUIREMENT_REVIEW_ASSIGNED_PERMISSION,
            ],
            actor_username="admin",
        )
        self.assertTrue(
            can_view_project_requirement("普通岗位", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_edit_project_requirement("普通岗位", "张三", user_service=self.service)
        )
        self.assertTrue(
            can_review_project_requirement(
                "普通岗位",
                "张三",
                "P1",
                engineers,
                user_service=self.service,
            )
        )
        self.assertFalse(
            can_review_project_requirement(
                "研发经理",
                "张三",
                "P2",
                engineers,
                user_service=self.service,
            )
        )

        self.service.set_position_permissions(
            position_id,
            [
                PROJECT_REQUIREMENT_VIEW_PERMISSION,
                PROJECT_REQUIREMENT_EDIT_PERMISSION,
                PROJECT_REQUIREMENT_REVIEW_ALL_PERMISSION,
                PROJECT_REQUIREMENT_REVOKE_PERMISSION,
                PROJECT_REQUIREMENT_DRAFT_MANAGE_ALL_PERMISSION,
            ],
            actor_username="admin",
        )
        self.assertTrue(
            can_review_project_requirement(
                "普通岗位",
                "张三",
                "P2",
                engineers,
                user_service=self.service,
            )
        )
        self.assertTrue(
            can_revoke_project_requirement_approval(
                "普通岗位",
                "张三",
                user_service=self.service,
            )
        )
        self.assertTrue(
            can_manage_all_project_requirement_drafts(
                "普通岗位",
                "张三",
                user_service=self.service,
            )
        )

    def test_project_overview_legacy_mode_keeps_item_role_rules(self):
        """旧 Excel 模式继续读取概述项自身的读写角色配置。"""
        config = {
            "role": "硬件",
            "permission": {
                "read_role": ["研发结构"],
                "edit_role": ["研发硬件"],
            },
        }
        self.assertTrue(can_view_overview_item(config, "研发结构", "张三", user_service=self.service))
        self.assertTrue(can_view_overview_item(config, "研发硬件", "张三", user_service=self.service))
        self.assertTrue(can_edit_overview_item(config, "研发硬件", "张三", user_service=self.service))
        self.assertFalse(can_edit_overview_item(config, "研发结构", "张三", user_service=self.service))

    def test_project_overview_database_mode_uses_label_and_assignment_permissions(self):
        """数据库模式按 label 授权，审批同时限制为流程快照中的具体人员。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.overview", name="概述测试部")
        position_id = self.service.save_position(code="overview.hardware", name="硬件概述岗位")
        self.service.set_primary_membership("张三", org_unit_id=org_unit_id, position_id=position_id)
        self.service.set_position_permissions(
            position_id,
            [
                project_overview_item_permission("drive_pcb", "edit"),
                PROJECT_OVERVIEW_BATCH_SUBMIT_PERMISSION,
                PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
                PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
            ],
            actor_username="admin",
        )
        hardware = {"role": "硬件", "label": "drive_pcb", "permission": {"read_role": [], "edit_role": []}}
        software = {
            "role": "软件",
            "label": "software_manual",
            "permission": {"read_role": [], "edit_role": ["研发硬件"]},
        }
        self.assertTrue(can_view_overview_item(hardware, "普通岗位", "张三", user_service=self.service))
        self.assertTrue(can_edit_overview_item(hardware, "普通岗位", "张三", user_service=self.service))
        self.assertFalse(can_view_overview_item(software, "研发硬件", "张三", user_service=self.service))
        self.assertTrue(
            can_submit_batch_overview("普通岗位", "张三", ["其它旧角色"], user_service=self.service)
        )

        assigned = {"submitter": "李四", "workflow_assignment": {"assignee_usernames": ["张三"]}}
        not_assigned = {"submitter": "李四", "workflow_assignment": {"assignee_usernames": ["王五"]}}
        self.assertTrue(can_review_batch_overview(assigned, "普通岗位", "张三", user_service=self.service))
        self.assertTrue(can_review_overview_correction(assigned, "普通岗位", "张三", user_service=self.service))
        self.assertFalse(can_review_batch_overview(not_assigned, "研发经理", "张三", user_service=self.service))
        self.assertFalse(can_review_overview_correction(not_assigned, "研发经理", "张三", user_service=self.service))

    def test_project_overview_label_catalog_detects_new_items_and_preserves_grants(self):
        """新增 label 自动登记且默认不授权，title 改名不会破坏原权限关系。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.dynamic.overview", name="动态概述部")
        position_id = self.service.save_position(code="overview.dynamic", name="动态概述岗位")
        self.service.set_primary_membership("张三", org_unit_id=org_unit_id, position_id=position_id)
        initial_config = {
            "硬件": {
                "测试分组": [
                    {"label": "alpha_item", "title": "原名称"},
                ]
            }
        }
        self.service.sync_permission_catalog(strict_overview=True, overview_config=initial_config)
        alpha_edit = project_overview_item_permission("alpha_item", "edit")
        self.service.set_position_permissions(position_id, [alpha_edit], actor_username="admin")

        updated_config = {
            "硬件": {
                "测试分组": [
                    {"label": "alpha_item", "title": "修改后的名称"},
                    {"label": "beta_item", "title": "新增项目"},
                ]
            }
        }
        self.service.sync_permission_catalog(strict_overview=True, overview_config=updated_config)
        permission_rows = {
            item["code"]: item for item in self.service.identity_store.list_permissions()
        }
        beta_edit = project_overview_item_permission("beta_item", "edit")
        self.assertEqual(permission_rows[alpha_edit]["name"], "维护 — 修改后的名称")
        self.assertIn(beta_edit, permission_rows)
        self.assertIn(alpha_edit, self.service.get_position_permission_codes(position_id))
        self.assertNotIn(beta_edit, self.service.get_position_permission_codes(position_id))

    def test_project_overview_label_catalog_rejects_duplicates(self):
        duplicate_config = {
            "光学": {"第一组": [{"label": "same_label", "title": "第一项"}]},
            "硬件": {"第二组": [{"label": "same_label", "title": "第二项"}]},
        }
        with self.assertRaisesRegex(ProjectOverviewPermissionCatalogError, "same_label.*重复"):
            project_overview_permission_definitions(duplicate_config)

    def test_project_overview_dimension_grants_expand_once_to_current_labels(self):
        """上一版专业级权限升级时展开到当前 label，之后不再重复覆盖管理员调整。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.overview.upgrade", name="概述升级部")
        position_id = self.service.save_position(code="overview.upgrade", name="概述升级岗位")
        old_code = project_overview_dimension_permission("hardware", "edit")
        self.service.identity_store.seed_permission_catalog(
            [{"code": old_code, "name": "旧硬件维护", "module": "项目概述", "description": ""}],
            {},
        )
        self.service.set_position_permissions(position_id, [old_code], actor_username="admin")
        self.service.sync_permission_catalog(strict_overview=True)

        current_codes = self.service.get_position_permission_codes(position_id)
        self.assertNotIn(old_code, current_codes)
        self.assertIn(project_overview_item_permission("drive_pcb", "edit"), current_codes)
        self.assertIn(project_overview_item_permission("electronic_testing", "edit"), current_codes)

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
        """各业务模块通知应在权限界面形成互相独立的分组。"""
        self.service.migrate_legacy_users()
        notification_modules = {
            item["module"]
            for item in self.service.list_permissions()
            if str(item["code"]).startswith("notifications.")
        }
        self.assertEqual(
            notification_modules,
            {
                SAMPLE_ORDER_NOTIFICATION_MODULE,
                ERROR_NOTIFICATION_MODULE,
                SAMPLE_ISSUE_NOTIFICATION_MODULE,
            },
        )

    def test_sample_issue_database_mode_ignores_legacy_role_and_uses_position_permissions(self):
        """样品问题模块迁移后只认稳定岗位权限，不再叠加旧角色授权。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.sample.issue", name="样品问题测试部")
        position_id = self.service.save_position(code="sample.issue.owner", name="样品问题岗位")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )

        self.assertFalse(
            self.service.has_permission(
                "张三",
                SAMPLE_ISSUE_VIEW_PERMISSION,
                legacy_role="研发硬件",
                legacy_allowed_roles=["研发硬件"],
            )
        )
        self.service.set_position_permissions(
            position_id,
            [SAMPLE_ISSUE_VIEW_PERMISSION, SAMPLE_ISSUE_CREATE_PERMISSION],
            actor_username="admin",
        )
        self.assertTrue(self.service.has_permission("张三", SAMPLE_ISSUE_VIEW_PERMISSION))
        self.assertTrue(self.service.has_permission("张三", SAMPLE_ISSUE_CREATE_PERMISSION))

    def test_design_knowledge_database_mode_uses_only_stable_permissions(self):
        """设计知识库迁移后不再从研发角色名称自动获得权限。"""
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.design.knowledge", name="知识测试部")
        position_id = self.service.save_position(code="design.knowledge.editor", name="知识录入岗位")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )

        self.assertFalse(
            self.service.has_permission(
                "张三",
                DESIGN_KNOWLEDGE_VIEW_PERMISSION,
                legacy_role="研发硬件",
                legacy_allowed_roles=["研发硬件"],
            )
        )
        self.service.set_position_permissions(
            position_id,
            [
                DESIGN_KNOWLEDGE_VIEW_PERMISSION,
                DESIGN_KNOWLEDGE_CREATE_PERMISSION,
                DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
            ],
            actor_username="admin",
        )
        self.assertTrue(self.service.has_permission("张三", DESIGN_KNOWLEDGE_VIEW_PERMISSION))
        self.assertTrue(self.service.has_permission("张三", DESIGN_KNOWLEDGE_CREATE_PERMISSION))
        self.assertTrue(self.service.has_permission("张三", DESIGN_KNOWLEDGE_REVIEW_PERMISSION))

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

    def test_sample_close_permissions_are_merged_and_workflow_references_are_updated(self):
        """两项旧关闭权限应合并，并同步迁移已保存流程的资格权限。"""
        self.service.migrate_legacy_users()
        self.service.identity_store.seed_permission_catalog(
            [
                {
                    "code": SAMPLE_ISSUE_LEGACY_CLOSE_DEFAULT_APPROVE_PERMISSION,
                    "name": "旧默认关闭审批",
                    "module": "样品问题跟进",
                },
                {
                    "code": SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION,
                    "name": "旧电子组关闭审批",
                    "module": "样品问题跟进",
                },
            ]
        )
        org_unit_id = self.service.save_org_unit(code="org.sample.close", name="关闭审批测试部")
        position_id = self.service.save_position(code="sample.close.approver", name="关闭审批岗位")
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=position_id,
        )
        self.service.set_position_permissions(
            position_id,
            [
                SAMPLE_ISSUE_LEGACY_CLOSE_DEFAULT_APPROVE_PERMISSION,
                SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION,
            ],
        )
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="sample_issue.close.legacy_electron",
            module="sample_issue",
            event="close_request",
            name="旧电子组关闭流程",
            priority=10,
            condition={
                "requester_org_unit_ids": [],
                "requester_position_ids": [],
                "include_child_org_units": True,
            },
            approver={
                "strategy": "permission",
                "permission_code": SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION,
            },
            required_permission_code=SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION,
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        self.service.sync_permission_catalog()

        permission_codes = {item["code"] for item in self.service.list_permissions()}
        self.assertIn(SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION, permission_codes)
        self.assertNotIn(SAMPLE_ISSUE_LEGACY_CLOSE_DEFAULT_APPROVE_PERMISSION, permission_codes)
        self.assertNotIn(SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION, permission_codes)
        self.assertEqual(
            self.service.get_position_permission_codes(position_id),
            {SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION},
        )
        workflow = next(
            item
            for item in self.service.list_approval_workflows(module="sample_issue")
            if item["workflow_id"] == workflow_id
        )
        self.assertEqual(
            workflow["active_version"]["required_permission_code"],
            SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
        )
        self.assertEqual(
            workflow["active_version"]["approver"]["permission_code"],
            SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
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
