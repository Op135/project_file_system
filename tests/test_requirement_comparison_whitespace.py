import unittest

from src import utils


def text_requirement_node(content: str, tolerance: str = "") -> dict:
    return {
        "node_id": "10",
        "answer_type": "多行文本",
        "user_must_out": {"1": content},
        "option_tolerance_out": {"1": tolerance},
        "ref_out": [],
    }


class RequirementComparisonWhitespaceTests(unittest.TestCase):
    def test_ignores_leading_and_trailing_text_whitespace_and_blank_lines(self):
        old_node = text_requirement_node(
            "  补充硬件要求：\n• 电源类型：DC\n\n  ",
            "  ±1%\n",
        )
        new_node = text_requirement_node(
            "补充硬件要求：\n• 电源类型：DC",
            "±1%",
        )

        diff = utils.compare_configs_by_id({"1": old_node}, {"1": new_node})

        self.assertEqual(diff["modified"], {})

    def test_keeps_internal_blank_line_changes_meaningful(self):
        old_node = text_requirement_node("第一段\n\n第二段")
        new_node = text_requirement_node("第一段\n第二段")

        diff = utils.compare_configs_by_id({"1": old_node}, {"1": new_node})

        self.assertIn("10", diff["modified"])

    def test_comparison_dialog_ignores_guide_content_boundary_whitespace(self):
        old_node = text_requirement_node("相同答案") | {"guide_content": "  引导说明\n"}
        new_node = text_requirement_node("相同答案") | {"guide_content": "引导说明"}

        diff = utils.compare_configs_by_id(
            {"1": old_node},
            {"1": new_node},
            ["guide_content"],
        )

        self.assertEqual(diff["modified"], {})


if __name__ == "__main__":
    unittest.main()
