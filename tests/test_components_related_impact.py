import unittest

from src.components import (
    _can_skip_related_impact,
    _get_active_chip_icon,
    _get_image_status_visuals,
    _has_real_related_selection,
    _normalize_related_labels,
)


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

    def test_media_type_icon_is_restored_after_reactivation(self):
        self.assertEqual(_get_active_chip_icon("image"), "image")
        self.assertEqual(_get_active_chip_icon("file"), "attachment")
        self.assertEqual(_get_active_chip_icon("video"), "play_circle")
        self.assertIsNone(_get_active_chip_icon("text"))

    def test_pending_image_uses_high_contrast_badge_and_border(self):
        border_classes, badge_classes = _get_image_status_visuals("question_mark")
        self.assertIn("border-amber-500", border_classes)
        self.assertIn("bg-amber-8", badge_classes)
        self.assertIn("text-white", badge_classes)

    def test_add_chip_can_use_no_related_impact_confirmation_in_both_overview_classes(self):
        self.assertTrue(_can_skip_related_impact("add_chip"))
        self.assertTrue(_can_skip_related_impact("activ_change"))
        self.assertFalse(_can_skip_related_impact("unknown"))


if __name__ == "__main__":
    unittest.main()
