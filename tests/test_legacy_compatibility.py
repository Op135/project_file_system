import logging
import unittest
from types import SimpleNamespace
from typing import Any

from src.legacy_compatibility import (
    record_legacy_compatibility_hit,
    reset_legacy_compatibility_telemetry,
)
from src.user_service import UserService


class LegacyCompatibilityTelemetryTests(unittest.TestCase):
    def setUp(self):
        reset_legacy_compatibility_telemetry()

    def tearDown(self):
        reset_legacy_compatibility_telemetry()

    def test_first_hit_is_logged_and_duplicate_is_suppressed(self):
        """同一用户和功能的重复命中不会持续刷屏。"""
        with self.assertLogs("src.legacy_compatibility", level=logging.WARNING) as captured:
            first_logged = record_legacy_compatibility_hit(
                "legacy_role_grant",
                "sample_issue.view",
                username="张三",
                detail="role=研发硬件",
                interval_seconds=3600,
            )
            second_logged = record_legacy_compatibility_hit(
                "legacy_role_grant",
                "sample_issue.view",
                username="张三",
                detail="role=研发硬件",
                interval_seconds=3600,
            )

        self.assertTrue(first_logged)
        self.assertFalse(second_logged)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("LEGACY_COMPAT_HIT", captured.output[0])
        self.assertIn("category=legacy_role_grant", captured.output[0])
        self.assertIn("feature=sample_issue.view", captured.output[0])
        self.assertIn("user=张三", captured.output[0])

    def test_next_summary_reports_suppressed_hits(self):
        """限频窗口结束后的日志会带上此前被抑制的命中数量。"""
        with self.assertLogs("src.legacy_compatibility", level=logging.WARNING) as captured:
            record_legacy_compatibility_hit(
                "legacy_workflow_route",
                "ecn.ecr_review",
                interval_seconds=3600,
            )
            record_legacy_compatibility_hit(
                "legacy_workflow_route",
                "ecn.ecr_review",
                interval_seconds=3600,
            )
            record_legacy_compatibility_hit(
                "legacy_workflow_route",
                "ecn.ecr_review",
                interval_seconds=0,
            )

        self.assertEqual(len(captured.output), 2)
        self.assertIn("total=3", captured.output[1])
        self.assertIn("suppressed=1", captured.output[1])

    def test_detail_removes_line_breaks(self):
        """动态说明保持单行，便于服务器按标记检索和统计。"""
        with self.assertLogs("src.legacy_compatibility", level=logging.WARNING) as captured:
            record_legacy_compatibility_hit(
                "excel_user_store",
                "authenticate",
                detail="第一行\n第二行",
            )

        self.assertIn("detail=第一行 第二行", captured.output[0])

    def test_legacy_user_service_grant_is_observable(self):
        """旧角色确实放行权限时通过统一服务写入可定位的功能编码。"""
        service: Any = UserService.__new__(UserService)
        service.identity_store = SimpleNamespace(has_database_users=lambda: False)

        with self.assertLogs("src.legacy_compatibility", level=logging.WARNING) as captured:
            allowed = service.has_permission(
                "张三",
                "sample_issue.view",
                legacy_role="研发硬件",
                legacy_allowed_roles=["研发硬件"],
            )

        self.assertTrue(allowed)
        self.assertIn("category=legacy_role_grant", captured.output[0])
        self.assertIn("feature=sample_issue.view", captured.output[0])


if __name__ == "__main__":
    unittest.main()
