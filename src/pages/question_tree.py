import json
import re

from nicegui import ui

from ..config import BASE_DIR


# ==========================================
# 1. 核心解析逻辑：JSON -> Mermaid 语法
# ==========================================
class QuestionTreeGenerator:
    def __init__(self, json_data: dict):
        self.raw_data = json_data.get("data", {})
        # 过滤非数字键并排序
        self.sorted_keys = sorted([k for k in self.raw_data.keys() if k.isdigit()], key=lambda x: int(x))
        self.mermaid_code = ""

    def _clean_text(self, text: str) -> str:
        """
        清洗文本以适应 Mermaid 语法
        关键修复：必须处理 ] 符号，否则会破坏 Mermaid 的 ["..."] 结构
        """
        if not text:
            return ""

        text = str(text)
        # 1. 替换双引号为单引号，防止截断字符串
        text = text.replace('"', "'")
        # 2. 替换换行为 HTML 换行
        text = text.replace("\n", "<br/>")
        # 3. 关键：替换方括号，防止破坏节点定义语法 Node["内容"]
        text = text.replace("[", "【").replace("]", "】")
        # 4. 替换 # 号，防止被识别为注释或ID
        text = text.replace("#", "")

        # 限制长度，防止图表过大
        if len(text) > 25:
            return text[:25] + "..."
        return text

    def _parse_condition_to_edges(self, node_id: str, condition: str) -> list:
        edges = []
        if node_id == "1":
            return []

        if condition == "无条件":
            prev_id = str(int(node_id) - 1)
            if prev_id in self.raw_data:
                return [(prev_id, "下一步")]
            return []

        # 解析逻辑表达式
        pattern = re.compile(r"(\d+)\s*(==|!=|any|in|>|<)")
        matches = pattern.findall(condition)

        if matches:
            for source_id, operator in matches:
                label = ""
                if operator == "==":
                    val_match = re.search(rf"{source_id}==([^ ]+)", condition)
                    label = val_match.group(1) if val_match else "=="
                elif operator == "any":
                    val_match = re.search(rf"{source_id}any(\[[^\]]+\])", condition)
                    label = "包含" + (val_match.group(1) if val_match else "")
                elif operator == "!=":
                    label = "非"

                label = self._clean_text(label)
                edges.append((source_id, label))
        else:
            # 兜底逻辑：如果正则没匹配到，尝试连接上一个节点
            prev_id = str(int(node_id) - 1)
            if prev_id in self.raw_data:
                edges.append((prev_id, f"条件: {self._clean_text(condition)}"))

        return edges

    def generate_mermaid(self) -> str:
        lines = ["graph TD"]

        # 节点定义
        for key in self.sorted_keys:
            node = self.raw_data[key]
            node_id = node.get("node_id")
            content = self._clean_text(node.get("guide_content", "无内容"))

            # 使用 HTML 标签加粗 ID
            # 注意：NodeID["..."] 中的引号是 Mermaid 语法的关键
            node_def = f'{node_id}["<b>[{node_id}]</b><br/>{content}"]'
            lines.append(node_def)

            # 简单着色：根据条件内容高亮
            if "代工" in str(node.get("condition", "")):
                lines.append(f"style {node_id} fill:#e1f5fe,stroke:#01579b")

        # 连线定义
        for key in self.sorted_keys:
            node = self.raw_data[key]
            node_id = node.get("node_id")
            condition = node.get("condition", "")

            edges = self._parse_condition_to_edges(node_id, condition)
            for source_id, label in edges:
                if source_id in self.raw_data:
                    lines.append(f'{source_id} -- "{label}" --> {node_id}')

        self.mermaid_code = "\n".join(lines)
        return self.mermaid_code


# ==========================================
# 2. NiceGUI 界面集成 (优化版)
# ==========================================
def load_config_data():
    # 这里请将你的 json 文件的完整内容读取进来
    # 为演示方便，这里仅示意
    try:
        with open(f"{BASE_DIR}/config_service.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"data": {}}  # 返回空数据防止崩溃


@ui.page("/question_tree")
def admin_question_tree():
    # CSS: 确保 Mermaid SVG 自适应容器
    ui.add_head_html("""
        <style>
            .mermaid-box svg { height: auto; width: auto; min-width: 100%; }
        </style>
    """)

    # 1. 状态管理：缩放比例
    scale = ui.number(value=1.0, min=0.1, max=5.0, step=0.1)

    # 定义 Mermaid 组件的引用，用于后续导出和更新
    mermaid_view = None

    with ui.column().classes("w-full h-screen p-0 gap-0"):
        # --- 顶部工具栏 ---
        with ui.row().classes("w-full items-center justify-between p-4 bg-gray-100 border-b"):
            with ui.row().classes("items-center gap-4"):
                ui.icon("account_tree", size="md").classes("text-blue-600")
                with ui.column().classes("gap-0"):
                    ui.label("需求问卷逻辑树").classes("text-lg font-bold")
                    ui.label("鼠标滚轮=上下移动 | Shift+滚轮=左右移动").classes("text-xs text-gray-500")

            # 缩放控制区
            with ui.row().classes("items-center gap-2"):
                # 减小缩放
                ui.button(icon="remove", on_click=lambda: scale.set_value(scale.value - 0.1)).props("round flat dense")

                # 滑块：监听 on_value_change 来更新样式，避免 Pylance 报错
                slider = ui.slider(min=0.1, max=3.0, step=0.1).classes("w-32")
                slider.bind_value(scale)  # 双向绑定数值

                ui.label().bind_text_from(scale, "value", backward=lambda x: f"{int(x * 100)}%")

                # 增加缩放
                ui.button(icon="add", on_click=lambda: scale.set_value(scale.value + 0.1)).props("round flat dense")

                ui.separator().props("vertical")

                # 导出源码
                def export_file():
                    if mermaid_view:
                        ui.download(mermaid_view.content.encode("utf-8"), "question_tree.mmd")
                        ui.notify("下载已开始")

                ui.button("导出源码", on_click=export_file, icon="download").props("outline size=sm")

                # 调试开关：显示源码
                debug_mode = ui.switch("调试模式")

        # --- 调试区域：显示生成的源码 (用于排查为什么不显示) ---
        code_area = (
            ui.code("Wait for load...").classes("w-full p-4 bg-gray-800 text-white").bind_visibility_from(debug_mode)
        )

        # --- 画布区域 ---
        # 使用 scroll_area 提供视口 (Viewport)
        with ui.scroll_area().classes("w-full h-full bg-gray-50 p-4"):
            # --- 缩放容器 (Canvas) ---
            # 我们不使用 bind_style，而是定义一个 Row 并通过 Python 代码更新它的 style
            canvas_row = ui.row().classes("origin-top-left transition-transform duration-200")

            # 定义样式更新函数
            def update_zoom_style():
                # 直接设置 style 字符串，这是最稳健的方法
                canvas_row.style(f"transform: scale({scale.value})")

            # 初始化样式
            update_zoom_style()

            # 监听 scale 变化，触发更新
            scale.on_value_change(update_zoom_style)

            # 在容器内渲染 Mermaid
            with canvas_row:
                try:
                    data = load_config_data()
                    if not data or not data.get("data"):
                        ui.label("错误：未读取到数据，请检查 config_service.json 路径").classes("text-red-500 text-xl")
                    else:
                        generator = QuestionTreeGenerator(data)
                        mermaid_source = generator.generate_mermaid()

                        # 将源码填入调试区
                        code_area.set_content(mermaid_source)

                        if not mermaid_source.strip():
                            ui.label("错误：生成了空的 Mermaid 代码").classes("text-red-500")
                        else:
                            # 渲染图表
                            # htmlLabels: true 允许我们在节点文字里使用 <b> 和 <br>
                            mermaid_config = {
                                "securityLevel": "loose",
                                "theme": "base",
                                "flowchart": {"useMaxWidth": False, "htmlLabels": True},
                                "maxTextSize": 1000000,
                            }
                            config_str = json.dumps(mermaid_config)

                            mermaid_view = (
                                ui.mermaid(mermaid_source)
                                .classes("mermaid-box bg-white shadow-sm border p-4 rounded")
                                .props(f"config='{config_str}'")
                            )

                except Exception as e:
                    ui.label(f"渲染异常: {str(e)}").classes("text-red-500 font-bold")
                    ui.code(str(e))
