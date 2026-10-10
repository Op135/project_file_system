import unittest
from unittest.mock import AsyncMock, call, patch

from src.overview_admin_maintenance import (
    apply_admin_pending_overview_states,
    collect_pending_overview_items,
)


class OverviewAdminMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.over_config: dict[str, object] = {
            "结构": {"基础信息": [{"label": "size", "title": "尺寸"}]},
            "硬件": {"电气信息": [{"label": "voltage", "title": "电压"}]},
        }
        self.overview_data: dict[str, dict[str, dict[str, object]]] = {
            "size": {
                "size-1": {
                    "content": "100 × 80 mm",
                    "select_activ_dic": {"1.0": None, "2.0": None, "3.0": True},
                    "timestamp": {
                        "2026-09-01 10:00:00": {
                            "creator": "结构工程师",
                            "reason": "初版待确认",
                            "select_activ_dic": {"1.0": None},
                        },
                        "2026-10-01 10:00:00": {
                            "creator": "结构工程师",
                            "reason": "需求变更",
                            "select_activ_dic": {"1.0": None, "2.0": None, "3.0": True},
                        },
                    },
                }
            },
            "voltage": {
                "voltage-1": {
                    "content": "24 V",
                    "select_activ_dic": {"1.0": False, "2.0": None},
                }
            },
        }

    def test_collects_all_pending_versions_grouped_by_role_with_each_reason(self) -> None:
        grouped = collect_pending_overview_items(self.overview_data, self.over_config)

        self.assertEqual(list(grouped), ["结构", "硬件"])
        structure_items = grouped["结构"]
        self.assertEqual([(item.title, item.version) for item in structure_items], [("尺寸", "1.0"), ("尺寸", "2.0")])
        self.assertEqual([item.pending_reason for item in structure_items], ["初版待确认", "需求变更"])
        self.assertEqual([(item.title, item.version) for item in grouped["硬件"]], [("电压", "2.0")])
        self.assertEqual(grouped["硬件"][0].pending_reason, "未记录待定原因")


class OverviewAdminMaintenanceAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_is_rejected_before_regular_state_api_call(self) -> None:
        with patch(
            "src.overview_admin_maintenance.update_overview_chip_state",
            new=AsyncMock(),
        ) as update_state:
            result = await apply_admin_pending_overview_states(
                project_name="P1",
                targets={("size", "size-1", "1.0")},
                target_state=True,
                actor="研发经理",
                operation_note="无权限尝试",
            )

        self.assertFalse(result.ok)
        update_state.assert_not_awaited()

    async def test_admin_uses_regular_api_for_multiple_versions_with_original_responsible(self) -> None:
        chip = {
            "creator": "原录入人",
            "select_activ_dic": {"1.0": None, "2.0": None},
        }
        update_state = AsyncMock(
            side_effect=[
                (True, "状态已修改", {"select_activ_dic": {"1.0": True, "2.0": None}}),
                (True, "状态已修改", {"select_activ_dic": {"1.0": True, "2.0": True}}),
            ]
        )

        with (
            patch("src.overview_admin_maintenance.db_storage.get_deep_item", return_value=chip),
            patch(
                "src.overview_admin_maintenance.get_overview_latest_responsible",
                return_value="原负责人",
            ),
            patch("src.overview_admin_maintenance.update_overview_chip_state", new=update_state),
        ):
            result = await apply_admin_pending_overview_states(
                project_name="P1",
                targets={("size", "size-1", "1.0"), ("size", "size-1", "2.0")},
                target_state=True,
                actor="admin",
                operation_note="纠正系统错误设置",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.changed_count, 2)
        self.assertEqual(result.changed_labels, frozenset({"size"}))
        expected_reason = "特殊维护（设为激活）：纠正系统错误设置"
        update_state.assert_has_awaits(
            [
                call(
                    "P1",
                    "size",
                    "size-1",
                    "1.0",
                    True,
                    "原负责人",
                    expected_reason,
                    expected_state=None,
                ),
                call(
                    "P1",
                    "size",
                    "size-1",
                    "2.0",
                    True,
                    "原负责人",
                    expected_reason,
                    expected_state=None,
                ),
            ]
        )

    async def test_versions_changed_after_dialog_opened_are_skipped(self) -> None:
        with (
            patch(
                "src.overview_admin_maintenance.db_storage.get_deep_item",
                return_value={"creator": "原负责人", "select_activ_dic": {"2.0": False}},
            ),
            patch(
                "src.overview_admin_maintenance.get_overview_latest_responsible",
                return_value="原负责人",
            ),
            patch(
                "src.overview_admin_maintenance.update_overview_chip_state",
                new=AsyncMock(return_value=(False, "概述当前状态已变化", {})),
            ),
        ):
            result = await apply_admin_pending_overview_states(
                project_name="P1",
                targets={("size", "size-1", "2.0")},
                target_state=False,
                actor="admin",
                operation_note="并发纠正",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.changed_count, 0)
        self.assertEqual(result.skipped_count, 1)


if __name__ == "__main__":
    unittest.main()
