from collections.abc import Mapping


# 项目阶段与概述问题共同决定警示级别：0=低，1=还好，2=中，3=高，4=严重
OVERVIEW_WARNING_LEVELS = {
    "研发": {"need": 0, "none": 1, "false": 2},
    "转产": {"need": 1, "none": 2, "false": 3},
    "试产": {"need": 1, "none": 3, "false": 4},
    "量产": {"need": 1, "none": 3, "false": 4},
}


def get_overview_warning(project_state: str, counts: dict[str, int]) -> tuple[str, int] | None:
    """返回当前项目最需关注的概述问题及其综合警示级别。"""
    # 状态缺失时按量产处理，避免未知项目被低估风险
    warning_levels = OVERVIEW_WARNING_LEVELS.get(project_state, OVERVIEW_WARNING_LEVELS["量产"])
    active_keys = [key for key, count in counts.items() if count > 0 and key in warning_levels]
    if not active_keys:
        return None

    active_key = max(active_keys, key=lambda key: warning_levels[key])
    return active_key, warning_levels[active_key]


def get_overview_counts(state_dic: Mapping[str, str]) -> dict[str, int]:
    """统计三类待处理概述问题的数量。"""
    states = list(state_dic.values())
    return {
        "false": states.count("缺必填"),
        "none": states.count("有待定"),
        "need": states.count("缺需填"),
    }


def sort_overview_pending_items(
    pending_items: list[tuple[str, dict[str, str]]], project_states: Mapping[str, str]
) -> list[tuple[str, dict[str, str]]]:
    """按综合警示级别从高到低稳定排列概述待办。"""

    def warning_level(item: tuple[str, dict[str, str]]) -> int:
        project_name, state_dic = item
        warning = get_overview_warning(project_states.get(project_name, "未知"), get_overview_counts(state_dic))
        return warning[1] if warning is not None else -1

    return sorted(pending_items, key=warning_level, reverse=True)


def get_urgent_overview_projects(
    pending_items: Mapping[str, Mapping[str, str]],
    project_states: Mapping[str, str],
    minimum_level: int = 3,
) -> list[tuple[str, int]]:
    """返回达到紧急级别的概述项目，并按警示级别稳定降序排列。"""
    urgent_projects: list[tuple[str, int]] = []
    for project_name, state_dic in pending_items.items():
        project_state = project_states.get(project_name, "未知")
        if project_state in ["作废", "待定"]:
            continue

        warning = get_overview_warning(project_state, get_overview_counts(state_dic))
        if warning is not None and warning[1] >= minimum_level:
            urgent_projects.append((project_name, warning[1]))

    return sorted(urgent_projects, key=lambda item: item[1], reverse=True)
