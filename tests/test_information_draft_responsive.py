import unittest
from pathlib import Path


class InformationDraftResponsiveTests(unittest.TestCase):
    """验证需求草稿操作区不会再被固定宽度推到小屏可视区之外。"""

    def test_draft_row_keeps_actions_visible_on_small_screens(self):
        source = (
            Path(__file__).resolve().parents[1] / "src" / "pages" / "information.py"
        ).read_text(encoding="utf-8")
        draft_section = source.split('ui_card_header("需求草稿箱"', 1)[1].split(
            'ui.label("暂无草稿记录")', 1
        )[0]

        self.assertNotIn("min-width: 632px", draft_section)
        self.assertIn("grid-cols-[minmax(0,1fr)_auto]", draft_section)
        self.assertIn("grid-cols-1 sm:grid-cols-", draft_section)
        self.assertIn("shrink-0 self-center", draft_section)


if __name__ == "__main__":
    unittest.main()
