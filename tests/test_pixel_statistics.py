# -*- encoding: utf-8 -*-
import io
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
from nicegui import ui
from nicegui.events import ScrollEventArguments

from src.tools.pixel_statistics import (
    AnalysisBatch,
    PixelStatisticsError,
    PixelStatisticsSettings,
    PixelStatisticsTool,
    _ExcelBytesBuffer,
    _add_formula_header_tooltips,
    _result_table_columns,
    _threshold_outline_chart_options,
    analyze_matrix,
    analyze_uploaded_files,
    build_excel_report,
    locate_center,
    merge_pixel_blocks,
)


class PixelMergeTests(unittest.TestCase):
    def test_merge_centers_crop_and_keeps_an_odd_grid(self):
        matrix = np.arange(24, dtype=float).reshape(4, 6)

        merged, description = merge_pixel_blocks(matrix, 2, True, False)

        np.testing.assert_allclose(merged, [[9.5, 11.5, 13.5]])
        self.assertEqual(merged.shape, (1, 3))
        self.assertIn("上/下 1/1 行", description)


class CenterLocationTests(unittest.TestCase):
    def test_threshold_center_uses_the_largest_component_containing_a_maximum(self):
        matrix = np.zeros((7, 9), dtype=float)
        matrix[1:4, 2:7] = 8.0
        matrix[6, 8] = 8.0
        settings = PixelStatisticsSettings(
            center_mode="threshold",
            threshold_percent=90,
            region_mode="full",
        )

        row, col, description, outline = locate_center(matrix, settings)

        self.assertEqual((row, col), (2.0, 4.0))
        self.assertIn("15 点", description)
        self.assertIn("R2:R4", description)
        assert outline is not None
        self.assertEqual(outline.region_points, 15)
        self.assertGreater(len(outline.edge_points), 0)
        self.assertEqual(len(outline.edge_points), len(outline.edge_values))
        self.assertTrue(all(value >= outline.threshold for value in outline.edge_values))

        result = analyze_matrix(matrix, "sample.csv", "数据", settings)
        chart = _threshold_outline_chart_options(result)
        x_span = chart["xAxis"]["max"] - chart["xAxis"]["min"]
        y_span = chart["yAxis"]["max"] - chart["yAxis"]["min"]
        self.assertEqual(chart["xAxis"]["min"], 0)
        self.assertEqual(chart["yAxis"]["min"], 0)
        self.assertEqual(chart["xAxis"]["max"], matrix.shape[1])
        self.assertEqual(chart["yAxis"]["max"], matrix.shape[0])
        self.assertAlmostEqual(
            chart["grid"]["width"] / x_span,
            chart["grid"]["height"] / y_span,
            places=1,
        )
        self.assertIn("toFixed(2)", chart["xAxis"]["axisLabel"][":formatter"])
        self.assertIn("toFixed(2)", chart["yAxis"]["axisLabel"][":formatter"])
        self.assertIn("X：", chart["tooltip"][":formatter"])
        self.assertIn("Y：", chart["tooltip"][":formatter"])
        self.assertIn("边缘点数值", chart["tooltip"][":formatter"])
        self.assertIn("判定阈值", chart["tooltip"][":formatter"])
        self.assertTrue(all(len(point) == 3 for point in chart["series"][0]["data"]))

    def test_threshold_outline_limits_chart_points_without_changing_region_size(self):
        matrix = np.ones((250, 250), dtype=float)
        matrix[125, 125] = 2.0
        settings = PixelStatisticsSettings(
            center_mode="threshold",
            threshold_percent=50,
            region_mode="full",
        )

        _, _, _, outline = locate_center(matrix, settings)

        assert outline is not None
        self.assertEqual(outline.region_points, matrix.size)
        self.assertLessEqual(len(outline.edge_points), 800)


class StatisticsTests(unittest.TestCase):
    def test_rectangle_contains_the_exact_requested_number_of_cells(self):
        matrix = np.arange(100, dtype=float).reshape(10, 10)
        settings = PixelStatisticsSettings(
            region_mode="rectangle",
            scale_pixels=2,
            scale_length_mm=1,
            rectangle_height_mm=2,
            rectangle_width_mm=3,
            center_mode="geometric",
        )

        result = analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

        self.assertEqual(result.sample_count, 24)
        self.assertIn("矩形 2×3 mm", result.region_description)
        self.assertIn("实际 4×6 网格", result.region_description)
        self.assertEqual((result.center_row, result.center_col), (5.5, 5.5))

    def test_rectangle_rejects_a_scaled_range_larger_than_the_data(self):
        matrix = np.ones((10, 10), dtype=float)
        settings = PixelStatisticsSettings(
            region_mode="rectangle",
            scale_pixels=10000,
            scale_length_mm=1,
            rectangle_height_mm=1,
            rectangle_width_mm=1,
        )

        with self.assertRaisesRegex(
            PixelStatisticsError,
            r"换算为 10000×10000 网格.*当前数据为 10×10 网格",
        ):
            analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

    def test_circle_rejects_a_scaled_radius_beyond_the_data_boundary(self):
        matrix = np.ones((10, 10), dtype=float)
        settings = PixelStatisticsSettings(
            region_mode="circle",
            scale_pixels=10000,
            scale_length_mm=1,
            radius_mm=1,
        )

        with self.assertRaisesRegex(PixelStatisticsError, "圆形统计范围无法完整容纳"):
            analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

    def test_merge_automatically_converts_the_scale_to_processed_grid(self):
        matrix = np.arange(400, dtype=float).reshape(20, 20)
        settings = PixelStatisticsSettings(
            merge_enabled=True,
            granularity=2,
            force_odd_grid=False,
            scale_pixels=2,
            scale_length_mm=1,
            region_mode="rectangle",
            rectangle_height_mm=4,
            rectangle_width_mm=6,
        )

        result = analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

        self.assertEqual(result.processed_pixels_per_mm, 1.0)
        self.assertEqual(result.sample_count, 24)

    def test_decimal_scale_values_are_used_without_integer_rounding(self):
        matrix = np.arange(100, dtype=float).reshape(10, 10)
        settings = PixelStatisticsSettings(
            scale_pixels=1.25,
            scale_length_mm=0.5,
            region_mode="rectangle",
            rectangle_height_mm=2,
            rectangle_width_mm=2,
        )

        result = analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

        self.assertEqual(result.raw_pixels_per_mm, 2.5)
        self.assertEqual(result.processed_pixels_per_mm, 2.5)
        self.assertEqual(result.sample_count, 25)

    def test_statistics_keep_relative_standard_deviations_only(self):
        matrix = np.array([[1.0, 2.0], [3.0, 4.0]])
        settings = PixelStatisticsSettings(region_mode="full")

        result = analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

        self.assertAlmostEqual(result.mean, 2.5)
        self.assertEqual(result.minimum, 1.0)
        self.assertEqual(result.maximum, 4.0)
        assert result.min_max_ratio is not None
        assert result.contrast_ratio is not None
        assert result.extreme_mean_deviation_ratio is not None
        assert result.relative_population_std is not None
        assert result.relative_sample_std is not None
        self.assertAlmostEqual(result.min_max_ratio, 0.25)
        self.assertAlmostEqual(result.contrast_ratio, 0.6)
        self.assertAlmostEqual(result.extreme_mean_deviation_ratio, 0.6)
        self.assertAlmostEqual(result.relative_population_std, float(np.std(matrix, ddof=0)) / 2.5)
        self.assertAlmostEqual(result.relative_sample_std, float(np.std(matrix, ddof=1)) / 2.5)
        summary = result.summary_row()
        self.assertNotIn("总体标准差", summary)
        self.assertNotIn("样本标准差", summary)

    def test_extreme_mean_deviation_uses_the_larger_absolute_deviation(self):
        matrix = np.array([[1.0, 9.0, 10.0]])
        result = analyze_matrix(
            matrix,
            "sample.xlsx",
            "Sheet1",
            PixelStatisticsSettings(region_mode="full"),
        )

        assert result.extreme_mean_deviation_ratio is not None
        self.assertAlmostEqual(result.extreme_mean_deviation_ratio, 0.85)

    def test_extreme_mean_deviation_ratio_is_undefined_when_mean_is_zero(self):
        matrix = np.array([[-1.0, 1.0]])
        result = analyze_matrix(
            matrix,
            "sample.xlsx",
            "Sheet1",
            PixelStatisticsSettings(region_mode="full"),
        )

        self.assertIsNone(result.extreme_mean_deviation_ratio)
        self.assertEqual(result.summary_row(formatted=True)["极值最大偏差/平均值"], "—")

    def test_matrix_uniformity_samples_each_cell_center_and_calculates_statistics(self):
        matrix = np.block(
            [
                [np.ones((4, 4)), np.full((4, 4), 2.0)],
                [np.full((4, 4), 3.0), np.full((4, 4), 4.0)],
            ]
        )
        settings = PixelStatisticsSettings(
            region_mode="full",
            matrix_uniformity_enabled=True,
            matrix_rows=2,
            matrix_cols=2,
            matrix_sample_side_mm=2,
        )

        result = analyze_matrix(matrix, "sample.xlsx", "Sheet1", settings)

        uniformity = result.matrix_uniformity
        assert uniformity is not None
        np.testing.assert_allclose(uniformity.sample_values, [[1.0, 2.0], [3.0, 4.0]])
        self.assertEqual(uniformity.sample_count, 4)
        self.assertAlmostEqual(uniformity.mean, 2.5)
        self.assertEqual(uniformity.minimum, 1.0)
        self.assertEqual(uniformity.maximum, 4.0)
        assert uniformity.min_max_ratio is not None
        assert uniformity.contrast_ratio is not None
        assert uniformity.extreme_mean_deviation_ratio is not None
        assert uniformity.relative_population_std is not None
        assert uniformity.relative_sample_std is not None
        self.assertAlmostEqual(uniformity.min_max_ratio, 0.25)
        self.assertAlmostEqual(uniformity.contrast_ratio, 0.6)
        self.assertAlmostEqual(uniformity.extreme_mean_deviation_ratio, 0.6)
        self.assertAlmostEqual(
            uniformity.relative_population_std,
            float(np.std([1, 2, 3, 4])) / 2.5,
        )
        self.assertAlmostEqual(
            uniformity.relative_sample_std,
            float(np.std([1, 2, 3, 4], ddof=1)) / 2.5,
        )
        self.assertEqual(result.summary_row()["矩阵划分"], "2×2")

    def test_matrix_uniformity_is_rejected_for_a_circle(self):
        settings = PixelStatisticsSettings(
            region_mode="circle",
            matrix_uniformity_enabled=True,
        )

        with self.assertRaisesRegex(PixelStatisticsError, "圆形统计区域"):
            settings.validate()

    def test_workbook_analysis_and_export_include_summary(self):
        source = _ExcelBytesBuffer()
        with pd.ExcelWriter(source, engine="openpyxl") as writer:
            pd.DataFrame([[1, 2], [3, 4]]).to_excel(writer, sheet_name="像素", index=False, header=False)
        settings = PixelStatisticsSettings(region_mode="full")

        batch = analyze_uploaded_files({"输入.xlsx": source.getvalue()}, settings)
        exported = build_excel_report(batch)

        self.assertEqual(len(batch.results), 1)
        self.assertEqual(batch.errors, [])
        workbook = pd.ExcelFile(io.BytesIO(exported))
        self.assertEqual(workbook.sheet_names, ["统计汇总", "处理参数"])
        summary = pd.read_excel(io.BytesIO(exported), sheet_name="统计汇总")
        self.assertEqual(summary.loc[0, "工作表"], "像素")
        self.assertEqual(summary.loc[0, "平均值"], 2.5)
        self.assertEqual(summary.loc[0, "极值最大偏差/平均值"], 0.6)
        self.assertNotIn("总体标准差", summary.columns)
        self.assertNotIn("样本标准差", summary.columns)

    def test_matrix_uniformity_values_are_exported_to_a_separate_sheet(self):
        source = _ExcelBytesBuffer()
        with pd.ExcelWriter(source, engine="openpyxl") as writer:
            pd.DataFrame(np.arange(64).reshape(8, 8)).to_excel(
                writer, sheet_name="像素", index=False, header=False
            )
        settings = PixelStatisticsSettings(
            region_mode="full",
            matrix_uniformity_enabled=True,
            matrix_rows=2,
            matrix_cols=2,
            matrix_sample_side_mm=2,
        )

        batch = analyze_uploaded_files({"输入.xlsx": source.getvalue()}, settings)
        exported = build_excel_report(batch)
        workbook = pd.ExcelFile(io.BytesIO(exported))

        self.assertEqual(workbook.sheet_names, ["统计汇总", "处理参数", "矩阵_输入_像素"])
        exported_matrix = pd.read_excel(
            io.BytesIO(exported), sheet_name="矩阵_输入_像素", header=None
        )
        self.assertEqual(exported_matrix.shape, (2, 2))

    def test_csv_with_bom_and_semicolon_is_analyzed_as_one_sheet(self):
        content = "\ufeff1;2\n3;4\n".encode("utf-8")
        settings = PixelStatisticsSettings(region_mode="full")

        batch = analyze_uploaded_files({"输入.csv": content}, settings)

        self.assertEqual(batch.errors, [])
        self.assertEqual(len(batch.results), 1)
        self.assertEqual(batch.results[0].sheet_name, "数据")
        self.assertEqual(batch.results[0].mean, 2.5)


class UploadStateTests(unittest.TestCase):
    def test_formula_columns_have_hover_definitions_in_the_header_slot(self):
        columns = _result_table_columns(["平均值", "最小/最大", "极值最大偏差/平均值"])
        table = ui.table(columns=columns, rows=[])

        _add_formula_header_tooltips(table)

        self.assertEqual(columns[0]["tooltip"], "")
        self.assertIn("最小值 ÷ 最大值", columns[1]["tooltip"])
        self.assertIn("max(|最大值 - 平均值|", columns[2]["tooltip"])
        self.assertIn("header-cell", table.slots)
        template = table.slots["header-cell"].template
        assert template is not None
        self.assertIn("q-tooltip", template)

    def test_more_than_six_outline_charts_are_appended_one_row_at_a_time(self):
        matrix = np.ones((5, 5), dtype=float)
        settings = PixelStatisticsSettings(
            center_mode="threshold",
            threshold_percent=50,
            region_mode="full",
        )
        result = analyze_matrix(matrix, "输入.csv", "数据", settings)
        tool = PixelStatisticsTool()
        tool.batch = AnalysisBatch(results=[result] * 7, errors=[], settings=settings)
        dialog = ui.dialog()

        tool.show(dialog)

        self.assertEqual(tool.outline_rendered_count, 6)
        scroll_event = MagicMock(spec=ScrollEventArguments)
        scroll_event.vertical_percentage = 0.95
        tool._handle_outline_scroll(scroll_event)
        self.assertEqual(tool.outline_rendered_count, 7)

    def test_refreshable_sections_are_registered_to_the_tool_instance(self):
        tool = PixelStatisticsTool()
        dialog = ui.dialog()

        tool.show(dialog)

        file_targets = tool.render_file_list.targets
        result_targets = tool.render_results.targets
        self.assertTrue(any(target.instance is tool for target in file_targets))
        self.assertTrue(any(target.instance is tool for target in result_targets))

    def test_custom_remove_clears_analysis_and_uploader_file(self):
        tool = PixelStatisticsTool()
        tool.uploaded_files = {"输入.csv": b"1,2"}
        client = MagicMock()
        tool.upload_control = SimpleNamespace(id=17, client=client)
        tool.render_file_list = MagicMock()
        tool.render_results = MagicMock()

        tool._remove_file("输入.csv")

        self.assertEqual(tool.uploaded_files, {})
        script = client.run_javascript.call_args.args[0]
        self.assertIn("removeFile", script)
        self.assertIn("输入.csv", script)

    def test_reset_clears_the_upload_control(self):
        tool = PixelStatisticsTool()
        tool.uploaded_files = {"输入.csv": b"1,2"}
        tool.upload_control = MagicMock()
        tool.render_file_list = MagicMock()
        tool.render_results = MagicMock()

        tool._reset()

        tool.upload_control.reset.assert_called_once_with()
        self.assertEqual(tool.uploaded_files, {})


if __name__ == "__main__":
    unittest.main()
