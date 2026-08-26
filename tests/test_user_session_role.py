# -*- encoding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import utils


class CurrentUserRoleSyncTests(unittest.TestCase):
    def test_existing_session_role_is_refreshed_from_current_user_table(self):
        user_storage = {
            "current_user": "叶子浩",
            "current_role": "研发样品",
            "is_admin": False,
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(user=user_storage),
            state=SimpleNamespace(
                users_data={"叶子浩": {"password": "hidden", "role": "研发样品组长"}}
            ),
        )

        with patch.object(utils, "app", fake_app):
            current_role = utils.sync_current_user_role()

        self.assertEqual(current_role, "研发样品组长")
        self.assertEqual(user_storage["current_role"], "研发样品组长")
        self.assertFalse(user_storage["is_admin"])

    def test_admin_flag_is_synchronized_with_latest_role(self):
        user_storage = {
            "current_user": "admin",
            "current_role": "普通用户",
            "is_admin": False,
        }
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(user=user_storage),
            state=SimpleNamespace(users_data={"admin": {"role": "admin"}}),
        )

        with patch.object(utils, "app", fake_app):
            current_role = utils.sync_current_user_role()

        self.assertEqual(current_role, "admin")
        self.assertTrue(user_storage["is_admin"])

    def test_live_user_service_takes_precedence_over_stale_process_cache(self):
        user_storage = {"current_user": "叶子浩", "current_role": "研发样品"}
        user_service = SimpleNamespace(
            get_user=lambda _username: {"role": "研发样品组长"}
        )
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(user=user_storage),
            state=SimpleNamespace(
                users_data={"叶子浩": {"role": "研发样品"}},
                user_service=user_service,
            ),
        )

        with patch.object(utils, "app", fake_app):
            current_role = utils.sync_current_user_role()

        self.assertEqual(current_role, "研发样品组长")
        self.assertEqual(user_storage["current_role"], "研发样品组长")

    def test_existing_session_receives_stable_user_id_after_migration(self):
        user_storage = {"current_user": "叶子浩", "current_role": "研发样品"}
        user_service = SimpleNamespace(
            get_user=lambda _username: {
                "user_id": "user-stable-id",
                "role": "研发样品组长",
                "status": "active",
            }
        )
        fake_app = SimpleNamespace(
            storage=SimpleNamespace(user=user_storage),
            state=SimpleNamespace(users_data={}, user_service=user_service),
        )

        with patch.object(utils, "app", fake_app):
            utils.sync_current_user_role()

        self.assertEqual(user_storage["current_user_id"], "user-stable-id")


if __name__ == "__main__":
    unittest.main()
