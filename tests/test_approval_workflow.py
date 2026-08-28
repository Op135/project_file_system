import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd

from src.approval_workflow import (
    create_approval_assignments,
    get_workflow_event_definition,
    import_design_knowledge_legacy_workflows,
    import_project_overview_legacy_workflows,
    is_assigned_approver,
    resolve_approval_workflow,
)
from src.permission_catalog import (
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,
    PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,
    SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
    SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION,
)
from src.user_service import UserService


class ApprovalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        excel_path = root / "users.xlsx"
        pd.DataFrame(
            [
                {"用户名": "admin", "密码": "admin-pass", "角色": "admin"},
                {"用户名": "张三", "密码": "123456", "角色": "研发硬件"},
                {"用户名": "李四", "密码": "123456", "角色": "电子主管"},
                {"用户名": "王五", "密码": "123456", "角色": "质量主管"},
            ]
        ).to_excel(excel_path, index=False, engine="openpyxl")
        self.service = UserService(
            excel_path=excel_path,
            db_path=root / "identity.db",
            password_iterations=1_000,
        )
        self.service.migrate_legacy_users()

        self.org_unit_id = self.service.save_org_unit(
            code="org.workflow.test",
            name="流程测试部",
        )
        self.requester_position_id = self.service.save_position(
            code="position.workflow.requester",
            name="申请岗位",
            org_unit_ids=[self.org_unit_id],
        )
        self.approver_position_id = self.service.save_position(
            code="position.workflow.approver",
            name="审批岗位",
            org_unit_ids=[self.org_unit_id],
        )
        self.observer_position_id = self.service.save_position(
            code="position.workflow.observer",
            name="其他有权岗位",
            org_unit_ids=[self.org_unit_id],
        )
        self.service.set_primary_membership(
            "张三",
            org_unit_id=self.org_unit_id,
            position_id=self.requester_position_id,
            manager_username="李四",
        )
        self.service.set_primary_membership(
            "李四",
            org_unit_id=self.org_unit_id,
            position_id=self.approver_position_id,
        )
        self.service.set_primary_membership(
            "王五",
            org_unit_id=self.org_unit_id,
            position_id=self.observer_position_id,
        )
        self.service.set_position_permissions(
            self.approver_position_id,
            [SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION],
        )
        self.service.set_position_permissions(
            self.observer_position_id,
            [SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_position_workflow(self, *, code="sample_issue.close.test", priority=10):
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code=code,
            module="sample_issue",
            event="close_request",
            name="样品关闭测试流程",
            priority=priority,
            condition={
                "requester_org_unit_ids": [self.org_unit_id],
                "requester_position_ids": [self.requester_position_id],
                "include_child_org_units": True,
            },
            approver={
                "strategy": "position",
                "position_ids": [self.approver_position_id],
                "org_scope": "any",
                "org_unit_ids": [],
            },
            required_permission_code=SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            approval_mode="any",
            notification={"notify_assignees": True},
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")
        self.workflow_id = workflow_id
        return workflow_id

    def test_stable_position_id_survives_display_name_change(self):
        self.create_position_workflow()

        first = resolve_approval_workflow(
            self.service,
            module="sample_issue",
            event="close_request",
            requester_username="张三",
        )
        self.assertEqual(first["status"], "matched")
        self.assertEqual([item["username"] for item in first["approvers"]], ["李四"])

        # 流程保存的是岗位 ID，因此管理员调整岗位显示名后仍能正确匹配。
        unchanged_id = self.service.save_position(
            code="position.workflow.approver",
            name="电子审批负责人",
            org_unit_ids=[self.org_unit_id],
        )
        self.assertEqual(unchanged_id, self.approver_position_id)
        second = resolve_approval_workflow(
            self.service,
            module="sample_issue",
            event="close_request",
            requester_username="张三",
        )
        self.assertEqual(second["status"], "matched")
        self.assertEqual([item["username"] for item in second["approvers"]], ["李四"])

    def test_assignment_is_exact_even_when_another_user_has_permission(self):
        self.create_position_workflow()
        result = create_approval_assignments(
            self.service,
            module="sample_issue",
            event="close_request",
            entity_id="sample-001",
            task_key="close_approval:req-001",
            requester_username="张三",
        )

        self.assertEqual(result["status"], "matched")
        self.assertTrue(
            is_assigned_approver(
                self.service,
                module="sample_issue",
                entity_id="sample-001",
                task_key="close_approval:req-001",
                username="李四",
            )
        )
        self.assertFalse(
            is_assigned_approver(
                self.service,
                module="sample_issue",
                entity_id="sample-001",
                task_key="close_approval:req-001",
                username="王五",
            )
        )
        self.assertTrue(
            self.service.complete_work_assignment(
                module="sample_issue",
                entity_id="sample-001",
                task_key="close_approval:req-001",
                username="李四",
                approval_mode="any",
            )
        )
        self.assertEqual(
            self.service.list_pending_assignment_usernames(
                module="sample_issue",
                entity_id="sample-001",
                task_key="close_approval:req-001",
            ),
            [],
        )

    def test_same_priority_match_is_rejected_as_ambiguous(self):
        self.create_position_workflow(code="sample_issue.close.first", priority=10)
        self.create_position_workflow(code="sample_issue.close.second", priority=10)

        result = resolve_approval_workflow(
            self.service,
            module="sample_issue",
            event="close_request",
            requester_username="张三",
        )

        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["matched_workflows"]), 2)

    def test_published_version_is_immutable_until_new_draft_is_published(self):
        workflow_id = self.create_position_workflow()
        before = self.service.list_approval_workflows()[0]
        self.assertEqual(before["active_version"]["version_number"], 1)
        self.assertIsNone(before["draft_version"])

        self.service.save_approval_workflow_draft(
            workflow_id=workflow_id,
            code="sample_issue.close.test",
            module="sample_issue",
            event="close_request",
            name="修改后的流程名称",
            priority=20,
            condition=before["active_version"]["condition"],
            approver=before["active_version"]["approver"],
            required_permission_code=SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            approval_mode="any",
            actor_username="admin",
        )
        during = self.service.list_approval_workflows()[0]
        self.assertEqual(during["active_version"]["version_number"], 1)
        self.assertEqual(during["active_version"]["priority"], 10)
        self.assertEqual(during["draft_version"]["version_number"], 2)
        self.assertEqual(during["draft_version"]["priority"], 20)

        self.service.publish_approval_workflow(workflow_id, actor_username="admin")
        after = self.service.list_approval_workflows()[0]
        self.assertEqual(after["active_version"]["version_number"], 2)
        self.assertEqual(after["active_version"]["priority"], 20)

    def test_design_knowledge_events_and_legacy_import_are_idempotent(self):
        knowledge_event = get_workflow_event_definition(
            "design_knowledge",
            "knowledge_review",
        )
        tag_event = get_workflow_event_definition(
            "design_knowledge",
            "tag_review",
        )
        self.assertIsNotNone(knowledge_event)
        self.assertIsNotNone(tag_event)
        assert knowledge_event is not None
        assert tag_event is not None
        self.assertEqual(
            knowledge_event.permission_codes,
            (DESIGN_KNOWLEDGE_REVIEW_PERMISSION,),
        )
        self.assertEqual(
            tag_event.permission_codes,
            (DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,),
        )

        created, warnings = import_design_knowledge_legacy_workflows(
            self.service,
            actor_username="admin",
        )
        self.assertGreater(created, 0)
        self.assertTrue(warnings)
        imported = self.service.list_approval_workflows(module="design_knowledge")
        self.assertEqual(len(imported), created)
        self.assertEqual(
            {item["event"] for item in imported},
            {"knowledge_review", "tag_review"},
        )
        created_again, _warnings_again = import_design_knowledge_legacy_workflows(
            self.service,
            actor_username="admin",
        )
        self.assertEqual(created_again, 0)

    def test_project_overview_events_and_legacy_import_are_idempotent(self):
        """概述两类审批事件可在管理界面配置，旧 JSON 只能单向生成草稿。"""
        batch_event = get_workflow_event_definition("project_overview", "batch_change")
        correction_event = get_workflow_event_definition("project_overview", "correction")
        self.assertIsNotNone(batch_event)
        self.assertIsNotNone(correction_event)
        assert batch_event is not None
        assert correction_event is not None
        self.assertEqual(batch_event.permission_codes, (PROJECT_OVERVIEW_BATCH_REVIEW_PERMISSION,))
        self.assertEqual(correction_event.permission_codes, (PROJECT_OVERVIEW_CORRECTION_REVIEW_PERMISSION,))

        created, warnings = import_project_overview_legacy_workflows(
            self.service,
            actor_username="admin",
        )
        self.assertGreater(created, 0)
        self.assertTrue(warnings)
        imported = self.service.list_approval_workflows(module="project_overview")
        self.assertEqual(len(imported), created)
        self.assertEqual({item["event"] for item in imported}, {"batch_change", "correction"})
        created_again, _warnings_again = import_project_overview_legacy_workflows(
            self.service,
            actor_username="admin",
        )
        self.assertEqual(created_again, 0)


class SampleIssueWorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from nicegui import app
        from src.pages import sample_issue_collection as sample_issue

        self.app = app
        self.sample_issue = sample_issue
        self.original_user_service = getattr(app.state, "user_service", None)
        self.original_can_view = sample_issue.can_view_sample_issue_collection
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        excel_path = root / "users.xlsx"
        pd.DataFrame(
            [
                {"用户名": "admin", "密码": "admin-pass", "角色": "admin"},
                {"用户名": "张三", "密码": "123456", "角色": "申请人"},
                {"用户名": "李四", "密码": "123456", "角色": "审批人"},
                {"用户名": "王五", "密码": "123456", "角色": "旁观者"},
            ]
        ).to_excel(excel_path, index=False, engine="openpyxl")
        self.service = UserService(
            excel_path=excel_path,
            db_path=root / "identity.db",
            password_iterations=1_000,
        )
        self.service.migrate_legacy_users()
        org_unit_id = self.service.save_org_unit(code="org.sample.flow", name="样品流程部")
        requester_position_id = self.service.save_position(
            code="position.sample.requester",
            name="样品申请岗位",
            org_unit_ids=[org_unit_id],
        )
        approver_position_id = self.service.save_position(
            code="position.sample.approver",
            name="样品审批岗位",
            org_unit_ids=[org_unit_id],
        )
        other_position_id = self.service.save_position(
            code="position.sample.other",
            name="其他审批岗位",
            org_unit_ids=[org_unit_id],
        )
        self.service.set_primary_membership(
            "张三",
            org_unit_id=org_unit_id,
            position_id=requester_position_id,
        )
        self.service.set_primary_membership(
            "李四",
            org_unit_id=org_unit_id,
            position_id=approver_position_id,
        )
        self.service.set_primary_membership(
            "王五",
            org_unit_id=org_unit_id,
            position_id=other_position_id,
        )
        self.service.set_position_permissions(
            approver_position_id,
            [SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION],
        )
        self.service.set_position_permissions(
            other_position_id,
            [SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION],
        )
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="sample_issue.close.integration",
            module="sample_issue",
            event="close_request",
            name="样品关闭集成流程",
            priority=10,
            condition={
                "requester_org_unit_ids": [org_unit_id],
                "requester_position_ids": [requester_position_id],
                "include_child_org_units": True,
            },
            approver={
                "strategy": "position",
                "position_ids": [approver_position_id],
                "org_scope": "any",
                "org_unit_ids": [],
            },
            required_permission_code=SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")
        self.workflow_id = workflow_id
        app.state.user_service = self.service
        sample_issue.can_view_sample_issue_collection = lambda role, username="": True

    async def asyncTearDown(self):
        self.app.state.user_service = self.original_user_service
        self.sample_issue.can_view_sample_issue_collection = self.original_can_view
        self.temp_dir.cleanup()

    async def test_sample_close_uses_snapshot_and_exact_assignment(self):
        issue = self.sample_issue.generate_initial_sample_issue_data("张三", "申请人")
        issue["issue_id"] = "SPI-WORKFLOW-001"
        issue["countermeasure"].update(
            {
                "owner": "张三",
                "reason_analysis": "定位异常",
                "temporary_action": "临时调整",
                "corrective_preventive_action": "修订设计",
                "due_date": "2026-08-30",
            }
        )
        stored = {"record": issue}

        async def fake_atomic_update(issue_id, update_function, **_kwargs):
            self.assertEqual(issue_id, "SPI-WORKFLOW-001")
            code, updated = update_function(copy.deepcopy(stored["record"]))
            changed = code == "updated"
            if changed:
                stored["record"] = updated
            return self.sample_issue.SampleIssueUpdateResult(
                db_success=True,
                changed=changed,
                code=code,
                record=copy.deepcopy(stored["record"]),
            )

        original_atomic_update = self.sample_issue.atomic_sample_issue_update
        original_record_nature = self.sample_issue.record_sample_closure_nature
        self.sample_issue.atomic_sample_issue_update = fake_atomic_update
        self.sample_issue.record_sample_closure_nature = AsyncMock(return_value=True)
        try:
            requested = await self.sample_issue.submit_sample_close_request(
                "SPI-WORKFLOW-001",
                "张三",
                "申请人",
            )
            self.assertTrue(requested.changed)
            self.assertIsNotNone(requested.record)
            assert requested.record is not None
            close_request = self.sample_issue.get_pending_close_request(
                requested.record["countermeasure"]
            )
            self.assertIsNotNone(close_request)
            assert close_request is not None
            assignment = close_request["workflow_assignment"]
            self.assertEqual(assignment["workflow_code"], "sample_issue.close.integration")
            self.assertEqual(assignment["assignee_usernames"], ["李四"])
            legacy_snapshot = copy.deepcopy(close_request)
            legacy_snapshot["workflow_assignment"]["required_permission_code"] = (
                SAMPLE_ISSUE_LEGACY_CLOSE_ELECTRON_APPROVE_PERMISSION
            )
            self.assertTrue(
                self.sample_issue.is_sample_close_approver(
                    "审批人",
                    legacy_snapshot,
                    "李四",
                )
            )

            # 王五拥有同一个稳定权限，但没有本单待办，因此不能代替李四审批。
            forbidden = await self.sample_issue.approve_sample_close_request(
                "SPI-WORKFLOW-001",
                close_request["id"],
                True,
                "王五",
                "旁观者",
                "设计问题",
            )
            self.assertEqual(forbidden.code, "forbidden")
            approved = await self.sample_issue.approve_sample_close_request(
                "SPI-WORKFLOW-001",
                close_request["id"],
                True,
                "李四",
                "审批人",
                "设计问题",
            )
            self.assertTrue(approved.changed)
            self.assertIsNotNone(approved.record)
            assert approved.record is not None
            self.assertEqual(approved.record["countermeasure"]["closed_by"], "李四")
            self.assertEqual(
                self.service.list_pending_assignment_usernames(
                    module="sample_issue",
                    entity_id="SPI-WORKFLOW-001",
                    task_key=assignment["task_key"],
                ),
                [],
            )
        finally:
            self.sample_issue.atomic_sample_issue_update = original_atomic_update
            self.sample_issue.record_sample_closure_nature = original_record_nature

    async def test_database_mode_rejects_new_close_request_without_published_workflow(self):
        """统一资格权限后不得再通过旧角色路由创建无具体审批人的申请。"""
        self.service.set_approval_workflow_status(
            self.workflow_id,
            "disabled",
            actor_username="admin",
        )

        result = await self.sample_issue.submit_sample_close_request(
            "SPI-NO-WORKFLOW",
            "张三",
            "申请人",
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.code, "workflow_no_match")


if __name__ == "__main__":
    unittest.main()
