"""首页卡片按可见数量和视窗上限均衡排列。"""


def get_dashboard_row_sizes(card_count: int, max_columns: int) -> list[int]:
    """优先采用等量的多列布局，无法整除时将各行数量差控制在一张。"""
    if card_count < 0 or max_columns < 1:
        raise ValueError("卡片数量不能为负数，每行上限必须大于零")
    if card_count == 0:
        return []
    for columns in range(min(card_count, max_columns), 1, -1):
        if card_count % columns == 0:
            return [columns] * (card_count // columns)
    row_count = (card_count + max_columns - 1) // max_columns
    columns, extra = divmod(card_count, row_count)
    return [columns + (row < extra) for row in range(row_count)]


def get_dashboard_layout_css(card_count: int) -> str:
    """生成各视窗断点的网格位置，半张卡片的网格步长使奇偶数量都能居中。"""
    styles = []
    for min_width, max_columns in ((0, 1), (640, 2), (1024, 4), (1280, 5)):
        rules = [
            ".dashboard-menu { "
            f"grid-template-columns: repeat({max_columns * 2}, minmax(0, 1fr)); "
            "}"
        ]
        index = 0
        for row, columns in enumerate(get_dashboard_row_sizes(card_count, max_columns), start=1):
            for column in range(columns):
                index += 1
                start = max_columns - columns + 1 + column * 2
                rules.append(
                    f".dashboard-menu > .dashboard-menu-card:nth-child({index}) {{ "
                    f"grid-row: {row}; grid-column: {start} / span 2; "
                    "}"
                )
        styles.append(f"@media (min-width: {min_width}px) {{ {' '.join(rules)} }}")
    return "\n".join(styles)
