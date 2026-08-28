import copy
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from nicegui import app

from src import db_storage
from src.pages import design_knowledge
from src.permission_catalog import (
    DESIGN_KNOWLEDGE_CREATE_PERMISSION,
    DESIGN_KNOWLEDGE_EDIT_PERMISSION,
    DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
    DESIGN_KNOWLEDGE_VIEW_PERMISSION,
)
from src.user_service import UserService


class DesignKnowledgeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        org_unit_id = self.service.save_org_unit(code="org.design.flow", name="设计流程部")
        requester_position_id = self.service.save_position(
            code="position.design.requester",
            name="知识录入岗位",
            org_unit_ids=[org_unit_id],
        )
        approver_position_id = self.service.save_position(
            code="position.design.approver",
            name="知识审批岗位",
            org_unit_ids=[org_unit_id],
        )
        observer_position_id = self.service.save_position(
            code="position.design.observer",
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
            position_id=observer_position_id,
        )
        self.service.set_position_permissions(
            requester_position_id,
            [
                DESIGN_KNOWLEDGE_VIEW_PERMISSION,
                DESIGN_KNOWLEDGE_CREATE_PERMISSION,
                DESIGN_KNOWLEDGE_EDIT_PERMISSION,
            ],
        )
        reviewer_permissions = [
            DESIGN_KNOWLEDGE_VIEW_PERMISSION,
            DESIGN_KNOWLEDGE_REVIEW_PERMISSION,
            DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION,
        ]
        self.service.set_position_permissions(approver_position_id, reviewer_permissions)
        self.service.set_position_permissions(observer_position_id, reviewer_permissions)
        for event, permission_code in [
            (design_knowledge.DESIGN_KNOWLEDGE_REVIEW_EVENT, DESIGN_KNOWLEDGE_REVIEW_PERMISSION),
            (design_knowledge.DESIGN_TAG_REVIEW_EVENT, DESIGN_KNOWLEDGE_TAG_REVIEW_PERMISSION),
        ]:
            workflow_id, _version_id = self.service.save_approval_workflow_draft(
                code=f"design_knowledge.{event}.test",
                module=design_knowledge.DESIGN_KNOWLEDGE_MODULE,
                event=event,
                name=f"设计知识 {event} 测试流程",
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
                required_permission_code=permission_code,
                approval_mode="any",
                actor_username="admin",
            )
            self.service.publish_approval_workflow(workflow_id, actor_username="admin")

        self.original_user_service = getattr(app.state, "user_service", None)
        app.state.user_service = self.service
        self.storage = {}
        self.original_get_item = design_knowledge.db_storage.get_item
        self.original_set_item = design_knowledge.db_storage.set_item
        self.original_atomic_update = design_knowledge.db_storage.atomic_deep_update

        def fake_get_item(key, default=None):
            return copy.deepcopy(self.storage.get(key, default))

        async def fake_set_item(key, value):
            self.storage[key] = copy.deepcopy(value)
            return True

        async def fake_atomic_update(path, update_function):
            key = path[0]
            current = copy.deepcopy(self.storage.get(key, {}))
            updated = update_function(current)
            if updated is not db_storage.ATOMIC_NO_UPDATE:
                self.storage[key] = copy.deepcopy(updated)
            return True

        design_knowledge.db_storage.get_item = fake_get_item
        design_knowledge.db_storage.set_item = fake_set_item
        design_knowledge.db_storage.atomic_deep_update = fake_atomic_update

    async def asyncTearDown(self):
        app.state.user_service = self.original_user_service
        design_knowledge.db_storage.get_item = self.original_get_item
        design_knowledge.db_storage.set_item = self.original_set_item
        design_knowledge.db_storage.atomic_deep_update = self.original_atomic_update
        self.temp_dir.cleanup()

    async def test_knowledge_and_tag_reviews_use_exact_assignments(self):
        record = design_knowledge.get_design_knowledge_template()
        record.update(
            {
                "title": "审批流程测试知识",
                "summary": "验证具体审批人待办",
                "tags": [design_knowledge.get_domain_tags(record["domain"])[0]],
                "status": design_knowledge.RECORD_STATUS_REVIEW,
            }
        )
        success, _message, saved_record = await design_knowledge.save_knowledge_record(
            record,
            "张三",
            "研发硬件",
        )
        self.assertTrue(success)
        self.assertIsNotNone(saved_record)
        assert saved_record is not None
        self.assertEqual(
            saved_record["workflow_assignment"]["assignee_usernames"],
            ["李四"],
        )
        self.assertTrue(
            design_knowledge.can_review_submission(saved_record, "李四", "电子主管")
        )
        self.assertFalse(
            design_knowledge.can_review_submission(saved_record, "王五", "质量主管")
        )

        tag_success, _tag_message = await design_knowledge.submit_tag_request(
            record["domain"],
            "流程测试标签",
            "验证标签审批",
            "张三",
            "研发硬件",
        )
        self.assertTrue(tag_success)
        tag_request = next(iter(self.storage[design_knowledge.DESIGN_TAG_REQUESTS_KEY].values()))
        self.assertTrue(
            design_knowledge.can_review_submission(
                tag_request,
                "李四",
                "电子主管",
                submission_type="tag",
            )
        )
        self.assertFalse(
            design_knowledge.can_review_submission(
                tag_request,
                "王五",
                "质量主管",
                submission_type="tag",
            )
        )
        self.assertEqual(
            design_knowledge.get_design_knowledge_dashboard_pending_count(
                self.storage[design_knowledge.DESIGN_KNOWLEDGE_DATA_KEY],
                "李四",
                "电子主管",
                self.storage[design_knowledge.DESIGN_TAG_REQUESTS_KEY],
            ),
            2,
        )

        denied, _message = await design_knowledge.update_tag_request_status(
            tag_request["request_id"],
            "已通过",
            "王五",
            "质量主管",
        )
        self.assertFalse(denied)
        approved, _message = await design_knowledge.update_tag_request_status(
            tag_request["request_id"],
            "已通过",
            "李四",
            "电子主管",
        )
        self.assertTrue(approved)
        self.assertIn("流程测试标签", design_knowledge.get_domain_tags(record["domain"]))


if __name__ == "__main__":
    unittest.main()
