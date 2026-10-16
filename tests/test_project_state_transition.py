import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.permission_catalog import (
    PROJECT_STATUS_TRIAL_TO_MASS_NOTIFY_PERMISSION,
    PROJECT_STATUS_TRIAL_TO_MASS_PERMISSION,
)
from src import project_state_transition as transition


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


if __name__ == "__main__":
    unittest.main()
