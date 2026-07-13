"""设计知识库审核路由配置回归测试。"""

import unittest

from src.design_knowledge_config import (
    DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES,
    DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES,
    can_review_design_knowledge_submission,
    is_design_knowledge_review_approver_role,
    resolve_design_knowledge_review_route,
    resolve_design_knowledge_submission_review_route,
)


class DesignKnowledgeReviewRoutingTests(unittest.TestCase):
    def test_research_roles_route_to_their_group_supervisor(self):
        cases = {
            "研发硬件工程师": ("rd_electronics", ["研发电子主管"]),
            "研发软件工程师": ("rd_electronics", ["研发电子主管"]),
            "研发结构工程师": ("rd_structure", ["研发结构组长"]),
            "研发光学工程师": ("rd_other", ["研发经理"]),
        }

        for submitter_role, (expected_key, expected_approvers) in cases.items():
            with self.subTest(submitter_role=submitter_role):
                route = resolve_design_knowledge_review_route(submitter_role)
                self.assertEqual(route["key"], expected_key)
                self.assertEqual(route["approver_roles"], expected_approvers)

    def test_specific_routes_are_ordered_before_general_research_route(self):
        route_keys = [rule["key"] for rule in DESIGN_KNOWLEDGE_REVIEW_ROUTING_RULES]
        self.assertLess(route_keys.index("rd_electronics"), route_keys.index("rd_other"))
        self.assertLess(route_keys.index("rd_structure"), route_keys.index("rd_other"))

    def test_unmatched_role_uses_fallback_approvers(self):
        route = resolve_design_knowledge_review_route("未配置部门工程师")
        self.assertEqual(route["key"], "fallback")
        self.assertEqual(route["approver_roles"], DESIGN_KNOWLEDGE_REVIEW_FALLBACK_APPROVER_ROLES)

    def test_resolved_route_is_a_copy(self):
        route = resolve_design_knowledge_review_route("研发软件")
        route["approver_roles"].append("不应污染配置")

        fresh_route = resolve_design_knowledge_review_route("研发软件")
        self.assertEqual(fresh_route["approver_roles"], ["研发电子主管"])

    def test_only_assigned_group_supervisor_can_review(self):
        submission = {
            "created_by": "张三",
            "created_role": "研发硬件工程师",
            "approver_roles": ["研发电子主管"],
        }

        self.assertTrue(can_review_design_knowledge_submission(submission, "李主管", "研发电子主管"))
        self.assertFalse(can_review_design_knowledge_submission(submission, "王组长", "研发结构组长"))
        self.assertFalse(can_review_design_knowledge_submission(submission, "张三", "研发电子主管"))
        self.assertTrue(can_review_design_knowledge_submission(submission, "admin", "admin"))

    def test_submission_keeps_route_snapshot(self):
        submission = {
            "created_by": "张三",
            "created_role": "研发软件工程师",
            "review_route_key": "historical_route",
            "review_route_label": "历史审核组",
            "approver_roles": ["历史主管"],
        }

        route = resolve_design_knowledge_submission_review_route(submission)
        self.assertEqual(route["key"], "historical_route")
        self.assertEqual(route["label"], "历史审核组")
        self.assertEqual(route["approver_roles"], ["历史主管"])
        self.assertTrue(can_review_design_knowledge_submission(submission, "审核员", "历史主管"))
        self.assertFalse(can_review_design_knowledge_submission(submission, "审核员", "研发电子主管"))

    def test_configured_group_leader_has_review_entry(self):
        self.assertTrue(is_design_knowledge_review_approver_role("研发结构组长"))


if __name__ == "__main__":
    unittest.main()
