import io
import unittest
from typing import cast

import numpy as np
import pandas as pd
from nicegui import ui

from src.tools.intensity_distribution import (
    IntensitySettings, IntensityDistributionTool, analyze_intensity, analyze_files,
    _distribution_interaction_setup_js, distribution_chart, export_intensity,
    first_crossing, target_angle_rows,
)
from src.permission_catalog import TOOL_PERMISSIONS
from src.custom_ui import CustomUploadRemovedEventArguments


class IntensityTests(unittest.TestCase):
    def test_constant_intensity_is_recovered_from_planar_illuminance(self):
        offsets = np.arange(-2, 3) * 0.25
        x, y = np.meshgrid(offsets, offsets)
        # 光强 100 cd，距离 2 m，平面照度为 I*d/r³，表格按 0.2 系数存储。
        matrix = 100 * 2 / np.power(4 + x*x + y*y, 1.5) / 0.2
        settings = IntensitySettings(scale_pixels=1, scale_length_mm=250,
                                     distance_mm=2000, lux_factor=0.2)
        result = analyze_intensity(matrix, "test.csv", "数据", settings)
        np.testing.assert_allclose(result.intensity, 100)
        np.testing.assert_allclose(result.horizontal.values, 100)
        self.assertAlmostEqual(result.center_intensity, 100)
        self.assertIsNone(result.horizontal.negative_angle)
        self.assertIsNone(result.vertical.width)

    def test_fractional_center_is_interpolated_and_added_to_both_profiles(self):
        result = analyze_intensity(
            np.array([[1., 2.], [3., 4.]]), "a.csv", "数据",
            IntensitySettings(center_mode="geometric"),
        )
        self.assertEqual((result.center_row, result.center_col), (0.5, 0.5))
        self.assertEqual(result.center_intensity, 2.5)
        for profile in (result.horizontal, result.vertical):
            np.testing.assert_allclose(profile.values[profile.angles == 0], [2.5])

    def test_cell_merge_averages_blocks_and_converts_the_processed_scale(self):
        matrix = np.arange(1, 17, dtype=float).reshape(4, 4)
        settings = IntensitySettings(
            scale_pixels=2,
            scale_length_mm=1000,
            center_mode="geometric",
            merge_enabled=True,
            granularity=2,
        )

        result = analyze_intensity(matrix, "a.csv", "数据", settings)

        self.assertEqual(result.intensity.shape, (2, 2))
        self.assertEqual((result.source_rows, result.source_cols), (4, 4))
        self.assertIn("颗粒度 2×2", result.merge_description)
        self.assertAlmostEqual(result.center_intensity, 8.5)
        self.assertAlmostEqual(result.horizontal.angles[0], -np.degrees(np.arctan(0.5)))
        self.assertAlmostEqual(result.horizontal.angles[-1], np.degrees(np.arctan(0.5)))

    def test_cell_merge_rejects_a_result_too_small_for_profiles(self):
        with self.assertRaisesRegex(ValueError, "合并后仅剩"):
            analyze_intensity(
                np.ones((4, 4)),
                "a.csv",
                "数据",
                IntensitySettings(merge_enabled=True, granularity=3),
            )

    def test_first_downward_crossing_interpolates_and_does_not_follow_side_lobes(self):
        angle = first_crossing(np.array([0., 10., 20., 30.]), np.array([100., 60., 20., 90.]), 50)
        self.assertEqual(angle, 12.5)
        self.assertIsNone(first_crossing(np.array([0., 10.]), np.array([100., 90.]), 50))

    def test_known_asymmetric_beam_angles_and_width(self):
        # 中心行光强为 [0,40,100,80,0]，角度为 arctan([-2,-1,0,1,2])。
        angles = np.arctan(np.arange(-2, 3, dtype=float))
        lux = np.array([0., 40., 100., 80., 0.]) * np.cos(angles)**3
        matrix = np.tile(lux, (5, 1))
        result = analyze_intensity(
            matrix, "a.csv", "数据",
            IntensitySettings(scale_length_mm=1000, center_mode="maximum"),
        )
        assert result.horizontal.negative_angle is not None
        assert result.horizontal.positive_angle is not None
        assert result.horizontal.width is not None
        self.assertAlmostEqual(result.horizontal.negative_angle, -37.5)
        self.assertAlmostEqual(result.horizontal.positive_angle, 45 + (np.degrees(np.arctan(2))-45)*0.375)
        self.assertAlmostEqual(result.horizontal.width,
                               result.horizontal.positive_angle - result.horizontal.negative_angle)
        for polar in (True, False):
            chart = distribution_chart(result, polar)
            angle_points = [series for series in chart["series"] if series["type"] == "scatter"]
            self.assertEqual(len(angle_points[0]["data"]), 2)
            self.assertFalse(chart["tooltip"]["show"])
            angle_lines = [series for series in chart["series"]
                           if series.get("silent") and len(series["data"]) == 2]
            crossing_count = sum(
                angle is not None
                for profile in (result.horizontal, result.vertical)
                for angle in (profile.negative_angle, profile.positive_angle)
            )
            self.assertEqual(len(angle_lines), crossing_count)
            self.assertTrue(all(series["lineStyle"]["type"] == "dashed" for series in angle_lines))
            for angle_line in angle_lines:
                intensity_index = 0 if polar else 1
                angle_index = 1 if polar else 0
                self.assertEqual(angle_line["data"][0][intensity_index], 0)
                self.assertEqual(angle_line["data"][1][intensity_index], result.target_intensity)
                self.assertEqual(
                    angle_line["data"][0][angle_index],
                    angle_line["data"][1][angle_index],
                )
            self.assertTrue(all(series["name"] == "目标光强 50%" for series in angle_lines))
            self.assertTrue(all(series["name"] == "目标光强 50%" for series in angle_points))
            self.assertEqual(chart["legend"]["data"], ["水平", "垂直", "目标光强 50%"])

        two_targets = distribution_chart(result, target_percents=(50, 10))
        self.assertEqual(two_targets["legend"]["data"][-2:], ["目标光强 50%", "目标光强 10%"])
        self.assertEqual(len(target_angle_rows(result, (50, 10))), 2)
        no_target = distribution_chart(result, target_percents=(0,))
        self.assertEqual(no_target["legend"]["data"], ["水平", "垂直"])
        self.assertEqual(len(no_target["series"]), 2)
        self.assertEqual(len(target_angle_rows(result, (0, 10))), 1)
        interaction = _distribution_interaction_setup_js(
            1, False, "%", "strength_to_angle"
        )
        self.assertIn("legendselectchanged", interaction)
        self.assertIn("sideCrossing", interaction)
        self.assertIn("addCircle", interaction)
        self.assertIn("strength_to_angle", interaction)
        polar_interaction = _distribution_interaction_setup_js(
            2, True, "%", "angle_to_strength"
        )
        self.assertIn("polarIndex: 0", polar_interaction)
        self.assertIn("addPath", polar_interaction)
        with self.assertRaises(ValueError):
            _distribution_interaction_setup_js(1, False, "%", "unknown")

    def test_radiometric_conversion_and_normalized_half_polar_chart(self):
        result = analyze_intensity(
            np.full((3, 3), 2.0),
            "radiant.csv",
            "数据",
            IntensitySettings(lux_factor=None, irradiance_factor=0.5),
        )

        self.assertEqual(result.output_quantity, "辐射强度")
        self.assertEqual(result.output_unit, "mW/sr")
        self.assertEqual(result.center_intensity, 10_000)
        self.assertIn("中心辐射强度(mW/sr)", result.summary())
        chart = distribution_chart(result, polar=True, normalized=True)
        self.assertEqual(chart["angleAxis"]["min"], -90)
        self.assertEqual(chart["angleAxis"]["max"], 90)
        self.assertEqual(chart["angleAxis"]["startAngle"], 180)
        self.assertEqual(chart["angleAxis"]["endAngle"], 0)
        self.assertEqual(chart["title"]["subtext"], "相对中心(%)")
        self.assertEqual(chart["polar"]["radius"], "150%")
        self.assertEqual(chart["series"][-1]["data"][0][0], 50)
        self.assertEqual(chart["legend"]["data"][-1], "目标辐射强度 50%")

    def test_input_validation_and_center_modes(self):
        for settings in (IntensitySettings(distance_mm=0), IntensitySettings(lux_factor=float("nan")),
                         IntensitySettings(target_percent=101),
                         IntensitySettings(lux_factor=None),
                         IntensitySettings(lux_factor=1, irradiance_factor=1),
                         IntensitySettings(granularity=cast(int, 1.5))):
            with self.assertRaises(ValueError):
                settings.validate()
        for matrix in (np.ones((1, 5)), np.array([[1., -1.], [2., 3.]]), np.zeros((3, 3))):
            with self.assertRaises(ValueError):
                analyze_intensity(matrix, "a", "数据", IntensitySettings())
        matrix = np.ones((5, 5))
        matrix[2, 3] = 10
        for mode in ("manual", "maximum", "threshold"):
            result = analyze_intensity(matrix, "a", "数据", IntensitySettings(
                center_mode=mode, manual_row=3, manual_col=4, center_threshold_percent=90, target_percent=100))
            self.assertEqual((result.center_row, result.center_col), (2, 3))
            self.assertEqual(result.horizontal.width, 0)
        self.assertEqual(IntensitySettings().center_mode, "threshold")
        self.assertEqual(IntensitySettings().center_threshold_percent, 10)

    def test_batch_errors_and_export(self):
        batch = analyze_files({"good.csv": b"1,2\n3,4", "bad.csv": b"a,b\nc,d"}, IntensitySettings())
        self.assertEqual(len(batch.results), 1)
        self.assertEqual(len(batch.errors), 1)
        content = export_intensity(batch, (50, 10))
        report = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
        self.assertEqual(len(report), 6)
        self.assertIn("角度汇总", report)
        summary = pd.read_excel(io.BytesIO(content), sheet_name="角度汇总")
        self.assertEqual(summary["目标比例(%)"].tolist(), [50, 10])
        zero_disabled = pd.read_excel(
            io.BytesIO(export_intensity(batch, (0, 10))), sheet_name="角度汇总"
        )
        self.assertEqual(zero_disabled["目标比例(%)"].tolist(), [10])
        with self.assertRaises(ValueError):
            export_intensity(batch, (-1, 10))

    def test_ui_refresh_and_permission_registration(self):
        tool = IntensityDistributionTool()
        dialog = ui.dialog()
        tool.show(dialog)
        tool.batch = analyze_files({"a.csv": b"1,2\n3,4"}, IntensitySettings())
        tool.render_results.refresh()
        self.assertTrue(any(t.instance is tool for t in tool.render_results.targets))
        self.assertTrue(any(p.code == "tools.intensity_distribution.use" for p in TOOL_PERMISSIONS))
        with self.assertRaises(ValueError):
            tool._settings()  # 距离与照度系数必须由用户明确填写。

    def test_removing_upload_invalidates_results_and_preserves_other_files(self):
        tool = IntensityDistributionTool()
        dialog = ui.dialog()
        tool.show(dialog)
        tool.files = {"a.csv": b"1,2\n3,4", "b.csv": b"4,3\n2,1"}
        tool.batch = analyze_files(tool.files, IntensitySettings())
        revision = tool.revision
        event = CustomUploadRemovedEventArguments(
            sender=tool.uploader, client=tool.uploader.client,
            files=[{"name": "a.csv"}], clear_all=False,
        )
        tool._removed(event)
        self.assertEqual(list(tool.files), ["b.csv"])
        self.assertIsNone(tool.batch)
        self.assertGreater(tool.revision, revision)

    def test_parameter_edit_invalidates_results(self):
        tool = IntensityDistributionTool()
        tool.show(ui.dialog())
        tool.batch = analyze_files({"a.csv": b"1,2\n3,4"}, IntensitySettings())
        tool.pixels.set_value(25)
        self.assertIsNone(tool.batch)

    def test_conversion_factor_inputs_are_mutually_exclusive(self):
        tool = IntensityDistributionTool()
        tool.show(ui.dialog())
        tool.lux_factor.set_value(2)
        tool.irradiance_factor.set_value(3)

        self.assertIsNone(tool.lux_factor.value)
        self.assertEqual(tool.irradiance_factor.value, 3)


if __name__ == "__main__":
    unittest.main()
