"""ECN执行负责人停用后的识别、改派和提醒回归。"""

import copy
import tempfile
import unittest
from pathlib import Path

from src.ecn_access import (
    build_ecn_access_snapshot,
    can_confirm_ecn_material_spec,
    get_ecn_execution_assignment_issues,
    is_ecn_pending_for_user,
    resolve_ecn_material_spec_responsibility,
)
from src.ecn_management_config import (
    ECNState,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    ECN_SCHEME_GROUP_MATERIAL,
    build_ecn_execution_info,
    get_ecn_material_execution_specs,
)
from src.modules.ecn import notifications
from src.modules.ecn.material_tasks import update_material_task_assignee
from src.modules.ecn.list_view import build_ecn_management_grid_row
from src.permission_catalog import (
    ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
    ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
    ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
    ECN_VIEW_PERMISSION,
)
from tests.test_ecn_notifications import Users
from tests.test_error_management_concurrency import load_isolated_db_storage


class AssignmentUsers(Users):
    def load_users(self):
        return {
            **super().load_users(),
            "sales_manager": {"role": "销售主管", "status": "active"},
        }

    def get_user(self, username):
        return self.load_users().get(username, {})

    def list_primary_memberships(self):
        return {
            "sales_manager": {
                "position_name": "销售主管",
                "manager_username": "",
            }
        }


class HierarchyUsers:
    storage_mode = "database"

    def __init__(self, supervisor_permissions: set[str]):
        self.users = {
            "sales": {"role": "销售", "status": "disabled"},
            "supervisor": {"role": "销售主管", "status": "active"},
            "director": {"role": "销售总监", "status": "active"},
        }
        self.permissions = {
            "supervisor": supervisor_permissions,
            "director": {
                ECN_VIEW_PERMISSION,
                ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
            },
        }
        self.memberships = {
            "sales": {"position_name": "项目销售", "manager_username": "supervisor"},
            "supervisor": {"position_name": "销售主管", "manager_username": "director"},
            "director": {"position_name": "销售总监", "manager_username": ""},
        }

    def load_users(self):
        return self.users

    def list_active_user_permission_codes(self):
        return self.permissions

    def list_primary_memberships(self):
        return self.memberships


class ECNResponsibilityEscalationTests(unittest.TestCase):
    @staticmethod
    def project_sales_spec(users: list[str]) -> dict:
        return {
            "available": True,
            "responsible_type": "project_sales",
            "responsible_key": "项目销售",
            "roles": [],
            "users": users,
            "project": "P1",
            "label": "P1 · 项目销售",
        }

    def test_stops_at_available_sales_supervisor(self):
        service = HierarchyUsers(
            {ECN_VIEW_PERMISSION, ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION}
        )
        snapshot = build_ecn_access_snapshot(service)
        resolved = resolve_ecn_material_spec_responsibility(
            self.project_sales_spec(["sales"]), user_service=service, access_snapshot=snapshot
        )

        self.assertEqual(resolved["users"], ["supervisor"])
        self.assertIn("销售主管", resolved["label"])
        self.assertNotIn("director", resolved["users"])
        self.assertTrue(
            can_confirm_ecn_material_spec(
                resolved,
                "销售主管",
                "supervisor",
                user_service=service,
                access_snapshot=snapshot,
            )
        )

    def test_available_project_sales_keeps_own_task(self):
        service = HierarchyUsers(
            {ECN_VIEW_PERMISSION, ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION}
        )
        service.users["sales"]["status"] = "active"
        service.permissions["sales"] = {
            ECN_VIEW_PERMISSION,
            ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
        }
        snapshot = build_ecn_access_snapshot(service)
        resolved = resolve_ecn_material_spec_responsibility(
            self.project_sales_spec(["sales"]), user_service=service, access_snapshot=snapshot
        )

        self.assertEqual(resolved["users"], ["sales"])
        self.assertEqual(resolved["responsible_type"], "project_sales")

    def test_skips_unavailable_supervisor_and_reaches_director(self):
        service = HierarchyUsers({ECN_VIEW_PERMISSION})
        snapshot = build_ecn_access_snapshot(service)
        resolved = resolve_ecn_material_spec_responsibility(
            self.project_sales_spec(["sales"]), user_service=service, access_snapshot=snapshot
        )

        self.assertEqual(resolved["users"], ["director"])
        self.assertIn("销售总监", resolved["label"])

    def test_direct_manager_can_be_sales_director(self):
        service = HierarchyUsers({ECN_VIEW_PERMISSION})
        service.memberships["sales"]["manager_username"] = "director"
        snapshot = build_ecn_access_snapshot(service)
        resolved = resolve_ecn_material_spec_responsibility(
            self.project_sales_spec(["sales"]), user_service=service, access_snapshot=snapshot
        )

        self.assertEqual(resolved["users"], ["director"])

    def test_missing_project_sales_starts_at_supervisor_level(self):
        service = HierarchyUsers(
            {ECN_VIEW_PERMISSION, ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION}
        )
        snapshot = build_ecn_access_snapshot(service)
        spec = self.project_sales_spec([])
        spec.update(
            key="客户/在途::项目销售::P1",
            responsible_type="sales_supervisor",
            responsible_key="销售主管",
            roles=["销售主管"],
        )
        resolved = resolve_ecn_material_spec_responsibility(
            spec, user_service=service, access_snapshot=snapshot
        )

        self.assertEqual(resolved["users"], ["supervisor"])
        self.assertIn("项目销售未识别，转销售主管：supervisor", resolved["label"])
        self.assertNotIn("supervisor（销售主管）", resolved["label"])

    def test_notification_targets_only_first_available_management_level(self):
        service = HierarchyUsers(
            {ECN_VIEW_PERMISSION, ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION}
        )
        item = {
            "item_id": "M1",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "更换",
            "projects": ["P1"],
            "traceability_levels": ["客户/在途"],
        }
        execution = build_ecn_execution_info([item])
        execution["stage"] = ECN_EXECUTION_STAGE_MATERIAL
        execution["material_confirmations"]["M1"]["traceability_tasks"] = {
            "客户/在途::项目销售::P1": {
                "level": "客户/在途",
                "responsible_key": "项目销售",
                "responsible_type": "project_sales",
                "label": "P1 · sales",
                "project": "P1",
                "stage_index": 0,
                "roles": [],
                "users": ["sales"],
                "required_permission_code": ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
                "workflow_assignment": {"workflow_code": "ecn.execution_customer_transit.test"},
                "confirmed": False,
                "history": [],
            }
        }
        record = {
            "ecn_id": "ECN-hierarchy",
            "basic_info": {"title": "层级通知测试"},
            "change_items": [item],
            "workflow": {"current_state": ECNState.ECN_EXECUTING},
            "execution_info": execution,
        }

        pending = notifications.collect_pending_users(record, service)

        self.assertIn("supervisor", pending)
        self.assertNotIn("director", pending)

    def test_other_department_also_stops_at_first_available_manager(self):
        service = HierarchyUsers({ECN_VIEW_PERMISSION})
        service.users.update(
            {
                "buyer": {"role": "采购", "status": "disabled"},
                "purchase_manager": {"role": "采购主管", "status": "active"},
                "purchase_director": {"role": "供应链总监", "status": "active"},
            }
        )
        service.permissions.update(
            {
                "purchase_manager": {
                    ECN_VIEW_PERMISSION,
                    ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
                },
                "purchase_director": {
                    ECN_VIEW_PERMISSION,
                    ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
                },
            }
        )
        service.memberships.update(
            {
                "buyer": {"position_name": "采购", "manager_username": "purchase_manager"},
                "purchase_manager": {
                    "position_name": "采购主管",
                    "manager_username": "purchase_director",
                },
                "purchase_director": {
                    "position_name": "供应链总监",
                    "manager_username": "",
                },
            }
        )
        snapshot = build_ecn_access_snapshot(service)
        resolved = resolve_ecn_material_spec_responsibility(
            {
                "responsible_type": "role",
                "responsible_key": "采购",
                "roles": ["采购"],
                "users": [],
                "label": "采购",
            },
            user_service=service,
            access_snapshot=snapshot,
        )

        self.assertEqual(resolved["users"], ["purchase_manager"])
        self.assertNotIn("purchase_director", resolved["users"])

    def test_purchase_keyword_matches_mass_production_purchase_role(self):
        service = HierarchyUsers({ECN_VIEW_PERMISSION})
        service.users["mass_buyer"] = {"role": "采购（量产）", "status": "active"}
        service.permissions["mass_buyer"] = {
            ECN_VIEW_PERMISSION,
            ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
        }
        service.memberships["mass_buyer"] = {
            "position_name": "采购专员（量产）",
            "manager_username": "",
        }
        snapshot = build_ecn_access_snapshot(service)
        resolved = resolve_ecn_material_spec_responsibility(
            {
                "responsible_type": "role",
                "responsible_key": "采购",
                "roles": ["采购"],
                "users": [],
                "label": "采购",
            },
            user_service=service,
            access_snapshot=snapshot,
        )

        self.assertEqual(resolved["users"], ["mass_buyer"])
        self.assertEqual(resolved["resolution_mode"], "current_level")


class ECNAssignmentHealthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = load_isolated_db_storage(
            "ecn_assignment_health_db", Path(self.temp.name) / "ecn.db"
        )
        await self.storage.init_db()
        self.service = AssignmentUsers()
        self.item = {
            "item_id": "M1",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "更换",
            "projects": ["P1"],
            "traceability_levels": ["客户/在途"],
            "disposition_measure": "报废",
        }
        execution = build_ecn_execution_info([self.item])
        execution["stage"] = ECN_EXECUTION_STAGE_MATERIAL
        execution["material_confirmations"]["M1"]["traceability_tasks"] = {
            "客户/在途::项目销售::P1": {
                "level": "客户/在途",
                "responsible_key": "项目销售",
                "responsible_type": "project_sales",
                "label": "P1 · inactive",
                "project": "P1",
                "stage_index": 0,
                "roles": [],
                "users": ["inactive"],
                "required_permission_code": ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
                "workflow_assignment": {"workflow_code": "ecn.execution_customer_transit.test"},
                "confirmed": False,
                "history": [],
            }
        }
        self.record = {
            "ecn_id": "ECN-health",
            "basic_info": {"title": "停用负责人测试"},
            "change_items": [self.item],
            "workflow": {"current_state": ECNState.ECN_EXECUTING},
            "execution_info": execution,
        }
        await self.save()

    async def asyncTearDown(self):
        await self.storage.close_db()
        self.temp.cleanup()

    async def save(self):
        await self.storage.set_item("ecn_management_data", {"ECN-health": self.record})

    def sales_task(self):
        return self.record["execution_info"]["material_confirmations"]["M1"][
            "traceability_tasks"
        ]["客户/在途::项目销售::P1"]

    async def test_disabled_specific_owner_creates_assistant_pending_and_grid_warning(self):
        issues = get_ecn_execution_assignment_issues(self.record, user_service=self.service)
        self.assertEqual(issues[0]["owner"], "inactive")
        self.assertTrue(
            is_ecn_pending_for_user(
                self.record, "manager", "研发经理", user_service=self.service
            )
        )
        row = build_ecn_management_grid_row(
            self.record,
            "manager",
            "研发经理",
            user_service=self.service,
        )
        self.assertEqual(row["attention"], "负责人异常·待改派")
        self.assertIn("待改派", str(row["progress"]))
        pending = notifications.collect_pending_users(self.record, self.service)
        details = notifications.pending_task_details(self.record, pending, self.service)
        self.assertTrue(any("原负责人：inactive" in item for item in details["manager"]))
        self.assertTrue(any("物料方案 #01" in item for item in details["manager"]))
        self.assertFalse(any("客户/在途::项目销售::P1" in item for item in details["manager"]))

    async def test_material_reassignment_changes_exact_owner_and_is_audited(self):
        result = await update_material_task_assignee(
            "ECN-health",
            "M1",
            "客户/在途::项目销售::P1",
            copy.deepcopy(self.sales_task()),
            username="manager",
            role="研发经理",
            service=self.service,
            assignee="writer",
            storage=self.storage,
        )
        self.assertTrue(result.ok)
        assert result.record is not None
        self.record = result.record
        specs = get_ecn_material_execution_specs(
            self.item,
            self.record["execution_info"]["material_confirmations"]["M1"],
        )
        spec = next(item for item in specs if item["key"] == "客户/在途::项目销售::P1")
        self.assertEqual(spec["responsible_type"], "assigned_user")
        self.assertEqual(spec["users"], ["writer"])
        self.assertTrue(
            can_confirm_ecn_material_spec(
                spec, "研发硬件", "writer", user_service=self.service
            )
        )
        self.assertFalse(
            can_confirm_ecn_material_spec(
                spec, "研发经理", "manager", user_service=self.service
            )
        )
        self.assertEqual(get_ecn_execution_assignment_issues(self.record, user_service=self.service), [])
        self.assertIn("改派", self.record["approval_log"][-1]["action"])

    async def test_stale_reassignment_and_disabled_target_are_rejected(self):
        baseline = copy.deepcopy(self.sales_task())
        first = await update_material_task_assignee(
            "ECN-health",
            "M1",
            "客户/在途::项目销售::P1",
            baseline,
            username="manager",
            role="研发经理",
            service=self.service,
            assignee="writer",
            storage=self.storage,
        )
        self.assertTrue(first.ok)
        stale = await update_material_task_assignee(
            "ECN-health",
            "M1",
            "客户/在途::项目销售::P1",
            baseline,
            username="manager",
            role="研发经理",
            service=self.service,
            assignee="manager",
            storage=self.storage,
        )
        self.assertFalse(stale.ok)
        assert first.record is not None
        current = first.record["execution_info"]["material_confirmations"]["M1"][
            "traceability_tasks"
        ]["客户/在途::项目销售::P1"]
        disabled = await update_material_task_assignee(
            "ECN-health",
            "M1",
            "客户/在途::项目销售::P1",
            current,
            username="manager",
            role="研发经理",
            service=self.service,
            assignee="inactive",
            storage=self.storage,
        )
        self.assertFalse(disabled.ok)

    async def test_disabled_special_owner_is_escalated_before_material_stage(self):
        self.record["execution_info"]["stage"] = ECN_EXECUTION_STAGE_ASSISTANT
        self.record["execution_info"]["ordinary_confirmations"] = {
            "S1": {"confirmed": False, "assignee": "inactive"}
        }
        issues = get_ecn_execution_assignment_issues(self.record, user_service=self.service)
        self.assertEqual([issue["kind"] for issue in issues], ["special"])
        pending = notifications.collect_pending_users(self.record, self.service)
        details = notifications.pending_task_details(self.record, pending, self.service)
        self.assertTrue(any("负责人异常，请改派" in item for item in details["manager"]))
