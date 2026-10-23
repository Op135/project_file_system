import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.overview_add_approval import (
    OverviewAddSaveResult,
    _execute_approved_add,
    approve_overview_add_request,
    normalize_overview_add_impacts,
    reject_overview_add_request,
    resubmit_overview_add_request,
    requires_overview_add_approval,
    save_or_submit_overview_addition,
    validate_overview_add_approval_config,
)


class OverviewAddApprovalConfigTests(unittest.TestCase):
    def test_related_impacts_are_limited_to_configured_labels(self):
        config = {"impact_list": ["a", "b", "a"]}

        self.assertEqual(
            normalize_overview_add_impacts(config, "selected", ["b", "outside"]),
            ("selected", ["b"]),
        )
        self.assertEqual(
            normalize_overview_add_impacts(config, "all", ["outside"]),
            ("all", ["a", "b"]),
        )
        self.assertEqual(normalize_overview_add_impacts(config, "selected", []), ("none", []))

    def test_missing_flag_defaults_to_no_approval(self):
        self.assertFalse(requires_overview_add_approval({"label": "ordinary"}))
        self.assertTrue(
            requires_overview_add_approval(
                {"label": "critical", "add_approval_required": True}
            )
        )

    def test_checked_in_overview_config_has_valid_boolean_flags(self):
        config_path = Path(__file__).resolve().parents[1] / "overview_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        validate_overview_add_approval_config(config)

    def test_non_boolean_flag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "必须是布尔值"):
            validate_overview_add_approval_config(
                {
                    "光学": {
                        "关键项": [
                            {
                                "label": "critical",
                                "add_approval_required": "true",
                            }
                        ]
                    }
                }
            )


class OverviewAddApprovalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_item_is_saved_immediately(self):
        set_item = AsyncMock(return_value=True)
        with patch("src.overview_add_approval.overview_storage.set_deep_item", new=set_item):
            result = await save_or_submit_overview_addition(
                project="P1",
                label="ordinary",
                chip={"id": "chip-1", "content": "普通概述"},
                config={"label": "ordinary"},
                requester_username="申请人",
                requester_role="研发",
            )

        self.assertEqual(result, OverviewAddSaveResult("saved", "内容已添加"))
        set_item.assert_awaited_once()

    async def test_critical_item_only_creates_pending_request(self):
        assignment = {
            "current_task_key": "overview_add_review:node:1:review",
            "status": "pending",
        }
        fake_service = SimpleNamespace(supersede_pending_work_assignments=Mock())
        insert_request = AsyncMock(return_value=True)
        set_item = AsyncMock(return_value=True)
        with (
            patch(
                "src.overview_add_approval.create_approval_sequence_assignments",
                return_value={"status": "matched", "assignment": assignment},
            ),
            patch("src.overview_add_approval._insert_request", new=insert_request),
            patch("src.overview_add_approval.overview_storage.set_deep_item", new=set_item),
        ):
            result = await save_or_submit_overview_addition(
                project="P1",
                label="critical",
                chip={"id": "chip-2", "content": "关键概述"},
                config={"label": "critical", "add_approval_required": True},
                requester_username="申请人",
                requester_role="研发",
                user_service=fake_service,
            )

        self.assertTrue(result.pending)
        insert_request.assert_awaited_once()
        set_item.assert_not_awaited()

    async def test_final_approval_executes_candidate_once(self):
        request = {
            "id": "request-1",
            "status": "pending",
            "workflow_assignment": {
                "current_task_key": "overview_add_review:node:1:review",
                "current_node_index": 0,
                "status": "pending",
            },
            "review_log": [],
        }
        fake_service = SimpleNamespace()
        execute_add = AsyncMock(return_value=(True, "审批通过，概述已生效"))
        update_request = AsyncMock(return_value=True)
        with (
            patch("src.overview_add_approval.db_storage.get_deep_item", return_value=request),
            patch("src.overview_add_approval.can_review_overview_add_request", return_value=True),
            patch(
                "src.overview_add_approval._advance_add_review",
                new=AsyncMock(return_value=(request, "completed")),
            ),
            patch("src.overview_add_approval._execute_approved_add", new=execute_add),
            patch("src.overview_add_approval._update_request", new=update_request),
        ):
            result = await approve_overview_add_request(
                "request-1",
                "审批人",
                user_service=fake_service,
            )

        self.assertEqual(result["status"], "approved")
        execute_add.assert_awaited_once_with(request)

    async def test_rejected_request_can_be_modified_and_resubmitted(self):
        request = {
            "id": "request-2",
            "project": "P1",
            "label": "critical",
            "submitter": "申请人",
            "submitter_role": "研发",
            "status": "rejected",
            "review_log": [{"action": "reject", "user": "审核人"}],
            "payload": {
                "chip": {
                    "id": "chip-2",
                    "type": "test",
                    "content": "旧内容",
                    "timestamp": {"2026-10-15 10:00:00": {"reason": "旧原因"}},
                },
                "config": {"label": "critical", "add_approval_required": True},
                "staged_file_path": "",
            },
        }
        assignment = {"current_task_key": "overview_add_review:node:1:review", "status": "pending"}
        update_request = AsyncMock(return_value=True)
        with (
            patch("src.overview_add_approval.db_storage.get_deep_item", return_value=request),
            patch("src.overview_add_approval.can_edit_overview_item", return_value=True),
            patch(
                "src.overview_add_approval.create_approval_sequence_assignments",
                return_value={"status": "matched", "assignment": assignment},
            ),
            patch("src.overview_add_approval._update_request", new=update_request),
        ):
            result = await resubmit_overview_add_request(
                "request-2",
                "申请人",
                chip={
                    "id": "chip-new",
                    "type": "test",
                    "content": "修改后的内容",
                    "test_select_data": {"state_select": "常温"},
                    "timestamp": {"2026-10-15 11:00:00": {"reason": "修改后的原因"}},
                },
                config={
                    "label": "critical",
                    "add_approval_required": True,
                    "impact_list": ["related-a", "related-b"],
                },
                impact_mode="selected",
                related_labels=["related-b", "outside"],
                user_service=SimpleNamespace(),
            )

        self.assertEqual(result.status, "pending")
        await_call = update_request.await_args
        assert await_call is not None
        changes = await_call.args[1]
        self.assertEqual(changes["status"], "pending")
        self.assertEqual(changes["payload"]["chip"]["content"], "修改后的内容")
        self.assertEqual(changes["payload"]["chip"]["test_select_data"], {"state_select": "常温"})
        self.assertEqual(changes["payload"]["chip"]["id"], "chip-2")
        self.assertEqual(changes["payload"]["impact_mode"], "selected")
        self.assertEqual(changes["payload"]["related_labels"], ["related-b"])
        self.assertEqual(changes["review_log"][-1]["action"], "resubmit")

    async def test_rejection_keeps_staged_file_for_possible_resubmission(self):
        request = {
            "id": "request-file",
            "status": "pending",
            "workflow_assignment": {"current_task_key": "overview_add_review:node:1:review"},
            "payload": {"staged_file_path": "pending-file"},
        }
        fake_service = SimpleNamespace(supersede_pending_work_assignments=Mock())
        update_request = AsyncMock(return_value=True)
        cleanup = Mock()
        with (
            patch("src.overview_add_approval._advance_add_review", new=AsyncMock(return_value=(request, "rejected"))),
            patch("src.overview_add_approval.db_storage.get_deep_item", return_value=request),
            patch("src.overview_add_approval.can_review_overview_add_request", return_value=True),
            patch("src.overview_add_approval._update_request", new=update_request),
            patch("src.overview_add_approval._cleanup_staged_file", new=cleanup),
        ):
            result = await reject_overview_add_request(
                "request-file",
                "审核人",
                "请修改",
                user_service=fake_service,
            )

        self.assertEqual(result["status"], "rejected")
        cleanup.assert_not_called()

    async def test_approved_add_applies_stored_related_impacts(self):
        config = {
            "label": "critical",
            "role": "结构",
            "add_approval_required": True,
            "impact_list": ["related"],
        }
        request = {
            "project": "P1",
            "label": "critical",
            "submitter": "申请人",
            "submitter_role": "研发",
            "payload": {
                "chip": {"id": "chip-3", "content": "关键概述"},
                "config": config,
                "impact_mode": "selected",
                "related_labels": ["related"],
            },
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(
                general={
                    "over_config_data_flat": {
                        "critical": config,
                        "related": {"role": "结构"},
                    },
                    "project_summary": {"P1": {"state": "研发"}},
                    "project_req_max_ver": {"P1": "1.0"},
                    "overview_role": {},
                }
            )
        )
        insert_chip = AsyncMock(return_value=(True, "已添加"))
        apply_impacts = AsyncMock(return_value={"related"})
        with (
            patch("src.overview_add_approval.app", fake_app),
            patch("src.overview_add_approval.can_edit_overview_item", return_value=True),
            patch("src.overview_batch_operations.insert_overview_chip", new=insert_chip),
            patch("src.overview_batch_operations.apply_related_overview_impacts", new=apply_impacts),
            patch("src.components.OverviewVersionManager.bump"),
            patch("src.utils.refresh_overview_pending_labels") as refresh_pending,
            patch("src.utils.overview_role_update"),
        ):
            effective, message = await _execute_approved_add(request)

        self.assertTrue(effective)
        self.assertIn("关联概述项", message)
        apply_impacts.assert_awaited_once()
        impact_call = apply_impacts.await_args
        assert impact_call is not None
        self.assertEqual(impact_call.kwargs["related_labels"], ["related"])
        refresh_pending.assert_called_once_with("P1", {"related"})


if __name__ == "__main__":
    unittest.main()
