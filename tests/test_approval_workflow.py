import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import pandas as pd

from src.approval_workflow import (
    advance_approval_sequence,
    create_approval_assignments,
    create_approval_sequence_assignments,
    get_workflow_event_definition,
    get_approval_workflow_editor_nodes,
    import_design_knowledge_legacy_workflows,
    import_project_overview_legacy_workflows,
    is_assigned_approver,
    resolve_approval_workflow,
)
from src.ecn_management_config import ECN_SCHEME_GROUP_MATERIAL, ECNState
from src.ecn_workflow import (
    build_ecn_execution_info_from_workflows,
    finish_ecr_approval,
    is_ecr_assigned_approver,
    reconcile_ecn_work_assignments,
    start_ecr_approval,
)
from src.notification_recipients import resolve_position_usernames
from src.permission_catalog import (
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
    ECN_ECR_APPROVE_PERMISSION,
    ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
    ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
    ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
    ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
    ECN_SCHEME_APPROVE_PERMISSION,
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

    def test_ecn_scheme_workflow_can_match_any_scheme_author_membership(self):
        author_org_id = self.service.save_org_unit(
            code="org.workflow.electronic",
            name="电子组",
        )
        author_position_id = self.service.save_position(
            code="position.workflow.electronic.engineer",
            name="电子工程师",
            org_unit_ids=[author_org_id],
        )
        self.service.set_primary_membership(
            "王五",
            org_unit_id=author_org_id,
            position_id=author_position_id,
        )
        self.service.set_position_permissions(
            self.approver_position_id,
            [SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION, ECN_SCHEME_APPROVE_PERMISSION],
        )
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="ecn.scheme.electronic.author",
            module="ecn",
            event="scheme_review",
            name="电子组方案评审",
            priority=10,
            condition={
                "requester_org_unit_ids": [],
                "requester_position_ids": [],
                "scheme_author_org_unit_ids": [author_org_id],
                "scheme_author_position_ids": [author_position_id],
                "include_child_scheme_author_org_units": True,
            },
            approver={
                "strategy": "position",
                "position_ids": [self.approver_position_id],
                "org_scope": "any",
                "org_unit_ids": [],
            },
            required_permission_code=ECN_SCHEME_APPROVE_PERMISSION,
            approval_mode="any",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        without_author = resolve_approval_workflow(
            self.service,
            module="ecn",
            event="scheme_review",
            requester_username="张三",
        )
        self.assertEqual(without_author["status"], "no_match")

        with_author = resolve_approval_workflow(
            self.service,
            module="ecn",
            event="scheme_review",
            requester_username="张三",
            context={"scheme_author_usernames": ["李四", "王五"]},
        )
        self.assertEqual(with_author["status"], "matched")
        self.assertEqual(with_author["workflow"]["code"], "ecn.scheme.electronic.author")

    def test_completion_cc_is_versioned_and_pinned_to_assignment(self):
        """完成抄送岗位应随发布版本和单据审批快照固定。"""
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="sample_issue.close.completion_cc",
            module="sample_issue",
            event="close_request",
            name="样品关闭完成抄送测试",
            priority=5,
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
            notification={
                "notify_assignees": True,
                "notify_requester_on_result": True,
                "completion_cc": {
                    "enabled": True,
                    "position_ids": [self.observer_position_id],
                },
            },
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        created = create_approval_assignments(
            self.service,
            module="sample_issue",
            event="close_request",
            entity_id="sample-completion-001",
            task_key="close_approval:req-completion-001",
            requester_username="张三",
        )
        self.assertEqual(created["status"], "matched")
        self.assertEqual(
            created["assignment"]["notification"]["completion_cc"],
            {"enabled": True, "position_ids": [self.observer_position_id]},
        )
        usernames, missing_positions = resolve_position_usernames(
            [self.observer_position_id],
            user_service=self.service,
        )
        self.assertEqual(usernames, ["王五"])
        self.assertEqual(missing_positions, [])

    def test_completion_cc_rejects_unknown_position(self):
        with self.assertRaisesRegex(ValueError, "不存在或已停用"):
            self.service.save_approval_workflow_draft(
                code="sample_issue.close.invalid_cc",
                module="sample_issue",
                event="close_request",
                name="无效完成抄送岗位",
                priority=5,
                condition={},
                approver={
                    "strategy": "position",
                    "position_ids": [self.approver_position_id],
                    "org_scope": "any",
                    "org_unit_ids": [],
                },
                required_permission_code=SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                notification={
                    "completion_cc": {
                        "enabled": True,
                        "position_ids": ["missing-position"],
                    }
                },
                actor_username="admin",
            )

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

    def test_sequential_nodes_support_any_then_all_approval(self):
        """串行流程应先完成任意一人节点，再等待下一节点全部人员会签。"""
        users = self.service.load_users()
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="sample_issue.close.sequence_test",
            module="sample_issue",
            event="close_request",
            name="样品关闭多节点测试流程",
            priority=5,
            condition={
                "requester_org_unit_ids": [self.org_unit_id],
                "requester_position_ids": [self.requester_position_id],
                "include_child_org_units": True,
            },
            approver={
                "nodes": [
                    {
                        "node_key": "technical_review",
                        "name": "技术审核",
                        "approval_mode": "any",
                        "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                        "approver": {
                            "strategy": "users",
                            "user_ids": [users["李四"]["user_id"], users["王五"]["user_id"]],
                        },
                    },
                    {
                        "node_key": "joint_review",
                        "name": "联合会签",
                        "approval_mode": "all",
                        "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                        "approver": {
                            "strategy": "users",
                            "user_ids": [users["李四"]["user_id"], users["王五"]["user_id"]],
                        },
                    },
                ]
            },
            required_permission_code=SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            approval_mode="sequential",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        created = create_approval_sequence_assignments(
            self.service,
            module="sample_issue",
            event="close_request",
            entity_id="sample-sequence-001",
            task_key="close_approval:req-sequence-001",
            requester_username="张三",
        )
        self.assertEqual(created["status"], "matched")
        assignment = created["assignment"]
        self.assertEqual(len(assignment["nodes"]), 2)
        self.assertEqual(assignment["current_node_index"], 0)

        first = advance_approval_sequence(
            self.service,
            module="sample_issue",
            entity_id="sample-sequence-001",
            assignment=assignment,
            username="李四",
        )
        self.assertEqual(first["status"], "advanced")
        assignment = first["assignment"]
        self.assertEqual(assignment["current_node_index"], 1)

        second = advance_approval_sequence(
            self.service,
            module="sample_issue",
            entity_id="sample-sequence-001",
            assignment=assignment,
            username="李四",
        )
        self.assertEqual(second["status"], "node_pending")
        self.assertEqual(second["remaining_usernames"], ["王五"])

        final = advance_approval_sequence(
            self.service,
            module="sample_issue",
            entity_id="sample-sequence-001",
            assignment=second["assignment"],
            username="王五",
        )
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["assignment"]["status"], "completed")

    def test_ecn_ecr_event_supports_configured_sequential_workflow(self):
        """ECR 应按管理员发布的节点顺序生成并推进具体人员待办。"""
        event = get_workflow_event_definition("ecn", "ecr_review")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event.supports_sequential)
        scheme_event = get_workflow_event_definition("ecn", "scheme_review")
        self.assertIsNotNone(scheme_event)
        assert scheme_event is not None
        self.assertTrue(scheme_event.supports_sequential)

        self.service.set_position_permissions(
            self.approver_position_id,
            [ECN_ECR_APPROVE_PERMISSION],
        )
        self.service.set_position_permissions(
            self.observer_position_id,
            [ECN_ECR_APPROVE_PERMISSION],
        )
        users = self.service.load_users()
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="ecn.ecr.sequence_test",
            module="ecn",
            event="ecr_review",
            name="ECR多节点测试流程",
            priority=5,
            condition={
                "requester_org_unit_ids": [self.org_unit_id],
                "requester_position_ids": [self.requester_position_id],
                "include_child_org_units": True,
            },
            approver={
                "nodes": [
                    {
                        "node_key": "technical_review",
                        "name": "技术审批",
                        "approval_mode": "any",
                        "required_permission_code": ECN_ECR_APPROVE_PERMISSION,
                        "approver": {
                            "strategy": "users",
                            "user_ids": [users["李四"]["user_id"]],
                        },
                    },
                    {
                        "node_key": "business_review",
                        "name": "业务审批",
                        "approval_mode": "any",
                        "required_permission_code": ECN_ECR_APPROVE_PERMISSION,
                        "approver": {
                            "strategy": "users",
                            "user_ids": [users["王五"]["user_id"]],
                        },
                    },
                ]
            },
            required_permission_code=ECN_ECR_APPROVE_PERMISSION,
            approval_mode="sequential",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        started = start_ecr_approval(
            "ecn-001",
            "张三",
            user_service=self.service,
        )
        self.assertEqual(started["status"], "matched")
        ecn_data = {
            "ecn_id": "ecn-001",
            "workflow": {"ecr_workflow_assignment": started["assignment"]},
        }
        first = finish_ecr_approval(
            ecn_data,
            "李四",
            rejected=False,
            user_service=self.service,
        )
        self.assertEqual(first["status"], "advanced")
        ecn_data["workflow"]["ecr_workflow_assignment"] = first["assignment"]
        final = finish_ecr_approval(
            ecn_data,
            "王五",
            rejected=False,
            user_service=self.service,
        )
        self.assertEqual(final["status"], "completed")

    def test_ecn_assignment_reconciliation_restores_json_snapshot(self):
        """身份库提前推进而业务快照未落盘时，应恢复快照节点并关闭后续待办。"""
        self.service.set_position_permissions(
            self.approver_position_id,
            [ECN_ECR_APPROVE_PERMISSION],
        )
        self.service.set_position_permissions(
            self.observer_position_id,
            [ECN_ECR_APPROVE_PERMISSION],
        )
        users = self.service.load_users()
        workflow_id, _version_id = self.service.save_approval_workflow_draft(
            code="ecn.ecr.reconcile_test",
            module="ecn",
            event="ecr_review",
            name="ECR待办自愈测试流程",
            priority=5,
            condition={
                "requester_org_unit_ids": [self.org_unit_id],
                "requester_position_ids": [self.requester_position_id],
                "include_child_org_units": True,
            },
            approver={
                "nodes": [
                    {
                        "node_key": "technical_review",
                        "name": "技术审批",
                        "approval_mode": "any",
                        "required_permission_code": ECN_ECR_APPROVE_PERMISSION,
                        "approver": {
                            "strategy": "users",
                            "user_ids": [users["李四"]["user_id"]],
                        },
                    },
                    {
                        "node_key": "business_review",
                        "name": "业务审批",
                        "approval_mode": "any",
                        "required_permission_code": ECN_ECR_APPROVE_PERMISSION,
                        "approver": {
                            "strategy": "users",
                            "user_ids": [users["王五"]["user_id"]],
                        },
                    },
                ]
            },
            required_permission_code=ECN_ECR_APPROVE_PERMISSION,
            approval_mode="sequential",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        started = start_ecr_approval(
            "ecn-reconcile-001",
            "张三",
            user_service=self.service,
        )
        self.assertEqual(started["status"], "matched")
        original_assignment = copy.deepcopy(started["assignment"])
        ecn_data = {
            "ecn_id": "ecn-reconcile-001",
            "workflow": {
                "current_phase": "ECR_PHASE",
                "current_state": ECNState.ECR_REVIEWING,
                "ecr_workflow_assignment": original_assignment,
            },
        }

        # 模拟身份库已经推进到下一节点，但进程在 ECN JSON 快照落盘前中断。
        advanced = finish_ecr_approval(
            ecn_data,
            "李四",
            rejected=False,
            user_service=self.service,
        )
        self.assertEqual(advanced["status"], "advanced")
        advanced_task_key = advanced["assignment"]["current_task_key"]
        self.assertEqual(
            self.service.list_pending_assignment_usernames(
                module="ecn",
                entity_id="ecn-reconcile-001",
                task_key=advanced_task_key,
            ),
            ["王五"],
        )
        self.service.replace_work_assignments(
            module="ecn",
            entity_id="ecn-orphan-001",
            task_key="ecr_review:node:1:orphan",
            assignee_usernames=["李四"],
            source_policy_code="ecn.orphan@1",
        )

        repaired = reconcile_ecn_work_assignments(
            {"ecn-reconcile-001": ecn_data},
            user_service=self.service,
        )
        self.assertEqual(repaired["status"], "repaired")
        self.assertEqual(repaired["repaired"], 2)
        self.assertEqual(repaired["orphaned"], 1)
        self.assertTrue(
            is_ecr_assigned_approver(
                ecn_data,
                "李四",
                user_service=self.service,
            )
        )
        self.assertEqual(
            self.service.list_pending_assignment_usernames(
                module="ecn",
                entity_id="ecn-reconcile-001",
                task_key=advanced_task_key,
            ),
            [],
        )
        self.assertEqual(
            self.service.list_pending_assignment_usernames(
                module="ecn",
                entity_id="ecn-orphan-001",
                task_key="ecr_review:node:1:orphan",
            ),
            [],
        )

        unchanged = reconcile_ecn_work_assignments(
            {"ecn-reconcile-001": ecn_data},
            user_service=self.service,
        )
        self.assertEqual(unchanged["status"], "unchanged")
        self.assertEqual(unchanged["repaired"], 0)
        self.assertEqual(unchanged["orphaned"], 0)

        malformed_ecn = copy.deepcopy(ecn_data)
        malformed_ecn["workflow"]["ecr_workflow_assignment"]["nodes"][0][
            "node_index"
        ] = "invalid"
        malformed_result = reconcile_ecn_work_assignments(
            {"ecn-reconcile-001": malformed_ecn},
            user_service=self.service,
        )
        self.assertTrue(malformed_result["warnings"])

    def test_published_execution_workflow_builds_concrete_task_snapshot(self):
        self.service.set_position_permissions(
            self.approver_position_id,
            [ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION],
        )
        workflow_id, _ = self.service.save_approval_workflow_draft(
            code="ecn.execution_supplier.test",
            module="ecn",
            event="execution_supplier",
            name="供应商执行测试",
            priority=10,
            condition={"requester_org_unit_ids": []},
            approver={
                "nodes": [
                    {
                        "node_key": "stage_1_purchase",
                        "name": "采购",
                        "approval_mode": "any",
                        "required_permission_code": ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
                        "stage_index": 0,
                        "responsible_key": "采购",
                        "project_scoped": False,
                        "approver": {
                            "strategy": "position",
                            "position_ids": [self.approver_position_id],
                            "org_scope": "any",
                            "org_unit_ids": [],
                        },
                    }
                ]
            },
            required_permission_code=ECN_EXECUTION_PURCHASE_CONFIRM_PERMISSION,
            approval_mode="sequential",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")
        item = {
            "item_id": "M1",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "更换",
            "traceability_levels": ["供应商"],
            "projects": ["P1"],
        }

        execution = build_ecn_execution_info_from_workflows(
            [item],
            {},
            "张三",
            user_service=self.service,
        )
        task = execution["material_confirmations"]["M1"]["traceability_tasks"]["供应商::采购"]

        self.assertEqual(execution["workflow_source"], "approval_workflows")
        self.assertEqual(task["users"], ["李四"])
        self.assertEqual(task["responsible_type"], "workflow_users")
        self.assertEqual(task["workflow_assignment"]["workflow_code"], "ecn.execution_supplier.test")

    def test_customer_execution_workflow_uses_supervisor_only_as_project_sales_fallback(self):
        self.service.set_position_permissions(
            self.requester_position_id,
            [ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION],
        )
        self.service.set_position_permissions(
            self.approver_position_id,
            [ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION],
        )
        self.service.set_position_permissions(
            self.observer_position_id,
            [ECN_EXECUTION_PMC_CONFIRM_PERMISSION],
        )
        nodes = [
            {
                "node_key": "stage_1_project_sales",
                "name": "项目销售",
                "approval_mode": "any",
                "required_permission_code": ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
                "stage_index": 0,
                "responsible_key": "项目销售",
                "project_scoped": True,
                "approver": {"strategy": "project_sales"},
            },
            {
                "node_key": "stage_1_sales_supervisor",
                "name": "销售主管",
                "approval_mode": "any",
                "required_permission_code": ECN_EXECUTION_SALES_SUPERVISOR_CONFIRM_PERMISSION,
                "stage_index": 0,
                "responsible_key": "销售主管",
                "project_scoped": True,
                "approver": {
                    "strategy": "position",
                    "position_ids": [self.approver_position_id],
                    "org_scope": "any",
                    "org_unit_ids": [],
                },
            },
            {
                "node_key": "stage_2_pmc",
                "name": "PMC",
                "approval_mode": "any",
                "required_permission_code": ECN_EXECUTION_PMC_CONFIRM_PERMISSION,
                "stage_index": 1,
                "responsible_key": "PMC",
                "project_scoped": False,
                "approver": {
                    "strategy": "position",
                    "position_ids": [self.observer_position_id],
                    "org_scope": "any",
                    "org_unit_ids": [],
                },
            },
        ]
        workflow_id, _ = self.service.save_approval_workflow_draft(
            code="ecn.execution_customer.test",
            module="ecn",
            event="execution_customer_transit",
            name="客户在途执行测试",
            priority=10,
            condition={"requester_org_unit_ids": []},
            approver={"nodes": nodes},
            required_permission_code=ECN_EXECUTION_MATERIAL_CONFIRM_PERMISSION,
            approval_mode="sequential",
            actor_username="admin",
        )
        self.service.publish_approval_workflow(workflow_id, actor_username="admin")
        item = {
            "item_id": "M1",
            "scheme_category": ECN_SCHEME_GROUP_MATERIAL,
            "change_type": "更换",
            "traceability_levels": ["客户/在途"],
            "projects": ["P1"],
        }

        execution = build_ecn_execution_info_from_workflows(
            [item],
            {"P1": "张三"},
            "张三",
            user_service=self.service,
        )
        tasks = execution["material_confirmations"]["M1"]["traceability_tasks"]

        self.assertEqual(tasks["客户/在途::项目销售::P1"]["users"], ["张三"])
        self.assertNotIn("客户/在途::销售主管::P1", tasks)
        self.assertEqual(tasks["客户/在途::PMC"]["users"], ["王五"])
        self.assertEqual(tasks["客户/在途::项目销售::P1"]["stage_index"], 0)
        self.assertEqual(tasks["客户/在途::PMC"]["stage_index"], 1)

        missing_sales_execution = build_ecn_execution_info_from_workflows(
            [item],
            {},
            "张三",
            user_service=self.service,
        )
        missing_sales_tasks = missing_sales_execution["material_confirmations"]["M1"]["traceability_tasks"]
        self.assertEqual(missing_sales_tasks["客户/在途::项目销售::P1"]["users"], [])
        self.assertNotIn("客户/在途::销售主管::P1", missing_sales_tasks)

    def test_workflow_editor_nodes_support_old_single_and_new_sequence_versions(self):
        """管理界面应把旧单节点与新串行版本统一转换成可编辑节点。"""
        single_nodes = get_approval_workflow_editor_nodes(
            {
                "approval_mode": "all",
                "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                "approver": {
                    "strategy": "position",
                    "position_ids": [self.approver_position_id],
                },
            }
        )
        self.assertEqual(single_nodes[0]["node_key"], "approval")
        self.assertEqual(single_nodes[0]["approval_mode"], "all")

        source_version = {
            "approval_mode": "sequential",
            "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            "approver": {
                "nodes": [
                    {
                        "node_key": "technical_review",
                        "name": "技术审核",
                        "approval_mode": "any",
                        "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                        "approver": {"strategy": "users", "user_ids": ["user-1"]},
                    },
                    {
                        "node_key": "joint_review",
                        "name": "联合会签",
                        "approval_mode": "all",
                        "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                        "approver": {"strategy": "permission"},
                    },
                ]
            },
        }
        sequence_nodes = get_approval_workflow_editor_nodes(source_version)
        self.assertEqual([node["node_key"] for node in sequence_nodes], ["technical_review", "joint_review"])
        sequence_nodes[0]["name"] = "界面临时修改"
        self.assertEqual(source_version["approver"]["nodes"][0]["name"], "技术审核")

    def test_sequential_workflow_rejects_duplicate_node_codes(self):
        """节点编码用于稳定待办键，同一版本内不允许重复。"""
        node = {
            "node_key": "review",
            "name": "审批",
            "approval_mode": "any",
            "required_permission_code": SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
            "approver": {
                "strategy": "position",
                "position_ids": [self.approver_position_id],
                "org_scope": "any",
                "org_unit_ids": [],
            },
        }
        with self.assertRaisesRegex(ValueError, "审批节点编码重复"):
            self.service.save_approval_workflow_draft(
                code="sample_issue.close.invalid_sequence",
                module="sample_issue",
                event="close_request",
                name="重复节点测试",
                priority=10,
                condition={},
                approver={"nodes": [node, copy.deepcopy(node)]},
                required_permission_code=SAMPLE_ISSUE_CLOSE_APPROVE_PERMISSION,
                approval_mode="sequential",
                actor_username="admin",
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
