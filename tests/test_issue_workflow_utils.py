"""问题跟进负责人只能按具体账号匹配，不再接受旧角色或姓名子串。"""

import unittest

from src.issue_workflow_utils import is_current_responsible


class ResponsibleIdentityTests(unittest.TestCase):
    def test_exact_usernames_and_supported_separators(self):
        for owners in ("张三", "李四，张三", "李四、张三", "李四;张三", "李四；张三", "李四\n张三", "李四|张三"):
            with self.subTest(owners=owners):
                self.assertTrue(is_current_responsible(owners, " 张三 ", "无关岗位"))
        self.assertTrue(is_current_responsible(" Alice , Bob ", "ALICE", "普通用户"))

    def test_name_substrings_never_grant_responsibility(self):
        for owner, user in (("张三", "张三丰"), ("张三丰", "张三"), ("Ann", "Joanna")):
            with self.subTest(owner=owner, user=user):
                self.assertFalse(is_current_responsible(owner, user, "普通用户"))

    def test_legacy_role_is_not_an_identity(self):
        self.assertFalse(is_current_responsible("研发硬件", "李四", "研发硬件"))
        self.assertFalse(is_current_responsible("研发", "李四", "研发硬件"))
        self.assertFalse(is_current_responsible("admin", "普通账号", "admin"))

    def test_missing_identity_is_denied(self):
        self.assertFalse(is_current_responsible("张三", "", "张三"))
        self.assertFalse(is_current_responsible("张三", "  ", "张三"))
        self.assertFalse(is_current_responsible(" , | ", "张三", "普通用户"))
