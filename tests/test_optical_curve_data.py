"""研发光学曲线数据处理的回归测试。"""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.optical_curve_data import (  # noqa: E402
    CurveDataError,
    curve_matches_filters,
    fuse_and_normalize_curve_records,
    fuse_curve_records,
    normalize_conditions,
    normalize_y_values,
    parse_curve_rows,
)


class OpticalCurveDataTests(unittest.TestCase):
    def test_parse_two_columns_supports_header_and_sorts_wavelengths(self):
        x_data, y_data = parse_curve_rows("波长 (nm)\t相对强度\n500\t5\n400\t2\n450\t4")
        self.assertEqual(x_data, [400.0, 450.0, 500.0])
        self.assertEqual(y_data, [2.0, 4.0, 5.0])

    def test_parse_rejects_duplicate_wavelengths_and_invalid_columns(self):
        with self.assertRaisesRegex(CurveDataError, "重复波长"):
            parse_curve_rows("400,1\n400,2")
        with self.assertRaisesRegex(CurveDataError, "恰好包含两列"):
            parse_curve_rows("400,1,3\n500,2")
        with self.assertRaisesRegex(CurveDataError, "非数字"):
            parse_curve_rows("400,错误\n500,2")

    def test_normalize_uses_maximum_absolute_value(self):
        normalized, factor = normalize_y_values([-2.0, 1.0, 4.0])
        self.assertEqual(factor, 4.0)
        self.assertEqual(normalized, [-0.5, 0.25, 1.0])

    def test_normalize_rejects_all_zero_values(self):
        with self.assertRaisesRegex(CurveDataError, "不能全部为 0"):
            normalize_y_values([0.0, 0.0])

    def test_conditions_are_optional_but_half_filled_or_duplicate_rows_are_rejected(self):
        self.assertEqual(normalize_conditions([{"name": "", "value": ""}]), [])
        with self.assertRaisesRegex(CurveDataError, "同时填写"):
            normalize_conditions([{"name": "材料", "value": ""}])
        with self.assertRaisesRegex(CurveDataError, "重复"):
            normalize_conditions(
                [{"name": "材料", "value": "石英"}, {"name": "材料", "value": "蓝宝石"}]
            )

    def test_curve_filter_requires_all_selected_conditions(self):
        record = {
            "title": "滤光片 A 透过率",
            "y_axis_name": "透过率",
            "conditions": [
                {"name": "材料", "value": "石英"},
                {"name": "温度", "value": "25℃"},
            ],
        }
        self.assertTrue(
            curve_matches_filters(
                record,
                title_query="滤光片",
                y_axis_name="透过率",
                conditions=[{"name": "材料", "value": "石英"}, {"name": "温度", "value": "25℃"}],
            )
        )
        self.assertFalse(curve_matches_filters(record, conditions=[{"name": "材料", "value": "蓝宝石"}]))
        self.assertTrue(curve_matches_filters(record, title_query="25℃"))
        self.assertTrue(curve_matches_filters(record, title_query="石英"))

    def test_fusion_interpolates_on_union_wavelength_grid_and_sums_values(self):
        x_data, y_data = fuse_curve_records(
            [
                {"x_data": [400, 500, 600], "y_data": [0.0, 1.0, 0.0]},
                {"x_data": [450, 550, 650], "y_data": [1.0, 0.5, 0.0]},
            ]
        )
        self.assertEqual(x_data, [400.0, 450.0, 500.0, 550.0, 600.0, 650.0])
        self.assertEqual(y_data, [0.0, 1.5, 1.75, 1.0, 0.25, 0.0])

    def test_fusion_requires_two_curves_but_allows_disjoint_x_ranges(self):
        with self.assertRaisesRegex(CurveDataError, "至少需要选择 2 条"):
            fuse_curve_records([{"x_data": [400, 500], "y_data": [0, 1]}])
        x_data, y_data = fuse_curve_records(
            [
                {"x_data": [400, 500], "y_data": [0, 1]},
                {"x_data": [600, 700], "y_data": [1, 0]},
            ]
        )
        self.assertEqual(x_data, [400.0, 500.0, 600.0, 700.0])
        self.assertEqual(y_data, [0.0, 1.0, 1.0, 0.0])

    def test_fusion_result_is_normalized_after_sum(self):
        x_data, y_data, factor = fuse_and_normalize_curve_records(
            [
                {"x_data": [400, 500, 600], "y_data": [0.0, 1.0, 0.0]},
                {"x_data": [450, 550, 650], "y_data": [1.0, 0.5, 0.0]},
            ]
        )
        self.assertEqual(x_data, [400.0, 450.0, 500.0, 550.0, 600.0, 650.0])
        self.assertEqual(factor, 1.75)
        self.assertEqual(max(abs(value) for value in y_data), 1.0)
        self.assertAlmostEqual(y_data[1], 1.5 / 1.75)


if __name__ == "__main__":
    unittest.main()
