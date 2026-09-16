import re
import unittest

from src.dashboard_layout import get_dashboard_layout_css, get_dashboard_row_sizes


class DashboardLayoutTests(unittest.TestCase):
    def test_equal_rows_when_multiple_columns_can_divide_count(self):
        for count, limit, expected in (
            (6, 5, [3, 3]),
            (8, 5, [4, 4]),
            (9, 5, [3, 3, 3]),
            (10, 5, [5, 5]),
            (6, 4, [3, 3]),
            (8, 4, [4, 4]),
            (6, 2, [2, 2, 2]),
        ):
            with self.subTest(count=count, limit=limit):
                self.assertEqual(get_dashboard_row_sizes(count, limit), expected)

    def test_indivisible_counts_are_balanced(self):
        self.assertEqual(get_dashboard_row_sizes(7, 5), [4, 3])
        self.assertEqual(get_dashboard_row_sizes(7, 4), [4, 3])
        self.assertEqual(get_dashboard_row_sizes(5, 4), [3, 2])

    def test_all_counts_preserve_cards_and_respect_viewport_limit(self):
        for count in range(101):
            for limit in (1, 2, 4, 5):
                with self.subTest(count=count, limit=limit):
                    rows = get_dashboard_row_sizes(count, limit)
                    self.assertEqual(sum(rows), count)
                    self.assertTrue(all(1 <= size <= limit for size in rows))
                    if rows:
                        self.assertLessEqual(max(rows) - min(rows), 1)

    def test_empty_and_single_card(self):
        self.assertEqual(get_dashboard_row_sizes(0, 5), [])
        self.assertEqual(get_dashboard_row_sizes(1, 5), [1])

    def test_css_places_every_card_without_overlap_and_centers_each_row(self):
        for count in range(11):
            css = get_dashboard_layout_css(count)
            sections = re.split(r"@media \(min-width: \d+px\)", css)[1:]
            self.assertEqual(len(sections), 4)
            for section, limit in zip(sections, (1, 2, 4, 5)):
                positions = re.findall(
                    r"nth-child\((\d+)\) \{ grid-row: (\d+); grid-column: (\d+) / span 2;",
                    section,
                )
                self.assertEqual([int(index) for index, _, _ in positions], list(range(1, count + 1)))
                rows: dict[int, list[int]] = {}
                for _, row, start in positions:
                    rows.setdefault(int(row), []).append(int(start))
                for starts in rows.values():
                    self.assertEqual(starts[0] - 1, 2 * limit - (starts[-1] + 1))
                    self.assertTrue(all(right - left == 2 for left, right in zip(starts, starts[1:])))

    def test_invalid_inputs(self):
        for count, limit in ((-1, 5), (1, 0)):
            with self.assertRaises(ValueError):
                get_dashboard_row_sizes(count, limit)
