import unittest
from types import SimpleNamespace
from unittest.mock import mock_open, patch

from src import utils


class OverviewConfigRefreshTests(unittest.TestCase):
    def _fake_app(self):
        return SimpleNamespace(
            state=SimpleNamespace(user_service=None),
            storage=SimpleNamespace(general={}),
        )

    def test_background_refresh_does_not_create_ui_notification(self):
        """系统启动同步没有 UI slot，不应尝试创建通知组件。"""
        with (
            patch("builtins.open", mock_open(read_data="{}")),
            patch.object(utils, "app", self._fake_app()),
            patch.object(utils, "project_overview_permission_definitions"),
            patch.object(utils, "update_requirement_overview_impact_config"),
            patch.object(utils, "update_overview_charge_pending_dic"),
            patch.object(utils.ui, "notify") as notify,
        ):
            self.assertTrue(utils.updata_overview_config(show_notification=False))
            notify.assert_not_called()

    def test_missing_ui_slot_does_not_turn_success_into_failure(self):
        """用户通知失去上下文时，已经完成的配置同步仍应返回成功。"""
        with (
            patch("builtins.open", mock_open(read_data="{}")),
            patch.object(utils, "app", self._fake_app()),
            patch.object(utils, "project_overview_permission_definitions"),
            patch.object(utils, "update_requirement_overview_impact_config"),
            patch.object(utils, "update_overview_charge_pending_dic"),
            patch.object(utils.ui, "notify", side_effect=RuntimeError("slot unavailable")),
        ):
            self.assertTrue(utils.updata_overview_config(show_notification=True))


if __name__ == "__main__":
    unittest.main()
