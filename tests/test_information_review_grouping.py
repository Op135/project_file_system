import ast
import unittest
from collections.abc import Callable
from pathlib import Path


SOURCE_PATH = Path(__file__).resolve().parents[1] / "src" / "pages" / "information.py"


def load_grouping_helper() -> Callable[..., dict[str, list[tuple[str, str]]]]:
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_group_visible_requirement_reviews"
    )
    namespace = {"Callable": Callable}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SOURCE_PATH), "exec"), namespace)
    grouping_helper = namespace["_group_visible_requirement_reviews"]
    assert callable(grouping_helper)
    return grouping_helper


class InformationReviewGroupingTests(unittest.TestCase):
    def test_reviews_are_grouped_by_state_and_existing_visibility_rules(self):
        group_reviews = load_grouping_helper()
        wait_review = {
            "提交项目": {
                "1.0": {"state": "待审", "submitter": "当前用户"},
                "2.0": {"state": "待修改", "submitter": "当前用户"},
            },
            "负责项目": {
                "1.0": {"state": "待审", "submitter": "其他人"},
                "2.0": {"state": "待修改", "submitter": "其他人"},
            },
            "无关项目": {"1.0": {"state": "待审", "submitter": "其他人"}},
            "已完成项目": {"1.0": {"state": "已审", "submitter": "当前用户"}},
        }

        groups = group_reviews(
            wait_review,
            current_user="当前用户",
            can_review_requirement=lambda project: project == "负责项目",
            can_review_all=False,
        )

        self.assertEqual(groups["待审"], [("提交项目", "1.0"), ("负责项目", "1.0")])
        self.assertEqual(groups["待修改"], [("提交项目", "2.0")])

    def test_global_reviewer_can_see_both_groups(self):
        group_reviews = load_grouping_helper()
        groups = group_reviews(
            {
                "项目A": {
                    "1.0": {"state": "待审", "submitter": "其他人"},
                    "2.0": {"state": "待修改", "submitter": "其他人"},
                }
            },
            current_user="当前用户",
            can_review_requirement=lambda _project: False,
            can_review_all=True,
        )

        self.assertEqual(groups["待审"], [("项目A", "1.0")])
        self.assertEqual(groups["待修改"], [("项目A", "2.0")])

    def test_expansion_defaults_and_state_handlers_refresh_the_board(self):
        tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        render = functions["render_requirement_review_board"]
        group_options = next(
            node.value
            for node in render.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "group_options" for target in node.targets)
        )
        semantic_options = tuple(option[:3] for option in ast.literal_eval(group_options))
        self.assertEqual(
            semantic_options,
            (("待审", "pending_actions", True), ("待修改", "edit_note", False)),
        )

        for function_name in ("set_review_revise", "set_review_pass", "remove_requirement_file"):
            calls = {
                ast.unparse(node.func)
                for node in ast.walk(functions[function_name])
                if isinstance(node, ast.Call)
            }
            self.assertIn("render_requirement_review_board.refresh", calls, function_name)


if __name__ == "__main__":
    unittest.main()
