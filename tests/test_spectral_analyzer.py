"""光谱分析界面配置的回归测试。"""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.spectral_analysis import (  # noqa: E402
    analyze_cct_reference,
    analyze_spectral_text,
    parse_chromaticity_text,
    spectral_example_text,
)
from src.tools.spectral_analyzer import (  # noqa: E402
    _chromaticity_chart_options,
    _cie_pointer_visibility_js,
    _comparison_source_options,
    _coordinate_summary_rows,
    _cri_pair_chromaticity_chart_options,
    _cri_value_rows,
    _default_series_styles,
    _option_text,
    _series_style,
    _spectrum_group_key,
    _spectrum_chart_options,
    _spectrum_reference_options,
    _spectrum_summary_rows,
)


class SpectralAnalyzerOptionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spectra = analyze_spectral_text(spectral_example_text())
        cls.coordinates = parse_chromaticity_text(
            "D65\t0.3127\t0.3290\n标准A光源\t0.44757\t0.40745",
            "xy",
        )

    def test_option_text_normalizes_none_and_rejects_unknown_value(self):
        self.assertEqual(_option_text(None), "")
        self.assertEqual(_option_text(" xy ", {"xy", "uv"}, "uv"), "xy")
        self.assertEqual(_option_text("错误", {"xy", "uv"}, "uv"), "uv")

    def test_cie_pointer_visibility_handler_only_hides_for_scatter_points(self):
        hide_handler = _cie_pointer_visibility_js(42, visible=False, scatter_only=True)
        restore_handler = _cie_pointer_visibility_js(42, visible=True, scatter_only=False)
        self.assertIn("seriesType !== 'scatter'", hide_handler)
        self.assertIn("getElement(42)", hide_handler)
        self.assertIn("opacity: 0", hide_handler)
        self.assertIn("show: false", hide_handler)
        self.assertIn("type: 'showTip'", hide_handler)
        self.assertIn("seriesIndex: params.seriesIndex", hide_handler)
        self.assertIn("dataIndex: params.dataIndex", hide_handler)
        self.assertNotIn("seriesType !== 'scatter'", restore_handler)
        self.assertIn("opacity: 0.8", restore_handler)
        self.assertIn("show: true", restore_handler)
        self.assertIn("type: 'hideTip'", restore_handler)

    def test_title_keywords_share_symbols_and_allow_manual_override(self):
        self.assertEqual(_spectrum_group_key("NBI 模式 1"), "模式")
        self.assertEqual(_spectrum_group_key("NBI-plus 模式"), "模式")
        self.assertEqual(_spectrum_group_key("导光束入射"), "导光束")
        self.assertEqual(_spectrum_group_key("红光"), "光")
        self.assertEqual(_spectrum_group_key("未知类型"), "未知类型")
        grouped_styles = _default_series_styles(
            [
                replace(self.spectra[0], name="WLI模式"),
                replace(self.spectra[0], name="ACI模式"),
                replace(self.spectra[0], name="红光"),
                replace(self.spectra[0], name="蓝光"),
            ]
        )
        self.assertEqual(grouped_styles["WLI模式"]["symbol"], grouped_styles["ACI模式"]["symbol"])
        self.assertEqual(grouped_styles["红光"]["symbol"], grouped_styles["蓝光"]["symbol"])
        self.assertNotEqual(grouped_styles["WLI模式"]["symbol"], grouped_styles["红光"]["symbol"])
        styles = _default_series_styles(self.spectra)
        styles[self.spectra[0].name]["symbol"] = "diamond"
        styles[self.spectra[0].name]["color"] = "#123456"
        self.assertEqual(_series_style(self.spectra[0].name, styles, 0), ("diamond", "#123456"))

    def test_spectrum_chart_supports_normalized_and_original_values(self):
        normalized = _spectrum_chart_options(self.spectra, normalized=True)
        original = _spectrum_chart_options(self.spectra, normalized=False)
        self.assertEqual(len(normalized["series"]), 2)
        self.assertEqual(normalized["yAxis"]["name"], "相对强度")
        self.assertEqual(original["yAxis"]["name"], "输入值")
        self.assertAlmostEqual(max(point[1] for point in normalized["series"][0]["data"]), 1.0)
        self.assertEqual(normalized["xAxis"]["min"], 380)
        self.assertEqual(normalized["xAxis"]["max"], 780)
        slider = normalized["dataZoom"][1]
        axis_name_offset = normalized["grid"]["bottom"] - normalized["xAxis"]["nameGap"]
        slider_top_offset = slider["bottom"] + slider["height"]
        self.assertGreater(axis_name_offset, slider_top_offset + 20)
        self.assertTrue(
            all(380 <= point[0] <= 780 for point in normalized["series"][0]["data"])
        )
        reference = analyze_cct_reference(self.spectra[0].cct or 6500)
        with_reference = _spectrum_chart_options(self.spectra, True, reference)
        self.assertEqual(len(with_reference["series"]), 3)
        self.assertEqual(with_reference["series"][-1]["lineStyle"]["type"], "dashed")
        custom_interval = _spectrum_chart_options(
            self.spectra,
            True,
            None,
            _default_series_styles(self.spectra),
            25,
            0.2,
        )
        self.assertNotIn("interval", custom_interval["xAxis"])
        self.assertNotIn("interval", custom_interval["yAxis"])
        self.assertEqual(custom_interval["xAxis"]["splitNumber"], 16)
        self.assertEqual(custom_interval["yAxis"]["splitNumber"], 5)

    def test_cri_values_are_exposed_as_rows_instead_of_bar_series(self):
        rows = _cri_value_rows(self.spectra)
        self.assertEqual(len(rows), 2)
        self.assertIn("ra", rows[0])
        self.assertIn("r15", rows[0])
        self.assertIn("rf", rows[0])

    def test_chromaticity_chart_contains_loci_and_all_points(self):
        xy_options = _chromaticity_chart_options(
            spectrum_results=self.spectra,
            coordinate_results=self.coordinates,
            coordinate_system="xy",
        )
        upvp_options = _chromaticity_chart_options(
            coordinate_results=self.coordinates,
            coordinate_system="upvp",
        )
        self.assertEqual(len(xy_options["series"]), 7)
        self.assertEqual(xy_options["xAxis"]["name"], "x")
        self.assertEqual(len(upvp_options["series"]), 5)
        self.assertEqual(upvp_options["xAxis"]["name"], "u′")
        self.assertEqual(xy_options["series"][0]["name"], "色度背景")
        self.assertEqual(xy_options["series"][0]["type"], "custom")
        self.assertIn(":renderItem", xy_options["series"][0])
        legend_names = [
            item if isinstance(item, str) else item["name"]
            for item in xy_options["legend"]["data"]
        ]
        self.assertIn("D65", legend_names)
        self.assertEqual(xy_options["legend"]["type"], "plain")
        self.assertEqual(xy_options["series"][2]["lineStyle"]["color"], "#111827")
        self.assertEqual(xy_options["series"][2]["z"], 4)
        self.assertEqual(xy_options["dataZoom"][0]["type"], "inside")
        self.assertEqual(len(xy_options["dataZoom"]), 2)
        self.assertTrue(xy_options["dataZoom"][0]["moveOnMouseMove"])
        self.assertEqual(xy_options["dataZoom"][0]["xAxisIndex"], [0])
        self.assertTrue(xy_options["dataZoom"][1]["moveOnMouseMove"])
        self.assertEqual(xy_options["dataZoom"][1]["yAxisIndex"], [0])
        self.assertEqual(xy_options["xAxis"]["max"], xy_options["yAxis"]["max"])
        self.assertTrue(xy_options["xAxis"]["splitLine"]["show"])
        self.assertTrue(xy_options["yAxis"]["splitLine"]["show"])
        self.assertEqual(xy_options["xAxis"]["z"], 1)
        self.assertIn("rgba", xy_options["xAxis"]["splitLine"]["lineStyle"]["color"])
        self.assertEqual(xy_options["xAxis"]["axisLabel"]["fontSize"], 15)
        self.assertEqual(xy_options["yAxis"]["axisLabel"]["fontSize"], 15)
        self.assertEqual(xy_options["xAxis"]["nameTextStyle"]["fontSize"], 18)
        self.assertEqual(xy_options["yAxis"]["nameTextStyle"]["fontSize"], 18)
        self.assertNotIn("interval", xy_options["xAxis"])
        self.assertEqual(xy_options["xAxis"]["splitNumber"], 9)
        self.assertEqual(xy_options["grid"]["left"], xy_options["grid"]["top"])
        self.assertEqual(xy_options["series"][3]["symbol"], "circle")
        self.assertEqual(xy_options["series"][-1]["symbol"], "triangle")
        self.assertFalse(xy_options["series"][3]["label"]["show"])
        self.assertIn(":formatter", xy_options["tooltip"])
        self.assertEqual(xy_options["tooltip"]["trigger"], "axis")
        self.assertEqual(xy_options["tooltip"]["axisPointer"]["type"], "cross")
        self.assertFalse(xy_options["tooltip"]["axisPointer"]["snap"])
        self.assertEqual(xy_options["tooltip"]["axisPointer"]["label"]["precision"], 6)
        self.assertEqual(xy_options["tooltip"]["axisPointer"]["label"]["fontSize"], 14)
        self.assertIn("Array.isArray", xy_options["tooltip"][":formatter"])

        standard_point = replace(self.coordinates[0], name="CIE D65 标准点")
        with_standards_and_isotherms = _chromaticity_chart_options(
            spectrum_results=self.spectra,
            standard_illuminant_results=[standard_point],
            coordinate_system="xy",
            show_isotherms=True,
        )
        standard_series = with_standards_and_isotherms["series"][-1]
        self.assertEqual(standard_series["symbol"], "diamond")
        standard_legend = next(
            item
            for item in with_standards_and_isotherms["legend"]["data"]
            if isinstance(item, dict) and item["name"] == standard_point.name
        )
        self.assertEqual(standard_legend["icon"], "diamond")
        self.assertTrue(
            any(item["name"].endswith("K 等色温线") for item in with_standards_and_isotherms["series"])
        )
        isotherm_series = [
            item
            for item in with_standards_and_isotherms["series"]
            if item["name"].endswith("K 等色温线")
        ]
        self.assertTrue(all(item["endLabel"]["show"] for item in isotherm_series))
        self.assertTrue(all(item["endLabel"]["formatter"].endswith(" K") for item in isotherm_series))
        self.assertTrue(all(item["labelLayout"]["hideOverlap"] is False for item in isotherm_series))
        self.assertTrue(
            all(
                item["z"] < with_standards_and_isotherms["series"][2]["z"]
                for item in isotherm_series
            )
        )

    def test_cri_chromaticity_chart_compares_two_sources_and_all_samples(self):
        options = _cri_pair_chromaticity_chart_options(
            self.spectra[0],
            self.spectra[1],
            coordinate_system="xy",
        )
        first_series = next(item for item in options["series"] if item["name"] == "D65")
        second_series = next(
            item for item in options["series"] if item["name"] == "标准A光源"
        )
        pair_lines = [item for item in options["series"] if item["name"].endswith("对应关系")]
        self.assertEqual(len(first_series["data"]), 16)
        self.assertEqual(len(second_series["data"]), 16)
        self.assertEqual(len(pair_lines), 15)
        self.assertEqual(first_series["data"][9]["name"], "R9")
        self.assertFalse(first_series["label"]["show"])
        self.assertTrue(options["xAxis"]["splitLine"]["show"])
        self.assertTrue(options["yAxis"]["splitLine"]["show"])
        self.assertEqual(options["tooltip"]["axisPointer"]["type"], "cross")
        self.assertEqual(options["xAxis"]["axisLabel"]["fontSize"], 15)
        self.assertEqual(options["yAxis"]["nameTextStyle"]["fontSize"], 18)

    def test_comparison_options_include_inputs_and_builtin_standards(self):
        options = _comparison_source_options(self.spectra)
        self.assertEqual(options["input:0"], "输入光谱 · D65")
        self.assertIn("standard:D65", options)
        self.assertIn("standard:LED-B3", options)
        self.assertIn("reference:0", options)
        reference_options = _spectrum_reference_options(self.spectra)
        self.assertIn("none", reference_options)
        self.assertIn("reference:0", reference_options)

    def test_summary_rows_expose_engineering_metrics(self):
        spectrum_row = _spectrum_summary_rows(self.spectra)[0]
        coordinate_row = _coordinate_summary_rows(self.coordinates)[0]
        self.assertEqual(spectrum_row["name"], "D65")
        self.assertIn("r15", spectrum_row)
        self.assertIn("rf", spectrum_row)
        self.assertEqual(coordinate_row["Y"], "100.0000")
        self.assertIn("duv", coordinate_row)


if __name__ == "__main__":
    unittest.main()
