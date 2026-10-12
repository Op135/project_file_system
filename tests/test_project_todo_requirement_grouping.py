import unittest

from src.project_todo_access import (
    get_default_requirement_review_state,
    get_requirement_todo_keys_by_state,
)


class ProjectTodoRequirementGroupingTests(unittest.TestCase):
    def setUp(self):
        self.wait_review = {
            "销售提交项目": {
                "1.0": {"state": "待修改", "submitter": "销售甲"},
                "2.0": {"state": "待审", "submitter": "销售甲"},
            },
            "工程师负责项目": {
                "1.0": {"state": "待审", "submitter": "销售乙"},
            },
        }

    def test_submitter_badge_todo_opens_pending_modification(self):
        todos = get_requirement_todo_keys_by_state(
            self.wait_review,
            current_user="销售甲",
            can_edit_requirements=True,
            can_review_requirement=lambda _project: False,
        )

        self.assertEqual(todos["待修改"], {("销售提交项目", "1.0")})
        self.assertEqual(todos["待审"], set())
        self.assertEqual(get_default_requirement_review_state(todos), "待修改")

    def test_reviewer_badge_todo_opens_pending_review(self):
        todos = get_requirement_todo_keys_by_state(
            self.wait_review,
            current_user="工程师甲",
            can_edit_requirements=False,
            can_review_requirement=lambda project: project == "工程师负责项目",
        )

        self.assertEqual(todos["待审"], {("工程师负责项目", "1.0")})
        self.assertEqual(todos["待修改"], set())
        self.assertEqual(get_default_requirement_review_state(todos), "待审")

    def test_review_todo_takes_priority_when_user_has_both_types(self):
        todos = get_requirement_todo_keys_by_state(
            self.wait_review,
            current_user="销售甲",
            can_edit_requirements=True,
            can_review_requirement=lambda project: project == "工程师负责项目",
        )

        self.assertTrue(todos["待审"])
        self.assertTrue(todos["待修改"])
        self.assertEqual(get_default_requirement_review_state(todos), "待审")

    def test_no_badge_todo_keeps_pending_review_as_fallback(self):
        todos = get_requirement_todo_keys_by_state(
            self.wait_review,
            current_user="旁观者",
            can_edit_requirements=False,
            can_review_requirement=lambda _project: False,
        )

        self.assertEqual(get_default_requirement_review_state(todos), "待审")


if __name__ == "__main__":
    unittest.main()
