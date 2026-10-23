import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nicegui import ui

from src.components import (
    OverviewTableGroup,
    OverviewReasonSelector,
    _can_skip_related_impact,
    _get_active_chip_icon,
    _get_image_status_visuals,
    _has_real_related_selection,
    _missing_configured_test_selection,
    _missing_test_other_text,
    _normalize_related_labels,
)


class RelatedImpactSelectionTests(unittest.TestCase):
    def test_test_validation_ignores_unconfigured_none_fields(self):
        test_data = {
            "test_nature_select": "抽检",
            "state_select": None,
            "node_select": None,
            "instrument_select": "游标卡尺",
        }
        option_lists = {
            "test_nature": ["抽检", "全检"],
            "state": [],
            "node": [],
            "instrument": ["游标卡尺"],
        }

        self.assertFalse(_missing_configured_test_selection(test_data, option_lists))

    def test_test_validation_requires_only_rendered_options(self):
        test_data = {
            "test_nature_select": None,
            "state_select": None,
            "node_select": None,
            "instrument_select": "游标卡尺",
        }
        option_lists = {
            "test_nature": ["抽检", "全检"],
            "state": [],
            "node": [],
            "instrument": ["游标卡尺"],
        }

        self.assertTrue(_missing_configured_test_selection(test_data, option_lists))

    def test_other_text_is_required_only_for_configured_other_selection(self):
        test_data = {
            "test_nature_select": "其它",
            "test_nature_other_text": "",
            "state_select": "其它",
            "state_other_text": "",
        }
        self.assertTrue(
            _missing_test_other_text(
                test_data,
                {"test_nature": ["其它"], "state": [], "node": [], "instrument": []},
            )
        )
        self.assertFalse(
            _missing_test_other_text(
                test_data,
                {"test_nature": [], "state": [], "node": [], "instrument": []},
            )
        )

    def test_autofill_dialog_accepts_multiple_project_candidates(self):
        table = OverviewTableGroup.__new__(OverviewTableGroup)
        table.autofill_dialog = ui.dialog()

        with patch("src.components.ui.radio", wraps=ui.radio) as radio_factory:
            table._show_autofill_dialog(
                "row-1",
                {
                    "RFME-2783-A": {"字段A": []},
                    "RFME-2784-B": {"字段A": []},
                },
                [],
            )

        self.assertTrue(table.autofill_dialog.value)
        self.assertEqual(radio_factory.call_args_list[0].args, (["RFME-2783-A"],))
        self.assertEqual(radio_factory.call_args_list[0].kwargs["value"], "RFME-2783-A")
        self.assertEqual(radio_factory.call_args_list[1].args, (["RFME-2784-B"],))
        self.assertIsNone(radio_factory.call_args_list[1].kwargs["value"])

    def test_overview_reason_selector_starts_empty_and_preserves_legacy_free_text(self):
        selector = OverviewReasonSelector("state_change")
        self.assertIsNone(selector.radio.value)
        self.assertEqual(selector.value, "")

        configured_label = selector.options[0]
        selector.value = configured_label
        self.assertEqual(selector.value, configured_label)

        selector.value = "历史自由填写原因"
        self.assertEqual(selector.radio.value, "其他")
        self.assertEqual(selector.value, "其他：历史自由填写原因")

        selector.value = ""
        self.assertIsNone(selector.radio.value)
        self.assertEqual(selector.value, "")

    def test_normalize_related_labels_handles_empty_values_and_duplicates(self):
        self.assertEqual(_normalize_related_labels(None), [])
        self.assertEqual(_normalize_related_labels([]), [])
        self.assertEqual(_normalize_related_labels(["light", "", None, "light", "lens"]), ["light", "lens"])

    def test_only_boolean_true_counts_as_a_real_selection(self):
        self.assertFalse(_has_real_related_selection({}))
        self.assertFalse(_has_real_related_selection({"light": False, "lens": None}))
        self.assertFalse(_has_real_related_selection({"light": 1, "lens": "true"}))
        self.assertTrue(_has_real_related_selection({"light": False, "lens": True}))

    def test_media_type_icon_is_restored_after_reactivation(self):
        self.assertEqual(_get_active_chip_icon("image"), "image")
        self.assertEqual(_get_active_chip_icon("file"), "attachment")
        self.assertEqual(_get_active_chip_icon("video"), "play_circle")
        self.assertIsNone(_get_active_chip_icon("text"))

    def test_pending_image_uses_high_contrast_badge_and_border(self):
        border_classes, badge_classes = _get_image_status_visuals("question_mark")
        self.assertIn("border-amber-500", border_classes)
        self.assertIn("bg-amber-8", badge_classes)
        self.assertIn("text-white", badge_classes)

    def test_add_chip_can_use_no_related_impact_confirmation_in_both_overview_classes(self):
        self.assertTrue(_can_skip_related_impact("add_chip"))
        self.assertTrue(_can_skip_related_impact("activ_change"))
        self.assertFalse(_can_skip_related_impact("unknown"))


class OverviewAutofillSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_clicks_create_only_one_set_of_chips(self):
        table = OverviewTableGroup.__new__(OverviewTableGroup)
        table.project = "P1"
        table.role = "研发"
        table._autofill_submitted_rows = set()
        app = SimpleNamespace(storage=SimpleNamespace(
            general={"project_req_max_ver": {"P1": "1.0"}},
            user={"current_user": "owner"},
        ))
        entered = asyncio.Event()
        release = asyncio.Event()
        writes = []

        async def save_once(**kwargs):
            writes.append(kwargs)
            entered.set()
            await release.wait()
            return SimpleNamespace(effective=True)

        combo = {
            "字段A": [{"type": "text", "content": "历史内容A"}],
            "字段B": [{"type": "text", "content": "历史内容B"}],
        }
        configs = [{"label": "field-a", "title": "字段A"}, {"label": "field-b", "title": "字段B"}]
        button = Mock()
        with patch("src.components.app", app), \
             patch.object(table, "_get_select_activ_dic", return_value={"1.0": True}), \
             patch.object(table, "autofill_dialog", create=True) as autofill_dialog, \
             patch("src.components._save_or_queue_overview_addition", side_effect=save_once), \
             patch("src.components.OverviewVersionManager.bump"), \
             patch("src.components.ui.notify"):
            first = asyncio.create_task(table._execute_autofill("row-1", combo, configs, "录入原因", btn=button))
            await entered.wait()
            await table._execute_autofill("row-1", combo, configs, "录入原因", btn=button)
            release.set()
            await first
            await table._execute_autofill("row-1", combo, configs, "录入原因", btn=button)

        self.assertEqual(len(writes), 2)
        self.assertEqual({entry["label"] for entry in writes}, {"field-a", "field-b"})
        self.assertTrue(all(entry["chip"]["row_id"] == "row-1" for entry in writes))
        button.disable.assert_called_once()
        autofill_dialog.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
