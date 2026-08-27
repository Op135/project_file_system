# -*- encoding: utf-8 -*-
import copy
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

from openpyxl import Workbook

from src import db_storage
from src import sample_order_dashboard_config as dashboard_config
from src.pages import sample_order_dashboard as dashboard


def make_record() -> dict:
    """构造一张字段完整的样品单测试记录。"""
    record = dashboard.get_sample_order_template()
    record["record_id"] = "record-1"
    record["basic_info"].update(
        {
            "sample_order_no": "Y26072001",
            "customer_code": "19020001",
            "product_model": "RFTS-0001",
            "application_qty": 2,
            "application_date": "2026-07-01",
            "applicant": "测试申请人",
            "planned_delivery_date": "2026-07-20",
        }
    )
    record["_revision"] = 1
    return record


class SampleOrderCalculationTests(unittest.TestCase):
    def test_empty_actual_delivery_date_displays_as_undelivered(self):
        self.assertEqual(dashboard.sample_order_delivery_display(""), "未交样")
        self.assertEqual(
            dashboard.sample_order_delivery_display("2026-07-20"),
            "2026-07-20",
        )

    def test_assessment_score_matches_excel_ranges(self):
        cases = {
            -6: 150,
            -5: 140,
            -4: 140,
            -3: 130,
            -1: 130,
            0: 120,
            1: 100,
            3: 100,
            4: 80,
            5: 80,
            6: 60,
            10: 60,
            11: 0,
        }
        for days, expected in cases.items():
            with self.subTest(days=days):
                self.assertEqual(dashboard.calculate_assessment_score(days), expected)

    def test_primary_delay_uses_original_delivery_date_for_assessment(self):
        record = make_record()
        record["execution"]["actual_delivery_date"] = "2026-07-25"
        record["extensions"] = [
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-1",
                    "target_date": "2026-07-25",
                    "reason": "样品组主责：排产延误",
                }
            )
        ]
        self.assertEqual(dashboard.calculate_assessment_days(record), 5)

    def test_non_primary_latest_delay_uses_latest_target(self):
        record = make_record()
        record["execution"]["actual_delivery_date"] = "2026-08-05"
        record["extensions"] = [
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-1",
                    "target_date": "2026-07-25",
                    "reason": "客户变更要求",
                }
            ),
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-2",
                    "target_date": "2026-07-30",
                    "reason": "等待客户确认",
                }
            ),
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-3",
                    "target_date": "2026-08-05",
                    "reason": "再次等待客户确认",
                }
            ),
        ]
        self.assertEqual(dashboard.calculate_assessment_days(record), 0)

    @patch.object(dashboard, "is_holiday", return_value=False)
    def test_business_days_exclude_weekends(self, _mock_holiday):
        self.assertEqual(
            dashboard.business_days_between(date(2026, 7, 17), date(2026, 7, 20)),
            1,
        )
        self.assertEqual(
            dashboard.business_days_between(date(2026, 7, 20), date(2026, 7, 17)),
            -1,
        )

    @patch.object(dashboard, "is_holiday", return_value=False)
    def test_overdue_without_first_extension_requires_target(self, _mock_holiday):
        record = make_record()
        metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 21))
        self.assertEqual(metrics["alert_message"], "第1次延期目标日期未填")
        self.assertEqual(metrics["attention_level"], "overdue")
        self.assertEqual(metrics["expected_status"], "延期")

    def test_completed_record_calculates_status_and_score(self):
        record = make_record()
        record["execution"]["actual_delivery_date"] = "2026-07-18"
        with patch.object(dashboard, "business_days_between") as workday_calculator:
            metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 21))
        self.assertEqual(metrics["attention_level"], "completed")
        self.assertEqual(metrics["expected_status"], "按期")
        self.assertEqual(metrics["assessment_days"], -2)
        self.assertEqual(metrics["assessment_score"], 130)
        self.assertIsNone(metrics["remaining_workdays"])
        workday_calculator.assert_not_called()

    def test_past_target_does_not_iterate_historical_workdays(self):
        record = make_record()
        with patch.object(dashboard, "business_days_between") as workday_calculator:
            metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 21))
        self.assertEqual(metrics["remaining_workdays"], -1)
        self.assertEqual(metrics["attention_level"], "overdue")
        workday_calculator.assert_not_called()

    def test_in_progress_status_distinguishes_plan_and_current_target(self):
        record = make_record()
        on_plan = dashboard.calculate_sample_order_metrics(
            record,
            date(2026, 7, 20),
        )
        self.assertEqual(on_plan["expected_status"], "按计划")

        record["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "排期调整"}
            )
        ]
        on_current_target = dashboard.calculate_sample_order_metrics(
            record,
            date(2026, 7, 25),
        )
        self.assertEqual(on_current_target["expected_status"], "按当前目标")

        overdue = dashboard.calculate_sample_order_metrics(
            record,
            date(2026, 7, 26),
        )
        self.assertEqual(overdue["expected_status"], "延期")

    def test_default_filter_hides_every_delivered_order(self):
        delivered = make_record()
        delivered["execution"]["actual_delivery_date"] = "2026-07-18"
        delivered["special_status"].update({"status": "暂停", "reason": "历史状态未恢复"})

        self.assertEqual(
            dashboard.DEFAULT_SAMPLE_ORDER_FILTER,
            dashboard.FILTER_IN_PROGRESS,
        )
        self.assertFalse(
            dashboard.sample_order_matches_filter(
                delivered,
                dashboard.DEFAULT_SAMPLE_ORDER_FILTER,
            )
        )
        self.assertTrue(
            dashboard.sample_order_matches_filter(
                delivered,
                dashboard.FILTER_COMPLETED,
            )
        )

    def test_in_progress_filter_excludes_undelivered_special_status_orders(self):
        for status in ("暂停", "作废"):
            with self.subTest(status=status):
                record = make_record()
                record["special_status"].update({"status": status, "reason": "等待处理"})

                self.assertFalse(
                    dashboard.sample_order_matches_filter(
                        record,
                        dashboard.FILTER_IN_PROGRESS,
                    )
                )
                self.assertTrue(
                    dashboard.sample_order_matches_filter(
                        record,
                        status,
                    )
                )

    def test_in_progress_kpi_excludes_special_status_orders(self):
        for status in ("暂停", "作废"):
            record = make_record()
            record["special_status"].update({"status": status, "reason": "等待处理"})
            metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 18))
            self.assertFalse(
                dashboard.sample_order_matches_kpi(
                    record,
                    metrics,
                    "制样中",
                ),
                status,
            )

    def test_special_status_overrides_normal_execution_status(self):
        record = make_record()
        record["special_status"].update({"status": "暂停", "reason": "等待客户确认"})
        metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 18))
        self.assertEqual(metrics["attention_level"], "paused")
        self.assertEqual(metrics["expected_status"], "暂停")

    def test_more_than_configured_delay_count_is_highlighted(self):
        record = make_record()
        record["extensions"] = [
            dashboard.normalize_extension(
                {
                    "extension_id": f"extension-{index}",
                    "target_date": f"2026-07-{20 + index:02d}",
                    "reason": "客户原因",
                }
            )
            for index in range(1, dashboard.SAMPLE_ORDER_DELAY_ATTENTION_THRESHOLD + 2)
        ]
        metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 18))
        self.assertTrue(metrics["many_delays"])

    def test_kpi_detail_matching_uses_the_same_status_rules(self):
        record = make_record()
        warning_metrics = {
            "attention_level": "warning",
            "many_delays": False,
            "assessment_score": None,
        }
        self.assertTrue(dashboard.sample_order_matches_kpi(record, warning_metrics, "制样中"))
        self.assertTrue(dashboard.sample_order_matches_kpi(record, warning_metrics, "预警"))
        self.assertFalse(dashboard.sample_order_matches_kpi(record, warning_metrics, "延期"))

        completed = make_record()
        completed["execution"]["actual_delivery_date"] = "2026-07-20"
        scored_metrics = {
            "attention_level": "completed",
            "many_delays": True,
            "assessment_score": 120,
        }
        self.assertFalse(dashboard.sample_order_matches_kpi(completed, scored_metrics, "执行中"))
        self.assertTrue(dashboard.sample_order_matches_kpi(completed, scored_metrics, "多次延期"))
        self.assertTrue(dashboard.sample_order_matches_kpi(completed, scored_metrics, "平均考核分"))

    def test_filter_and_row_reuse_precalculated_metrics(self):
        record = make_record()
        metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 18))
        with patch.object(dashboard, "calculate_sample_order_metrics") as calculator:
            self.assertTrue(
                dashboard.sample_order_matches_filter(
                    record,
                    dashboard.FILTER_WARNING,
                    calculated_metrics=metrics,
                )
            )
            row = dashboard.build_sample_order_row(record, calculated_metrics=metrics)
        self.assertEqual(row["attention_level"], metrics["attention_level"])
        calculator.assert_not_called()

    def test_grid_row_keeps_card_information_and_detail_entry(self):
        record = make_record()
        record["execution"]["actual_delivery_date"] = "2026-07-21"
        record["extensions"] = [
            dashboard.normalize_extension(
                {"extension_id": "extension-1", "target_date": "2026-07-22", "reason": "等待物料"}
            )
        ]
        metrics = dashboard.calculate_sample_order_metrics(record, date(2026, 7, 21))

        row = dashboard.build_sample_order_grid_row(record, calculated_metrics=metrics)

        self.assertEqual(row["record_id"], "record-1")
        self.assertEqual(row["detail_action"], "详情")
        self.assertEqual(row["current_target_date"], "2026-07-22")
        self.assertEqual(row["actual_delivery_display"], "2026-07-21")
        self.assertIn("分", str(row["assessment_display"]))

    def test_grid_columns_use_header_filters_and_keep_action_unfiltered(self):
        columns = dashboard.get_sample_order_grid_columns()

        self.assertGreater(len(columns), 10)
        self.assertEqual(columns[0]["field"], "detail_action")
        self.assertEqual(columns[0]["pinned"], "left")
        self.assertFalse(columns[0]["filter"])
        self.assertTrue(all(column.get("filter") for column in columns[1:]))
        self.assertTrue(all(column.get("headerClass") == "sample-order-grid-header-center" for column in columns))
        self.assertTrue(
            all(
                isinstance(cell_style := column.get("cellStyle"), dict)
                and cell_style.get("textAlign") == "center"
                for column in columns
            )
        )
        self.assertTrue(all("width" in column for column in columns))
        self.assertTrue(all("Width" not in column for column in columns))

    def test_monthly_statistics_use_planned_month_and_completion_date(self):
        on_time = make_record()
        on_time["basic_info"]["planned_delivery_date"] = "2026-07-20"
        on_time["execution"]["actual_delivery_date"] = "2026-07-20"

        delayed = make_record()
        delayed["record_id"] = "record-delayed"
        delayed["basic_info"]["planned_delivery_date"] = "2026-07-31"
        delayed["execution"]["actual_delivery_date"] = "2026-08-01"

        incomplete = make_record()
        incomplete["record_id"] = "record-incomplete"
        incomplete["basic_info"]["planned_delivery_date"] = "2026-06-15"
        incomplete["special_status"].update({"status": "暂停", "reason": "等待客户"})

        outside_range = make_record()
        outside_range["record_id"] = "record-outside"
        outside_range["basic_info"]["planned_delivery_date"] = "2025-07-31"

        future = make_record()
        future["record_id"] = "record-future"
        future["basic_info"]["planned_delivery_date"] = "2026-09-15"

        statistics = dashboard.get_sample_order_monthly_statistics(
            {
                record["record_id"]: record
                for record in (on_time, delayed, incomplete, outside_range, future)
            },
            date(2026, 7, 28),
        )
        by_month = {item["month"]: item for item in statistics}

        self.assertEqual(len(statistics), 14)
        self.assertEqual(statistics[0]["month"], "2025-08")
        self.assertEqual(statistics[-1]["month"], "2026-09")
        self.assertEqual(by_month["2026-06"]["incomplete"], 1)
        self.assertEqual(by_month["2026-06"]["total"], 1)
        self.assertEqual(by_month["2026-07"]["on_time_completed"], 1)
        self.assertEqual(by_month["2026-07"]["delayed_completed"], 1)
        self.assertEqual(by_month["2026-07"]["total"], 2)
        self.assertEqual(by_month["2026-09"]["incomplete"], 1)
        self.assertEqual(by_month["2026-09"]["total"], 1)

    def test_statistics_chart_is_stacked_and_displays_monthly_totals(self):
        statistics = [
            {
                "month": "2026-07",
                "on_time_completed": 2,
                "delayed_completed": 1,
                "incomplete": 3,
                "total": 6,
            }
        ]

        chart = dashboard.build_sample_order_statistics_chart(statistics)

        self.assertEqual(chart["xAxis"]["data"], ["2026-07"])
        self.assertEqual([item["stack"] for item in chart["series"][:3]], ["statistics"] * 3)
        self.assertEqual(chart["series"][3]["data"], [6])
        self.assertTrue(chart["series"][3]["label"]["show"])

    def test_sample_count_basis_sums_application_quantity(self):
        on_time = make_record()
        on_time["basic_info"]["application_qty"] = 2
        on_time["basic_info"]["planned_delivery_date"] = "2026-07-20"
        on_time["execution"]["actual_delivery_date"] = "2026-07-20"

        delayed = make_record()
        delayed["record_id"] = "record-delayed"
        delayed["basic_info"]["application_qty"] = 3
        delayed["basic_info"]["planned_delivery_date"] = "2026-07-20"
        delayed["execution"]["actual_delivery_date"] = "2026-07-21"

        incomplete = make_record()
        incomplete["record_id"] = "record-incomplete"
        incomplete["basic_info"]["application_qty"] = 4
        incomplete["basic_info"]["planned_delivery_date"] = "2026-07-20"

        statistics = dashboard.get_sample_order_monthly_statistics(
            {record["record_id"]: record for record in (on_time, delayed, incomplete)},
            date(2026, 7, 28),
            count_basis="samples",
        )
        july = next(item for item in statistics if item["month"] == "2026-07")

        self.assertEqual(july["on_time_completed"], 2)
        self.assertEqual(july["delayed_completed"], 3)
        self.assertEqual(july["incomplete"], 4)
        self.assertEqual(july["total"], 9)

        chart = dashboard.build_sample_order_statistics_chart(statistics, value_name="样品数")
        self.assertEqual(chart["yAxis"]["name"], "样品数")
        self.assertEqual(chart["series"][-1]["name"], "总样品数")

    def test_statistics_details_match_clicked_month_category_and_basis(self):
        on_time = make_record()
        on_time["basic_info"].update({"sample_order_no": "Y-ON-TIME", "application_qty": 2})
        on_time["basic_info"]["planned_delivery_date"] = "2026-07-31"
        on_time["execution"]["actual_delivery_date"] = "2026-07-30"

        delayed = make_record()
        delayed["record_id"] = "record-delayed"
        delayed["basic_info"].update({"sample_order_no": "Y-DELAYED", "application_qty": 3})
        delayed["basic_info"]["planned_delivery_date"] = "2026-07-31"
        delayed["execution"]["actual_delivery_date"] = "2026-08-02"

        records = {record["record_id"]: record for record in (on_time, delayed)}
        planned_details = dashboard.get_sample_order_statistics_details(
            records,
            "2026-07",
            "延期完成",
            date_basis="planned",
        )
        actual_details = dashboard.get_sample_order_statistics_details(
            records,
            "2026-08",
            "延期完成",
            date_basis="actual",
        )

        self.assertEqual([item["sample_order_no"] for item in planned_details], ["Y-DELAYED"])
        self.assertEqual([item["sample_order_no"] for item in actual_details], ["Y-DELAYED"])
        self.assertEqual(actual_details[0]["application_qty"], 3)

    def test_delay_reason_statistics_support_threshold_top_n_and_sample_count(self):
        first = make_record()
        first["basic_info"].update({"planned_delivery_date": "2026-07-20", "application_qty": 3})
        first["extensions"] = [
            dashboard.normalize_extension({"reason": "等待物料", "target_date": "2026-07-25"}),
            dashboard.normalize_extension({"reason": "等待物料", "target_date": "2026-07-28"}),
            dashboard.normalize_extension({"reason": "客户变更", "target_date": "2026-07-30"}),
        ]
        first["delay_nature"]["tag"] = "等待物料"

        second = make_record()
        second["record_id"] = "record-second"
        second["basic_info"].update({"planned_delivery_date": "2026-07-21", "application_qty": 2})
        second["extensions"] = [
            dashboard.normalize_extension({"reason": "等待物料", "target_date": "2026-07-26"})
        ]
        second["delay_nature"]["tag"] = "等待物料"

        third = make_record()
        third["record_id"] = "record-third"
        third["basic_info"].update({"planned_delivery_date": "2026-06-20", "application_qty": 4})
        third["extensions"] = [
            dashboard.normalize_extension({"reason": "内部排产", "target_date": "2026-06-25"})
        ]
        third["delay_nature"]["tag"] = "内部排产"

        outside = make_record()
        outside["record_id"] = "record-outside"
        outside["basic_info"]["planned_delivery_date"] = "2025-08-20"
        outside["extensions"] = [
            dashboard.normalize_extension({"reason": "等待物料", "target_date": "2025-08-25"})
        ]
        outside["delay_nature"]["tag"] = "等待物料"

        unmarked = make_record()
        unmarked["record_id"] = "record-unmarked"
        unmarked["basic_info"]["planned_delivery_date"] = "2026-07-22"
        unmarked["extensions"] = [
            dashboard.normalize_extension({"reason": "等待物料", "target_date": "2026-07-27"})
        ]
        records = {
            record["record_id"]: record
            for record in (first, second, third, outside, unmarked)
        }

        order_statistics = dashboard.get_sample_order_delay_reason_statistics(
            records,
            date(2026, 8, 10),
            minimum_threshold=1,
            top_n=2,
        )
        self.assertEqual([item["reason"] for item in order_statistics["visible_reasons"]], ["等待物料"])
        self.assertEqual([item["reason"] for item in order_statistics["top_reasons"]], ["等待物料", "内部排产"])
        waiting_material = order_statistics["top_reasons"][0]
        july_index = order_statistics["months"].index("2026-07")
        self.assertEqual(waiting_material["total"], 2)
        self.assertEqual(waiting_material["monthly"][july_index], 2)

        sample_statistics = dashboard.get_sample_order_delay_reason_statistics(
            records,
            date(2026, 8, 10),
            count_basis="samples",
            minimum_threshold=4,
            top_n=2,
        )
        self.assertEqual(sample_statistics["visible_reasons"][0]["reason"], "等待物料")
        self.assertEqual(sample_statistics["visible_reasons"][0]["total"], 5)
        impact_chart = dashboard.build_sample_order_delay_reason_impact_chart(sample_statistics)
        trend_chart = dashboard.build_sample_order_delay_reason_trend_chart(sample_statistics)
        self.assertEqual(impact_chart["xAxis"]["data"], ["等待物料"])
        self.assertEqual(impact_chart["yAxis"]["name"], "样品数")
        self.assertEqual(impact_chart["yAxis"]["nameLocation"], "middle")
        self.assertEqual(trend_chart["yAxis"]["name"], "样品数")
        self.assertEqual(trend_chart["yAxis"]["nameLocation"], "middle")

    def test_actual_date_basis_groups_completed_orders_and_excludes_incomplete(self):
        on_time = make_record()
        on_time["basic_info"]["planned_delivery_date"] = "2026-07-31"
        on_time["execution"]["actual_delivery_date"] = "2026-07-30"

        delayed = make_record()
        delayed["record_id"] = "record-delayed"
        delayed["basic_info"]["planned_delivery_date"] = "2026-07-31"
        delayed["execution"]["actual_delivery_date"] = "2026-08-02"

        incomplete = make_record()
        incomplete["record_id"] = "record-incomplete"
        incomplete["basic_info"]["planned_delivery_date"] = "2026-08-15"

        statistics = dashboard.get_sample_order_monthly_statistics(
            {record["record_id"]: record for record in (on_time, delayed, incomplete)},
            date(2026, 8, 20),
            date_basis="actual",
        )
        by_month = {item["month"]: item for item in statistics}

        self.assertEqual(by_month["2026-07"]["on_time_completed"], 1)
        self.assertEqual(by_month["2026-07"]["total"], 1)
        self.assertEqual(by_month["2026-08"]["delayed_completed"], 1)
        self.assertEqual(by_month["2026-08"]["incomplete"], 0)
        self.assertEqual(by_month["2026-08"]["total"], 1)

        chart = dashboard.build_sample_order_statistics_chart(
            statistics,
            include_incomplete=False,
        )
        self.assertEqual([item["name"] for item in chart["series"][:-1]], ["按时完成", "延期完成"])

    def test_legacy_two_delay_fields_are_migrated_to_extension_history(self):
        raw = make_record()
        raw.pop("extensions", None)
        raw["delay"] = {
            "first_target_date": "2026-07-25",
            "first_reason": "第一次延期",
            "second_target_date": "2026-07-30",
            "second_reason": "第二次延期",
        }
        merged = dashboard.merge_with_sample_order_template(raw)
        self.assertEqual(len(merged["extensions"]), 2)
        self.assertEqual(merged["extensions"][1]["target_date"], "2026-07-30")

    def test_pending_count_ignores_completed_record(self):
        overdue = make_record()
        completed = make_record()
        completed["record_id"] = "record-2"
        completed["execution"]["actual_delivery_date"] = "2026-07-20"
        records = {"record-1": overdue, "record-2": completed}
        self.assertEqual(
            dashboard.get_sample_order_dashboard_pending_count(
                records,
                date(2026, 7, 21),
                current_role="研发样品组长",
            ),
            1,
        )

    def test_leader_badge_counts_only_overdue_orders(self):
        overdue = make_record()

        warning = make_record()
        warning["record_id"] = "record-warning"
        warning["basic_info"]["planned_delivery_date"] = "2026-07-24"

        missing_date = make_record()
        missing_date["record_id"] = "record-missing"
        missing_date["basic_info"]["planned_delivery_date"] = ""

        paused = make_record()
        paused["record_id"] = "record-paused"
        paused["special_status"].update({"status": "暂停", "reason": "等待物料"})

        records = {
            record["record_id"]: record
            for record in [overdue, warning, missing_date, paused]
        }
        self.assertEqual(
            dashboard.get_sample_order_dashboard_pending_count(
                records,
                date(2026, 7, 21),
                current_role="研发样品组长",
            ),
            1,
        )

    def test_completed_delayed_order_requires_nature_mark(self):
        record = make_record()
        record["execution"]["actual_delivery_date"] = "2026-07-25"
        record["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "排产调整"}
            )
        ]

        self.assertTrue(dashboard.is_delay_nature_pending(record))
        record["delay_nature"]["tag"] = "内部排产"
        self.assertFalse(dashboard.is_delay_nature_pending(record))

    def test_delay_nature_catalog_prioritizes_frequently_used_tags(self):
        first = make_record()
        first["delay_nature"]["tag"] = "客户变更"
        second = make_record()
        second["record_id"] = "record-2"
        second["delay_nature"]["tag"] = "内部排产"
        third = make_record()
        third["record_id"] = "record-3"
        third["delay_nature"]["tag"] = "客户变更"

        catalog = dashboard.get_delay_nature_catalog(
            {"record-1": first, "record-2": second, "record-3": third}
        )

        self.assertEqual(catalog, ["客户变更", "内部排产"])

    def test_pending_nature_card_is_yellow_only_for_marker_role(self):
        manager_classes, manager_border = dashboard.get_sample_order_card_palette(
            "completed",
            nature_pending=True,
            can_mark_delay_nature=True,
        )
        other_classes, other_border = dashboard.get_sample_order_card_palette(
            "completed",
            nature_pending=True,
            can_mark_delay_nature=False,
        )

        self.assertIn("bg-yellow-50", manager_classes)
        self.assertEqual(manager_border, "#eab308")
        self.assertIn("bg-green-50", other_classes)
        self.assertEqual(other_border, "#22c55e")

    def test_manager_badge_counts_completed_delayed_orders_without_nature(self):
        pending = make_record()
        pending["execution"]["actual_delivery_date"] = "2026-07-25"
        pending["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "原因较随意"}
            )
        ]
        tagged = copy.deepcopy(pending)
        tagged["record_id"] = "record-2"
        tagged["delay_nature"]["tag"] = "供应异常"
        in_progress = make_record()
        in_progress["record_id"] = "record-3"
        records = {"record-1": pending, "record-2": tagged, "record-3": in_progress}

        self.assertEqual(
            dashboard.get_sample_order_dashboard_pending_count(
                records,
                date(2026, 7, 21),
                current_role="研发经理",
            ),
            1,
        )
        self.assertEqual(
            dashboard.get_sample_order_dashboard_pending_count(
                records,
                date(2026, 7, 21),
                current_role="研发助理",
            ),
            0,
        )
        self.assertEqual(
            dashboard.get_sample_order_dashboard_pending_count(
                records,
                date(2026, 7, 21),
                current_role="研发样品组长",
            ),
            1,
        )

    def test_combined_permissions_badge_counts_pending_union(self):
        """同时拥有延期和性质权限时，应合并两类待办并按订单计数。"""
        overdue = make_record()
        nature_pending = make_record()
        nature_pending["record_id"] = "record-nature"
        nature_pending["execution"]["actual_delivery_date"] = "2026-07-25"
        nature_pending["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "内部排产"}
            )
        ]
        normal = make_record()
        normal["record_id"] = "record-normal"
        normal["basic_info"]["planned_delivery_date"] = "2026-07-30"
        records = {
            record["record_id"]: record
            for record in [overdue, nature_pending, normal]
        }

        with (
            patch.object(dashboard, "is_sample_order_delay_editor", return_value=True),
            patch.object(
                dashboard,
                "is_sample_order_delay_nature_marker",
                return_value=True,
            ),
        ):
            count = dashboard.get_sample_order_dashboard_pending_count(
                records,
                date(2026, 7, 21),
                current_user="测试用户",
                current_role="任意历史角色",
            )

        self.assertEqual(count, 2)

    def test_my_pending_filter_uses_current_operation_permissions(self):
        """我的待办筛选应只显示当前用户有权处理的业务记录。"""
        overdue = make_record()
        nature_pending = make_record()
        nature_pending["execution"]["actual_delivery_date"] = "2026-07-25"
        nature_pending["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "内部排产"}
            )
        ]

        self.assertTrue(
            dashboard.sample_order_matches_filter(
                overdue,
                dashboard.FILTER_MY_PENDING,
                date(2026, 7, 21),
                can_edit_delay=True,
            )
        )
        self.assertFalse(
            dashboard.sample_order_matches_filter(
                overdue,
                dashboard.FILTER_MY_PENDING,
                date(2026, 7, 21),
                can_mark_delay_nature=True,
            )
        )
        self.assertTrue(
            dashboard.sample_order_matches_filter(
                nature_pending,
                dashboard.FILTER_MY_PENDING,
                date(2026, 7, 21),
                can_mark_delay_nature=True,
            )
        )


class SampleOrderExcelImportTests(unittest.TestCase):
    def test_excel_parser_maps_business_columns_and_reports_invalid_rows(self):
        workbook = Workbook()
        worksheet = workbook.active
        assert worksheet is not None
        worksheet.title = "导入数据"
        worksheet.append(["研发部样品单执行情况记录表"])
        worksheet.append([])
        worksheet.append(
            [
                "样品单号",
                "客户编码",
                "产品型号 ",
                "申请数量",
                "申请日期",
                "申请人",
                "计划\n交货日期",
                "备注",
                "实际\n交货日期",
                "制样\n负责人",
                "首次延期\n目标日期",
                "首次\n延期原因",
                "二次延期\n目标日期",
                "二次\n延期原因",
            ]
        )
        worksheet.append(
            [
                "Y26072101",
                19021034,
                "RFTS-TEST",
                2,
                datetime(2026, 7, 21),
                "申请人A",
                datetime(2026, 7, 25),
                "测试备注",
                None,
                "负责人A",
                datetime(2026, 7, 26),
                "等待确认",
                datetime(2026, 7, 27),
                "再次调整",
            ]
        )
        worksheet.append(
            [
                "Y26072102",
                19021035,
                "RFTS-BAD",
                1,
                datetime(2026, 7, 21),
                "",
                datetime(2026, 7, 25),
            ]
        )
        worksheet.append(
            [
                "Y26072103",
                19021036,
                "RFTS-WARNING",
                1,
                datetime(2026, 7, 21),
                "申请人B",
                datetime(2026, 7, 25),
                "",
                None,
                "负责人B",
                datetime(2026, 7, 26),
                "",
            ]
        )
        output = io.BytesIO()
        workbook.save(output)

        preview = dashboard.parse_sample_order_excel(output.getvalue(), "导入测试.xlsx")

        self.assertEqual(preview.total_rows, 3)
        self.assertEqual(len(preview.records), 2)
        self.assertEqual(len(preview.errors), 1)
        self.assertEqual(len(preview.warnings), 1)
        record = preview.records[0]
        self.assertEqual(record["basic_info"]["customer_code"], "19021034")
        self.assertEqual(record["basic_info"]["application_date"], "2026-07-21")
        self.assertEqual(record["execution"]["sample_owner"], "叶子浩")
        self.assertEqual(len(record["extensions"]), 2)
        self.assertIn("请填写：申请人", preview.errors[0])
        warning_record = preview.records[1]
        self.assertEqual(
            warning_record["extensions"][0]["reason"],
            "历史Excel未填写延期原因",
        )


class SampleOrderValidationTests(unittest.TestCase):
    def test_role_permissions_are_separated(self):
        self.assertTrue(dashboard.is_sample_order_base_editor("研发助理"))
        self.assertFalse(dashboard.is_sample_order_delay_editor("研发助理"))
        self.assertTrue(dashboard.is_sample_order_delay_editor("研发样品组长"))
        self.assertFalse(dashboard.is_sample_order_base_editor("研发样品组长"))
        self.assertTrue(dashboard.is_sample_order_special_status_editor("研发样品组长"))
        self.assertTrue(dashboard.is_sample_order_delay_nature_marker("研发经理"))
        self.assertFalse(dashboard.is_sample_order_delay_nature_marker("研发样品组长"))
        self.assertTrue(dashboard.is_sample_order_admin("admin"))
        self.assertTrue(dashboard.can_view_sample_order_average_score("研发样品组长"))
        self.assertTrue(dashboard.can_view_sample_order_average_score("研发经理"))
        self.assertFalse(dashboard.can_view_sample_order_average_score("研发助理"))
        self.assertFalse(dashboard.can_view_sample_order_average_score("admin"))

    def test_database_runtime_uses_stable_permission_instead_of_role_text(self):
        """存在用户服务时，页面权限判断必须委托给稳定权限入口。"""
        fake_app = SimpleNamespace(state=SimpleNamespace(user_service=object()))
        with (
            patch.object(dashboard, "app", fake_app),
            patch.object(dashboard, "can", return_value=True) as permission_check,
        ):
            allowed = dashboard.is_sample_order_base_editor("完全无关的旧角色", "张三")

        self.assertTrue(allowed)
        self.assertEqual(
            permission_check.call_args.args[2],
            dashboard.SAMPLE_ORDER_BASE_EDIT_PERMISSION,
        )

    def test_delay_date_and_reason_must_be_filled_together(self):
        record = make_record()
        record["extensions"] = [dashboard.normalize_extension({"target_date": "2026-07-25"})]
        errors = dashboard.validate_sample_order_submission(
            record,
            check_basic=False,
            check_execution=False,
            check_delay=True,
            check_special_status=False,
        )
        self.assertIn("第1次延期的目标日期和原因必须完整填写", errors)

    def test_new_extension_target_can_be_earlier_than_previous_target(self):
        record = make_record()
        record["extensions"] = [
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-1",
                    "target_date": "2026-07-28",
                    "reason": "客户原因",
                }
            ),
            dashboard.normalize_extension({"target_date": "2026-07-25", "reason": "再次变更"}),
        ]
        errors = dashboard.validate_sample_order_submission(
            record,
            check_basic=False,
            check_execution=False,
            check_delay=True,
            check_special_status=False,
            today=date(2026, 7, 20),
        )
        self.assertEqual(errors, [])

    def test_new_extension_target_cannot_be_earlier_than_today(self):
        record = make_record()
        record["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-19", "reason": "排期提前"}
            )
        ]
        errors = dashboard.validate_sample_order_submission(
            record,
            check_basic=False,
            check_execution=False,
            check_delay=True,
            check_special_status=False,
            today=date(2026, 7, 20),
        )
        self.assertIn("第1次延期目标日期不能早于当天", errors)

    def test_actual_delivery_date_cannot_be_later_than_today(self):
        record = make_record()
        record["execution"]["actual_delivery_date"] = "2026-07-21"

        errors = dashboard.validate_sample_order_submission(
            record,
            check_basic=False,
            check_execution=True,
            check_delay=False,
            check_special_status=False,
            today=date(2026, 7, 20),
        )

        self.assertIn("实际交货日期不能晚于当天", errors)

        record["execution"]["actual_delivery_date"] = "2026-07-20"
        errors = dashboard.validate_sample_order_submission(
            record,
            check_basic=False,
            check_execution=True,
            check_delay=False,
            check_special_status=False,
            today=date(2026, 7, 20),
        )
        self.assertNotIn("实际交货日期不能晚于当天", errors)

    def test_pause_requires_reason_when_configured(self):
        record = make_record()
        record["special_status"]["status"] = "暂停"
        errors = dashboard.validate_sample_order_submission(
            record,
            check_basic=False,
            check_execution=False,
            check_delay=False,
            check_special_status=True,
        )
        self.assertIn("设置订单特殊状态时必须填写原因", errors)


class SampleOrderAtomicSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_save_preserves_leader_fields(self):
        stored = make_record()
        stored["extensions"] = [
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-1",
                    "target_date": "2026-07-25",
                    "reason": "客户原因",
                }
            )
        ]
        stored["special_status"].update({"status": "暂停", "reason": "等待客户"})
        submitted = copy.deepcopy(stored)
        submitted["basic_info"]["remark"] = "研发助理更新"
        submitted["extensions"][0]["reason"] = "不应被写入"
        submitted["special_status"].update({"status": "作废", "reason": "不应被写入"})
        saved: dict[str, object] = {}

        async def fake_atomic(_namespace, _entity_id, callback):
            updated = callback(copy.deepcopy(stored))
            saved["record"] = updated
            return True

        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=AsyncMock()),
        ):
            result = await dashboard.save_sample_order_record(
                submitted,
                "助理A",
                "研发助理",
                is_new=False,
            )

        self.assertTrue(result.changed)
        saved_record = cast(dict[str, Any], saved["record"])
        self.assertEqual(saved_record["basic_info"]["remark"], "研发助理更新")
        self.assertEqual(saved_record["extensions"][0]["reason"], "客户原因")
        self.assertEqual(saved_record["special_status"]["status"], "暂停")

    async def test_leader_save_cannot_change_execution_and_can_append_extension(self):
        stored = make_record()
        submitted = copy.deepcopy(stored)
        submitted["basic_info"]["product_model"] = "不应被写入"
        submitted["execution"]["sample_owner"] = "不应被写入"
        submitted["extensions"].append(
            dashboard.normalize_extension(
                {
                    "target_date": (date.today() + timedelta(days=1)).isoformat(),
                    "reason": "等待客户确认",
                }
            )
        )
        saved: dict[str, object] = {}

        async def fake_atomic(_namespace, _entity_id, callback):
            updated = callback(copy.deepcopy(stored))
            saved["record"] = updated
            return True

        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=AsyncMock()),
            patch.object(
                dashboard,
                "schedule_background_task",
                side_effect=lambda coroutine, _name: coroutine.close(),
            ),
        ):
            result = await dashboard.save_sample_order_record(
                submitted,
                "组长A",
                "研发样品组长",
                is_new=False,
            )

        self.assertTrue(result.changed)
        saved_record = cast(dict[str, Any], saved["record"])
        self.assertEqual(saved_record["basic_info"]["product_model"], "RFTS-0001")
        self.assertEqual(saved_record["execution"]["sample_owner"], "")
        self.assertEqual(saved_record["extensions"][0]["reason"], "等待客户确认")

    async def test_existing_extension_history_cannot_be_rewritten(self):
        stored = make_record()
        stored["extensions"] = [
            dashboard.normalize_extension(
                {
                    "extension_id": "extension-1",
                    "target_date": "2026-07-25",
                    "reason": "客户原因",
                }
            )
        ]
        submitted = copy.deepcopy(stored)
        submitted["extensions"][0]["reason"] = "篡改历史"

        async def fake_atomic(_namespace, _entity_id, callback):
            result = callback(copy.deepcopy(stored))
            self.assertIs(result, db_storage.ATOMIC_NO_UPDATE)
            return True

        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=AsyncMock()),
        ):
            result = await dashboard.save_sample_order_record(
                submitted,
                "组长A",
                "研发样品组长",
                is_new=False,
            )

        self.assertFalse(result.changed)
        self.assertEqual(result.code, "extension_history_conflict")

    async def test_special_status_change_is_recorded(self):
        stored = make_record()
        submitted = copy.deepcopy(stored)
        submitted["special_status"].update({"status": "暂停", "reason": "等待物料"})
        saved: dict[str, object] = {}

        async def fake_atomic(_namespace, _entity_id, callback):
            updated = callback(copy.deepcopy(stored))
            saved["record"] = updated
            return True

        schedule = Mock(side_effect=lambda coroutine, _name: coroutine.close())
        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=AsyncMock()),
            patch.object(dashboard, "schedule_background_task", new=schedule),
        ):
            result = await dashboard.save_sample_order_record(
                submitted,
                "组长A",
                "研发样品组长",
                is_new=False,
            )

        self.assertTrue(result.changed)
        saved_record = cast(dict[str, Any], saved["record"])
        self.assertEqual(saved_record["special_status"]["status"], "暂停")
        self.assertEqual(len(saved_record["special_status"]["history"]), 1)
        schedule.assert_called_once()

    async def test_stale_revision_is_rejected_inside_atomic_update(self):
        stored = make_record()
        stored["_revision"] = 2
        submitted = make_record()

        async def fake_atomic(_namespace, _entity_id, callback):
            result = callback(copy.deepcopy(stored))
            self.assertIs(result, db_storage.ATOMIC_NO_UPDATE)
            return True

        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=AsyncMock()),
        ):
            result = await dashboard.save_sample_order_record(
                submitted,
                "助理A",
                "研发助理",
                is_new=False,
            )

        self.assertFalse(result.changed)
        self.assertEqual(result.code, "revision_conflict")

    async def test_manager_marks_delay_nature_atomically(self):
        stored = make_record()
        stored["execution"]["actual_delivery_date"] = "2026-07-25"
        stored["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "排产调整"}
            )
        ]
        saved: dict[str, object] = {}

        async def fake_atomic(_namespace, _entity_id, callback):
            updated = callback(copy.deepcopy(stored))
            saved["record"] = updated
            return True

        set_version = AsyncMock()
        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=set_version),
        ):
            result = await dashboard.mark_sample_order_delay_nature(
                "record-1",
                "内部排产",
                "经理A",
                "研发经理",
                expected_revision=1,
            )

        self.assertTrue(result.changed)
        saved_record = cast(dict[str, Any], saved["record"])
        self.assertEqual(saved_record["delay_nature"]["tag"], "内部排产")
        self.assertEqual(saved_record["delay_nature"]["marked_by"], "经理A")
        self.assertEqual(len(saved_record["delay_nature"]["history"]), 1)
        self.assertEqual(saved_record["_revision"], 2)
        set_version.assert_awaited_once()

    async def test_delay_nature_mark_rejects_stale_revision(self):
        stored = make_record()
        stored["_revision"] = 2
        stored["execution"]["actual_delivery_date"] = "2026-07-25"
        stored["extensions"] = [
            dashboard.normalize_extension(
                {"target_date": "2026-07-25", "reason": "排产调整"}
            )
        ]

        async def fake_atomic(_namespace, _entity_id, callback):
            updated = callback(copy.deepcopy(stored))
            self.assertIs(updated, db_storage.ATOMIC_NO_UPDATE)
            return True

        with (
            patch.object(db_storage, "atomic_json_entity_update", side_effect=fake_atomic),
            patch.object(db_storage, "set_item", new=AsyncMock()),
        ):
            result = await dashboard.mark_sample_order_delay_nature(
                "record-1",
                "内部排产",
                "经理A",
                "研发经理",
                expected_revision=1,
            )

        self.assertFalse(result.changed)
        self.assertEqual(result.code, "revision_conflict")

    async def test_non_manager_cannot_mark_delay_nature(self):
        with patch.object(db_storage, "atomic_json_entity_update", new=AsyncMock()) as atomic_update:
            result = await dashboard.mark_sample_order_delay_nature(
                "record-1",
                "内部排产",
                "组长A",
                "研发样品组长",
                expected_revision=1,
            )

        self.assertFalse(result.changed)
        self.assertEqual(result.code, "forbidden")
        atomic_update.assert_not_awaited()

    async def test_excel_import_is_atomic_and_keeps_identical_rows(self):
        first = make_record()
        first["record_id"] = ""
        second = copy.deepcopy(first)
        second["basic_info"]["application_qty"] = 3
        saved: dict[str, object] = {}

        async def fake_insert(_namespace, entities):
            saved["records"] = entities
            return True

        with (
            patch.object(dashboard, "get_all_sample_order_records", return_value={}),
            patch.object(db_storage, "insert_json_entities", side_effect=fake_insert),
            patch.object(db_storage, "set_item", new=AsyncMock()),
        ):
            result = await dashboard.import_sample_order_records(
                [first, copy.deepcopy(first), second],
                "助理A",
                "研发助理",
                source_name="导入测试.xlsx",
            )

        self.assertEqual(result.imported_count, 3)
        stored_records = cast(dict[str, Any], saved["records"])
        self.assertEqual(len(stored_records), 3)
        self.assertTrue(
            all(record["import_info"]["source_name"] == "导入测试.xlsx" for record in stored_records.values())
        )
        self.assertTrue(all(record["_revision"] == 1 for record in stored_records.values()))
        self.assertTrue(
            all(record["execution"]["sample_owner"] == "叶子浩" for record in stored_records.values())
        )


class SampleOrderNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_debug_mode_redirects_all_extension_notifications_to_manager(self):
        record = make_record()

        send_message = AsyncMock(return_value=(True, "已发送"))
        events = [
            {
                "extension_id": "extension-1",
                "extension_number": 1,
                "target_date": "2026-07-25",
                "reason": "第一次延期",
                "created_by": "组长A",
                "created_role": "研发样品组长",
            },
            {
                "extension_id": "extension-3",
                "extension_number": 3,
                "target_date": "2026-08-05",
                "reason": "第三次延期",
                "created_by": "组长A",
                "created_role": "研发样品组长",
            },
        ]
        permission_resolver = AsyncMock(return_value="manager_userid")
        with (
            patch.object(
                dashboard,
                "resolve_permission_wecom_recipients",
                new=permission_resolver,
            ),
            patch.object(dashboard, "send_wecom_text_message", new=send_message),
            patch.object(dashboard, "SAMPLE_ORDER_REDIRECT_APPLICANT_NOTIFICATIONS_TO_MANAGER", True),
        ):
            failures = await dashboard._send_sample_order_change_notifications(record, events, None)

        self.assertEqual(failures, ())
        first_recipients = send_message.await_args_list[0].args[1]
        third_recipients = send_message.await_args_list[1].args[1]
        self.assertEqual(first_recipients, "manager_userid")
        self.assertEqual(third_recipients, "manager_userid")
        self.assertEqual(
            permission_resolver.await_args.args[0],
            dashboard.SAMPLE_ORDER_EXTENSION_NOTIFY_PERMISSION,
        )

    async def test_debug_mode_redirects_special_status_notification_to_manager(self):
        record = make_record()

        send_message = AsyncMock(return_value=(True, "已发送"))
        status_event = {
            "history_id": "status-1",
            "old_status": "正常",
            "status": "作废",
            "reason": "客户取消",
            "updated_by": "组长A",
            "updated_role": "研发样品组长",
        }
        permission_resolver = AsyncMock(return_value="manager_userid")
        with (
            patch.object(
                dashboard,
                "resolve_permission_wecom_recipients",
                new=permission_resolver,
            ),
            patch.object(dashboard, "send_wecom_text_message", new=send_message),
            patch.object(dashboard, "SAMPLE_ORDER_REDIRECT_APPLICANT_NOTIFICATIONS_TO_MANAGER", True),
        ):
            failures = await dashboard._send_sample_order_change_notifications(record, [], status_event)

        self.assertEqual(failures, ())
        send_message.assert_awaited_once()
        awaited_call = send_message.await_args
        assert awaited_call is not None
        recipients = awaited_call.args[1]
        self.assertEqual(recipients, "manager_userid")
        self.assertEqual(
            permission_resolver.await_args.args[0],
            dashboard.SAMPLE_ORDER_SPECIAL_STATUS_NOTIFY_PERMISSION,
        )

    async def test_disabling_debug_redirect_restores_applicant_notification(self):
        record = make_record()

        async def fake_resolve(targets, fallback_touser="", **_kwargs):
            if isinstance(targets, list) and targets and isinstance(targets[0], dict) and "names" in targets[0]:
                return "applicant_userid"
            return "manager_userid"

        send_message = AsyncMock(return_value=(True, "已发送"))
        events = [
            {
                "extension_id": "extension-1",
                "extension_number": 1,
                "target_date": "2026-07-25",
                "reason": "第一次延期",
                "created_by": "组长A",
                "created_role": "研发样品组长",
            }
        ]
        with (
            patch.object(
                dashboard,
                "SAMPLE_ORDER_REDIRECT_APPLICANT_NOTIFICATIONS_TO_MANAGER",
                False,
            ),
            patch.object(dashboard, "resolve_wecom_recipients", side_effect=fake_resolve),
            patch.object(dashboard, "send_wecom_text_message", new=send_message),
        ):
            failures = await dashboard._send_sample_order_change_notifications(record, events, None)

        self.assertEqual(failures, ())
        send_message.assert_awaited_once()
        awaited_call = send_message.await_args
        assert awaited_call is not None
        self.assertEqual(awaited_call.args[1], "applicant_userid")


class SampleOrderConfigTests(unittest.TestCase):
    def test_json_configuration_controls_warning_and_roles(self):
        custom_config = {
            "public_base_url": "http://example.test/",
            "warning_days": 5,
            "base_editor_roles": ["助理测试角色"],
            "delay_editor_roles": ["组长测试角色"],
            "special_status_editor_roles": ["状态测试角色"],
            "delay_nature_marker_roles": ["性质测试角色"],
            "admin_roles": ["管理员测试角色"],
            "special_statuses": ["暂停", "作废"],
            "special_status_reason_required": False,
            "delay_attention_threshold": 3,
            "wecom": {
                "redirect_applicant_notifications_to_manager": True,
                "notify_applicant_on_extension": False,
                "notify_applicant_on_special_status": True,
                "manager_notify_targets": [{"position": "经理测试职位"}],
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sample_order_dashboard_config.json"
            config_path.write_text(json.dumps(custom_config, ensure_ascii=False), encoding="utf-8")
            loaded = dashboard_config.load_sample_order_dashboard_config(config_path)

        self.assertEqual(loaded["warning_days"], 5)
        self.assertEqual(loaded["base_editor_roles"], ["助理测试角色"])
        self.assertEqual(loaded["delay_nature_marker_roles"], ["性质测试角色"])
        self.assertEqual(loaded["delay_attention_threshold"], 3)
        self.assertEqual(loaded["special_statuses"][0], "正常")
        self.assertTrue(loaded["wecom"]["redirect_applicant_notifications_to_manager"])
        self.assertFalse(loaded["wecom"]["notify_applicant_on_extension"])


if __name__ == "__main__":
    unittest.main()
