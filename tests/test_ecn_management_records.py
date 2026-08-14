import unittest

from src.pages.ecn_management import (
    append_ecn_approval_log_once,
    deactivate_overview_chip_for_ecn,
)


class ECNManagementRecordTests(unittest.TestCase):
    def test_adjacent_identical_approval_log_is_not_appended_twice(self):
        entry = {
            "user": "张三",
            "role": "研发结构",
            "action": "发起申请",
            "time": "2026-08-14 18:13:45",
        }
        approval_log = [entry.copy()]

        self.assertFalse(append_ecn_approval_log_once(approval_log, entry))
        self.assertEqual(approval_log, [entry])

        next_entry = {**entry, "action": "撤回修改"}
        self.assertTrue(append_ecn_approval_log_once(approval_log, next_entry))
        self.assertEqual(len(approval_log), 2)

    def test_ecn_deactivation_updates_recent_operator_and_matching_history(self):
        original = {
            "creator": "原操作人",
            "req_ver": "1.0",
            "notes": "原录入注释",
            "enabled": True,
            "select_activ_dic": {"1.0": True},
            "timestamp": {
                "2026-08-13 10:00:00": {
                    "creator": "原操作人",
                    "select_activ_dic": {"1.0": True},
                }
            },
        }
        operation_time = "2026-08-14 18:13:45"

        result = deactivate_overview_chip_for_ecn(original, "2.0", "ECN26081401", operation_time)

        self.assertEqual(result["creator"], "ECN自动执行 (ECN26081401)")
        self.assertEqual(result["timestamp"][operation_time]["creator"], result["creator"])
        self.assertEqual(result["select_activ_dic"], {"1.0": True, "2.0": False})
        self.assertEqual(result["req_ver"], "1.0")
        self.assertEqual(result["notes"], "原录入注释")
        self.assertEqual(original["creator"], "原操作人")
        self.assertNotIn("2.0", original["select_activ_dic"])


if __name__ == "__main__":
    unittest.main()
