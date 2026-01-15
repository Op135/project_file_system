import json
import os
import re
from collections import defaultdict

from nicegui import ui


# 1. 数据加载与深度逻辑解析
def load_config():
    file_path = os.path.join(os.path.dirname(__file__), "config_service.json")
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f).get("data", {})

    child_map = defaultdict(list)
    parent_map = defaultdict(list)

    for c_id, info in data.items():
        cond = info.get("condition", "")
        if cond == "无条件":
            continue
        # 提取父 ID
        parents = re.findall(r"(\d+)(?=any|all|==|!=)", cond)
        for p_id in set(parents):
            child_map[p_id].append(c_id)
            parent_map[c_id].append(p_id)

    return data, child_map, parent_map


DATA, CHILD_MAP, PARENT_MAP = load_config()


# 2. 递归获取所有祖先节点及其层级距离
def get_all_ancestors(node_id):
    ancestors = {}  # {id: distance}
    queue = [(p_id, 1) for p_id in PARENT_MAP.get(node_id, [])]
    while queue:
        curr_p, dist = queue.pop(0)
        if curr_p not in ancestors or dist < ancestors[curr_p]:
            ancestors[curr_p] = dist
            # 继续向上追溯
            for next_p in PARENT_MAP.get(curr_p, []):
                queue.append((next_p, dist + 1))
    return ancestors


# 3. 极简颜色配置
def get_type_base_color(ans_type):
    if any(x in ans_type for x in ["单选", "多选", "下拉"]):
        return "bg-blue-50 border-blue-200"  # 选项类：浅蓝
    return "bg-slate-50 border-slate-200"  # 文本/数字类：浅灰


# --- 全局状态 ---
selected_path = []  # 已选路径
active_ancestors = {}  # 当前选中项的所有祖先 {id: level}
column_refreshers = []


def on_node_select(depth, nid):
    """点击节点并刷新溯源"""
    global selected_path, active_ancestors
    selected_path = selected_path[:depth] + [nid]
    # 执行递归追溯
    active_ancestors = get_all_ancestors(nid)
    # 刷新所有可见列
    for r in column_refreshers:
        r.refresh()


@ui.refreshable
def render_col_content(depth, p_id=None):
    """渲染列内容，包含递归置顶逻辑"""
    if depth == 0:
        nodes = [k for k, v in DATA.items() if v["condition"] == "无条件"]
        # 置顶所有祖先节点
        sorted_nodes = sorted(nodes, key=lambda x: x in active_ancestors, reverse=True)
        for nid in sorted_nodes:
            build_card(0, nid)
    else:
        if not p_id:
            return
        children_ids = CHILD_MAP.get(p_id, [])
        groups = defaultdict(list)
        for cid in children_ids:
            groups[DATA[cid]["condition"]].append(cid)

        # 逻辑组置顶：如果组内有任何祖先节点，则整组置顶
        sorted_group_keys = sorted(
            groups.keys(), key=lambda g: any(cid in active_ancestors for cid in groups[g]), reverse=True
        )

        for cond in sorted_group_keys:
            cids = groups[cond]
            # 组内节点置顶
            sorted_cids = sorted(cids, key=lambda x: x in active_ancestors, reverse=True)
            is_anc_group = any(cid in active_ancestors for cid in cids)

            # 分组容器样式
            g_border = "border-orange-500 ring-2 ring-orange-100" if is_anc_group else "border-blue-200"
            with ui.column().classes(f"mb-1 w-full border-2 {g_border} rounded bg-white overflow-hidden shadow-sm"):
                with ui.row().classes(
                    f"w-full {'bg-orange-50' if is_anc_group else 'bg-blue-50'} p-1 items-center gap-1"
                ):
                    ui.icon("hub", size="12px", color="blue-800")
                    ui.label(cond).classes("text-[9px] font-bold text-blue-900 break-all leading-none")

                with ui.column().classes("p-1 gap-1 w-full"):
                    for nid in sorted_cids:
                        build_card(depth, nid)


def build_card(depth, nid):
    """构建极简卡片"""
    item = DATA[nid]
    ans_type = item.get("answer_type", "单选")
    base_style = get_type_base_color(ans_type)

    is_sel = len(selected_path) > depth and selected_path[depth] == nid
    anc_level = active_ancestors.get(nid)  # 获取祖先级数

    # 状态样式
    card_cls = "w-full cursor-pointer transition-all duration-200 p-1.5 border-2 rounded-sm "
    if is_sel:
        card_cls += "border-blue-600 ring-2 ring-blue-400 z-10 bg-white"
    elif anc_level:
        # 递归前置高亮：级数越高，光感越淡
        ring_strength = max(1, 4 - anc_level)
        card_cls += f"border-orange-500 ring-{ring_strength} ring-orange-300 z-10 scale-[1.01] shadow-md"
    else:
        card_cls += "border-transparent hover:border-slate-300"

    with ui.card().tight().classes(card_cls + " " + base_style).on("click", lambda: on_node_select(depth, nid)):
        if anc_level:
            label = "直接前置" if anc_level == 1 else f"{anc_level}级溯源"
            ui.badge(label, color="orange-9").props("floating").classes("text-[8px] px-1 font-bold")

        with ui.row().classes("items-start no-wrap gap-1"):
            ui.label(nid).classes("text-[9px] bg-black/10 px-1 rounded text-black font-mono")
            ui.label(item["guide_content"]).classes("text-[13px] font-black leading-tight flex-grow text-slate-800")

        with ui.row().classes("mt-1 items-center justify-between w-full opacity-60"):
            ui.label(ans_type).classes("text-[9px] font-bold text-slate-500")
            if nid in CHILD_MAP:
                ui.icon("chevron_right", size="14px", color="blue-900")


# --- 界面布局 ---
ui.query(".q-page").classes("bg-slate-300")

with ui.header(elevated=True).classes("bg-slate-900 p-2 shadow-lg"):
    with ui.row().classes("items-center justify-between w-full"):
        ui.label("研发需求决策表 - 全链路递归溯源版").classes("text-lg font-black text-white tracking-tighter")
        ui.button(
            "重置路径",
            icon="refresh",
            on_click=lambda: (
                selected_path.clear(),
                active_ancestors.clear(),
                [r.refresh() for r in column_refreshers],
            ),
        ).props("flat color=white dense")

with ui.row().classes("no-wrap h-[90vh] items-start gap-0 bg-slate-300 overflow-x-auto"):
    for d in range(12):  # 增加列数以应对深层递归
        with ui.column().classes("w-72 h-full border-r bg-slate-50 flex-shrink-0"):
            ui.label(f"{'初始' if d == 0 else f'层级 {d}'}").classes(
                "w-full bg-slate-800 text-white p-1 text-[11px] font-bold sticky top-0 z-20"
            )
            with ui.scroll_area().classes("flex-grow w-full p-1"):

                @ui.refreshable
                def col_content(depth=d):
                    p_id = selected_path[depth - 1] if depth > 0 and len(selected_path) >= depth else None
                    render_col_content(depth, p_id)

                column_refreshers.append(col_content)
                col_content()


ui.run(title="需求决策逻辑表", port=8080, native=True, reload=True)
