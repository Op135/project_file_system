import ast
import json
import logging
import os
import re
from collections import defaultdict

from nicegui import app, ui

# --- 严格按照项目结构导入 ---
from .. import db_storage
from ..config import BASE_DIR, IMG_DIR, PRESET_AVATARS
from ..utils import get_cache_busted_path, logout

logger = logging.getLogger(__name__)

# 配置文件路径
CONFIG_PATH = f"{BASE_DIR}/config_service.json"


# ==========================================
# 核心逻辑：图关系构建 (Graph Builder)
# ==========================================
def translate_val_to_content(node_data, target_val):
    """
    将逻辑值 (option_out) 翻译为显示文本 (option_content)
    例如: True -> "是", "结构" -> "机械结构"
    """
    if not node_data or "options" not in node_data:
        return target_val

    target_str = str(target_val).strip()

    # 遍历该节点的所有选项进行匹配
    for opt in node_data.get("options", []):
        # 比较 option_out 和 target_val (都转字符串比对)
        if str(opt.get("option_out", "")).strip() == target_str:
            return opt.get("option_content", target_str)

    return target_val


def format_logic_value_with_trans(raw_val, operator, source_node):
    """
    格式化并翻译逻辑值
    raw_val: 原始值字符串 (如 "['A', 'B']" 或 "True")
    source_node: 来源节点对象 (用于查找翻译字典)
    """
    final_val = raw_val

    # 1. 尝试解析列表 (处理 any/all)
    if "[" in raw_val and "]" in raw_val:
        try:
            val_list = ast.literal_eval(raw_val)
            if isinstance(val_list, list):
                # 翻译列表中的每一项
                trans_list = [translate_val_to_content(source_node, v) for v in val_list]

                connector = " 且 " if "all" in operator else " 或 "
                return connector.join(f"“{v}”" for v in trans_list)
        except Exception:
            pass  # 解析失败降级处理

    # 2. 单值处理
    # 去除引号
    clean_val = raw_val.strip("'").strip('"')
    trans_val = translate_val_to_content(source_node, clean_val)
    return f"“{trans_val}”"


def format_logic_value(raw_val, operator):
    """
    根据操作符将列表值转换为逻辑字符串
    ['A', 'B'] + any -> "A 或 B"
    ['A', 'B'] + all -> "A 且 B"
    """
    # 1. 尝试解析列表
    final_val = raw_val
    is_list = False

    if "[" in raw_val and "]" in raw_val:
        try:
            val_list = ast.literal_eval(raw_val)
            if isinstance(val_list, list):
                is_list = True
                # 根据操作符决定连接词
                connector = " 且 " if "all" in operator else " 或 "
                final_val = connector.join(str(v) for v in val_list)
        except Exception:
            # 解析失败，手动清洗
            final_val = raw_val.replace("[", "").replace("]", "").replace("'", "").replace('"', "")
    else:
        # 普通单值，去引号
        final_val = raw_val.strip("'").strip('"')

    return final_val


def parse_condition_parent(condition_str):
    """从条件字符串中提取父节点ID"""
    if not condition_str or condition_str == "无条件":
        return None
    # 匹配字符串中的第一个数字序列作为父ID
    match = re.search(r"(\d+)", str(condition_str))
    if match:
        return match.group(1)
    return None


def build_graph_relationships(config_data):
    """
    构建双向图关系：
    1. nodes: ID -> 节点详情
    2. parent_map: ID -> [父节点ID列表] (上游)
    3. children_map: ID -> [子节点ID列表] (下游)
    """
    nodes_data = config_data.get("data", {})
    nodes = {}  # 存储节点详细信息
    parent_map = defaultdict(list)
    children_map = defaultdict(list)
    roots = []

    # 第一遍遍历：建立基本索引
    for node_id, node_info in nodes_data.items():
        # 确保 ID 是字符串类型，方便统一处理
        str_id = str(node_id)
        nodes[str_id] = node_info

        # 解析父节点
        raw_cond = node_info.get("condition")
        parent_id = parse_condition_parent(raw_cond)

        if parent_id:
            parent_id = str(parent_id)
            # 记录关系
            parent_map[str_id].append(parent_id)
            children_map[parent_id].append(str_id)
        else:
            # 没有父节点，视为根节点
            roots.append(str_id)

    # 对根节点排序
    roots.sort(key=lambda x: int(x) if x.isdigit() else 0)

    return nodes, parent_map, children_map, roots


# ==========================================
# 辅助解析逻辑
# ==========================================


def parse_incoming_logic(condition_str, nodes):
    """
    解析当前节点的进入条件 (Left Column) - 翻译版
    """
    if not condition_str or condition_str == "无条件":
        return []

    logic_delimiters = ["and", "or"]
    logic_pattern = "|".join(f"({delimiter})" for delimiter in logic_delimiters)

    parts = re.split(logic_pattern, str(condition_str))

    parsed_blocks = []
    current_relation = "START"

    for part in parts:
        if not part:
            continue
        part = part.strip()
        if part in logic_delimiters:
            current_relation = part.upper()
            continue

        ops_pattern = r"(\d+)\s*(not\s+)?(any|all|==|!=|in)\s*(.*)"
        match = re.search(ops_pattern, part)

        if match:
            ref_id = match.group(1)
            is_not = match.group(2)
            operator = match.group(3)
            raw_val = match.group(4)

            ref_node = nodes.get(ref_id, {})

            # === 核心修改：传入 ref_node 进行翻译 ===
            display_val = format_logic_value_with_trans(raw_val, operator, ref_node)

            op_display = "等于"
            if operator == "!=":
                op_display = "不等于"
            elif operator == "any":
                op_display = "包含"
            elif operator == "all":
                op_display = "包含"

            if is_not:
                op_display = f"非 ({op_display})"

            parsed_blocks.append(
                {
                    "source_id": ref_id,
                    "source_title": ref_node.get("guide_content", "未知节点"),
                    "relation": current_relation,
                    "operator_display": op_display,
                    "trigger_value": display_val,
                    "raw": part,
                }
            )
        else:
            # 纯数字ID兜底
            if part.isdigit():
                parsed_blocks.append(
                    {
                        "source_id": part,
                        "source_title": nodes.get(part, {}).get("guide_content", "未知"),
                        "relation": current_relation,
                        "operator_display": "关联",
                        "trigger_value": "-",
                        "raw": part,
                    }
                )

    return parsed_blocks


def group_children_by_trigger(current_id, children_ids, nodes):
    """
    将子节点按“选项”分组 (Right Column) - 全逻辑展示版
    同时展示当前节点的选择和外部依赖条件
    """
    groups = defaultdict(list)

    # 获取当前节点（作为翻译源）
    current_node = nodes.get(current_id, {})

    # 分割正则：捕获 and/or 以便保留它们，\b 确保单词边界
    logic_split_pattern = r"(\b(?:and|or)\b)"

    for cid in children_ids:
        child = nodes.get(cid, {})
        raw_cond = child.get("condition", "")

        # 如果无条件
        if not raw_cond or raw_cond == "无条件":
            groups["直接跳转 (无条件)"].append(cid)
            continue

        # 1. 切割条件字符串
        # "5==模组 and 7any['结构']" -> ['5==模组 ', 'and', ' 7any['结构']']
        parts = re.split(logic_split_pattern, str(raw_cond))

        display_parts = []

        for p in parts:
            p = p.strip()
            if not p:
                continue

            # --- 处理连接符 ---
            if p == "and":
                display_parts.append(" 且 ")
                continue
            if p == "or":
                display_parts.append(" 或 ")
                continue

            # --- 处理条件单元 ---
            # 匹配: ID + (not) + 操作符 + 值
            match = re.search(r"(\d+)\s*(not\s+)?(any|all|!=|==|in)\s*(.*)", p)

            if match:
                ref_id = match.group(1)
                is_not_prefix = match.group(2)
                operator = match.group(3)
                raw_val = match.group(4).strip()

                # 查找引用节点和它的翻译
                ref_node = nodes.get(ref_id, {})
                formatted_val = format_logic_value_with_trans(raw_val, operator, ref_node)

                # === 情况 A: 当前节点的条件 (Local Choice) ===
                if str(ref_id) == str(current_id):
                    prefix = "若选择: "
                    if operator == "!=" or is_not_prefix or (operator == "in" and "not" in p):
                        prefix = "排除: "
                    elif operator == "any":
                        prefix = "若包含: "

                    display_parts.append(f"{prefix}{formatted_val}")

                # === 情况 B: 外部节点的条件 (External Constraint) ===
                else:
                    # 获取外部节点的标题简写 (比如前6个字)
                    ref_title = ref_node.get("guide_content", f"#{ref_id}")
                    # if len(ref_title) > 6:
                    #     ref_title = ref_title[:6] + "..."

                    op_text = "是"
                    if operator == "!=":
                        op_text = "不是"
                    elif operator == "any" or operator == "in":
                        op_text = "包含"

                    if is_not_prefix:
                        op_text = f"非{op_text}"

                    # 外部条件加个括号格式
                    display_parts.append(f"({ref_title} {op_text} {formatted_val})")

            else:
                # 兜底：处理无法解析的片段或纯ID
                if p == str(current_id):
                    display_parts.append("直接跳转")
                else:
                    display_parts.append(p)

        # 2. 生成最终分组 Key
        full_key = "".join(display_parts)
        groups[full_key].append(cid)

    return groups


# ==========================================
# 页面渲染 (Miller Columns UI)
# ==========================================


@ui.page("/question_tree_tabs")
def question_tree_page():
    # 1. 权限检查
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")
        return

    current_user = app.storage.user.get("current_user")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])
    current_display_path = get_cache_busted_path(current_avatar_path)

    # 2. 数据加载与预处理
    nodes = {}
    parent_map = {}
    children_map = {}
    roots = []

    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                full_json = json.load(f)
                # 构建图谱
                nodes, parent_map, children_map, roots = build_graph_relationships(full_json)
        else:
            ui.notify(f"找不到配置文件: {CONFIG_PATH}", type="warning")
    except Exception as e:
        logger.error(f"Data load failed: {e}")
        ui.notify(f"数据加载错误: {str(e)}", type="negative")

    # 3. 状态管理
    # 当前选中的节点ID，默认为第一个根节点
    state = {"current_id": roots[0] if roots else None}

    # -------------------------------------------------
    # 核心组件：米勒列渲染器 (Refreshable)
    # -------------------------------------------------
    @ui.refreshable
    def render_miller_columns():
        curr_id = state["current_id"]
        if not curr_id or curr_id not in nodes:
            ui.label("未选中节点").classes("p-4")
            return

        current_node = nodes[curr_id]

        # 准备数据
        incoming_logic = parse_incoming_logic(current_node.get("condition"), nodes)
        children_ids = children_map.get(curr_id, [])
        grouped_children = group_children_by_trigger(curr_id, children_ids, nodes)

        with ui.row().classes("w-full h-full gap-4 p-4 bg-gray-100 items-stretch"):
            # ============================================================
            # 左栏：前置条件 (Logic Source) - 适配新逻辑解析
            # ============================================================
            with ui.column().classes("w-1/4 h-full border-r border-gray-200 bg-gray-50"):
                ui.label("前置条件 (Input)").classes("text-xs font-bold text-gray-500 uppercase p-4 pb-2")

                with ui.scroll_area().classes("w-full flex-grow px-4 pb-4"):
                    if not incoming_logic:
                        with ui.card().classes("w-full p-4 bg-white border-l-4 border-gray-300"):
                            ui.label("无前置条件 (根节点)").classes("text-sm text-gray-400")
                    else:
                        for idx, block in enumerate(incoming_logic):
                            # --- 逻辑连接符渲染 (AND / OR) ---
                            # 第一个块显示 START (通常隐藏)，后续块显示与上一个的关系
                            rel = block["relation"]
                            if rel != "START":
                                badge_color = "orange" if rel == "OR" else "blue"
                                with ui.row().classes("w-full justify-center my-2 relative"):
                                    # 分割线
                                    ui.separator().classes("absolute top-1/2 w-full z-0")
                                    # 徽章
                                    ui.label(rel).classes(
                                        f"z-10 text-[10px] font-bold text-white bg-{badge_color}-400 px-2 py-0.5 rounded-full"
                                    )

                            # --- 逻辑卡片 ---
                            with ui.card().classes(
                                "w-full p-3 bg-white border border-gray-200 shadow-sm relative group hover:border-blue-300 transition-all"
                            ):
                                # 来源 ID 和 标题
                                with ui.row().classes("items-center justify-between w-full mb-1"):
                                    ui.label(f"来自 #{block['source_id']}").classes(
                                        "text-[10px] text-gray-400 font-mono"
                                    )
                                    # 跳转按钮 (悬停出现)
                                    ui.button(
                                        icon="arrow_back", on_click=lambda i=block["source_id"]: select_node(i)
                                    ).props("round flat size=xs color=grey").classes(
                                        "opacity-0 group-hover:opacity-100 transition-opacity"
                                    )

                                ui.label(block["source_title"]).classes(
                                    "text-xs font-medium text-gray-700 line-clamp-2 leading-tight mb-2"
                                )

                                # 条件详情区 (灰色背景)
                                with ui.column().classes("w-full bg-gray-50 rounded p-2 gap-1 border border-gray-100"):
                                    # 操作符 (如: 等于, 包含任一)
                                    ui.label(block["operator_display"]).classes("text-[10px] text-gray-500")
                                    # 目标值 (高亮显示)
                                    ui.label(block["trigger_value"]).classes(
                                        "text-sm font-bold text-blue-600 break-all leading-tight"
                                    )

            # ============================================================
            # 中栏：当前问题 (The Anchor)
            # 对应图中：当前问题
            # ============================================================
            with ui.column().classes("w-1/4 h-full"):
                ui.label("当前节点 (Current)").classes("text-xs font-bold text-blue-500 uppercase mb-2")

                with ui.card().classes("w-full h-full bg-white border-t-4 border-blue-500 shadow-md flex flex-col p-0"):
                    # 标题头
                    with ui.column().classes("w-full bg-blue-50 p-6 border-b border-blue-100"):
                        ui.label(f"ID: {curr_id}").classes("text-xs font-mono text-blue-400 mb-1")
                        ui.label(current_node.get("guide_content", "无内容")).classes(
                            "text-xl font-bold text-gray-800 leading-snug"
                        )

                    # 内容体
                    with ui.scroll_area().classes("flex-grow p-6"):
                        with ui.column().classes("gap-4"):
                            # 类型
                            with ui.row().classes("items-center gap-3"):
                                ui.icon("category", color="grey-6").classes("text-xl")
                                with ui.column().classes("gap-0"):
                                    ui.label("交互类型").classes("text-xs text-gray-400")
                                    ui.label(current_node.get("answer_type", "Unknown")).classes("text-sm font-medium")

                            ui.separator()

                            # 备注/option_hint
                            if current_node.get("option_hint"):
                                with ui.column().classes("gap-1 bg-gray-50 p-3 rounded w-full"):
                                    ui.label("提示语").classes("text-xs text-gray-400")
                                    ui.label(current_node.get("option_hint")).classes("text-sm text-gray-600 italic")
                            ui.separator().classes("my-2")

                            # === 新增：当前节点可用选项展示 ===
                            node_options = current_node.get("options", [])
                            if node_options:
                                ui.label(f"可用选项 ({len(node_options)})").classes(
                                    "text-xs font-bold text-gray-500 uppercase mt-2"
                                )

                                # 使用 List 或 Chips 展示选项
                                with ui.column().classes("w-full gap-2"):
                                    for opt in node_options:
                                        # 获取内容和逻辑值
                                        opt_content = opt.get("option_content", "")
                                        # 某些输入型节点可能 options 里是空的或只有空字典，做个过滤
                                        if not opt_content and not opt.get("option_show"):
                                            continue

                                        # 如果 option_content 为空（比如输入框），尝试显示 option_show 模板
                                        display_text = opt_content if opt_content else opt.get("option_show", "输入项")
                                        logic_val = opt.get("option_out", "")

                                        with ui.row().classes(
                                            "w-full items-center justify-between p-2 bg-blue-50 rounded border border-blue-100"
                                        ):
                                            # 左侧：显示内容
                                            with ui.row().classes("items-center gap-2"):
                                                ui.icon("radio_button_unchecked", size="xs").classes("text-blue-400")
                                                ui.label(display_text).classes("text-sm text-gray-800 font-medium")

                                            # 右侧：显示逻辑值 (作为参考，字号更小)
                                            # 比如: "是" (True)
                                            ui.label(f"Val: {logic_val}").classes(
                                                "text-[10px] text-gray-400 font-mono bg-white px-1 rounded border border-gray-200"
                                            )
            # ============================================================
            # 右栏：按选项分组的后续 (Grouped Output)
            # 对应图中：当前问题选项A -> 问题10,21... | 当前问题选项B -> 问题50,61...
            # ============================================================
            with ui.column().classes("flex-grow h-full"):  # 使用 flex-grow 占据剩余空间
                ui.label("后续分支 (Outcomes)").classes("text-xs font-bold text-green-600 uppercase mb-2")

                with ui.scroll_area().classes("w-full flex-grow pr-2"):
                    if not grouped_children:
                        with ui.column().classes(
                            "w-full h-32 items-center justify-center border-2 border-dashed border-gray-300 rounded-lg"
                        ):
                            ui.icon("stop_circle", size="md", color="grey-4")
                            ui.label("流程结束").classes("text-gray-400 mt-2")

                    # 遍历每一个选项分组
                    for option_val, child_list in grouped_children.items():
                        # 外层框：代表一个选项 (红框效果)
                        with ui.card().classes(
                            "w-full mb-4 bg-white border border-gray-200 shadow-sm overflow-visible"
                        ):
                            # 选项标题条
                            with ui.row().classes(
                                "w-full bg-green-50 px-4 py-2 border-b border-green-100 items-center justify-between"
                            ):
                                with ui.row().classes("items-center gap-2"):
                                    ui.icon("check_circle_outline", size="xs", color="green")
                                    ui.label(f"{option_val}").classes("text-sm font-bold text-green-800")
                                ui.badge(f"{len(child_list)} 个后续").props("color=green-2 text-color=green-9 outline")

                            # 该选项下的子节点列表
                            with ui.column().classes("w-full p-2 gap-2"):
                                for child_id in child_list:
                                    child_node = nodes.get(child_id, {})
                                    child_title = child_node.get("guide_content", "未命名")
                                    child_cond = child_node.get("condition", "")

                                    # 子节点条目
                                    with (
                                        ui.row()
                                        .classes(
                                            "w-full p-2 hover:bg-gray-50 rounded cursor-pointer border border-transparent hover:border-gray-300 items-center transition-all group"
                                        )
                                        .on("click", lambda i=child_id: select_node(i))
                                    ):
                                        ui.icon("subdirectory_arrow_right", size="xs").classes("text-gray-300 mr-2")

                                        with ui.column().classes("gap-0 flex-grow"):
                                            with ui.row().classes("items-center gap-2"):
                                                ui.label(f"#{child_id}").classes("text-xs font-mono text-gray-400")
                                                # 如果条件很复杂，显示完整条件作为提示
                                                if len(child_cond) > len(option_val) + 10:
                                                    ui.icon("info", size="xs", color="grey-4").tooltip(
                                                        f"完整条件: {child_cond}"
                                                    )

                                            ui.label(child_title).classes("text-sm text-gray-700 line-clamp-1")

                                        ui.icon("chevron_right", size="sm").classes(
                                            "text-gray-300 group-hover:text-blue-500"
                                        )

    def select_node(node_id):
        state["current_id"] = str(node_id)
        render_miller_columns.refresh()
        # 注意：此处删除了 tree_ui 相关的报错代码

    # -------------------------------------------------
    # UI 布局框架
    # -------------------------------------------------

    # Header
    with ui.header(elevated=True).classes("flex justify-between items-center bg-blue-600 h-14 px-4 shadow-md"):
        with ui.row().classes("items-center"):
            ui.icon("alt_route", size="md").classes("text-white mr-2")
            ui.label("逻辑全景浏览器").classes("text-white text-lg font-bold tracking-wide")

        with ui.row().classes("items-center gap-4"):
            ui.button("返回主页", icon="home", on_click=lambda: ui.navigate.to("/main")).props(
                "flat text-color=white dense"
            )
            with ui.avatar(size="md").classes("cursor-pointer border-2 border-white"):
                ui.image(current_display_path)

    # Body
    with ui.row().classes("w-full h-[calc(100vh-3.5rem)] gap-0"):
        # --- 最左侧：全局目录树 (Navigator) ---
        # 对应图中的“左侧目录树列表”，用于快速定位
        with ui.column().classes("w-64 h-full border-r border-gray-300 bg-white flex-shrink-0 flex flex-col"):
            ui.label("全局索引").classes("p-3 font-bold text-gray-700 bg-gray-50 border-b w-full")

            # 构建 Tree 数据结构供 ui.tree 使用
            # 这里简单地把所有根节点作为顶级，子节点懒加载或全部加载
            # 为了简单起见，这里做一个简单的 ID 列表搜索，或者简单的层级展示

            with ui.scroll_area().classes("flex-grow px-2 py-2"):
                # 搜索框
                search_input = (
                    ui.input(placeholder="搜索 ID 或 内容...").props("dense outlined rounded").classes("w-full mb-2")
                )

                # 节点列表 (点击跳转)
                # 由于节点可能很多，这里用 Virtual Scroll 或者简单列表
                # 我们按 ID 排序显示
                sorted_ids = sorted(nodes.keys(), key=lambda x: int(x) if x.isdigit() else 9999)

                def run_search(e):
                    term = e.value.lower()
                    filtered_ids = [
                        nid for nid in sorted_ids if term in nid or term in nodes[nid].get("guide_content", "").lower()
                    ]
                    list_container.clear()
                    with list_container:
                        render_search_list(filtered_ids)

                search_input.on("input", run_search)

                list_container = ui.column().classes("w-full gap-1")

                def render_search_list(id_list):
                    # 限制显示数量防止卡顿
                    for nid in id_list[:50]:
                        content = nodes[nid].get("guide_content", "")
                        with (
                            ui.row()
                            .classes(
                                "w-full items-center p-2 hover:bg-blue-50 rounded cursor-pointer border-b border-gray-100"
                            )
                            .on("click", lambda i=nid: select_node(i))
                        ):
                            ui.label(f"#{nid}").classes("text-xs font-mono text-blue-500 font-bold w-10")
                            ui.label(content).classes("text-xs text-gray-700 line-clamp-1 flex-grow")

                    if len(id_list) > 50:
                        ui.label(f"...还有 {len(id_list) - 50} 项").classes("text-xs text-gray-400 p-2")

                with list_container:
                    render_search_list(sorted_ids)

        # 右侧主区域
        with ui.column().classes("flex-grow h-full relative"):
            render_miller_columns()
