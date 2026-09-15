"""ECN执行负责人停用后的识别、改派和提醒回归。"""

import copy
import tempfile
import unittest
from pathlib import Path

from src.ecn_access import (
    can_confirm_ecn_material_spec,
    get_ecn_execution_assignment_issues,
    is_ecn_pending_for_user,
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
from tests.test_ecn_notifications import Users
from tests.test_error_management_concurrency import load_isolated_db_storage


class AssignmentUsers(Users):
    def get_user(self, username):
        return self.load_users().get(username, {})


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
        execution = build_ecn_execution_info([self.item], {"P1": "inactive"})
        execution["stage"] = ECN_EXECUTION_STAGE_MATERIAL
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
