"""ECN物料料号补充阶段、权限和并发保护回归。"""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.ecn_access import build_ecn_access_snapshot, is_ecn_pending_for_user
from src.ecn_management_config import (
    ECNState,
    get_ecn_material_change_display,
    get_ecn_material_change_missing_fields,
    get_ecn_material_code_entries,
    get_ecn_material_code_missing_fields,
    get_ecn_missing_material_code_items,
)
from src.modules.ecn import actions
from src.modules.ecn.models import get_ecn_template
from src.modules.ecn import notifications
from src.modules.ecn.detail import sync_detail_scheme_snapshot
from src.permission_catalog import (
    ECN_MATERIAL_CODE_EDIT_PERMISSION,
    ECN_VIEW_PERMISSION,
    PERMISSION_CODES,
    build_legacy_default_grants,
)
from tests.test_error_management_concurrency import load_isolated_db_storage


def material_item(item_id: str, name: str) -> dict:
    return {
        "item_id": item_id,
        "author": "engineer",
        "type": "text_desc",
        "scheme_category": "material",
        "projects": ["P1"],
        "change_type": "新增",
        "material_change": {
            "material_name": name,
            "quantity": 1,
            "unit": "pcs",
        },
        "traceability_levels": ["文件"],
    }


class ECNMaterialCodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = load_isolated_db_storage(
            "ecn_material_code_db",
            Path(self.temp.name) / "ecn.db",
        )
        await self.storage.init_db()
        self.service = SimpleNamespace(storage_mode="legacy_excel")
        self.record = get_ecn_template()
        self.record["ecn_id"] = "ECN-code"
        self.record["basic_info"].update(applicant="engineer", file_no="ECN-code")
        self.record["workflow"].update(
            current_state=ECNState.MATERIAL_CODE_PENDING,
            current_phase="ECN_MATERIAL_CODE_PHASE",
        )
        self.record["change_items"] = [
            material_item("M1", "白光LED"),
            material_item("M2", "螺钉"),
        ]
        await self.storage.set_item("ecn_management_data", {"ECN-code": self.record})

    async def asyncTearDown(self):
        await self.storage.close_db()
        self.temp.cleanup()

    async def fresh(self) -> dict:
        records = await self.storage.get_fresh_item("ecn_management_data", {})
        return records["ECN-code"]

    async def update(self, item: dict, code: str, *, allowed: bool = True):
        with (
            patch.object(actions, "can_edit_ecn_material_codes", return_value=allowed),
            patch.object(
                actions,
                "build_ecn_execution_info_from_workflows",
                return_value={"stage": "assistant_confirmation"},
            ),
        ):
            return await actions.update_material_codes(
                "ECN-code",
                copy.deepcopy(item),
                {"main:material_code": code},
                "assistant",
                "研发助理",
                user_service=self.service,
                storage=self.storage,
            )

    def test_display_and_missing_fields_put_code_above_material_name(self):
        item = material_item("M1", "白光LED")
        self.assertEqual(get_ecn_material_code_missing_fields(item), ["料号"])
        self.assertEqual(
            get_ecn_material_change_display(item)[1],
            "料号：待补充\n白光LED\n用量：1 pcs",
        )
        item["material_change"]["material_code"] = "LED-001"
        self.assertEqual(get_ecn_material_code_missing_fields(item), [])
        self.assertEqual(
            get_ecn_material_change_display(item)[1],
            "料号：LED-001\n白光LED\n用量：1 pcs",
        )

    def test_alternative_materials_are_displayed_and_require_codes(self):
        item = material_item("M1", "白光LED")
        item["material_change"].update(
            material_code="LED-001",
            alternative_materials=[
                {
                    "alternative_id": "A1",
                    "material_code": "",
                    "material_name": "暖白LED",
                },
                {
                    "alternative_id": "A2",
                    "material_code": "LED-ALT-002",
                    "material_name": "中性光LED",
                },
            ],
        )
        self.assertEqual(
            get_ecn_material_code_missing_fields(item),
            ["新增替换料 1 料号（暖白LED）"],
        )
        self.assertEqual(
            get_ecn_material_change_display(item)[1],
            "料号：LED-001\n白光LED\n用量：1 pcs\n\n替换料：\n"
            "1. 料号：待补充\n   暖白LED\n2. 料号：LED-ALT-002\n   中性光LED",
        )
        self.assertEqual(
            [entry["token"] for entry in get_ecn_material_code_entries(item)],
            ["main:material_code", "alternative_materials:A1", "alternative_materials:A2"],
        )
        item["material_change"]["alternative_materials"][0]["material_name"] = ""
        self.assertIn(
            "新增替换料 1 物料名称",
            get_ecn_material_change_missing_fields(item["change_type"], item["material_change"]),
        )

    def test_replace_displays_before_and_after_alternative_materials(self):
        item = material_item("M1", "unused")
        item["change_type"] = "更改"
        item["material_change"] = {
            "old_material_code": "OLD-1",
            "old_material_name": "旧主料",
            "old_quantity": 1,
            "old_unit": "pcs",
            "new_material_code": "NEW-1",
            "new_material_name": "新主料",
            "new_quantity": 1,
            "new_unit": "pcs",
            "old_alternative_materials": [
                {"alternative_id": "O1", "material_code": "OLD-A", "material_name": "旧替换料"}
            ],
            "new_alternative_materials": [
                {"alternative_id": "N1", "material_code": "", "material_name": "新替换料"}
            ],
        }
        before, after = get_ecn_material_change_display(item)
        self.assertIn("替换料：\n1. 料号：OLD-A\n   旧替换料", before)
        self.assertIn("替换料：\n1. 料号：待补充\n   新替换料", after)
        self.assertEqual(get_ecn_material_code_missing_fields(item), ["更改后替换料 1 料号（新替换料）"])

    def test_scheme_review_completion_waits_only_when_material_code_is_missing(self):
        record = copy.deepcopy(self.record)
        actions.enter_next_phase(record, "ECN_SCHEME_REVIEW_PHASE", {}, self.service)
        self.assertEqual(record["workflow"]["current_state"], ECNState.MATERIAL_CODE_PENDING)
        self.assertEqual(record["workflow"]["current_phase"], "ECN_MATERIAL_CODE_PHASE")
        self.assertEqual(len(get_ecn_missing_material_code_items(record)), 2)

    def test_execution_stage_refresh_replaces_stale_scheme_rows(self):
        local = copy.deepcopy(self.record)
        local["change_items"] = local["change_items"][:1]
        participants = {"engineer": "confirmed"}
        fresh = copy.deepcopy(self.record)
        fresh["workflow"].update(
            current_state=ECNState.ECN_EXECUTING,
            current_phase="ECN_EXECUTION_PHASE",
            scheme_participants={"engineer": "confirmed", "other": "confirmed"},
        )
        fresh["change_items"].append(material_item("M3", "连接器"))

        self.assertTrue(sync_detail_scheme_snapshot(local, participants, fresh))
        self.assertEqual(len(local["change_items"]), 3)
        self.assertEqual(participants, {"engineer": "confirmed", "other": "confirmed"})
        self.assertFalse(sync_detail_scheme_snapshot(local, participants, fresh))

    def test_new_permission_drives_home_pending_and_notification_content(self):
        class Users:
            storage_mode = "database"

            @staticmethod
            def load_users():
                return {
                    "assistant": {"role": "研发助理", "status": "active"},
                    "viewer": {"role": "研发工程师", "status": "active"},
                }

            @staticmethod
            def list_active_user_permission_codes():
                return {
                    "assistant": {ECN_VIEW_PERMISSION, ECN_MATERIAL_CODE_EDIT_PERMISSION},
                    "viewer": {ECN_VIEW_PERMISSION},
                }

        service = Users()
        snapshot = build_ecn_access_snapshot(service)
        self.assertIn(ECN_MATERIAL_CODE_EDIT_PERMISSION, PERMISSION_CODES)
        self.assertIn(
            ECN_MATERIAL_CODE_EDIT_PERMISSION,
            build_legacy_default_grants({}, known_role_names=["研发助理"])["研发助理"],
        )
        self.assertTrue(
            is_ecn_pending_for_user(
                self.record,
                "assistant",
                "研发助理",
                user_service=service,
                access_snapshot=snapshot,
            )
        )
        self.assertFalse(
            is_ecn_pending_for_user(
                self.record,
                "viewer",
                "研发工程师",
                user_service=service,
                access_snapshot=snapshot,
            )
        )
        pending = notifications.collect_pending_users(self.record, service, snapshot)
        self.assertEqual(pending, {"assistant": "研发助理"})
        details = notifications.pending_task_details(self.record, pending, service, snapshot)
        self.assertIn("方案：#01", details["assistant"][0])
        self.assertIn("缺少：料号", details["assistant"][0])

    async def test_permission_stale_snapshot_and_execution_lock(self):
        denied = await self.update(self.record["change_items"][0], "LED-001", allowed=False)
        self.assertFalse(denied.ok)
        self.assertIn("权限", denied.message)

        first = await self.update(self.record["change_items"][0], "LED-001")
        self.assertTrue(first.ok, first.message)
        assert first.record is not None
        self.assertEqual(first.record["workflow"]["current_state"], ECNState.MATERIAL_CODE_PENDING)

        stale = await self.update(self.record["change_items"][0], "LED-002")
        self.assertFalse(stale.ok)
        self.assertIn("其他页面修改", stale.message)

        second_item = (await self.fresh())["change_items"][1]
        final = await self.update(second_item, "SCREW-001")
        self.assertTrue(final.ok, final.message)
        assert final.record is not None
        self.assertEqual(final.record["workflow"]["current_state"], ECNState.ECN_EXECUTING)
        self.assertEqual(final.record["workflow"]["current_phase"], "ECN_EXECUTION_PHASE")
        self.assertEqual(final.record["execution_info"]["stage"], "assistant_confirmation")

        locked_item = final.record["change_items"][0]
        locked = await self.update(locked_item, "LED-003")
        self.assertFalse(locked.ok)
        self.assertIn("不能修改", locked.message)

    async def test_authorized_user_can_fill_main_and_alternative_codes_together(self):
        first_item = self.record["change_items"][0]
        first_item["material_change"]["alternative_materials"] = [
            {
                "alternative_id": "A1",
                "material_code": "",
                "material_name": "暖白LED",
            }
        ]
        await self.storage.set_item("ecn_management_data", {"ECN-code": self.record})
        with (
            patch.object(actions, "can_edit_ecn_material_codes", return_value=True),
            patch.object(
                actions,
                "build_ecn_execution_info_from_workflows",
                return_value={"stage": "assistant_confirmation"},
            ),
        ):
            result = await actions.update_material_codes(
                "ECN-code",
                copy.deepcopy(first_item),
                {
                    "main:material_code": "LED-001",
                    "alternative_materials:A1": "LED-ALT-001",
                },
                "assistant",
                "研发助理",
                user_service=self.service,
                storage=self.storage,
            )
        self.assertTrue(result.ok, result.message)
        assert result.record is not None
        saved = result.record["change_items"][0]["material_change"]
        self.assertEqual(saved["material_code"], "LED-001")
        self.assertEqual(saved["alternative_materials"][0]["material_code"], "LED-ALT-001")
        self.assertEqual(result.record["workflow"]["current_state"], ECNState.MATERIAL_CODE_PENDING)
