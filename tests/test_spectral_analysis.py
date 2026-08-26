"""光谱与色坐标分析核心的回归测试。"""

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.spectral_analysis import (  # noqa: E402
    SpectralAnalysisError,
    SpectrumInput,
    analyze_spectrum_chromaticity,
    analyze_cct_reference,
    analyze_spectral_text,
    analyze_standard_illuminant,
    analyze_standard_illuminant_chromaticity,
    calculate_power_limited_mix,
    chromaticity_background_image,
    chromaticity_isotherms,
    chromaticity_loci,
    macadam_ellipse_points,
    mix_spectra_by_peak_ratio,
    pairwise_chromaticity_distances,
    parse_chromaticity_text,
    parse_spectral_text,
    spectral_example_text,
    solve_three_spectrum_mix,
)


class SpectralParsingTests(unittest.TestCase):
    def test_parse_multiple_spectra_supports_header_and_sorts_wavelengths(self):
        spectra = parse_spectral_text(
            "波长(nm)\t样品 A\t样品 B\n780\t0.1\t0.2\n360\t0.3\t0.4\n500\t0.8\t0.9"
        )
        self.assertEqual([item.name for item in spectra], ["样品 A", "样品 B"])
        self.assertEqual(spectra[0].wavelengths, (360.0, 500.0, 780.0))
        self.assertEqual(spectra[1].values, (0.4, 0.9, 0.2))

    def test_parse_without_header_assigns_names(self):
        spectra = parse_spectral_text("360,0.1,0.2\n780,0.3,0.4")
        self.assertEqual([item.name for item in spectra], ["光谱1", "光谱2"])

    def test_parse_rejects_duplicate_wavelength_negative_value_and_bad_columns(self):
        with self.assertRaisesRegex(SpectralAnalysisError, "重复"):
            parse_spectral_text("波长\t样品\n360\t1\n360\t2")
        with self.assertRaisesRegex(SpectralAnalysisError, "不能为负"):
            parse_spectral_text("波长\t样品\n360\t-1\n780\t2")
        with self.assertRaisesRegex(SpectralAnalysisError, "恰好包含"):
            parse_spectral_text("波长\t样品A\t样品B\n360\t1\n780\t2\t3")

    def test_parse_rejects_duplicate_names_and_all_zero_spectrum(self):
        with self.assertRaisesRegex(SpectralAnalysisError, "名称不能重复"):
            parse_spectral_text("波长\t样品\t样品\n360\t1\t2\n780\t2\t3")
        with self.assertRaisesRegex(SpectralAnalysisError, "不能全部为 0"):
            parse_spectral_text("波长\t样品\n360\t0\n780\t0")

    def test_parse_does_not_limit_spectrum_column_count(self):
        names = [f"样品{index}" for index in range(1, 16)]
        header = "波长\t" + "\t".join(names)
        values = "\t".join("1" for _ in names)
        spectra = parse_spectral_text(f"{header}\n360\t{values}\n780\t{values}")
        self.assertEqual(len(spectra), 15)
        self.assertEqual(spectra[-1].name, "样品15")


class SpectralCalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = analyze_spectral_text(spectral_example_text())

    def test_standard_illuminants_return_expected_metrics(self):
        d65, illuminant_a = self.results
        self.assertEqual(d65.name, "D65")
        self.assertAlmostEqual(d65.cct or 0, 6502.1, delta=1.0)
        self.assertAlmostEqual(d65.ra or 0, 100.0, delta=0.01)
        self.assertAlmostEqual(d65.rf or 0, 100.0, delta=0.02)
        self.assertEqual(len(d65.ri), 15)
        self.assertEqual(len(d65.cri_samples), 15)
        self.assertAlmostEqual(dict(d65.ri)[15], 100.0, delta=0.01)
        self.assertIn("CIE 日光参考源", d65.reference_name or "")
        self.assertAlmostEqual((d65.reference_xy or (0, 0))[0], d65.xy[0], places=4)
        self.assertAlmostEqual(illuminant_a.cct or 0, 2855.8, delta=1.0)
        self.assertAlmostEqual(illuminant_a.ra or 0, 100.0, delta=0.01)

    def test_result_normalizes_xyz_and_plot_values(self):
        d65 = self.results[0]
        self.assertAlmostEqual(d65.XYZ[1], 100.0)
        self.assertAlmostEqual(max(d65.normalized_values), 1.0)
        self.assertLess(d65.cri_reference_distance or 1, 5.4e-3)
        self.assertEqual(d65.peak_wavelength, 460.0)
        self.assertIsNotNone(d65.dominant_wavelength)
        self.assertIsNone(d65.complementary_wavelength)
        illuminant_a = self.results[1]
        self.assertAlmostEqual(illuminant_a.dominant_wavelength or 0, 583.5, delta=0.1)
        self.assertNotEqual(
            illuminant_a.dominant_wavelength,
            round(illuminant_a.dominant_wavelength or 0),
        )

    def test_monochromatic_spectrum_peak_and_dominant_wavelength_agree(self):
        result = analyze_spectrum_chromaticity(
            SpectrumInput(
                "555 nm 窄带",
                (360.0, 554.0, 555.0, 556.0, 780.0),
                (0.0, 0.0, 1.0, 0.0, 0.0),
            )
        )
        self.assertEqual(result.peak_wavelength, 555.0)
        self.assertAlmostEqual(result.dominant_wavelength or 0, 555.0, delta=0.6)
        self.assertIsNone(result.complementary_wavelength)

    def test_purple_spectrum_reports_complement_instead_of_dominant_wavelength(self):
        result = analyze_spectrum_chromaticity(
            SpectrumInput(
                "紫色双峰",
                (360.0, 449.0, 450.0, 451.0, 649.0, 650.0, 651.0, 780.0),
                (0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
            )
        )
        self.assertIsNone(result.dominant_wavelength)
        self.assertAlmostEqual(result.complementary_wavelength or 0, 566.0, delta=1.0)

    def test_calculation_requires_visible_range_coverage(self):
        with self.assertRaisesRegex(SpectralAnalysisError, "至少应覆盖"):
            analyze_spectral_text("波长\t样品\n400\t1\n780\t1")

    def test_macadam_ellipse_has_true_sdcm_scale_and_upvp_projection(self):
        center = (0.305, 0.323)
        one_sdcm = np.asarray(macadam_ellipse_points(center, 1), dtype=float)
        three_sdcm = np.asarray(macadam_ellipse_points(center, 3), dtype=float)
        upvp = np.asarray(macadam_ellipse_points(center, 1, "upvp"), dtype=float)

        self.assertEqual(one_sdcm.shape, (121, 2))
        np.testing.assert_allclose(one_sdcm[0], one_sdcm[-1], atol=1e-12)
        np.testing.assert_allclose(
            three_sdcm - np.asarray(center),
            3 * (one_sdcm - np.asarray(center)),
            atol=1e-12,
        )
        self.assertLess(float(np.max(np.linalg.norm(one_sdcm - np.asarray(center), axis=1))), 0.004)
        denominator = -2 * one_sdcm[:, 0] + 12 * one_sdcm[:, 1] + 3
        expected_upvp = np.column_stack(
            (4 * one_sdcm[:, 0] / denominator, 9 * one_sdcm[:, 1] / denominator)
        )
        np.testing.assert_allclose(upvp, expected_upvp, atol=1e-12)

    def test_macadam_ellipse_rejects_unsupported_order(self):
        with self.assertRaisesRegex(SpectralAnalysisError, "仅支持"):
            macadam_ellipse_points((0.3127, 0.3290), 2)

    def test_loci_are_available_for_both_diagrams(self):
        xy_spectral, xy_planckian = chromaticity_loci("xy")
        uv_spectral, uv_planckian = chromaticity_loci("upvp")
        self.assertGreater(len(xy_spectral), 400)
        self.assertGreater(len(xy_planckian), 200)
        self.assertEqual(len(xy_spectral), len(uv_spectral))
        self.assertEqual(len(xy_planckian), len(uv_planckian))

    def test_isotherms_are_available_for_both_diagrams(self):
        xy_isotherms = chromaticity_isotherms("xy")
        upvp_isotherms = chromaticity_isotherms("upvp")
        self.assertGreaterEqual(len(xy_isotherms), 8)
        self.assertEqual([item[0] for item in xy_isotherms], [item[0] for item in upvp_isotherms])
        self.assertTrue(all(len(item) == 3 for item in xy_isotherms))

    def test_continuous_color_background_is_available_for_both_diagrams(self):
        xy_image = chromaticity_background_image("xy")
        upvp_image = chromaticity_background_image("upvp")
        self.assertTrue(xy_image.startswith("data:image/png;base64,"))
        self.assertTrue(upvp_image.startswith("data:image/png;base64,"))
        self.assertGreater(len(xy_image), 50000)

    def test_builtin_standard_illuminant_provides_cri_samples(self):
        result = analyze_standard_illuminant("LED-B3")
        self.assertIn("LED-B3", result.name)
        self.assertEqual(len(result.cri_samples), 15)
        self.assertIsNotNone(result.ra)

    def test_builtin_standard_illuminant_chromaticity_uses_lightweight_result(self):
        result = analyze_standard_illuminant_chromaticity("D65")
        self.assertIn("D65", result.name)
        self.assertAlmostEqual(result.xy[0], 0.3127, places=4)
        self.assertAlmostEqual(result.upvp[1], 0.4683, places=4)

    def test_equal_cct_reference_matches_selected_source_temperature(self):
        source = self.results[0]
        reference = analyze_cct_reference(source.cct or 6500)
        self.assertAlmostEqual(reference.cct or 0, source.cct or 0, delta=2)
        self.assertEqual(len(reference.cri_samples), 15)

    def test_peak_ratio_mix_supports_endpoints_and_intermediate_chromaticity(self):
        first_result, second_result = self.results
        first = SpectrumInput(first_result.name, first_result.wavelengths, first_result.values)
        second = SpectrumInput(second_result.name, second_result.wavelengths, second_result.values)
        first_only = mix_spectra_by_peak_ratio(first, second, 1.0)
        second_only = mix_spectra_by_peak_ratio(first, second, 0.0)
        midpoint = mix_spectra_by_peak_ratio(first, second, 0.5)
        self.assertAlmostEqual(first_only.xy[0], first_result.xy[0], places=5)
        self.assertAlmostEqual(second_only.xy[0], second_result.xy[0], places=5)
        self.assertGreater(midpoint.xy[0], min(first_result.xy[0], second_result.xy[0]))
        self.assertLess(midpoint.xy[0], max(first_result.xy[0], second_result.xy[0]))
        self.assertIn(midpoint.peak_wavelength, midpoint.wavelengths)
        self.assertTrue(
            midpoint.dominant_wavelength is not None
            or midpoint.complementary_wavelength is not None
        )

    def test_three_spectrum_solver_recovers_known_nonnegative_peak_ratios(self):
        source_results = (
            analyze_standard_illuminant("A"),
            analyze_standard_illuminant("D65"),
            analyze_standard_illuminant("LED-B3"),
        )
        sources = (
            SpectrumInput(
                source_results[0].name, source_results[0].wavelengths, source_results[0].values
            ),
            SpectrumInput(
                source_results[1].name, source_results[1].wavelengths, source_results[1].values
            ),
            SpectrumInput(
                source_results[2].name, source_results[2].wavelengths, source_results[2].values
            ),
        )
        grid = np.arange(360, 781, dtype=float)
        basis = []
        for source in sources:
            values = np.interp(grid, source.wavelengths, source.values, left=0.0, right=0.0)
            basis.append(values / np.max(values))
        expected_ratios = np.asarray((0.2, 0.3, 0.5), dtype=float)
        target_values = np.zeros_like(grid, dtype=float)
        for ratio, values in zip(expected_ratios, basis):
            target_values += float(ratio) * values
        target = analyze_spectrum_chromaticity(
            SpectrumInput("已知目标", tuple(grid), tuple(target_values))
        )
        solution = solve_three_spectrum_mix(sources, target.xy)
        self.assertTrue(np.allclose(solution.peak_ratios, expected_ratios, atol=1e-6))
        self.assertAlmostEqual(solution.result.xy[0], target.xy[0], places=6)
        self.assertAlmostEqual(solution.result.xy[1], target.xy[1], places=6)
        self.assertGreater(solution.luminous_flux, 0)
        with self.assertRaisesRegex(SpectralAnalysisError, "可混合范围之外"):
            solve_three_spectrum_mix(sources, (0.70, 0.20))

    def test_power_limited_mix_reports_absolute_power_flux_and_limiting_source(self):
        sources = tuple(
            SpectrumInput(result.name, result.wavelengths, result.values)
            for result in self.results
        )
        power_result = calculate_power_limited_mix(
            sources,
            (0.25, 0.75),
            (1.0, 0.4),
        )
        self.assertGreater(power_result.radiant_power, 0)
        self.assertGreater(power_result.luminous_flux, 0)
        self.assertGreater(power_result.luminous_efficacy, 0)
        self.assertLessEqual(power_result.luminous_efficacy, 683)
        self.assertLessEqual(power_result.source_powers[0], 1.0 + 1e-9)
        self.assertLessEqual(power_result.source_powers[1], 0.4 + 1e-9)
        self.assertTrue(power_result.limiting_source_indices)
        limiting_index = power_result.limiting_source_indices[0]
        self.assertAlmostEqual(
            power_result.source_powers[limiting_index],
            (1.0, 0.4)[limiting_index],
            places=8,
        )


class ChromaticityTests(unittest.TestCase):
    def test_parse_xy_with_header_and_convert_coordinates(self):
        results = parse_chromaticity_text(
            "名称\tx\ty\nD65\t0.3127\t0.3290\nA\t0.44757\t0.40745",
            "xy",
        )
        self.assertEqual([item.name for item in results], ["D65", "A"])
        self.assertAlmostEqual(results[0].upvp[0], 0.19783, places=5)
        self.assertAlmostEqual(results[0].upvp[1], 0.46832, places=5)
        self.assertAlmostEqual(results[0].cct or 0, 6503.7, delta=1.0)

    def test_parse_uv_upvp_and_xyz_reach_same_xy(self):
        uv = parse_chromaticity_text("D65\t0.19783\t0.31221", "uv")[0]
        upvp = parse_chromaticity_text("D65\t0.19783\t0.46832", "upvp")[0]
        xyz = parse_chromaticity_text("D65\t95.0456\t100\t108.906", "XYZ")[0]
        for result in (uv, upvp, xyz):
            self.assertAlmostEqual(result.xy[0], 0.3127, places=4)
            self.assertAlmostEqual(result.xy[1], 0.3290, places=4)

    def test_pairwise_distances_are_reported(self):
        results = parse_chromaticity_text("A\t0.3\t0.3\nB\t0.4\t0.4\nC\t0.2\t0.2", "xy")
        distances = pairwise_chromaticity_distances(results)
        self.assertEqual(len(distances), 3)
        self.assertEqual((distances[0].first_name, distances[0].second_name), ("A", "B"))
        self.assertGreater(distances[0].delta_upvp, 0)

    def test_invalid_coordinate_and_duplicate_name_are_rejected(self):
        with self.assertRaisesRegex(SpectralAnalysisError, "无效"):
            parse_chromaticity_text("错误\t0.8\t0.5", "xy")
        with self.assertRaisesRegex(SpectralAnalysisError, "名称不能重复"):
            parse_chromaticity_text("同名\t0.3\t0.3\n同名\t0.4\t0.4", "xy")


if __name__ == "__main__":
    unittest.main()
