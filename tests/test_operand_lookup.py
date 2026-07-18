# -*- coding: utf-8 -*-
from unittest import TestCase

from src.tools.operand_lookup import CATEGORY_GROUPS, CATEGORY_LABELS, OperandLookupTool


class OperandLookupTests(TestCase):
    def setUp(self) -> None:
        self.tool = OperandLookupTool()

    def test_all_categories_are_assigned_to_one_reference_group(self) -> None:
        grouped_keys = [key for group in CATEGORY_GROUPS for key in group["categories"]]
        data_keys = [self.tool._category_key(category) for category in self.tool.data["categories"]]
        self.assertEqual(len(grouped_keys), len(set(grouped_keys)))
        self.assertEqual(set(grouped_keys), set(data_keys))
        self.assertEqual(set(CATEGORY_LABELS), set(data_keys))
        self.assertEqual([len(group["categories"]) for group in CATEGORY_GROUPS], [9, 14, 6, 5])

    def test_search_ignores_current_category_and_finds_code_globally(self) -> None:
        self.tool.query = "GCOS"
        results = self.tool._filtered_operands()
        self.assertTrue(any(item["code"] == "GCOS" for item in results))

    def test_default_category_follows_parameter_group_order(self) -> None:
        category = self.tool._selected_category_data()
        self.assertIsNotNone(category)
        assert category is not None
        self.assertEqual(self.tool._category_key(category), "Changing_System_Data")

    def test_category_arrows_follow_reference_group_order(self) -> None:
        category = self.tool._selected_category_data()
        self.assertIsNotNone(category)
        assert category is not None
        previous_category, next_category = self.tool._adjacent_categories(category)
        self.assertIsNone(previous_category)
        self.assertIsNotNone(next_category)
        assert next_category is not None
        self.assertEqual(self.tool._category_key(next_category), "Constraints_on_Lens_Data")

    def test_only_admin_and_rd_manager_can_update_data(self) -> None:
        self.assertTrue(self.tool._role_can_update("admin"))
        self.assertTrue(self.tool._role_can_update("研发经理"))
        self.assertFalse(self.tool._role_can_update("研发光学"))
        self.assertFalse(self.tool._role_can_update("boss"))
        self.assertFalse(self.tool._role_can_update(None))
