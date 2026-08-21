import unittest

from src.pages.ecn_management import (
    append_ecn_approval_log_once,
    build_ecn_operation_note,
    build_overview_activation_state,
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

        result = deactivate_overview_chip_for_ecn(
            original,
            "2.0",
            "ECN26081401",
            operation_time,
            "方案提供人",
        )

        self.assertEqual(result["creator"], "方案提供人")
        self.assertEqual(result["timestamp"][operation_time]["creator"], result["creator"])
        self.assertEqual(result["select_activ_dic"], {"1.0": True, "2.0": False})
        self.assertEqual(result["req_ver"], "1.0")
        self.assertEqual(result["notes"], "原录入注释\nECN操作：依据 ECN26081401 执行")
        self.assertEqual(original["creator"], "原操作人")
        self.assertNotIn("2.0", original["select_activ_dic"])

    def test_no_requirement_project_uses_version_zero_entry_node(self):
        req_ver, activations = build_overview_activation_state(None)

        self.assertEqual(req_ver, "0.0")
        self.assertEqual(activations, {"0.0": True})

    def test_ecn_operation_note_is_not_repeated(self):
        first = build_ecn_operation_note("人工注释", "ECN26081401")
        second = build_ecn_operation_note(first, "ECN26081401")

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
