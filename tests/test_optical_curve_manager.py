"""研发光学曲线界面配置的回归测试。"""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.optical_curve_manager import (  # noqa: E402
    _build_curve_tree,
    _chart_options,
    _fusion_pending_status,
    _int_at_least,
    _optional_float,
)


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
        self.assertEqual(tree[0]["children"][0]["children"][0]["children"][0]["id"], "curve-1")

    def test_legend_contains_title_and_condition_values_but_not_y_axis_name(self):
        options = _chart_options(self.records)
        legend_name = options["series"][0]["name"]
        self.assertIn("滤光片 A", legend_name)
        self.assertIn("石英", legend_name)
        self.assertIn("25℃", legend_name)
        self.assertNotIn("透过率", legend_name)
        self.assertEqual(options["yAxis"]["name"], "透过率")

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
        self.assertEqual(options["legend"]["textStyle"]["fontSize"], 16)

    def test_optional_numeric_conversion_handles_none_and_invalid_values(self):
        self.assertIsNone(_optional_float(None))
        self.assertIsNone(_optional_float(""))
        self.assertIsNone(_optional_float([]))
        self.assertEqual(_optional_float("12.5"), 12.5)
        self.assertEqual(_int_at_least(None, 12, 8), 12)
        self.assertEqual(_int_at_least("5", 12, 8), 8)

    def test_fusion_prompt_is_hidden_until_one_curve_is_selected(self):
        self.assertEqual(_fusion_pending_status(0), "")
        self.assertIn("还需再选择 1 条", _fusion_pending_status(1))
        self.assertEqual(_fusion_pending_status(2), "")


if __name__ == "__main__":
    unittest.main()
