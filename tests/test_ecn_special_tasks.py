"""移交与确认的独立数据库回归，微信发送全部模拟。"""

import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.ecn_access import is_ecn_pending_for_user
from src.ecn_management_config import (
    ECNState,
    ECN_EXECUTION_STAGE_ASSISTANT,
    ECN_EXECUTION_STAGE_MATERIAL,
    is_ecn_assistant_execution_ready,
    load_ecn_config,
)
from src.modules.ecn import notifications
from src.modules.ecn.special_tasks import update_special_task, finish_if_complete
from src.modules.ecn.list_view import build_ecn_management_grid_row, get_ecn_management_grid_columns
from tests.test_error_management_concurrency import load_isolated_db_storage
from tests.test_ecn_notifications import Users


class SpecialUsers(Users):
    def get_user(self, username):
        return self.load_users().get(username, {})


class SpecialTasksTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ecn.db"
        self.storage = load_isolated_db_storage("special_db", self.path)
        await self.storage.init_db()
        self.service = SpecialUsers()
        self.record = {
            "ecn_id": "ECN-test",
            "basic_info": {"title": "测试"},
            "workflow": {"current_state": ECNState.ECN_EXECUTING},
            "execution_info": {
                "stage": ECN_EXECUTION_STAGE_ASSISTANT,
                "ordinary_confirmations": {"A": {"confirmed": False, "history": []}},
                "erp_confirmation": {"confirmed": True},
                "material_confirmations": {},
            },
        }
        await self.storage.set_item("ecn_management_data", {"ECN-test": self.record})

    async def asyncTearDown(self):
        await self.storage.close_db()
        self.temp.cleanup()

    def item(self):
        return copy.deepcopy(self.record["execution_info"]["ordinary_confirmations"]["A"])

    async def update(self, baseline=None, **kwargs):
        result = await update_special_task(
            "ECN-test",
            "A",
            baseline if baseline is not None else self.item(),
            username=kwargs.pop("username", "manager"),
            role="研发经理",
            service=self.service,
            storage=self.storage,
            **kwargs,
        )
        if result.ok and result.record is not None:
            self.record = result.record
        return result

    async def material_stage(self):
        self.record["execution_info"]["stage"] = ECN_EXECUTION_STAGE_MATERIAL
        await self.storage.set_item("ecn_management_data", {"ECN-test": self.record})

    async def test_assigned_item_unblocks_overview_but_does_not_close_ecn(self):
        self.assertFalse(is_ecn_assistant_execution_ready(self.record["execution_info"]))
        self.assertTrue((await self.update(assignee="writer")).ok)
        self.assertTrue(is_ecn_assistant_execution_ready(self.record["execution_info"]))
        await self.material_stage()
        self.assertFalse(finish_if_complete(self.record, "manager", "研发经理", "now"))
        self.assertTrue(is_ecn_pending_for_user(self.record, "writer", "研发硬件", user_service=self.service))
        self.assertTrue((await self.update(username="writer", confirmed=True)).ok)
        self.assertEqual(self.record["workflow"]["current_state"], ECNState.CLOSED)

    async def test_old_person_and_stale_window_cannot_confirm(self):
        await self.update(assignee="writer")
        old = self.item()
        await self.update(assignee="manager")
        self.assertFalse((await self.update(baseline=old, username="writer", confirmed=True)).ok)
        self.assertFalse((await self.update(username="writer", confirmed=True)).ok)
        self.assertEqual(len(self.record["execution_info"]["transfer_notices"]), 1)

    async def test_invalid_recipient_and_late_recall_are_rejected(self):
        self.assertFalse((await self.update(assignee="inactive")).ok)
        self.assertFalse((await self.update(assignee="missing")).ok)
        await self.update(assignee="writer")
        await self.material_stage()
        self.assertFalse((await self.update(assignee="")).ok)
        self.assertTrue((await self.update(assignee="manager")).ok)

    async def test_unassigned_and_confirmed_item_guards(self):
        await self.material_stage()
        self.assertFalse((await self.update(confirmed=True)).ok)
        self.record["execution_info"]["stage"] = ECN_EXECUTION_STAGE_ASSISTANT
        await self.storage.set_item("ecn_management_data", {"ECN-test": self.record})
        self.assertTrue((await self.update(confirmed=True)).ok)
        self.assertFalse((await self.update(assignee="writer")).ok)

    async def test_concurrent_reassignments_use_independent_connections(self):
        other = load_isolated_db_storage("special_db_other", self.path)
        await other.init_db()
        try:
            baseline = self.item()
            results = await asyncio.gather(
                *[
                    update_special_task(
                        "ECN-test",
                        "A",
                        baseline,
                        username="manager",
                        role="研发经理",
                        service=self.service,
                        storage=storage,
                        assignee=name,
                    )
                    for storage, name in ((self.storage, "writer"), (other, "manager"))
                ]
            )
            self.assertEqual(sum(result.ok for result in results), 1)
        finally:
            await other.close_db()

    async def test_grid_column_and_progress(self):
        await self.update(assignee="writer")
        row = build_ecn_management_grid_row(
            self.record,
            "writer",
            "研发硬件",
            user_service=self.service,
        )
        self.assertIs(row["is_my_pending"], True)
        unrelated_row = build_ecn_management_grid_row(
            self.record,
            "active",
            "品质QE",
            user_service=self.service,
        )
        self.assertIs(unrelated_row["is_my_pending"], False)
        self.assertEqual(row["special_tasks"], "待确认 0/1")
        columns = get_ecn_management_grid_columns()
        fields = [column["field"] for column in columns]
        self.assertLess(fields.index("special_tasks"), fields.index("traceability_0"))

    async def test_cancel_and_new_owner_notifications_and_retry_dedup(self):
        await self.update(assignee="writer")
        await self.update(username="writer", assignee="manager")
        await self.material_stage()
        settings = load_ecn_config()["wecom"]
        settings.update(test_mode=False, cc_manager_enabled=False, public_base_url="https://example.test")
        sender = AsyncMock(return_value=(True, "ok"))
        with patch.object(notifications, "send_wecom_textcard_message", sender):
            count = await notifications.check_and_send_ecn_reminders(
                config=settings, user_service=self.service, storage=self.storage
            )
            self.assertEqual(count, (2, 0))
            calls = {call.args[1]: call for call in sender.await_args_list}
            self.assertIn("取消", calls["writer_wx"].kwargs["title"])
            self.assertIn("特定事项", calls["manager_wx"].kwargs["title"])
            self.assertIn("应执行内容：", calls["manager_wx"].args[0])
            self.assertEqual(
                await notifications.check_and_send_ecn_reminders(
                    config=settings, user_service=self.service, storage=self.storage
                ),
                (0, 0),
            )

    async def test_pending_material_prevents_closing_on_special_completion(self):
        await self.update(assignee="writer")
        self.record["execution_info"]["material_confirmations"] = {"M": {"status": "open"}}
        await self.material_stage()
        await self.update(username="writer", confirmed=True)
        self.assertEqual(self.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)

    async def test_permission_revocation_rejects_assignment_and_keeps_record(self):
        with patch.object(self.service, "has_permission", return_value=False):
            self.assertFalse((await self.update(assignee="writer")).ok)
        self.assertNotIn("assignee", self.item())

    async def test_debug_cancellation_and_assignment_never_reach_actual_people(self):
        await self.update(assignee="writer")
        await self.update(username="writer", assignee="manager")
        await self.material_stage()
        settings = load_ecn_config()["wecom"]
        settings.update(test_mode=True, cc_manager_enabled=True, public_base_url="https://example.test")
        sender = AsyncMock(return_value=(True, "ok"))
        with (
            patch.object(notifications, "send_wecom_textcard_message", sender),
            patch.object(notifications, "resolve_wecom_recipients", AsyncMock(return_value="debug_wx")),
        ):
            self.assertEqual(
                await notifications.check_and_send_ecn_reminders(
                    config=settings, user_service=self.service, storage=self.storage
                ),
                (2, 0),
            )
            self.assertEqual({call.args[1] for call in sender.await_args_list}, {"debug_wx"})

    async def test_failed_cancellation_retries_without_resending_new_owner(self):
        from src.modules.ecn.transfer_notifications import STATE_KEY

        await self.update(assignee="writer")
        await self.update(username="writer", assignee="manager")
        await self.material_stage()
        settings = load_ecn_config()["wecom"]
        settings.update(test_mode=False, cc_manager_enabled=False, public_base_url="https://example.test")
        sender = AsyncMock(side_effect=[(False, "失败"), (True, "ok")])
        with patch.object(notifications, "send_wecom_textcard_message", sender):
            self.assertEqual(
                await notifications.check_and_send_ecn_reminders(
                    config=settings, user_service=self.service, storage=self.storage
                ),
                (1, 1),
            )
            self.assertEqual(
                await notifications.check_and_send_ecn_reminders(
                    config=settings, user_service=self.service, storage=self.storage
                ),
                (0, 0),
            )
            states = await self.storage.get_fresh_item(STATE_KEY, {})
            for event in states["ECN-test"].values():
                for route in event.values():
                    for delivery in route.values():
                        delivery["attempt"] = 0
            await self.storage.set_item(STATE_KEY, states)
            sender.reset_mock(side_effect=True)
            sender.return_value = (True, "ok")
            self.assertEqual(
                await notifications.check_and_send_ecn_reminders(
                    config=settings, user_service=self.service, storage=self.storage
                ),
                (1, 0),
            )
            sender.assert_awaited_once()
            self.assertEqual(sender.call_args.args[1], "writer_wx")

    async def test_deleted_record_cannot_be_recreated_by_confirmation(self):
        baseline = self.item()
        await self.storage.set_item("ecn_management_data", {})
        self.assertFalse((await self.update(baseline=baseline, confirmed=True)).ok)
        self.assertEqual(await self.storage.get_fresh_item("ecn_management_data", {}), {})

    async def test_erp_can_be_transferred_and_confirmed_independently(self):
        self.record["execution_info"]["ordinary_confirmations"]["A"]["confirmed"] = True
        self.record["execution_info"]["erp_confirmation"] = {"confirmed": False}
        await self.storage.set_item("ecn_management_data", {"ECN-test": self.record})
        result = await update_special_task(
            "ECN-test",
            "__erp__",
            {"confirmed": False},
            username="manager",
            role="研发经理",
            service=self.service,
            storage=self.storage,
            assignee="writer",
        )
        assert result.record is not None
        self.assertTrue(result.ok)
        self.assertTrue(is_ecn_assistant_execution_ready(result.record["execution_info"]))

    async def test_return_to_operator_omits_self_notice_but_keeps_badge(self):
        for target in ("", "manager"):
            with self.subTest(target=target):
                await self.update(assignee="writer")
                await self.update(assignee=target)
                pending = notifications.collect_pending_users(self.record, self.service)
                self.assertIn("manager", pending)
                tasks = notifications.pending_task_details(self.record, pending, self.service)
                self.assertEqual(tasks["manager"], [])
                settings = load_ecn_config()["wecom"]
                settings.update(test_mode=False, cc_manager_enabled=False, public_base_url="https://example.test")
                sender = AsyncMock(return_value=(True, "ok"))
                with patch.object(notifications, "send_wecom_textcard_message", sender):
                    await notifications.check_and_send_ecn_reminders(
                        config=settings, user_service=self.service, storage=self.storage
                    )
                self.assertNotIn("manager_wx", {call.args[1] for call in sender.await_args_list})
                self.assertTrue(any("取消" in call.kwargs["title"] for call in sender.await_args_list))

    async def test_other_pending_work_is_not_suppressed_by_self_return(self):
        await self.update(assignee="writer")
        await self.update(assignee="")
        self.record["execution_info"]["erp_confirmation"]["confirmed"] = False
        pending = notifications.collect_pending_users(self.record, self.service)
        self.assertTrue(notifications.pending_task_details(self.record, pending, self.service)["manager"])
