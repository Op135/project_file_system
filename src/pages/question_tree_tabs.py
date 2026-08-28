import ast
import asyncio
import json
import logging
import re
from collections import defaultdict

from nicegui import app, ui

# --- 严格按照项目结构导入 ---
from ..config import BASE_DIR, IMG_DIR, PRESET_AVATARS
from ..question_tree_access import can_view_question_tree
from ..utils import get_cache_busted_path, logout, setup_global_activity_tracking, sync_current_user_role

logger = logging.getLogger(__name__)


# 1. 数据加载 (保持不变)
def load_config():
    file_path = f"{BASE_DIR}/config_service.json"
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f).get("data", {})
    except FileNotFoundError:
        data = {}

    child_map = defaultdict(list)
    parent_map = defaultdict(list)

    for c_id, info in data.items():
        cond = info.get("condition", "")
        if cond == "无条件":
            continue
        parents = re.findall(r"(\d+)\s*(?:any|all|==|!=|in)", cond)
        for p_id in set(parents):
            child_map[p_id].append(c_id)
            parent_map[c_id].append(p_id)

    return data, child_map, parent_map


DATA, CHILD_MAP, PARENT_MAP = load_config()


# --- 条件翻译 (保持不变) ---
def translate_condition(cond_str):
    if not cond_str or cond_str == "无条件":
        return "初始问题"

    result = cond_str.replace(" and ", " 且 ").replace(" or ", " 或 ")

    op_map = {
        "==": "选了",
        "!=": "非",
        "any": "包含",
        "all": "全含",
        "in": "位于",
    }

    pattern = r"(\d+)\s*(any|all|==|!=|in)\s*((?:\[.*?\])|(?:'[^']*')|(?:\"[^\"]*\")|(?:True|False)|(?:[\u4e00-\u9fa5a-zA-Z0-9_]+))"

    def replacer(match):
        nid = match.group(1)
        op = match.group(2)
        raw_val = match.group(3)

        try:
            val = ast.literal_eval(raw_val)
        except (ValueError, SyntaxError):
            val = raw_val

        node_opts = DATA.get(nid, {}).get("options", [])

        def get_label(v):
            v_str = str(v)
            for opt in node_opts:
                if str(opt.get("option_out")) == v_str:
                    return opt.get("option_content")
            if v_str == "True":
                return "是"
            if v_str == "False":
                return "否"
            return v_str

        if isinstance(val, list):
            val_display = "/".join([get_label(x) for x in val])
        else:
            val_display = get_label(val)

        op_display = op_map.get(op, op)
        return f"({nid}){op_display}“{val_display}”"

    translated = re.sub(pattern, replacer, result)
    return translated


# 2. 递归获取所有祖先 (保持不变)
def get_all_ancestors(node_id):
    ancestors = {}
    queue = [(p_id, 1) for p_id in PARENT_MAP.get(node_id, [])]
    while queue:
        curr_p, dist = queue.pop(0)
        if curr_p not in ancestors or dist < ancestors[curr_p]:
            ancestors[curr_p] = dist
            for next_p in PARENT_MAP.get(curr_p, []):
                queue.append((next_p, dist + 1))
    return ancestors


# 3. 基础卡片颜色配置
def get_type_base_color(ans_type):
    # >>> [颜色配置] 卡片基础底色
    # 如果是选择题类型，使用淡蓝色背景(bg-blue-50)和蓝边框(border-blue-200)
    # 否则（如填空题），使用淡灰色背景(bg-slate-50)和灰边框(border-slate-200)
    if any(x in ans_type for x in ["单选", "多选", "下拉"]):
        return "bg-amber-50 border-amber-200"
    return "bg-blue-50 border-blue-200"


# --- 全局状态 ---
selected_path = []
active_ancestors = {}
column_refreshers = []
column_scroll_areas = []
column_wrappers = []


def get_path_to_node(target_id):
    path = [target_id]
    curr = target_id
    loop_limit = 100
    while PARENT_MAP.get(curr) and loop_limit > 0:
        curr = PARENT_MAP[curr][0]
        path.insert(0, curr)
        loop_limit -= 1
    return path


def update_ui_state():
    visible_count = len(selected_path) + 1
    for i, wrapper in enumerate(column_wrappers):
        wrapper.set_visibility(i < visible_count)

    for i in range(min(len(column_refreshers), visible_count)):
        column_refreshers[i].refresh()


async def jump_to_node(target_id, client=None):
    global selected_path, active_ancestors

    if not client:
        try:
            client = ui.context.client
        except Exception:
            pass

    new_path = get_path_to_node(target_id)
    selected_path = new_path
    active_ancestors = get_all_ancestors(target_id)

    update_ui_state()

    await asyncio.sleep(0.1)

    for i, sa in enumerate(column_scroll_areas):
        if i < len(selected_path) - 1:
            sa.scroll_to(percent=0, duration=0.1)

    depth = len(selected_path) - 1
    if client:
        client.run_javascript(f"smartScrollTo('node-{target_id}')")

        client.run_javascript(
            f'var el = document.getElementById("col-wrapper-{depth + 1}"); if(el) el.scrollIntoView({{behavior: "smooth", inline: "end", block: "nearest"}})'
        )

        await asyncio.sleep(0.1)
        client.run_javascript(f"flashNode('node-{target_id}')")


async def handle_search(query_str, dialog_ref):
    client = ui.context.client
    if not query_str:
        ui.notify("请输入搜索内容", type="warning")
        return

    if query_str.isdigit():
        if query_str in DATA:
            await jump_to_node(query_str, client)
        else:
            ui.notify(f"未找到 ID 为 {query_str} 的问题", type="negative")
        return

    matches = []
    for nid, info in DATA.items():
        content = info.get("guide_content", "")
        if query_str.lower() in content.lower():
            matches.append({"id": nid, "content": content})

    if len(matches) == 0:
        ui.notify("未找到相关内容", type="negative")
    elif len(matches) == 1:
        await jump_to_node(matches[0]["id"], client)
    else:
        dialog_ref.clear()
        with dialog_ref, ui.card().classes("w-96 max-w-full"):
            ui.label(f"找到 {len(matches)} 个匹配项").classes("text-lg font-medium mb-2")
            with ui.scroll_area().classes("h-60 border rounded p-2"):
                for m in matches:

                    def create_handler(target_id):
                        async def handler(_):
                            dialog_ref.close()
                            await jump_to_node(target_id, client)

                        return handler

                    with (
                        ui.item()
                        .classes("hover:bg-blue-50 cursor-pointer rounded border-b border-gray-100 p-2")
                        .props("clickable")
                        .on("click", create_handler(m["id"]))
                    ):
                        with ui.column().classes("gap-0"):
                            ui.label(m["id"]).classes("text-xs font-bold bg-gray-200 px-1 rounded w-fit")
                            ui.label(m["content"]).classes("text-sm text-gray-700 leading-tight")
            ui.button("关闭", on_click=dialog_ref.close).props("flat full-width")
        dialog_ref.open()


async def on_node_select(depth, nid):
    client = ui.context.client
    global selected_path, active_ancestors

    selected_path = selected_path[:depth] + [nid]
    active_ancestors = get_all_ancestors(nid)

    update_ui_state()

    await asyncio.sleep(0.05)

    for i, sa in enumerate(column_scroll_areas):
        if i != depth:
            sa.scroll_to(percent=0, duration=0.2)

    client.run_javascript(f"smartScrollTo('node-{nid}')")

    next_col_id = f"col-wrapper-{depth + 1}"
    await asyncio.sleep(0.05)
    client.run_javascript(
        f'var el = document.getElementById("{next_col_id}"); if(el) el.scrollIntoView({{behavior: "smooth", inline: "end", block: "nearest"}})'
    )


# 渲染列内容
@ui.refreshable
def render_col_content(depth, p_id=None):
    current_active_depth = len(selected_path) - 1 if selected_path else -1
    current_col_selected_id = selected_path[depth] if len(selected_path) > depth else None

    is_upstream_col = depth <= current_active_depth

    def sort_key(nid):
        return nid in active_ancestors

    if depth == 0:
        nodes = [k for k, v in DATA.items() if v["condition"] == "无条件"]
        sorted_nodes = sorted(nodes, key=sort_key, reverse=True)
        for nid in sorted_nodes:
            build_card(depth, nid, is_ghost=False)
    else:
        if not p_id:
            return
        children_ids = CHILD_MAP.get(p_id, [])
        groups = defaultdict(list)
        for cid in children_ids:
            groups[DATA[cid]["condition"]].append(cid)

        def group_sort_key(g_key):
            cids = groups[g_key]
            return any(cid in active_ancestors for cid in cids)

        sorted_group_keys = sorted(groups.keys(), key=group_sort_key, reverse=True)

        for cond in sorted_group_keys:
            cids = groups[cond]
            sorted_cids = sorted(cids, key=sort_key, reverse=True)

            has_ancestor = any(cid in active_ancestors for cid in cids)
            has_current_selection = any(cid == current_col_selected_id for cid in cids)
            should_highlight_group = has_ancestor or has_current_selection

            is_ghost_group = False

            if should_highlight_group:
                # >>> [颜色配置] 分组容器 - 包含高亮路径或当前选中项
                # 边框: 橙色 (border-red-500)
                # 标题头: 橙色背景 (bg-red-50), 深蓝色文字 (text-blue-900)
                # 图标: 深蓝色 (blue-800)
                container_cls = "border-red-500 ring-1 ring-red-100 opacity-100 bg-white"
                header_cls = "bg-red-50 text-blue-900"
                icon_color = "blue-800"
                is_ghost_group = False
            elif is_upstream_col:
                # >>> [颜色配置] 分组容器 - 历史路径中的未选中项 (GHOST状态)
                # 效果: 灰色边框, 30%透明度, 黑白滤镜 (grayscale)
                container_cls = "border-slate-100 opacity-50 grayscale bg-slate-50"
                header_cls = "bg-slate-100 text-slate-400"
                icon_color = "slate-400"
                is_ghost_group = True
            else:
                # >>> [颜色配置] 分组容器 - 普通状态 (当前最新列的未选中组)
                # 边框: 淡蓝色 (border-blue-200)
                # 标题头: 淡蓝色背景 (bg-blue-50)
                container_cls = "border-blue-200 opacity-100 bg-white"
                header_cls = "bg-blue-50 text-blue-900"
                icon_color = "blue-800"
                is_ghost_group = False

            with ui.column().classes(
                f"mb-1 w-full border {container_cls} rounded overflow-hidden shadow-sm transition-all duration-300"
            ):
                readable_cond = translate_condition(cond)
                with ui.row().classes(f"w-full {header_cls} p-1 items-center gap-1"):
                    ui.icon("hub", size="12px", color=icon_color)
                    ui.label(readable_cond).classes("text-[10px] font-bold break-words leading-tight whitespace-normal")

                with ui.column().classes("p-1 gap-1 w-full"):
                    for nid in sorted_cids:
                        build_card(depth, nid, is_ghost=is_ghost_group)


# 构建单张卡片
def build_card(depth, nid, is_ghost=False):
    item = DATA[nid]
    ans_type = item.get("answer_type", "单选")
    # 获取基础底色 (见 get_type_base_color 函数)
    base_style = get_type_base_color(ans_type)

    is_sel = len(selected_path) > depth and selected_path[depth] == nid

    is_current_active = is_sel and (depth == len(selected_path) - 1)
    anc_level = active_ancestors.get(nid)

    hint_text = item.get("option_hint", "")
    options_list = item.get("options", [])
    valid_opts = [str(o.get("option_content", "")) for o in options_list if o.get("option_content")]
    options_display = " / ".join(valid_opts)

    card_cls = "w-full cursor-pointer transition-all duration-200 p-1.5 border rounded-sm "

    # >>> [颜色配置] 卡片交互状态样式
    if is_current_active:
        # 优先级 1: 当前最新点中的节点 -> 强制蓝色高亮 (Focus)
        card_cls += "border-green-600 ring-1 ring-green-400 z-10 bg-green-50"
    elif anc_level:
        # 优先级 2: 祖先节点 是祖先(溯源): 橙色边框(border-red-500), 橙色光圈(ring-red-200)
        ring_strength = max(1, 4 - anc_level)
        card_cls += f"border-red-500 ring-{ring_strength} ring-red-200 z-10 scale-[1.005] shadow-sm"
    elif is_sel:
        # 优先级 3: 路径中的其他选中节点 深蓝边框(border-blue-600), 浅蓝光圈(ring-blue-400), 白底
        card_cls += "border-blue-600 ring-1 ring-blue-400 z-10 bg-white"
    else:
        # 优先级 4: 普通未选中节点 透明边框, 悬停时灰色边框(hover:border-slate-300)
        card_cls += "border-transparent hover:border-slate-300"

    dom_id = f"node-{nid}"
    if is_ghost:
        dom_id += "-ghost"

    with (
        ui.card()
        .tight()
        .classes(card_cls + " " + base_style)
        .props(f'id="{dom_id}"')
        .on("click", lambda: on_node_select(depth, nid))
    ):
        if anc_level:
            label = "直接前置" if anc_level == 1 else f"{anc_level}级溯源"
            # >>> [颜色配置] 溯源徽标: 深橙色背景(red-8)
            ui.badge(label, color="red-8").props("floating").classes("text-[8px] px-1 font-normal")

        with ui.row().classes("items-start no-wrap gap-1"):
            # >>> [颜色配置] ID标签: 黑色5%透明度背景(bg-black/5), 深灰字(text-slate-700)
            ui.label(nid).classes("text-[10px] bg-black/5 px-1 rounded text-slate-800 font-mono flex-shrink-0")
            # >>> [颜色配置] 问题内容: 深灰字(text-slate-700)
            ui.label(item["guide_content"]).classes("text-[13px] font-medium leading-tight flex-grow text-slate-700")

        if is_current_active and hint_text:
            # >>> [颜色配置] 提示信息框(option_hint)
            # 背景: 琥珀色淡底(bg-amber-50), 边框: 琥珀色淡边(border-amber-100)
            # 图标: 深琥珀色(amber-700), 文字: 深琥珀色(text-amber-900)
            with ui.row().classes("w-full mt-1.5 bg-amber-50 border border-amber-100 rounded p-1 items-start gap-1"):
                ui.icon("tips_and_updates", size="10px", color="amber-700").classes("mt-0.5 flex-shrink-0")
                ui.label(hint_text).classes("text-[10px] text-amber-900 leading-tight flex-grow break-all")

        with ui.row().classes("mt-1 items-baseline justify-between w-full opacity-80 gap-1"):
            with ui.element("div").classes(
                "text-[9px] leading-tight flex flex-wrap gap-x-1 items-baseline flex-grow pr-1"
            ):
                # >>> [颜色配置] 问题类型文字: 深灰加粗(text-slate-600)
                ui.label(ans_type).classes("font-bold text-slate-600 whitespace-nowrap")
                if options_display:
                    # >>> [颜色配置] 选项列表文字: 浅灰(text-slate-400)
                    ui.label(options_display).classes("text-slate-400 font-normal break-all")

            if nid in CHILD_MAP:
                # >>> [颜色配置] 右侧小箭头: 深蓝色(blue-900)
                ui.icon("chevron_right", size="14px", color="blue-900").classes("flex-none opacity-60 self-center")


# 布局容器
def layout_columns_container():
    column_refreshers.clear()
    column_scroll_areas.clear()
    column_wrappers.clear()

    MAX_DEPTH = 20

    for d in range(MAX_DEPTH):
        col_id = f"col-wrapper-{d}"
        # >>> [颜色配置] 列容器整体: 淡灰背景(bg-slate-50), 右侧边框(border-r)
        with (
            ui.column().classes("w-72 h-full border-r bg-white flex-shrink-0 relative").props(f"id={col_id}") as wrapper
        ):
            column_wrappers.append(wrapper)
            wrapper.set_visibility(False)

            # >>> [颜色配置] 列顶部标题(层级x): 深蓝灰背景(bg-slate-800), 白字(text-white)
            ui.label(f"{'初始' if d == 0 else f'层级 {d}'}").classes(
                "w-full bg-slate-800 text-white p-1 text-[11px] font-medium sticky top-0 z-20 text-center"
            )

            sa = ui.scroll_area().classes("flex-grow w-full p-1")
            column_scroll_areas.append(sa)
            with sa:

                @ui.refreshable
                def col_content(depth=d):
                    p_id = selected_path[depth - 1] if depth > 0 and len(selected_path) >= depth else None
                    render_col_content(depth, p_id)

                column_refreshers.append(col_content)
                col_content()

    update_ui_state()


# --- 新增：打印清单页面 (背景色分组优化版) ---
@ui.page("/print_list")
def print_list_page():
    stored_current_user = app.storage.user.get("current_user")
    if not isinstance(stored_current_user, str) or not stored_current_user:
        ui.navigate.to("/login")
        return
    if not can_view_question_tree(sync_current_user_role(), stored_current_user):
        ui.notify("当前账号没有查看需求项结构的权限", type="negative")
        ui.navigate.to("/main")
        return

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    ui.add_head_html("""
        <style>
            @media print {
                .no-print { display: none !important; }
                body { padding: 0; margin: 0; }
                /* 强制背景色打印，这对本功能的实现至关重要 */
                * { -webkit-print-color-adjust: exact !important; print-color-adjust: exact !important; }
                .print-row { page-break-inside: avoid; }
            }
        </style>
    """)

    with ui.column().classes("w-full max-w-5xl mx-auto p-8 bg-white"):
        # 1. 顶部工具栏 (打印时隐藏)
        with ui.row().classes("w-full justify-between items-center mb-6 no-print"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("format_list_numbered", size="md", color="slate-800")
                ui.label("需求项完整清单").classes("text-2xl font-bold text-slate-800")

            with ui.row().classes("gap-3"):
                ui.button("打印清单", icon="print", on_click=lambda: ui.run_javascript("window.print()")).classes(
                    "bg-slate-900 text-white shadow-lg"
                )
                # ui.button("关闭", icon="close", on_click=lambda: ui.navigate.to("/question_tree_tabs")).props(
                #     "flat color=grey"
                # )

        # 2. 表头
        with ui.row().classes(
            "w-full border-b-2 border-slate-800 pb-2 mb-0 items-center text-sm font-bold text-slate-900 gap-0"
        ):
            ui.label("ID").classes("w-14 text-center")
            ui.label("内容详情").classes("flex-1 px-4")
            ui.label("激活条件").classes("w-48 text-right pr-2")

        # 排序
        def smart_sort(k):
            return int(k) if k.isdigit() else k

        sorted_ids = sorted(DATA.keys(), key=smart_sort)

        # >>> 初始化分组状态变量 <<<
        last_cond = None  # 记录上一行的条件
        is_alt_bg = False  # 背景色开关 (False=白, True=灰)

        # 3. 列表内容
        with ui.column().classes("w-full gap-0 border-b border-slate-200"):
            for nid in sorted_ids:
                item = DATA[nid]
                content = item.get("guide_content", "")
                raw_cond = item.get("condition", "")
                options = item.get("options", [])

                readable_cond = translate_condition(raw_cond)

                # >>> 核心逻辑：检测条件是否变化 <<<
                # 如果当前条件和上一个不一样，切换背景色状态
                if readable_cond != last_cond:
                    is_alt_bg = not is_alt_bg
                    last_cond = readable_cond

                # 根据状态决定使用哪种背景色
                # bg-white: 纯白
                # bg-slate-50: 极淡的灰色 (适合打印)
                row_bg_color = "bg-amber-50/30" if is_alt_bg else "bg-sky-50/30"

                # 条件文字的胶囊样式
                cond_class = (
                    "text-slate-300 font-light scale-90 origin-right"
                    if readable_cond == "初始问题"
                    else "text-sky-600 px-1 rounded font-medium"  # 去掉了背景色，直接用文字颜色区分，因为行背景已经变了
                )

                # >>> 行容器 <<<
                # print-row: 防断页
                # items-stretch: 确保竖线拉伸到底
                # row_bg_color: 应用动态计算的背景色
                with ui.row().classes(
                    f"w-full items-stretch border-t border-slate-200 py-1 print-row transition-colors gap-0 {row_bg_color}"
                ):
                    # [左] ID
                    with ui.element("div").classes(
                        "w-10 flex-none border-r border-slate-200 flex items-start justify-center pt-0.5"
                    ):
                        ui.label(str(nid)).classes("font-mono text-xs font-bold text-slate-400")

                    # [中] 内容
                    with ui.element("div").classes("flex-1 w-0 border-r border-slate-200 px-4 flex flex-col gap-1"):
                        ui.label(content).classes("text-sm font-bold text-slate-800 leading-snug break-words")
                        if options:
                            valid_opts = [str(o.get("option_content", "")) for o in options if o.get("option_content")]
                            if valid_opts:
                                opt_str = " / ".join(valid_opts)
                                ui.label(f"选项: {opt_str}").classes(
                                    "text-[11px] text-slate-500 leading-tight break-words"
                                )

                    # [右] 条件
                    with ui.element("div").classes("w-80 flex-none flex items-start justify-end pl-2"):
                        ui.label(readable_cond).classes(
                            f"text-[10px] leading-tight break-words text-right {cond_class}"
                        )


@ui.page("/question_tree_tabs")
def question_tree_page():
    stored_current_user = app.storage.user.get("current_user")
    if not isinstance(stored_current_user, str) or not stored_current_user:
        ui.navigate.to("/login")
        return
    if not can_view_question_tree(sync_current_user_role(), stored_current_user):
        ui.notify("当前账号没有查看需求项结构的权限", type="negative")
        ui.navigate.to("/main")
        return

    # >>> [颜色配置] 搜索定位动画 (CSS)
    # node-shake 动画颜色配置
    # border-color: #f97316 (red-500) - 晃动时的边框色
    # box-shadow: rgba(249, 115, 22, 0.5) - 晃动时的发光阴影色
    # background-color: #fff7ed (red-50) - 晃动时的背景微亮色
    ui.add_head_html("""
    <style>
        body { overflow: hidden !important; }
        
        @keyframes node-shake {
            0%, 100% { transform: translateX(0); }
            10%, 30%, 50%, 70%, 90% { transform: translateX(-6px); }
            20%, 40%, 60%, 80% { transform: translateX(6px); }
        }
        
        .node-highlight-anim {
            animation: node-shake 0.8s cubic-bezier(.36,.07,.19,.97) both;
            border-color: #f97316 !important; 
            box-shadow: 0 0 15px rgba(249, 115, 22, 0.5) !important;
            z-index: 100 !important;
            background-color: #fff7ed !important;
        }
    </style>
    """)

    ui.add_head_html("""
    <script>
    function smartScrollTo(elementId) {
        const el = document.getElementById(elementId);
        if (!el) return;
        const rect = el.getBoundingClientRect();
        const winHeight = window.innerHeight;
        const margin = winHeight * 0.2; 
        if (rect.top < margin || rect.bottom > (winHeight - margin)) {
            el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
        }
    }

    function flashNode(elementId) {
        const el = document.getElementById(elementId);
        if (!el) return;
        el.classList.remove('node-highlight-anim');
        void el.offsetWidth;
        el.classList.add('node-highlight-anim');
        setTimeout(() => {
            el.classList.remove('node-highlight-anim');
        }, 850);
    }
    /* >>> 新增: 修复 Chrome 自动填充背景色变蓝的问题 (使用过渡延迟法) <<< */
    input:-webkit-autofill,
    input:-webkit-autofill:hover, 
    input:-webkit-autofill:focus, 
    input:-webkit-autofill:active {
        transition: background-color 5000s ease-in-out 0s;
        -webkit-text-fill-color: #374151 !important; /* 保持文字颜色深灰 */
    }
    </script>
    """)

    selected_path.clear()
    active_ancestors.clear()

    setup_global_activity_tracking()

    current_user = stored_current_user
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])
    current_display_path = get_cache_busted_path(current_avatar_path)
    search_dialog = ui.dialog()

    # >>> [颜色配置] 页面顶部 Header: 蓝色背景(bg-blue-500)
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4 z-50")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("需求项结构").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # >>> [颜色配置] 页面总背景: 灰色(bg-slate-200)
    with ui.element("div").classes("fixed top-12 bottom-0 left-0 right-0 flex flex-col gap-0 overflow-hidden bg-white"):
        with ui.row().classes("w-full bg-white border-b p-2 items-center justify-between z-40 shadow-sm flex-none"):
            with (
                ui.input(placeholder="输入 ID 或内容搜索...")
                # 1. props: 保持 dense outlined rounded, 添加 autocomplete="off" 和 input-style
                # input-style="-webkit-box-shadow..." 用于强力覆盖浏览器默认的自动填充背景色
                .props('dense outlined rounded bg-color="white" autocomplete="off"')
                # 2. classes:
                #    - 移除 "rounded" (避免裁切)
                #    - 移除 "bg-white" (已由 props 接管)
                .classes("w-64") as search_input
            ):
                with search_input.add_slot("append"):
                    ui.icon("search", color="blue").classes("cursor-pointer").on(
                        "click", lambda: handle_search(search_input.value, search_dialog)
                    )
                search_input.on("keydown.enter", lambda: handle_search(search_input.value, search_dialog))

            # >>> [修改点] 在这里插入一个 Row 来包含右侧的功能按钮 <<<
            with ui.row().classes("items-center gap-2"):
                # --- 新增的按钮：打印/查看清单 ---
                ui.button(
                    "问题清单",
                    icon="print",
                    # new_tab=True 会在浏览器新标签页打开我们刚才定义的页面
                    on_click=lambda: ui.navigate.to("/print_list", new_tab=True),
                ).props("flat dense color=blue-700").tooltip("在新窗口打开完整列表以打印")

                # --- 原有的重置按钮 ---
                ui.button(
                    "重置路径",
                    icon="refresh",
                    on_click=lambda: (
                        selected_path.clear(),
                        active_ancestors.clear(),
                        update_ui_state(),
                        search_input.set_value("") if search_input else None,
                    ),
                ).props("flat dense color=grey-8")  # 我稍微加了个颜色让它和主色区分开

        # >>> [颜色配置] 滚动列的轨道背景: 稍深一点的灰色(bg-slate-300)
        with ui.row().classes("w-full flex-grow overflow-x-auto overflow-y-hidden no-wrap items-start bg-white gap-0"):
            layout_columns_container()
