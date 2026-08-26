import unittest

from src.identity_matching import build_wecom_user_match_plan


class IdentityMatchingTests(unittest.TestCase):
    def test_account_match_has_priority_over_name_match(self):
        users = {
            "wecom-id": {"display_name": "不同姓名", "status": "active"},
        }
        contacts = [
            {"userid": "wecom-id", "name": "企业微信姓名", "is_active": True},
        ]

        plan = build_wecom_user_match_plan(users, contacts)

        self.assertEqual(plan[0]["status"], "matched")
        self.assertEqual(plan[0]["contact"]["userid"], "wecom-id")
        self.assertIn("账号", plan[0]["reason"])

    def test_unique_same_name_matches_but_duplicate_name_is_ambiguous(self):
        contacts = [
            {"userid": "u1", "name": "张三", "is_active": True},
            {"userid": "u2", "name": "李四", "is_active": True},
            {"userid": "u3", "name": "李四", "is_active": True},
        ]
        plan = build_wecom_user_match_plan(
            {
                "张三": {"display_name": "张三", "status": "active"},
                "李四": {"display_name": "李四", "status": "active"},
            },
            contacts,
        )
        by_username = {item["username"]: item for item in plan}

        self.assertEqual(by_username["张三"]["status"], "matched")
        self.assertEqual(by_username["李四"]["status"], "ambiguous")
        self.assertIsNone(by_username["李四"]["contact"])

    def test_two_system_users_cannot_claim_one_contact(self):
        contacts = [{"userid": "shared", "name": "同一个人", "is_active": True}]
        plan = build_wecom_user_match_plan(
            {
                "shared": {"display_name": "账号匹配", "status": "active"},
                "另一个系统账号": {"display_name": "同一个人", "status": "active"},
            },
            contacts,
        )

        self.assertTrue(all(item["status"] == "ambiguous" for item in plan))
        self.assertTrue(all(item["contact"] is None for item in plan))

    def test_existing_bindings_and_inactive_users_are_not_rematched(self):
        plan = build_wecom_user_match_plan(
            {
                "已绑定": {"display_name": "已绑定", "status": "active"},
                "已停用": {"display_name": "已停用", "status": "disabled"},
                "待匹配": {"display_name": "待匹配", "status": "active"},
            },
            [
                {"userid": "bound", "name": "已绑定", "is_active": True},
                {"userid": "disabled", "name": "已停用", "is_active": True},
                {"userid": "pending", "name": "待匹配", "is_active": True},
            ],
            {"已绑定": {"external_userid": "bound"}},
        )

        self.assertEqual([item["username"] for item in plan], ["待匹配"])
        self.assertEqual(plan[0]["status"], "matched")


if __name__ == "__main__":
    unittest.main()
