import unittest

from src.overview_operation import (
    append_overview_timestamp,
    get_latest_overview_operator,
    get_latest_overview_reason,
    resolve_overview_reason,
)


class OverviewOperationTests(unittest.TestCase):
    def test_resolve_overview_reason_keeps_label_and_expands_other(self):
        self.assertEqual(resolve_overview_reason("需求变更"), "需求变更")
        self.assertEqual(resolve_overview_reason("其他", "客户指定"), "其他：客户指定")
        self.assertEqual(resolve_overview_reason("其他", ""), "")

    def test_latest_reason_uses_record_and_legacy_notes_only_as_fallback(self):
        chip = {
            "creator": "原录入人",
            "notes": "旧注释",
            "timestamp": {
                "2026-08-21 10:00:00": {"creator": "原录入人", "select_activ_dic": {"1.0": True}},
            },
        }
        self.assertEqual(get_latest_overview_reason(chip), "旧注释")
        append_overview_timestamp(
            chip,
            creator="最近负责人",
            reason="需求影响待确认",
            operation_time="2026-08-21 11:00:00",
        )
        self.assertEqual(get_latest_overview_reason(chip), "需求影响待确认")
        self.assertEqual(get_latest_overview_operator(chip), "最近负责人")

    def test_append_timestamp_snapshots_state_and_source_id(self):
        chip = {"select_activ_dic": {"1.0": True, "2.0": None}}
        append_overview_timestamp(
            chip,
            creator="方案人",
            reason="ECN失效",
            operation_time="2026-08-21 12:00:00",
            source_id="ECN26082101",
        )
        chip["select_activ_dic"]["2.0"] = False
        record = chip["timestamp"]["2026-08-21 12:00:00"]
        self.assertEqual(record["select_activ_dic"], {"1.0": True, "2.0": None})
        self.assertEqual(record["source_id"], "ECN26082101")


if __name__ == "__main__":
    unittest.main()
