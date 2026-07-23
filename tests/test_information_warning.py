from src.overview_warning import (
    get_overview_warning,
    get_urgent_overview_projects,
    sort_overview_pending_items,
)


def test_overview_warning_uses_project_and_overview_status_matrix():
    expected_levels = {
        "研发": {"need": 0, "none": 1, "false": 2},
        "转产": {"need": 1, "none": 2, "false": 3},
        "试产": {"need": 1, "none": 3, "false": 4},
        "量产": {"need": 1, "none": 3, "false": 4},
    }

    for project_state, issue_levels in expected_levels.items():
        for issue_key, warning_level in issue_levels.items():
            assert get_overview_warning(project_state, {issue_key: 1}) == (issue_key, warning_level)


def test_overview_warning_selects_highest_active_combination():
    assert get_overview_warning("量产", {"need": 3, "none": 2, "false": 0}) == ("none", 3)
    assert get_overview_warning("研发", {"need": 3, "none": 0, "false": 1}) == ("false", 2)


def test_overview_warning_handles_empty_and_unknown_project_state():
    assert get_overview_warning("研发", {"need": 0, "none": 0, "false": 0}) is None
    assert get_overview_warning("未知", {"false": 1}) == ("false", 4)


def test_overview_pending_items_are_stably_sorted_by_warning_level():
    pending_items = [
        ("低级项目", {"概述A": "缺需填"}),
        ("高级项目A", {"概述A": "缺必填"}),
        ("严重项目", {"概述A": "缺必填"}),
        ("中级项目", {"概述A": "缺必填"}),
        ("高级项目B", {"概述A": "有待定"}),
    ]
    project_states = {
        "低级项目": "研发",
        "高级项目A": "转产",
        "严重项目": "量产",
        "中级项目": "研发",
        "高级项目B": "试产",
    }

    sorted_items = sort_overview_pending_items(pending_items, project_states)

    assert [project_name for project_name, _ in sorted_items] == [
        "严重项目",
        "高级项目A",
        "高级项目B",
        "中级项目",
        "低级项目",
    ]


def test_urgent_overview_projects_filters_and_sorts_dashboard_alerts():
    pending_items = {
        "三级项目A": {"概述A": "缺必填"},
        "四级项目": {"概述A": "缺必填"},
        "二级项目": {"概述A": "缺必填"},
        "三级项目B": {"概述A": "有待定"},
        "作废项目": {"概述A": "缺必填"},
    }
    project_states = {
        "三级项目A": "转产",
        "四级项目": "量产",
        "二级项目": "研发",
        "三级项目B": "试产",
        "作废项目": "作废",
    }

    assert get_urgent_overview_projects(pending_items, project_states) == [
        ("四级项目", 4),
        ("三级项目A", 3),
        ("三级项目B", 3),
    ]
