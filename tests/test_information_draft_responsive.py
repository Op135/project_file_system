import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INFORMATION_PATH = ROOT / "src" / "pages" / "information.py"
DASHBOARD_PATH = ROOT / "src" / "pages" / "main_dashboard.py"


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


class InformationSplitTests(unittest.TestCase):
    def test_information_registers_separate_overview_and_requirement_routes(self):
        source = INFORMATION_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        routes = {
            ast.literal_eval(decorator.args[0])
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and ast.unparse(decorator.func) == "ui.page"
            and decorator.args
        }

        self.assertIn("/project_overview_todo", routes)
        self.assertIn("/project_requirement_todo", routes)
        self.assertIn("/information", routes)

    def test_requirement_only_actions_stay_out_of_overview_page(self):
        source = INFORMATION_PATH.read_text(encoding="utf-8")
        self.assertIn("can_view_project_overview_todo(current_role, current_user)", source)
        self.assertIn("can_view_project_requirement_todo(current_role, current_user)", source)
        self.assertIn(
            'if section == "requirement" and can_revoke_project_requirement_approval',
            source,
        )
        self.assertIn('if section == "overview":', source)
        self.assertIn("render_conversion_reviews()", source)
        self.assertIn("render_overview_pending()", source)
        self.assertIn("render_overview_requests()", source)
        self.assertIn("render_requirement_review_board()", source)
        self.assertIn('ui_card_header("需求草稿箱"', source)

    def test_dashboard_uses_two_cards_and_separate_pending_counts(self):
        source = DASHBOARD_PATH.read_text(encoding="utf-8")
        self.assertIn(
            '("description", "项目概述待办", "处理项目概述相关待办", "/project_overview_todo")',
            source,
        )
        self.assertIn(
            '("rule", "项目需求待办", "处理项目需求相关待办", "/project_requirement_todo")',
            source,
        )
        self.assertIn('if target == "/project_overview_todo":', source)
        self.assertIn('elif target == "/project_requirement_todo":', source)
        self.assertIn("pending_count = len(requirement_pending_keys)", source)
        self.assertIn('items[3] == "/project_overview_todo" and not can_view_project_overview_todo', source)
        self.assertIn('items[3] == "/project_requirement_todo" and not can_view_project_requirement_todo', source)


if __name__ == "__main__":
    unittest.main()
