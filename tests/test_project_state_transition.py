import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.permission_catalog import (
    PROJECT_STATUS_TRANSFER_TO_TRIAL_PERMISSION,
    PROJECT_STATUS_TRIAL_TO_MASS_NOTIFY_PERMISSION,
    PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION,
    PROJECT_TRIAL_PROGRESS_VIEW_PERMISSION,
)
from src import project_state_transition as transition
from src import overview_trial_progress as progress
from tests.test_db_storage_entities import load_isolated_db_storage


class _PermissionService:
    def __init__(self, permission_codes: set[str]):
        self.permission_codes = permission_codes

    def has_permission(self, username: str, permission_code: str, **_kwargs) -> bool:
        return username == "tester" and permission_code in self.permission_codes


class ProjectStateTransitionTests(unittest.IsolatedAsyncioTestCase):
    def test_state_path_rejects_cross_stage_and_keeps_legal_forward_steps(self):
        self.assertTrue(transition.is_valid_project_state_path("研发", "转产"))
        self.assertTrue(transition.is_valid_project_state_path("转产", "试产"))
        self.assertTrue(transition.is_valid_project_state_path("试产", "量产"))
        self.assertFalse(transition.is_valid_project_state_path("研发", "试产"))
        self.assertFalse(transition.is_valid_project_state_path("转产", "量产"))
        self.assertFalse(transition.is_valid_project_state_path("量产", "研发"))

    async def test_special_permission_executes_transition_and_notifies_subscribers(self):
        service = _PermissionService({PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION})
        storage = SimpleNamespace(
            general={
                "project_summary": {
                    "P1": {"state": "试产", "model_notes": "old", "project": "P1系列"}
                },
                "overview_charge_pending": {},
            }
        )
        fake_app = SimpleNamespace(storage=storage, state=SimpleNamespace(user_service=service))

        with tempfile.TemporaryDirectory() as temp_value:
            summary_path = Path(temp_value) / "project_summary.json"
            summary_path.write_text(
                json.dumps({"P1": {"state": "试产", "model_notes": "old"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            resolve_recipients = AsyncMock(return_value="wecom-user")
            send_message = AsyncMock(return_value=(True, "ok"))
            with (
                patch.object(transition, "app", fake_app),
                patch.object(transition, "PROJECT_SUMMARY_PATH", summary_path),
                patch.object(
                    transition,
                    "resolve_permission_wecom_recipients",
                    resolve_recipients,
                ),
                patch.object(transition, "send_wecom_text_message", send_message),
            ):
                result = await transition.transition_project_state(
                    "P1",
                    "量产",
                    actor="tester",
                    user_service=service,
                    project_updates={"model_notes": "new", "state": "错误值"},
                )

            self.assertTrue(result.ok)
            self.assertTrue(result.changed)
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["P1"]["state"], "量产")
            self.assertEqual(saved["P1"]["model_notes"], "new")
            self.assertEqual(storage.general["project_summary"]["P1"]["state"], "量产")
            self.assertEqual(storage.general["project_summary"]["P1"]["project"], "P1系列")
            resolve_recipients.assert_awaited_once_with(
                PROJECT_STATUS_TRIAL_TO_MASS_NOTIFY_PERMISSION,
                fallback_touser="",
            )
            send_message.assert_awaited_once()

    async def test_special_permission_cannot_execute_another_transition(self):
        service = _PermissionService({PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION})
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(
                general={
                    "project_summary": {"P1": {"state": "转产"}},
                    "overview_charge_pending": {},
                }
            ),
            state=SimpleNamespace(user_service=service),
        )
        with patch.object(transition, "app", fake_app):
            result = await transition.transition_project_state(
                "P1",
                "试产",
                actor="tester",
                user_service=service,
            )

        self.assertFalse(result.ok)
        self.assertIn("权限", result.message)

    async def test_detail_update_preserves_latest_project_state(self):
        storage = SimpleNamespace(
            general={
                "project_summary": {
                    "P1": {"state": "试产", "model_notes": "old", "project": "P1系列"}
                }
            }
        )
        fake_app = SimpleNamespace(storage=storage)
        with tempfile.TemporaryDirectory() as temp_value:
            summary_path = Path(temp_value) / "project_summary.json"
            summary_path.write_text(
                json.dumps({"P1": {"state": "量产", "model_notes": "old"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                patch.object(transition, "app", fake_app),
                patch.object(transition, "PROJECT_SUMMARY_PATH", summary_path),
            ):
                ok, _message = await transition.update_project_details(
                    "P1",
                    {"state": "试产", "model_notes": "new"},
                )

            self.assertTrue(ok)
            saved = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["P1"]["state"], "量产")
            self.assertEqual(saved["P1"]["model_notes"], "new")
            self.assertEqual(storage.general["project_summary"]["P1"]["state"], "量产")
            self.assertEqual(storage.general["project_summary"]["P1"]["project"], "P1系列")

    async def test_successful_trial_transition_clears_importance(self):
        service = _PermissionService({PROJECT_STATUS_TRANSFER_TO_TRIAL_PERMISSION})
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(general={
                "project_summary": {"P1": {"state": "转产"}},
                "overview_charge_pending": {},
            }),
            state=SimpleNamespace(user_service=service),
        )
        with tempfile.TemporaryDirectory() as temp_value:
            summary_path = Path(temp_value) / "project_summary.json"
            summary_path.write_text(json.dumps({"P1": {"state": "转产"}}), encoding="utf-8")
            clear_importance = AsyncMock(return_value=True)
            with (
                patch.object(transition, "app", fake_app),
                patch.object(progress, "app", fake_app),
                patch.object(transition, "PROJECT_SUMMARY_PATH", summary_path),
                patch.object(transition, "clear_project_importance", clear_importance),
                patch.object(transition, "_send_transition_notification", AsyncMock(return_value="")),
            ):
                result = await transition.transition_project_state(
                    "P1", "试产", actor="tester", user_service=service,
                )
            self.assertTrue(result.ok)
            self.assertEqual(fake_app.storage.general["project_summary"]["P1"]["state"], "试产")
            clear_importance.assert_awaited_once_with("P1")


class TrialImportanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_connections_preserve_other_projects_and_prune_completed_transition(self):
        service = _PermissionService({PROJECT_TRIAL_PROGRESS_VIEW_PERMISSION})
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(general={
                "project_summary": {"P1": {"state": "转产"}, "P2": {"state": "转产"}},
                "project_req_max_ver": {"P1": "1", "P2": "1"},
            }),
            state=SimpleNamespace(user_service=service),
        )
        with tempfile.TemporaryDirectory() as temp_value:
            db_path = Path(temp_value) / "importance.db"
            left = load_isolated_db_storage("test_importance_left", db_path)
            right = load_isolated_db_storage("test_importance_right", db_path)
            await left.init_db()
            await right.init_db()
            try:
                with patch.object(progress, "app", fake_app), patch.object(progress, "db_storage", left):
                    self.assertTrue(await progress.set_project_importance("P1", 3, actor="tester"))
                with patch.object(progress, "app", fake_app), patch.object(progress, "db_storage", right):
                    self.assertTrue(await progress.set_project_importance("P2", 5, actor="tester"))
                with patch.object(progress, "app", fake_app), patch.object(progress, "db_storage", left):
                    self.assertEqual(await progress.get_latest_importance(), {"P1": 3, "P2": 5})
                    fake_app.storage.general["project_summary"]["P1"]["state"] = "试产"
                    self.assertTrue(await progress.prune_inactive_importance(
                        fake_app.storage.general["project_summary"]
                    ))
                    self.assertEqual(await progress.get_latest_importance(), {"P2": 5})
                    self.assertTrue(await progress.prune_inactive_importance({}))
                    self.assertEqual(await progress.get_latest_importance(), {"P2": 5})
                    self.assertFalse(await progress.set_project_importance("P1", 4, actor="tester"))
                    self.assertTrue(await progress.clear_project_importance("P2"))
                    self.assertEqual(await progress.get_latest_importance(), {})
            finally:
                await left.close_db()
                await right.close_db()


class TrialProgressTests(unittest.TestCase):
    def test_only_required_overviews_block_trial_transition(self):
        storage = SimpleNamespace(general={
            "overview_charge_pending": {
                "张三": {"P1": {"required": "缺必填", "specifications": "有待定", "optional": "有待定"}}
            },
            "overview_trial_exempt_labels": ["required", "specifications"],
            "over_config_data_flat": {
                "required": {"nature": "必填"},
                "specifications": {"nature": "必填"},
                "optional": {"nature": "需填"},
            },
        })
        fake_app = SimpleNamespace(storage=storage)
        with patch.object(transition, "app", fake_app):
            self.assertEqual(transition.collect_trial_transition_blockers("P1"), {"张三": ("required", "specifications")})

    def test_progress_counts_only_required_and_skipped_roles_and_shows_unassigned(self):
        rows = progress.build_trial_progress(
            {"P1": {"state": "转产"}, "P2": {"state": "试产"}},
            {"P1": "1", "P2": "1"},
            {"P1": {
                "研发": {"latest_user": "负责人：张三"},
                "工艺": {"latest_user": ""},
                "采购": {"latest_user": "——"},
            }},
            {
                "研发": {"常规": [
                    {"label": "a", "title": "A", "nature": "必填"},
                    {"label": "specifications", "title": "规格", "nature": "必填"},
                    {"label": "optional", "title": "可选", "nature": "需填"},
                ]},
                "工艺": {"常规": [{"label": "b", "title": "B", "nature": "必填"}]},
                "采购": {"常规": [{"label": "c", "title": "C", "nature": "必填"}]},
            },
            {"张三": {"P1": {"specifications": "有待定", "optional": "有待定"}},
             "待定负责人": {"P1": {"b": "缺必填"}}},
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].completed, rows[0].total, rows[0].percentage), (1, 3, 33))
        self.assertEqual(rows[0].roles[0].missing, (("规格", "有待定"),))
        self.assertEqual(rows[0].roles[1].assignee, "待定")
        self.assertEqual(rows[0].roles[1].missing, (("B", "缺必填"),))
        self.assertTrue(rows[0].roles[2].skipped)

    def test_progress_never_rounds_incomplete_project_to_full(self):
        self.assertEqual(progress.ProjectProgress("P1", 199, 200, ()).percentage, 99)


if __name__ == "__main__":
    unittest.main()
