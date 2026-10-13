"""项目名称分类相关的共享规则。"""


def is_other_project_name(project_name: object) -> bool:
    """是否属于项目总表的“其它”分类。"""
    return "-" not in str(project_name or "")


def get_project_table_category(project_name: str) -> tuple[str, str]:
    """返回项目总表筛选使用的“大类、小类”。"""
    if is_other_project_name(project_name):
        return "其它", project_name

    parts = project_name.split("-")
    return parts[0], parts[1][:2]


def match_other_project_name(content: str, project_names: list[str]) -> tuple[str, list[str]]:
    """从内容中识别“其它”项目号，并排除较长项目号包含的短名称。"""
    normalized_content = content.casefold()
    matches = sorted(
        {
            project_name
            for project_name in project_names
            if project_name
            and is_other_project_name(project_name)
            and project_name.casefold() in normalized_content
        },
        key=lambda project_name: (-len(project_name), project_name),
    )
    if not matches:
        return "", []

    specific_matches = [
        project_name
        for project_name in matches
        if not any(
            project_name != other_name
            and project_name.casefold() in other_name.casefold()
            for other_name in matches
        )
    ]
    if len(specific_matches) != 1:
        return "", specific_matches
    return specific_matches[0], matches


def build_project_select_options(project_names: list[str]) -> dict[str, list[str]]:
    """按项目总表的大类、小类规则生成筛选选项。"""
    select_options: dict[str, list[str]] = {"所有": ["所有"], "RFTS": ["所有"]}

    for project_name in sorted(project_names):
        major, minor = get_project_table_category(project_name)
        if major == "其它":
            other_options = select_options.setdefault(major, ["所有"])
            if project_name not in other_options:
                other_options.append(project_name)
                other_options.sort(reverse=True)
            continue

        major_options = select_options.setdefault(major, ["所有"])
        if minor not in major_options:
            major_options.append(minor)
            major_options.sort(reverse=True)

    return select_options
