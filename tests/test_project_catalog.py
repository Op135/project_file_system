import unittest

from src.project_catalog import (
    build_project_select_options,
    get_project_table_category,
    is_other_project_name,
    match_other_project_name,
)


class ProjectCatalogTests(unittest.TestCase):
    def test_other_project_rule_matches_project_table_behavior(self):
        self.assertTrue(is_other_project_name("RM5000"))
        self.assertFalse(is_other_project_name("RFFM-1519-A"))
        self.assertEqual(get_project_table_category("RM5000"), ("其它", "RM5000"))
        self.assertEqual(get_project_table_category("RFFM-1519-A"), ("RFFM", "15"))

    def test_build_project_select_options_keeps_special_names_under_other(self):
        options = build_project_select_options(["RFFM-1519-A", "RFFM-1520-A", "RM5000"])

        self.assertEqual(options["RFFM"], ["所有", "15"])
        self.assertEqual(options["其它"], ["所有", "RM5000"])

    def test_other_project_match_prefers_unique_longest_name(self):
        matched, candidates = match_other_project_name(
            "RM3000-firmware.bin", ["RM300", "RM3000", "RFFM-1519"]
        )

        self.assertEqual(matched, "RM3000")
        self.assertEqual(candidates, ["RM3000", "RM300"])

    def test_other_project_match_reports_equal_length_ambiguity(self):
        matched, candidates = match_other_project_name(
            "RM3000-RM5000.bin", ["RM3000", "RM5000"]
        )

        self.assertEqual(matched, "")
        self.assertEqual(candidates, ["RM3000", "RM5000"])

    def test_unrelated_matches_remain_ambiguous_even_when_lengths_differ(self):
        matched, candidates = match_other_project_name(
            "SPECIAL-RM5000.bin", ["SPECIAL", "RM5000"]
        )

        self.assertEqual(matched, "")
        self.assertEqual(candidates, ["SPECIAL", "RM5000"])


if __name__ == "__main__":
    unittest.main()
