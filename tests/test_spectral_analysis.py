"""光谱与色坐标分析核心的回归测试。"""

import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.tools.spectral_analysis import (  # noqa: E402
    SpectralAnalysisError,
    analyze_cct_reference,
    analyze_spectral_text,
    analyze_standard_illuminant,
    analyze_standard_illuminant_chromaticity,
    chromaticity_background_image,
    chromaticity_isotherms,
    chromaticity_loci,
    pairwise_chromaticity_distances,
    parse_chromaticity_text,
    parse_spectral_text,
    spectral_example_text,
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

    def test_calculation_requires_visible_range_coverage(self):
        with self.assertRaisesRegex(SpectralAnalysisError, "至少应覆盖"):
            analyze_spectral_text("波长\t样品\n400\t1\n780\t1")

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
