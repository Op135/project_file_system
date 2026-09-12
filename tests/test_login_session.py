"""登录会话复用与站内返回地址回归，不创建真实浏览器会话。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.pages import login


class LoginSessionTests(unittest.TestCase):
    def resume(self, session: dict, user_info, target="/ecn_management"):
        navigate = Mock()
        service = SimpleNamespace(get_user=Mock(return_value=user_info))
        app = SimpleNamespace(storage=SimpleNamespace(user=session), state=SimpleNamespace(user_service=service))
        with patch.object(login, "app", app), patch.object(login, "ui", SimpleNamespace(navigate=navigate)):
            resumed = login._resume_existing_session(target)
        return resumed, navigate, service

    def test_valid_session_resumes_without_password_and_refreshes_role(self):
        session = {"current_user": "user", "current_role": "旧岗位"}
        resumed, navigate, _ = self.resume(session, {"role": "研发经理", "status": "active"})
        self.assertTrue(resumed)
        navigate.to.assert_called_once_with("/ecn_management")
        self.assertEqual(session["current_role"], "研发经理")

    def test_login_page_returns_before_constructing_form_for_existing_session(self):
        with patch.object(login, "_resume_existing_session", return_value=True) as resume:
            login.login_page("/ecn_management")
        resume.assert_called_once_with("/ecn_management")

    def test_missing_session_requires_login(self):
        resumed, navigate, service = self.resume({}, {"role": "研发经理"})
        self.assertFalse(resumed)
        service.get_user.assert_not_called()
        navigate.to.assert_not_called()

    def test_removed_and_disabled_accounts_cannot_resume(self):
        for info in (None, {"status": "inactive"}):
            with self.subTest(info=info):
                session = {"current_user": "user", "current_role": "admin", "is_admin": True}
                resumed, navigate, _ = self.resume(session, info)
                self.assertFalse(resumed)
                self.assertNotIn("current_user", session)
                navigate.to.assert_not_called()

    def test_return_url_preserves_module_query(self):
        target = "/sample_issue_collection?issue_id=SPI-test"
        self.assertEqual(login._safe_login_target(target), target)

    def test_external_and_login_loop_targets_are_rejected(self):
        for target in (
            "",
            "https://example.test",
            "//example.test",
            "/\\example.test",
            "/%2fexample.test",
            "/\n/evil",
            "/login",
            "/login?redirect_to=/login",
        ):
            with self.subTest(target=target):
                self.assertEqual(login._safe_login_target(target), "/main")
