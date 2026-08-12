import unittest

from src.components import _has_real_related_selection, _normalize_related_labels


class RelatedImpactSelectionTests(unittest.TestCase):
    def test_normalize_related_labels_handles_empty_values_and_duplicates(self):
        self.assertEqual(_normalize_related_labels(None), [])
        self.assertEqual(_normalize_related_labels([]), [])
        self.assertEqual(_normalize_related_labels(["light", "", None, "light", "lens"]), ["light", "lens"])

    def test_only_boolean_true_counts_as_a_real_selection(self):
        self.assertFalse(_has_real_related_selection({}))
        self.assertFalse(_has_real_related_selection({"light": False, "lens": None}))
        self.assertFalse(_has_real_related_selection({"light": 1, "lens": "true"}))
        self.assertTrue(_has_real_related_selection({"light": False, "lens": True}))


if __name__ == "__main__":
    unittest.main()
