import html
import json
import re

from nicegui import ui

from ..config import BASE_DIR

# 配置文件路径
CONFIG_FILE_PATH = f"{BASE_DIR}/config_service.json"


# ==========================================
# 1. 核心解析逻辑
# ==========================================
class QuestionTreeGenerator:
    def __init__(self, json_data: dict):
        self.raw_data = json_data.get("data", {})
        self.groups = {}

        for node_id, node in self.raw_data.items():
            gid = str(node.get("option_group_id", "Uncategorized"))
            if gid not in self.groups:
                self.groups[gid] = []
            self.groups[gid].append(node_id)

        self.sorted_group_ids = sorted(list(self.groups.keys()), key=lambda x: int(x) if x.isdigit() else 9999)

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = str(text)
        if len(text) > 15:
            text = text[:15] + "..."
        text = html.escape(text)  # HTML转义
        text = text.replace('"', "'").replace("\n", "<br/>")
        text = text.replace("[", "【").replace("]", "】")
        text = text.replace("(", "（").replace(")", "）")
        return text

    def _get_node_group(self, node_id: str) -> str:
        node = self.raw_data.get(node_id)
        return str(node.get("option_group_id", "Unknown")) if node else "Unknown"

    def _parse_parents(self, node_id: str, condition: str) -> list:
        parents = []
        if node_id == "1":
            return []

        if condition == "无条件":
            prev_id = str(int(node_id) - 1)
            if prev_id in self.raw_data:
                return [(prev_id, "下一步")]
            return []

        pattern = re.compile(r"(\d+)\s*(==|!=|any|in|>|<)")
        matches = pattern.findall(condition)

        if matches:
            for source_id, operator in matches:
                label = "关联"
                if operator == "==":
                    label = "等于"
                elif operator == "!=":
                    label = "非"
                elif operator == "any":
                    label = "包含"
                parents.append((source_id, label))
        else:
            prev_id = str(int(node_id) - 1)
            if prev_id in self.raw_data:
                parents.append((prev_id, "条件"))
        return parents

    def generate_mermaid_for_group(self, target_group_id: str) -> str:
        lines = ["graph TD"]

        # --- 样式定义 (已移除 rx, ry) ---
        lines.append("classDef internal fill:#e3f2fd,stroke:#1565c0,stroke-width:2px;")
        lines.append("classDef ghost fill:#f5f5f5,stroke:#9e9e9e,stroke-dasharray: 5 5;")

        defined_nodes = set()
        edges = []

        for node_id, node_data in self.raw_data.items():
            current_group = str(node_data.get("option_group_id"))
            parents = self._parse_parents(node_id, node_data.get("condition", ""))

            for parent_id, label in parents:
                if parent_id not in self.raw_data:
                    continue
                parent_group = self._get_node_group(parent_id)

                # 组内
                if parent_group == target_group_id and current_group == target_group_id:
                    edges.append(f'{parent_id} -- "{label}" --> {node_id}')

                # 入站 (外部 -> 内部)
                elif parent_group != target_group_id and current_group == target_group_id:
                    if parent_id not in defined_nodes:
                        p_txt = self._clean_text(self.raw_data[parent_id].get("guide_content"))
                        # 使用圆括号 () 表示圆角
                        lines.append(f'{parent_id}("<b>来自组{parent_group}</b><br/>{p_txt}"):::ghost')
                        defined_nodes.add(parent_id)
                    edges.append(f'{parent_id} -. "{label}" .-> {node_id}')

                # 出站 (内部 -> 外部)
                elif parent_group == target_group_id and current_group != target_group_id:
                    if node_id not in defined_nodes:
                        n_txt = self._clean_text(node_data.get("guide_content"))
                        # 使用圆括号 () 表示圆角
                        lines.append(f'{node_id}("<b>去往组{current_group}</b><br/>{n_txt}"):::ghost')
                        defined_nodes.add(node_id)
                    edges.append(f'{parent_id} -. "{label}" .-> {node_id}')

        if target_group_id in self.groups:
            for node_id in self.groups[target_group_id]:
                if node_id not in defined_nodes:
                    node = self.raw_data[node_id]
                    content = self._clean_text(node.get("guide_content", "无内容"))
                    # 使用方括号 [] 表示直角
                    lines.append(f'{node_id}["<b>[{node_id}]</b><br/>{content}"]:::internal')
                    defined_nodes.add(node_id)

        lines.extend(edges)
        return "\n".join(lines)


# ==========================================
# 2. 界面代码
# ==========================================
def load_data():
    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


@ui.page("/question_tree_tabs")
def question_tree_tabs():
    ui.add_head_html("<style>.mermaid-box svg { height: auto; width: auto; min-width: 100%; }</style>")

    full_data = load_data()
    if not full_data:
        ui.label("数据加载失败").classes("text-red-500")
        return

    generator = QuestionTreeGenerator(full_data)

    current_group = ui.number(value=1)
    scale = ui.number(value=1.0)

    # 注意：这里我们不再在外面定义 show_source，而是直接在布局里定义

    with ui.splitter(value=20).classes("w-full h-screen") as splitter:
        # === 左侧导航 ===
        with splitter.before:
            with ui.column().classes("w-full h-full p-2 bg-gray-100"):
                ui.label("分组导航").classes("font-bold text-gray-700 mb-2")
                with ui.scroll_area().classes("w-full h-full"):
                    with ui.tabs().props("vertical").bind_value(current_group).classes("w-full") as tabs:
                        for gid in generator.sorted_group_ids:
                            count = len(generator.groups[gid])
                            with ui.tab(gid).classes("justify-between w-full pl-2 pr-2"):
                                ui.label(f"Group {gid}")
                                ui.badge(str(count), color="grey-6").props("floating")

        # === 右侧画布 ===
        with splitter.after:
            with ui.column().classes("w-full h-full p-0 gap-0 bg-white"):
                # 工具栏
                with ui.row().classes("w-full p-2 border-b items-center justify-between bg-gray-50"):
                    ui.label().bind_text_from(
                        current_group, "value", backward=lambda x: f"当前查看: Group {x}"
                    ).classes("font-bold")

                    with ui.row().classes("items-center gap-2"):
                        with ui.row().classes("gap-2 text-xs text-gray-500 mr-4"):
                            ui.html(
                                '<span style="display:inline-block;width:10px;height:10px;background:#e3f2fd;border:1px solid #1565c0;"></span> 内部',
                                sanitize=False,
                            )
                            ui.html(
                                '<span style="display:inline-block;width:10px;height:10px;background:#f5f5f5;border:1px dashed #9e9e9e;"></span> 跨组',
                                sanitize=False,
                            )

                        ui.label("源码:").classes("text-xs")

                        # --- 修复点：直接在这里定义 Switch，不要调用 .render() ---
                        show_source = ui.switch("调试用").props("dense")

                        ui.separator().props("vertical")

                        ui.button(icon="remove", on_click=lambda: scale.set_value(scale.value - 0.1)).props(
                            "flat round dense"
                        )
                        ui.slider(min=0.5, max=3.0, step=0.1).bind_value(scale).classes("w-24")
                        ui.button(icon="add", on_click=lambda: scale.set_value(scale.value + 0.1)).props(
                            "flat round dense"
                        )

                # 源码显示区 (绑定到上面刚刚定义的 show_source)
                source_code_area = (
                    ui.code("loading...")
                    .classes("w-full p-4 bg-gray-800 text-white text-xs")
                    .bind_visibility_from(show_source)
                )

                # 图表显示区
                with ui.scroll_area().classes("w-full h-full p-4 bg-dots"):
                    canvas = ui.row().classes("origin-top-left transition-transform duration-200")

                    def do_zoom(e):
                        canvas.style(f"transform: scale({e.value})")

                    scale.on_value_change(do_zoom)

                    with canvas:
                        container = ui.column().classes("w-full")

                        def refresh():
                            container.clear()
                            gid = str(current_group.value)
                            try:
                                code = generator.generate_mermaid_for_group(gid)
                                source_code_area.set_content(code)

                                if not code.strip() or "graph TD" not in code:
                                    with container:
                                        ui.label("该组无数据").classes("text-gray-400")
                                    return

                                mermaid_config = {
                                    "flowchart": {"htmlLabels": True, "useMaxWidth": False},
                                    "securityLevel": "loose",
                                    "theme": "base",
                                }

                                with container:
                                    ui.mermaid(code).props(f"config='{json.dumps(mermaid_config)}'").classes(
                                        "mermaid-box bg-white p-4 border rounded shadow-sm"
                                    )

                            except Exception as e:
                                with container:
                                    ui.label(f"Error: {e}").classes("text-red-500")

                        refresh()
                        current_group.on_value_change(refresh)
