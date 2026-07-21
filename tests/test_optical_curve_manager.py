"""研发光学曲线界面配置的回归测试。"""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.optical_curve_manager import (  # noqa: E402
    OpticalCurveManagerTool,
    _build_curve_tree,
    _chart_options,
    _clipboard_click_handler,
    _curve_alias,
    _curve_data_text,
    _curve_tree_group_ids,
    _int_at_least,
    _optional_float,
    _prepare_curve_data,
    _select_textarea_script,
)
from src.tools.optical_curve_data import CurveDataError  # noqa: E402


class OpticalCurveManagerTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "id": "curve-1",
                "title": "滤光片 A",
                "y_axis_name": "透过率",
                "conditions": [
                    {"name": "材料", "value": "石英"},
                    {"name": "温度", "value": "25℃"},
                ],
                "x_data": [400, 500],
                "y_data": [0.0, 1.0],
            }
        ]

    def test_tree_groups_by_y_axis_then_each_condition(self):
        tree = _build_curve_tree(self.records)
        self.assertEqual(tree[0]["label"], "透过率")
        self.assertEqual(tree[0]["children"][0]["label"], "材料：石英")
        self.assertEqual(tree[0]["children"][0]["children"][0]["label"], "温度：25℃")
        leaf = tree[0]["children"][0]["children"][0]["children"][0]
        self.assertEqual(leaf["id"], "curve-1")
        self.assertEqual(leaf["label"], "滤光片 A")

    def test_tree_group_ids_include_all_expandable_levels(self):
        tree = _build_curve_tree(self.records)
        group_ids = _curve_tree_group_ids(tree)
        self.assertEqual(len(group_ids), 3)
        self.assertTrue(all(group_id.startswith("group:") for group_id in group_ids))

    def test_legend_contains_title_and_condition_values_but_not_y_axis_name(self):
        options = _chart_options(self.records)
        legend_name = options["series"][0]["name"]
        self.assertIn("滤光片 A", legend_name)
        self.assertIn("石英", legend_name)
        self.assertIn("25℃", legend_name)
        self.assertNotIn("透过率", legend_name)
        self.assertEqual(options["yAxis"]["name"], "透过率")
        self.assertEqual(options["legend"][0]["type"], "plain")
        self.assertEqual(options["legend"][0]["orient"], "horizontal")
        self.assertEqual(options["legend"][0]["left"], "center")

    def test_legend_reserves_more_top_space_for_multiple_titles(self):
        single_options = _chart_options(self.records)
        many_records = [dict(self.records[0], id=f"curve-{index}") for index in range(8)]
        many_options = _chart_options(many_records)
        self.assertGreater(many_options["grid"]["top"], single_options["grid"]["top"])
        self.assertGreater(len(many_options["legend"]), 1)
        self.assertTrue(all(row["left"] == "center" for row in many_options["legend"]))

    def test_curve_data_text_uses_two_tab_separated_columns(self):
        self.assertEqual(_curve_data_text(self.records[0]), "400\t0.0\n500\t1.0")
        self.assertEqual(_curve_data_text({"x_data": [400], "y_data": []}), "")

    def test_clipboard_handler_uses_api_only_in_secure_context(self):
        handler = _clipboard_click_handler('400\t0.5\n500\t"0.95"')
        self.assertIn('const text = "400\\t0.5\\n500\\t\\"0.95\\"";', handler)
        self.assertIn("navigator.clipboard?.writeText", handler)
        self.assertIn("reason: 'insecure'", handler)
        self.assertNotIn("document.execCommand('copy')", handler)
        self.assertIn("emit({", handler)

    def test_manual_copy_script_selects_the_complete_textarea(self):
        script = _select_textarea_script(42, delay_ms=150)
        self.assertIn("getHtmlElement(42)", script)
        self.assertIn("textarea.select()", script)
        self.assertIn("setSelectionRange(0, textarea.value.length)", script)
        self.assertIn("}, 150);", script)

    def test_prepare_curve_data_only_normalizes_the_left_input(self):
        auto_data = _prepare_curve_data("400\t0.5\n500\t0.95", "")
        self.assertEqual(auto_data["normalization_mode"], "auto_normalize")
        self.assertAlmostEqual(auto_data["normalization_factor"], 0.95)
        self.assertEqual(auto_data["y_data"][-1], 1.0)

        preserved_data = _prepare_curve_data("", "400\t0.5\n500\t0.95")
        self.assertEqual(preserved_data["normalization_mode"], "keep_original")
        self.assertEqual(preserved_data["normalization_factor"], 1.0)
        self.assertEqual(preserved_data["y_data"], [0.5, 0.95])

    def test_prepare_curve_data_requires_exactly_one_input(self):
        with self.assertRaises(CurveDataError):
            _prepare_curve_data("", "")
        with self.assertRaises(CurveDataError):
            _prepare_curve_data("400\t1\n500\t2", "400\t0.5\n500\t0.8")

    def test_editing_loads_existing_curve_into_keep_original_input(self):
        manager = OpticalCurveManagerTool()
        manager._load_edit_record(self.records[0])
        self.assertEqual(manager.edit_record_id, "curve-1")
        self.assertEqual(manager.edit_form["normalize_data_text"], "")
        self.assertEqual(manager.edit_form["preserve_data_text"], "400\t0.0\n500\t1.0")

    def test_axis_intervals_and_fonts_are_applied(self):
        options = _chart_options(
            self.records,
            settings={
                "x_interval": 50,
                "y_interval": 0.2,
                "x_min": 420,
                "x_max": 680,
                "font_family": "Arial",
                "font_size": 14,
                "legend_font_size": 16,
            },
        )
        self.assertEqual(options["xAxis"]["interval"], 50)
        self.assertEqual(options["yAxis"]["interval"], 0.2)
        self.assertEqual(options["xAxis"]["min"], 420)
        self.assertEqual(options["xAxis"]["max"], 680)
        self.assertEqual(options["dataZoom"][0]["startValue"], 420)
        self.assertEqual(options["dataZoom"][1]["endValue"], 680)
        self.assertEqual(options["xAxis"]["axisLabel"]["fontFamily"], "Arial")
        self.assertEqual(options["xAxis"]["axisLabel"]["fontSize"], 14)
        self.assertEqual(options["xAxis"]["axisLabel"][":formatter"], "value => String(value)")
        self.assertEqual(options["legend"][0]["textStyle"]["fontSize"], 16)

    def test_optional_numeric_conversion_handles_none_and_invalid_values(self):
        self.assertIsNone(_optional_float(None))
        self.assertIsNone(_optional_float(""))
        self.assertIsNone(_optional_float([]))
        self.assertEqual(_optional_float("12.5"), 12.5)
        self.assertEqual(_int_at_least(None, 12, 8), 12)
        self.assertEqual(_int_at_least("5", 12, 8), 8)

    def test_curve_aliases_continue_after_z(self):
        self.assertEqual(_curve_alias(0), "a")
        self.assertEqual(_curve_alias(25), "z")
        self.assertEqual(_curve_alias(26), "aa")
        self.assertEqual(_curve_alias(51), "az")
        self.assertEqual(_curve_alias(52), "ba")


if __name__ == "__main__":
    unittest.main()
