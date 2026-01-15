from nicegui import ui

# 1. 极简数据
tree_data = [
    {
        "name": "根节点",
        "value": "ROOT",
        "children": [{"name": "子节点A", "value": "A"}, {"name": "子节点B", "value": "B"}],
    }
]

# 2. 极简配置
echarts_option = {
    "tooltip": {"show": True},
    "series": [
        {
            "type": "tree",
            "data": tree_data,
            "top": "10%",
            "bottom": "10%",
            "left": "10%",
            "right": "10%",
            "symbol": "emptyCircle",
            "symbolSize": 20,
            "expandAndCollapse": True,  # 开启默认折叠，测试最基础功能
            "label": {"show": True, "position": "right", "fontSize": 16},
        }
    ],
}


@ui.page("/")
def main():
    with ui.column().classes("w-full h-screen p-4"):
        ui.label("ECharts 点击测试 (洁净室模式)").classes("text-xl font-bold")

        # 使用 SVG 渲染
        chart = ui.echart(echarts_option, renderer="svg").classes("w-full h-full border")

        # 简单绑定
        chart.on(
            "click", lambda e: ui.notify(f"点击成功! ID: {e.args['data']['value']}"), ["data", "componentType"]
        )  # 传回标准字段


ui.run()
