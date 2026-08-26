"""光谱分析界面配置的回归测试。"""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.spectral_analysis import (  # noqa: E402
    SpectrumChromaticityResult,
    analyze_cct_reference,
    analyze_spectral_text,
    parse_chromaticity_text,
    spectral_example_text,
)
from src.tools.spectral_analyzer import (  # noqa: E402
    _chromaticity_chart_options,
    _chromaticity_result_key,
    _cie_clicked_point_series,
    _cie_interaction_setup_js,
    _comparison_source_options,
    _coordinate_summary_rows,
    _cri_pair_chromaticity_chart_options,
    _cri_value_rows,
    _default_series_styles,
    _mixing_graph_details,
    _mixing_node_options,
    _mixing_nodes_and_active_ids,
    _option_text,
    _series_style,
    _sdcm_key,
    _sdcm_orders,
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
        self.assertEqual(_sdcm_orders([5, "3", 5, 2, None]), (3, 5))

    def test_cie_interaction_uses_pixel_level_crosshair_and_emits_clicked_coordinate(self):
        xy_handler = _cie_interaction_setup_js(42, "xy")
        upvp_handler = _cie_interaction_setup_js(43, "upvp")
        self.assertIn("getElement(42)", xy_handler)
        self.assertIn("requestAnimationFrame", xy_handler)
        self.assertIn("getZr().on('mousemove'", xy_handler)
        self.assertIn("getZr().on('click'", xy_handler)
        self.assertIn("convertFromPixel", xy_handler)
        self.assertIn("toFixed(6)", xy_handler)
        self.assertIn("emit({first:", xy_handler)
        self.assertIn("x: ", xy_handler)
        self.assertIn("u′: ", upvp_handler)

    def test_cie_clicked_point_marker_only_shows_meaningful_cct(self):
        d65_marker = _cie_clicked_point_series(0.3127, 0.3290, "xy")
        far_marker = _cie_clicked_point_series(0.2, 0.7, "xy")
        self.assertEqual(d65_marker["id"], "cie-click-marker")
        self.assertEqual(d65_marker["data"], [[0.3127, 0.329]])
        self.assertIn("CCT:", d65_marker["label"]["formatter"])
        self.assertNotIn("CCT:", far_marker["label"]["formatter"])

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
        self.assertEqual(normalized["yAxis"]["nameLocation"], "middle")
        self.assertEqual(normalized["yAxis"]["nameRotate"], 90)
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
        hidden = _spectrum_chart_options(
            self.spectra,
            hidden_series_names={self.spectra[0].name},
            compact_layout=True,
        )
        self.assertFalse(hidden["legend"]["selected"][self.spectra[0].name])
        self.assertTrue(hidden["legend"]["selected"][self.spectra[1].name])
        self.assertEqual(hidden["legend"]["type"], "scroll")
        self.assertLess(hidden["grid"]["top"], normalized["grid"]["top"])

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
        compact_options = _chromaticity_chart_options(
            spectrum_results=self.spectra,
            coordinate_system="xy",
            compact=True,
        )
        self.assertEqual(compact_options["grid"]["width"], compact_options["grid"]["height"])
        self.assertEqual(compact_options["legend"]["type"], "plain")
        self.assertEqual(compact_options["legend"]["orient"], "vertical")
        self.assertTrue(str(compact_options["legend"]["left"]).endswith("%"))
        self.assertEqual(compact_options["legend"]["textStyle"]["overflow"], "breakAll")
        self.assertNotIn("ellipsis", compact_options["legend"]["textStyle"])
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
        self.assertEqual(xy_options["tooltip"]["trigger"], "item")
        self.assertEqual(xy_options["tooltip"]["triggerOn"], "mousemove")
        self.assertNotIn("axisPointer", xy_options["tooltip"])
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

    def test_chromaticity_chart_adds_independent_sdcm_ellipses(self):
        options = _chromaticity_chart_options(
            spectrum_results=self.spectra,
            coordinate_results=self.coordinates,
            coordinate_system="xy",
            sdcm_orders={
                _sdcm_key("spectrum", "D65"): [3, 5],
                _sdcm_key("coordinate", "D65"): [1],
            },
        )
        ellipse_series = [item for item in options["series"] if "SDCM" in item["name"]]
        self.assertEqual(
            [item["name"] for item in ellipse_series],
            ["D65 · 3 SDCM", "D65 · 5 SDCM", "D65 · 1 SDCM"],
        )
        self.assertTrue(all(item["type"] == "line" for item in ellipse_series))
        self.assertTrue(all(item["silent"] for item in ellipse_series))
        self.assertTrue(all(len(item["data"]) == 121 for item in ellipse_series))
        self.assertTrue(all(item["endLabel"]["show"] for item in ellipse_series))
        self.assertEqual(ellipse_series[0]["endLabel"]["formatter"], "3 SDCM")
        self.assertTrue(all(item["id"].startswith("sdcm:") for item in ellipse_series))
        legend_names = [
            item if isinstance(item, str) else item["name"]
            for item in options["legend"]["data"]
        ]
        self.assertNotIn("D65 · 3 SDCM", legend_names)

        upvp_options = _chromaticity_chart_options(
            spectrum_results=[self.spectra[0]],
            coordinate_system="upvp",
            sdcm_orders={_sdcm_key("spectrum", "D65"): [5]},
        )
        upvp_ellipse = next(item for item in upvp_options["series"] if "SDCM" in item["name"])
        self.assertEqual(upvp_ellipse["name"], "D65 · 5 SDCM")

    def test_chromaticity_chart_connects_multiple_sources_to_one_target(self):
        target_key = _chromaticity_result_key("spectrum", "D65")
        spectrum_source_key = _chromaticity_result_key("spectrum", "标准A光源")
        coordinate_source_key = _chromaticity_result_key("coordinate", "D65")
        options = _chromaticity_chart_options(
            spectrum_results=self.spectra,
            coordinate_results=self.coordinates,
            coordinate_system="xy",
            connection_target=target_key,
            connection_sources=[spectrum_source_key, coordinate_source_key, target_key],
        )
        connections = [
            item for item in options["series"] if str(item.get("id", "")).startswith("coordinate-connection:")
        ]
        self.assertEqual([item["name"] for item in connections], ["标准A光源 → D65", "D65 → D65"])
        self.assertEqual(connections[0]["data"][0], list(self.spectra[1].xy))
        self.assertEqual(connections[0]["data"][1], list(self.spectra[0].xy))
        self.assertEqual(connections[1]["data"][0], list(self.coordinates[0].xy))
        self.assertEqual(connections[1]["data"][1], list(self.spectra[0].xy))
        legend_names = [
            item if isinstance(item, str) else item["name"]
            for item in options["legend"]["data"]
        ]
        self.assertNotIn("标准A光源 → D65", legend_names)

        upvp_options = _chromaticity_chart_options(
            spectrum_results=self.spectra,
            coordinate_system="upvp",
            connection_target=target_key,
            connection_sources=[spectrum_source_key],
        )
        upvp_connection = next(
            item
            for item in upvp_options["series"]
            if str(item.get("id", "")).startswith("coordinate-connection:")
        )
        self.assertEqual(upvp_connection["data"], [list(self.spectra[1].upvp), list(self.spectra[0].upvp)])

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
        self.assertEqual(options["tooltip"]["trigger"], "item")
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

    def test_mixing_graph_supports_parallel_and_multilevel_combinations(self):
        sources = [replace(self.spectra[index % 2], name=f"光谱 {index + 1}") for index in range(6)]
        steps = [
            {
                "id": "mix:1",
                "name": "混合 1",
                "first_id": "source:0",
                "second_id": "source:1",
                "ratio": 25,
            },
            {
                "id": "mix:2",
                "name": "混合 2",
                "first_id": "source:2",
                "second_id": "source:3",
                "ratio": 60,
            },
            {
                "id": "mix:3",
                "name": "混合 3",
                "first_id": "mix:1",
                "second_id": "mix:2",
                "ratio": 40,
            },
        ]
        nodes, active_ids = _mixing_nodes_and_active_ids(sources, steps)
        self.assertEqual(active_ids, ["source:4", "source:5", "mix:3"])
        self.assertIn("mix:1", nodes)
        self.assertIn("mix:2", nodes)
        self.assertIn("mix:3", nodes)
        mixed_result = nodes["mix:3"]
        self.assertIsInstance(mixed_result, SpectrumChromaticityResult)
        assert isinstance(mixed_result, SpectrumChromaticityResult)
        self.assertGreater(mixed_result.xy[0], 0)
        options = _mixing_node_options(nodes, active_ids)
        self.assertTrue(options["source:4"].startswith("原始 ·"))
        self.assertEqual(options["mix:3"], "混合 3")
        spectrum_options = _spectrum_chart_options([mixed_result], normalized=True)
        mixing_results = [nodes[f"mix:{index}"] for index in range(1, 4)]
        mixing_styles = _default_series_styles(mixing_results)
        self.assertEqual(
            {mixing_styles[item.name]["symbol"] for item in mixing_results},
            {mixing_styles[mixing_results[0].name]["symbol"]},
        )
        cie_options = _chromaticity_chart_options(
            coordinate_results=[mixed_result],
            coordinate_system="xy",
            series_styles=mixing_styles,
        )
        self.assertGreater(len(spectrum_options["series"][0]["data"]), 300)
        self.assertEqual(cie_options["series"][-1]["name"], "混合 3")
        self.assertEqual(
            cie_options["legend"]["data"][-1]["icon"],
            mixing_styles["混合 3"]["symbol"],
        )

        _, detailed_active_ids, coefficients = _mixing_graph_details(sources, steps)
        self.assertEqual(detailed_active_ids, active_ids)
        self.assertEqual(len(coefficients["mix:3"]), len(sources))
        self.assertTrue(all(value > 0 for value in coefficients["mix:3"][:4]))
        self.assertEqual(coefficients["mix:3"][4:], (0.0, 0.0))
        self.assertAlmostEqual(
            coefficients["mix:1"][0] / coefficients["mix:1"][1],
            25 / 75,
            places=8,
        )

    def test_summary_rows_expose_engineering_metrics(self):
        spectrum_row = _spectrum_summary_rows(self.spectra)[0]
        coordinate_row = _coordinate_summary_rows(self.coordinates)[0]
        self.assertEqual(spectrum_row["name"], "D65")
        self.assertEqual(spectrum_row["peak_wavelength"], "460.0")
        self.assertIn("nm", spectrum_row["dominant_wavelength"])
        self.assertEqual(_spectrum_summary_rows(self.spectra)[1]["dominant_wavelength"], "583.5 nm")
        self.assertIn("r15", spectrum_row)
        self.assertIn("rf", spectrum_row)
        self.assertEqual(coordinate_row["Y"], "100.0000")
        self.assertIn("duv", coordinate_row)


if __name__ == "__main__":
    unittest.main()
