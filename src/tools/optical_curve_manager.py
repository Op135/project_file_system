# -*- encoding: utf-8 -*-
"""研发光学曲线数据录入、筛选与可视化工具。"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from nicegui import app, ui

from .. import db_storage
from .optical_curve_data import (
    CurveDataError,
    curve_matches_filters,
    fuse_and_normalize_curve_records,
    normalize_conditions,
    normalize_y_values,
    parse_curve_rows,
)

OPTICAL_CURVE_DATA_KEY = "optical_curve_records"
CURVE_COLOR_PALETTE = [
    "#0891b2",
    "#2563eb",
    "#7c3aed",
    "#db2777",
    "#ea580c",
    "#16a34a",
    "#4f46e5",
    "#0f766e",
]


def _optional_float(value: object) -> float | None:
    """显式收窄可转换类型，避免把 ``Any | None`` 直接传给 ``float``。"""

    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int_at_least(value: object, default: int, minimum: int) -> int:
    numeric = _optional_float(value)
    return max(minimum, int(numeric)) if numeric is not None else default


def _fusion_pending_status(selected_count: int) -> str:
    if selected_count <= 0:
        return ""
    if selected_count == 1:
        return "已选择 1 条融合曲线，还需再选择 1 条"
    return ""


def _curve_color(record: dict[str, Any], fallback_index: int = 0) -> str:
    saved_color = str(record.get("color") or "").strip()
    if saved_color:
        return saved_color
    identity = str(record.get("id") or record.get("title") or fallback_index)
    return CURVE_COLOR_PALETTE[sum(ord(char) for char in identity) % len(CURVE_COLOR_PALETTE)]


def _curve_legend_label(record: dict[str, Any]) -> str:
    """图例显示标题和去重后的条件值，但不混入 Y 轴表征名。"""

    title = str(record.get("title") or "未命名曲线")
    condition_values: list[str] = []
    for condition in record.get("conditions", []):
        if not isinstance(condition, dict):
            continue
        value = str(condition.get("value", "") or "").strip()
        if value and value not in condition_values:
            condition_values.append(value)
    return f"{title} & {' & '.join(condition_values)}" if condition_values else title


def _build_curve_tree(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 Y 轴表征名和条件层级构建只在叶节点勾选曲线的树。"""

    roots: list[dict[str, Any]] = []
    root_lookup: dict[str, dict[str, Any]] = {}

    for record in records:
        y_axis_name = str(record.get("y_axis_name") or "未分类表征").strip()
        root = root_lookup.get(y_axis_name)
        if root is None:
            root = {
                "id": f"group:y:{len(root_lookup)}",
                "label": y_axis_name,
                "children": [],
                "_lookup": {},
            }
            root_lookup[y_axis_name] = root
            roots.append(root)

        current = root
        conditions = sorted(
            (
                {
                    "name": str(item.get("name", "") or "").strip(),
                    "value": str(item.get("value", "") or "").strip(),
                }
                for item in record.get("conditions", [])
                if isinstance(item, dict) and str(item.get("name", "") or "").strip()
            ),
            key=lambda item: item["name"].casefold(),
        )
        if not conditions:
            conditions = [{"name": "条件", "value": "无附加条件"}]

        path_parts = [y_axis_name]
        for condition in conditions:
            label = f"{condition['name']}：{condition['value']}"
            path_parts.append(label)
            lookup = current["_lookup"]
            child = lookup.get(label)
            if child is None:
                child = {
                    "id": f"group:c:{len(roots)}:{'|'.join(path_parts)}",
                    "label": label,
                    "children": [],
                    "_lookup": {},
                }
                lookup[label] = child
                current["children"].append(child)
            current = child

        current["children"].append(
            {
                "id": str(record.get("id", "")),
                "label": _curve_legend_label(record),
                "icon": "show_chart",
            }
        )

    def remove_internal_lookup(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            node.pop("_lookup", None)
            remove_internal_lookup(node.get("children", []))

    remove_internal_lookup(roots)
    return roots


def _chart_options(
    records: list[dict[str, Any]],
    *,
    preview: bool = False,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成录入预览或筛选结果使用的 ECharts 配置。"""

    settings = settings or {}
    font_family = str(settings.get("font_family") or "Microsoft YaHei")
    font_size = _int_at_least(settings.get("font_size"), 12, 8)
    legend_font_size = _int_at_least(settings.get("legend_font_size"), font_size, 8)
    x_min = settings.get("x_min")
    x_max = settings.get("x_max")
    x_interval = settings.get("x_interval")
    y_interval = settings.get("y_interval")
    series = []
    y_axis_names: list[str] = []
    for index, record in enumerate(records):
        title = _curve_legend_label(record)
        y_axis_name = str(record.get("y_axis_name") or "Y轴").strip()
        if not record.get("is_fused") and y_axis_name and y_axis_name not in y_axis_names:
            y_axis_names.append(y_axis_name)
        color = _curve_color(record, index)
        is_fused = bool(record.get("is_fused"))
        series.append(
            {
                "name": title,
                "type": "line",
                "showSymbol": (not is_fused) and len(record.get("x_data", [])) <= 30,
                "symbolSize": 5,
                "smooth": False,
                "lineStyle": {"color": color, "width": 4 if is_fused else 2},
                "itemStyle": {"color": color},
                "z": 10 if is_fused else 2,
                "data": [
                    [x_value, y_value] for x_value, y_value in zip(record.get("x_data", []), record.get("y_data", []))
                ],
            }
        )

    options: dict[str, Any] = {
        "animation": not preview,
        "textStyle": {"fontFamily": font_family, "fontSize": font_size},
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "cross"},
            "textStyle": {"fontFamily": font_family, "fontSize": font_size},
        },
        "legend": {
            "type": "scroll",
            "top": 0,
            "textStyle": {"fontFamily": font_family, "fontSize": legend_font_size},
        },
        "grid": {"top": 52, "left": 86, "right": 36, "bottom": 72, "containLabel": True},
        "toolbox": {
            "right": 12,
            "feature": {"dataZoom": {}, "restore": {}, "saveAsImage": {"title": "保存图片"}},
        },
        "dataZoom": [{"type": "inside"}, {"type": "slider", "height": 20, "bottom": 18}],
        "xAxis": {
            "type": "value",
            "name": "波长 (nm)",
            "nameLocation": "middle",
            "nameGap": 34,
            "scale": True,
            "axisLabel": {"fontFamily": font_family, "fontSize": font_size},
            "splitLine": {"lineStyle": {"type": "dashed", "color": "#e2e8f0"}},
        },
        "yAxis": {
            "type": "value",
            "name": " / ".join(y_axis_names) if y_axis_names else "归一化值",
            "nameLocation": "middle",
            "nameRotate": 90,
            "nameGap": 58,
            "nameTextStyle": {
                # "fontWeight": "bold",
                "color": "#475569",
                "fontFamily": font_family,
                "fontSize": font_size,
            },
            "scale": True,
            "axisLabel": {"fontFamily": font_family, "fontSize": font_size},
            "splitLine": {"lineStyle": {"type": "dashed", "color": "#e2e8f0"}},
        },
        "series": series,
    }

    if isinstance(x_interval, (int, float)) and x_interval > 0:
        options["xAxis"]["interval"] = x_interval
    if isinstance(y_interval, (int, float)) and y_interval > 0:
        options["yAxis"]["interval"] = y_interval
    valid_x_min = _optional_float(x_min)
    valid_x_max = _optional_float(x_max)
    if valid_x_min is not None and valid_x_max is not None and valid_x_min >= valid_x_max:
        valid_x_min = valid_x_max = None
    if valid_x_min is not None:
        options["xAxis"]["min"] = valid_x_min
        for zoom in options["dataZoom"]:
            zoom["startValue"] = valid_x_min
    if valid_x_max is not None:
        options["xAxis"]["max"] = valid_x_max
        for zoom in options["dataZoom"]:
            zoom["endValue"] = valid_x_max
    return options


class OpticalCurveManagerTool:
    """面向研发光学角色的曲线资料库工具。"""

    def __init__(self) -> None:
        self.form = {"title": "", "y_axis_name": "", "data_text": ""}
        self.condition_rows = [{"name": "", "value": ""}]
        self.filter_state = {"title_query": "", "y_axis_name": ""}
        self.filter_rows = [{"name": "", "value": ""}]
        self.selected_curve_ids: list[str] = []
        self.fusion_curve_ids: list[str] = []
        self.chart_settings: dict[str, Any] = {
            "x_min": None,
            "x_max": None,
            "x_interval": 100,
            "y_interval": 0.1,
            "font_family": "Arial",
            "font_size": 15,
            "legend_font_size": 15,
        }
        self.axis_range_draft: dict[str, Any] = {"x_min": None, "x_max": None}
        self.left_sidebar_open = False
        self.right_sidebar_open = False
        self.preview_record: dict[str, Any] | None = None

    @staticmethod
    def _all_records() -> list[dict[str, Any]]:
        stored = db_storage.get_item(OPTICAL_CURVE_DATA_KEY, {})
        if not isinstance(stored, dict):
            return []
        records = [record for record in stored.values() if isinstance(record, dict)]
        return sorted(records, key=lambda item: str(item.get("created_at", "")), reverse=True)

    @staticmethod
    def _set_row_value(rows: list[dict[str, str]], index: int, key: str, value: Any) -> None:
        if 0 <= index < len(rows):
            rows[index][key] = str(value or "")

    def show(self, dialog: ui.dialog) -> None:
        """在全屏工具弹窗中渲染界面。"""

        with ui.column().classes("w-full h-full p-0 gap-0 bg-slate-50"):
            with ui.row().classes("w-full bg-white px-5 py-3 border-b items-center justify-between shadow-sm"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("show_chart", size="34px").classes("text-cyan-700")
                    with ui.column().classes("gap-0"):
                        ui.label("研发光学曲线资料库").classes("text-xl font-bold text-slate-800")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round").tooltip("关闭")

            tabs = ui.tabs().classes("w-full bg-white text-slate-600")
            with tabs:
                entry_tab = ui.tab("数据录入", icon="edit_note")
                query_tab = ui.tab("筛选与曲线", icon="query_stats")

            with ui.tab_panels(tabs, value=entry_tab).classes("w-full flex-1 bg-slate-50"):
                with ui.tab_panel(entry_tab).classes("p-0"):
                    with ui.scroll_area().classes("w-full h-[calc(100vh-112px)]"):
                        with ui.column().classes("w-full max-w-[1800px] mx-auto p-0 gap-3"):
                            with ui.grid().classes("w-full grid-cols-1 lg:grid-cols-12 gap-2 items-stretch"):
                                with ui.card().classes("lg:col-span-3 w-full h-full p-4 rounded-xl shadow-sm"):
                                    ui.label("1. 曲线信息").classes("text-lg font-bold text-slate-800")
                                    ui.label("标题、表征名及条件用于后续检索。").classes("text-xs text-slate-500 mb-1")
                                    ui.input("本次数据标题 *", placeholder="例如：白光LED/550nm二向色").bind_value(
                                        self.form, "title"
                                    ).props("outlined dense clearable").classes("w-full")
                                    ui.input(
                                        "Y 轴表征名 *", placeholder="例如：透过率/反射率/相对光谱功率密度"
                                    ).bind_value(self.form, "y_axis_name").props("outlined dense clearable").classes(
                                        "w-full"
                                    )

                                    with ui.row().classes("w-full items-center justify-between mt-1"):
                                        ui.label("成立条件（可选）").classes("font-semibold text-slate-700")
                                        ui.button(
                                            "增加条件",
                                            icon="add",
                                            on_click=lambda: self._add_condition(render_condition_rows),
                                        ).props("flat dense no-caps color=cyan-8")

                                    @ui.refreshable
                                    def render_condition_rows() -> None:
                                        with ui.column().classes("w-full gap-2"):
                                            if not self.condition_rows:
                                                ui.label("未设置成立条件").classes("text-sm text-slate-400")
                                            for index, row in enumerate(self.condition_rows):
                                                with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                                                    ui.input(
                                                        "条件名",
                                                        value=row["name"],
                                                        placeholder="如：角度/结温/If",
                                                        on_change=lambda e, i=index: self._set_row_value(
                                                            self.condition_rows, i, "name", e.value
                                                        ),
                                                    ).props("outlined dense").classes("flex-1")
                                                    ui.input(
                                                        "条件值",
                                                        value=row["value"],
                                                        placeholder="如：0°/25℃/70mA",
                                                        on_change=lambda e, i=index: self._set_row_value(
                                                            self.condition_rows, i, "value", e.value
                                                        ),
                                                    ).props("outlined dense").classes("flex-1")
                                                    ui.button(
                                                        icon="delete_outline",
                                                        on_click=self._make_remove_handler(
                                                            self.condition_rows, index, render_condition_rows
                                                        ),
                                                    ).props("flat dense round color=grey-6").tooltip("删除条件")

                                    render_condition_rows()

                                with ui.card().classes("lg:col-span-3 w-full h-full p-4 rounded-xl shadow-sm"):
                                    ui.label("2. 粘贴两列数据").classes("text-lg font-bold text-slate-800")
                                    ui.label("第一列为波长（nm），第二列为 Y；支持从 Excel 复制粘贴。").classes(
                                        "text-xs text-slate-500"
                                    )
                                    data_input = (
                                        ui.textarea(
                                            "X轴：波长 (nm)        Y轴：原始数据 *",
                                            placeholder="400\t0.12\n450\t0.48\n500\t0.96\n550\t0.72",
                                        )
                                        .bind_value(self.form, "data_text")
                                        .props("outlined rows=15 input-style='font-family: monospace'")
                                        .classes("w-full")
                                    )
                                    ui.label(
                                        "保存时系统按 Y 的最大绝对值自动归一化，原始 Y 序列不会写入数据库。"
                                    ).classes("text-xs text-cyan-700")
                                    with ui.row().classes("w-full justify-end"):
                                        ui.button(
                                            "解析并预览",
                                            icon="preview",
                                            on_click=lambda: parse_preview(),
                                        ).props("outline no-caps color=cyan-8")
                                        ui.button(
                                            "保存曲线",
                                            icon="save",
                                            on_click=lambda: save_record(),
                                        ).props("unelevated no-caps color=cyan-8")

                                with ui.card().classes("lg:col-span-6 w-full h-full p-4 rounded-xl shadow-sm"):
                                    with ui.row().classes("w-full items-center justify-between"):
                                        ui.label("归一化预览").classes("text-lg font-bold text-slate-800")
                                        preview_summary = ui.label("请先解析两列数据").classes("text-sm text-slate-500")
                                    preview_chart = ui.echart(_chart_options([], preview=True)).classes(
                                        "w-full h-[500px]"
                                    )

                            def parse_preview(*, notify: bool = True) -> dict[str, Any] | None:
                                try:
                                    x_data, raw_y_data = parse_curve_rows(self.form["data_text"])
                                    y_data, factor = normalize_y_values(raw_y_data)
                                except CurveDataError as exc:
                                    self.preview_record = None
                                    if notify:
                                        ui.notify(str(exc), type="warning")
                                    return None

                                preview = {
                                    "title": self.form["title"].strip() or "当前录入预览",
                                    "y_axis_name": self.form["y_axis_name"].strip() or "Y轴数据",
                                    "x_data": x_data,
                                    "y_data": y_data,
                                    "normalization_factor": factor,
                                }
                                self.preview_record = preview
                                preview_chart.options.clear()
                                preview_chart.options.update(_chart_options([preview], preview=True))
                                preview_chart.update()
                                preview_summary.set_text(f"{len(x_data)} 个数据点 · 归一化因子 {factor:.6g}")
                                if notify:
                                    ui.notify("数据解析成功，已生成归一化预览", type="positive")
                                return preview

                            async def save_record() -> None:
                                title = str(self.form["title"] or "").strip()
                                y_axis_name = str(self.form["y_axis_name"] or "").strip()
                                if not title or not y_axis_name:
                                    ui.notify("请填写数据标题和 Y 轴表征名", type="warning")
                                    return

                                try:
                                    conditions = normalize_conditions(self.condition_rows)
                                except CurveDataError as exc:
                                    ui.notify(str(exc), type="warning")
                                    return

                                preview = parse_preview(notify=False)
                                if preview is None:
                                    ui.notify("两列数据格式不正确，请检查后重试", type="warning")
                                    return

                                record_id = uuid4().hex
                                now = datetime.now().isoformat(timespec="seconds")
                                record = {
                                    "id": record_id,
                                    "title": title,
                                    "y_axis_name": y_axis_name,
                                    "conditions": conditions,
                                    "x_data": preview["x_data"],
                                    "y_data": preview["y_data"],
                                    "normalization_factor": preview["normalization_factor"],
                                    "created_by": app.storage.user.get("current_user", "未知用户"),
                                    "created_role": app.storage.user.get("current_role", ""),
                                    "created_at": now,
                                }

                                def add_record(current: Any) -> dict[str, Any]:
                                    records = copy.deepcopy(current) if isinstance(current, dict) else {}
                                    records[record_id] = record
                                    return records

                                success = await db_storage.atomic_deep_update([OPTICAL_CURVE_DATA_KEY], add_record)
                                if not success:
                                    ui.notify("保存失败，请稍后重试", type="negative")
                                    return

                                self.form.update({"title": "", "y_axis_name": "", "data_text": ""})
                                self.condition_rows[:] = [{"name": "", "value": ""}]
                                self.preview_record = None
                                data_input.update()
                                render_condition_rows.refresh()
                                filter_panel.refresh()
                                ui.notify("曲线已归一化并保存", type="positive")

                with ui.tab_panel(query_tab).classes("p-0"):
                    with ui.element("div").classes("w-full h-[calc(100vh-112px)] overflow-hidden"):

                        @ui.refreshable
                        def filter_panel() -> None:
                            all_records = self._all_records()
                            condition_catalog: dict[str, set[str]] = {}
                            for record in all_records:
                                for item in record.get("conditions", []):
                                    if not isinstance(item, dict):
                                        continue
                                    name = str(item.get("name", "") or "").strip()
                                    value = str(item.get("value", "") or "").strip()
                                    if name and value:
                                        condition_catalog.setdefault(name, set()).add(value)

                            y_axis_options = sorted(
                                {
                                    str(record.get("y_axis_name", "") or "").strip()
                                    for record in all_records
                                    if str(record.get("y_axis_name", "") or "").strip()
                                }
                            )

                            def get_matches() -> list[dict[str, Any]]:
                                selected_conditions = [
                                    row for row in self.filter_rows if row.get("name") or row.get("value")
                                ]
                                return [
                                    record
                                    for record in self._all_records()
                                    if curve_matches_filters(
                                        record,
                                        title_query=self.filter_state["title_query"],
                                        y_axis_name=self.filter_state["y_axis_name"],
                                        conditions=selected_conditions,
                                    )
                                ]

                            @ui.refreshable
                            def render_filter_rows() -> None:
                                with ui.column().classes("w-full gap-1"):
                                    for index, row in enumerate(self.filter_rows):
                                        with ui.row().classes("w-full items-center gap-1 flex-nowrap"):
                                            available_names = [""] + sorted(condition_catalog)
                                            ui.select(
                                                available_names,
                                                label="成立条件",
                                                value=row["name"] if row["name"] in available_names else "",
                                                on_change=lambda e, i=index: self._change_filter_name(
                                                    i, e.value, render_filter_rows
                                                ),
                                            ).props("outlined dense clearable options-dense").classes("flex-1")
                                            values = [""] + sorted(condition_catalog.get(row["name"], set()))
                                            ui.select(
                                                values,
                                                label="条件值",
                                                value=row["value"] if row["value"] in values else "",
                                                on_change=lambda e, i=index: self._set_row_value(
                                                    self.filter_rows, i, "value", e.value
                                                ),
                                            ).props("outlined dense clearable options-dense").classes("flex-1")
                                            ui.button(
                                                icon="delete_outline",
                                                on_click=self._make_remove_handler(
                                                    self.filter_rows, index, render_filter_rows
                                                ),
                                            ).props("flat dense round color=grey-6").tooltip("删除筛选条件")

                            def apply_filters() -> None:
                                if any(
                                    bool(str(row.get("name", "")).strip()) != bool(str(row.get("value", "")).strip())
                                    for row in self.filter_rows
                                ):
                                    ui.notify("每个筛选条件都需要同时选择条件名和条件值", type="warning")
                                    return
                                self.left_sidebar_open = True
                                render_workspace.refresh()

                            @ui.refreshable
                            def render_workspace() -> None:
                                matches = get_matches()
                                all_record_lookup = {
                                    str(record.get("id", "")): record
                                    for record in self._all_records()
                                    if str(record.get("id", ""))
                                }
                                match_ids = {str(record.get("id", "")) for record in matches}
                                self.selected_curve_ids = [
                                    record_id for record_id in self.selected_curve_ids if record_id in all_record_lookup
                                ]
                                selected_records = [
                                    all_record_lookup[record_id]
                                    for record_id in self.selected_curve_ids
                                    if record_id in all_record_lookup
                                ]
                                self.fusion_curve_ids = [
                                    record_id
                                    for record_id in self.fusion_curve_ids
                                    if record_id in self.selected_curve_ids
                                ]
                                tree_nodes = _build_curve_tree(matches)
                                chart_holder: dict[str, Any] = {"chart": None}
                                fusion_status_holder: dict[str, Any] = {"label": None}

                                def build_display_records() -> tuple[list[dict[str, Any]], str]:
                                    display_records = list(selected_records)
                                    selected = [
                                        record
                                        for record in selected_records
                                        if str(record.get("id", "")) in self.fusion_curve_ids
                                    ]
                                    pending_status = _fusion_pending_status(len(selected))
                                    if len(selected) < 2:
                                        return display_records, pending_status
                                    try:
                                        fused_x, fused_y, fusion_factor = fuse_and_normalize_curve_records(selected)
                                    except CurveDataError as exc:
                                        return display_records, str(exc)
                                    display_records.append(
                                        {
                                            "id": "temporary_fusion",
                                            "title": f"融合曲线（{len(selected)}条累加）",
                                            "y_axis_name": "",
                                            "x_data": fused_x,
                                            "y_data": fused_y,
                                            "color": "#e11d48",
                                            "is_fused": True,
                                        }
                                    )
                                    return (
                                        display_records,
                                        f"已融合 {len(selected)} 条曲线并重新归一化"
                                        f"（因子 {fusion_factor:.6g}）· 仅临时显示，不保存数据",
                                    )

                                def refresh_chart() -> None:
                                    chart = chart_holder.get("chart")
                                    if chart is None:
                                        return
                                    display_records, status_text = build_display_records()
                                    chart.options.clear()
                                    chart.options.update(_chart_options(display_records, settings=self.chart_settings))
                                    chart.update()
                                    status_label = fusion_status_holder.get("label")
                                    if status_label is not None:
                                        status_label.set_text(status_text)

                                def change_fusion_selection(event: Any) -> None:
                                    self.fusion_curve_ids = [str(value) for value in (event.value or [])]
                                    refresh_chart()

                                def change_curve_selection(event: Any) -> None:
                                    hidden_selected = [
                                        record_id for record_id in self.selected_curve_ids if record_id not in match_ids
                                    ]
                                    visible_selected = [
                                        str(value) for value in (event.value or []) if str(value) in match_ids
                                    ]
                                    self.selected_curve_ids = list(dict.fromkeys(hidden_selected + visible_selected))
                                    self.fusion_curve_ids = [
                                        record_id
                                        for record_id in self.fusion_curve_ids
                                        if record_id in self.selected_curve_ids
                                    ]
                                    self.left_sidebar_open = True
                                    render_workspace.refresh()

                                def select_all_matches() -> None:
                                    self.selected_curve_ids = list(
                                        dict.fromkeys(self.selected_curve_ids + sorted(match_ids))
                                    )
                                    self.left_sidebar_open = True
                                    render_workspace.refresh()

                                def clear_all_selected() -> None:
                                    self.selected_curve_ids = []
                                    self.fusion_curve_ids = []
                                    self.left_sidebar_open = True
                                    render_workspace.refresh()

                                def reset_filters_from_left() -> None:
                                    self.left_sidebar_open = True
                                    self._reset_filters(render_workspace)

                                def refresh_library_from_left() -> None:
                                    self.left_sidebar_open = True
                                    filter_panel.refresh()

                                def close_left_sidebar() -> None:
                                    if not self.left_sidebar_open:
                                        return
                                    self.left_sidebar_open = False
                                    render_workspace.refresh()

                                def close_right_sidebar() -> None:
                                    if not self.right_sidebar_open:
                                        return
                                    self.right_sidebar_open = False
                                    render_workspace.refresh()

                                def make_chart_setting_handler(key: str) -> Callable[[Any], None]:
                                    def change_setting(event: Any) -> None:
                                        value = event.value
                                        if key in {"x_interval", "y_interval"}:
                                            numeric_value = _optional_float(value)
                                            value = (
                                                numeric_value
                                                if numeric_value is not None and numeric_value > 0
                                                else None
                                            )
                                        elif key in {"font_size", "legend_font_size"}:
                                            value = _int_at_least(value, 12, 8)
                                        self.chart_settings[key] = value
                                        refresh_chart()

                                    return change_setting

                                def apply_x_range() -> None:
                                    raw_min = self.axis_range_draft.get("x_min")
                                    raw_max = self.axis_range_draft.get("x_max")
                                    x_min = _optional_float(raw_min)
                                    x_max = _optional_float(raw_max)
                                    if (raw_min is not None and raw_min != "" and x_min is None) or (
                                        raw_max is not None and raw_max != "" and x_max is None
                                    ):
                                        ui.notify("请输入有效的X轴首尾数字", type="warning")
                                        return
                                    if x_min is not None and x_max is not None and x_min >= x_max:
                                        ui.notify("X轴显示终点必须大于起点", type="warning")
                                        return
                                    self.chart_settings["x_min"] = x_min
                                    self.chart_settings["x_max"] = x_max
                                    refresh_chart()

                                def clear_x_range() -> None:
                                    self.axis_range_draft.update({"x_min": None, "x_max": None})
                                    self.chart_settings.update({"x_min": None, "x_max": None})
                                    self.right_sidebar_open = True
                                    render_workspace.refresh()

                                def make_color_handler(record: dict[str, Any]) -> Callable[[Any], Any]:
                                    record_id = str(record.get("id", ""))

                                    async def change_color(event: Any) -> None:
                                        color = str(event.value or "").strip()
                                        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                                            ui.notify("请输入有效的六位十六进制颜色", type="warning")
                                            return
                                        success = await db_storage.atomic_deep_update(
                                            [OPTICAL_CURVE_DATA_KEY, record_id, "color"],
                                            lambda _: color,
                                        )
                                        if not success:
                                            ui.notify("颜色保存失败，请稍后重试", type="negative")
                                            return
                                        record["color"] = color
                                        refresh_chart()
                                        ui.notify("曲线颜色已自动保存", type="positive")

                                    return change_color

                                def render_filter_and_tree() -> None:
                                    with ui.card().classes("w-full p-3 rounded-xl shadow-sm gap-2"):
                                        with ui.row().classes("w-full items-center justify-between"):
                                            with ui.column().classes("gap-0"):
                                                ui.label("筛选曲线").classes("text-base font-bold text-slate-800")
                                                ui.label(
                                                    f"资料库共 {len(all_records)} 条，当前命中 {len(matches)} 条"
                                                ).classes("text-xs text-slate-500")
                                            ui.button(icon="refresh", on_click=refresh_library_from_left).props(
                                                "flat dense round color=cyan-8"
                                            ).tooltip("刷新资料库")
                                        ui.input("搜索关键词", placeholder="标题、表征名或条件值").bind_value(
                                            self.filter_state, "title_query"
                                        ).props("outlined dense clearable").classes("w-full")
                                        with ui.row().classes("w-full items-center gap-2 flex-nowrap"):
                                            ui.select(
                                                [""] + y_axis_options,
                                                label="Y 轴表征名",
                                                value=self.filter_state["y_axis_name"],
                                                on_change=lambda e: self.filter_state.update(y_axis_name=e.value or ""),
                                            ).props("outlined dense clearable options-dense").classes("flex-1")
                                            ui.button(
                                                "增加条件",
                                                icon="add",
                                                on_click=lambda: self._add_condition_filter(render_filter_rows),
                                            ).props("outline dense no-caps color=cyan-8")
                                        render_filter_rows()
                                        with ui.row().classes("w-full justify-end gap-1"):
                                            ui.button(
                                                "重置",
                                                icon="restart_alt",
                                                on_click=reset_filters_from_left,
                                            ).props("flat dense no-caps color=grey-7")
                                            ui.button(
                                                "应用筛选",
                                                icon="filter_alt",
                                                on_click=apply_filters,
                                            ).props("unelevated dense no-caps color=cyan-8")

                                    with ui.card().classes("w-full p-3 rounded-xl shadow-sm gap-2"):
                                        with ui.row().classes("w-full items-center justify-between"):
                                            with ui.column().classes("gap-0"):
                                                ui.label("层级勾选曲线").classes("text-base font-bold text-slate-800")
                                                ui.label(
                                                    f"候选 {len(matches)} 条 · 已选 {len(selected_records)} 条"
                                                ).classes("text-xs text-slate-500")
                                            with ui.row().classes("gap-1"):
                                                ui.button("全选候选", on_click=select_all_matches).props(
                                                    "flat dense no-caps color=cyan-8"
                                                )
                                                ui.button("清空", on_click=clear_all_selected).props(
                                                    "flat dense no-caps color=grey-7"
                                                )
                                        if tree_nodes:
                                            with ui.scroll_area().classes("w-full h-[calc(100vh-500px)] min-h-[260px]"):
                                                curve_tree = (
                                                    ui.tree(
                                                        tree_nodes,
                                                        on_tick=change_curve_selection,
                                                        tick_strategy="leaf",
                                                    )
                                                    .props("dense no-connectors")
                                                    .classes("w-full text-sm")
                                                )
                                                curve_tree.tick(
                                                    [
                                                        record_id
                                                        for record_id in self.selected_curve_ids
                                                        if record_id in match_ids
                                                    ]
                                                )
                                                curve_tree.expand([node["id"] for node in tree_nodes])
                                        else:
                                            ui.label("没有符合搜索或筛选条件的曲线").classes(
                                                "text-sm text-slate-400 py-4"
                                            )

                                def render_display_settings() -> None:
                                    with ui.card().classes("w-full p-3 rounded-xl shadow-sm gap-2"):
                                        ui.label("显示、融合与颜色").classes("text-base font-bold text-slate-800")
                                        ui.label("临时融合曲线").classes("text-sm font-semibold text-slate-700")
                                        ui.label("按 X 点并集插值累加，范围外按 0，融合结果再单独归一化。").classes(
                                            "text-xs text-slate-500"
                                        )
                                        fusion_options = {
                                            str(record.get("id", "")): _curve_legend_label(record)
                                            for record in selected_records
                                        }
                                        ui.select(
                                            fusion_options,
                                            label="指定参与融合的曲线",
                                            value=self.fusion_curve_ids,
                                            multiple=True,
                                            clearable=True,
                                            on_change=change_fusion_selection,
                                        ).props("outlined dense use-chips options-dense").classes("w-full")
                                        ui.separator().classes("my-1")
                                        ui.label("图表显示设置").classes("text-sm font-semibold text-slate-700")
                                        with ui.grid().classes("w-full grid-cols-2 gap-2"):
                                            ui.number("X 轴显示起点").bind_value(self.axis_range_draft, "x_min").props(
                                                "outlined dense clearable"
                                            ).on("blur", apply_x_range)
                                            ui.number("X 轴显示终点").bind_value(self.axis_range_draft, "x_max").props(
                                                "outlined dense clearable"
                                            ).on("blur", apply_x_range)
                                            with ui.row().classes(
                                                "col-span-2 w-full items-center justify-between gap-1"
                                            ):
                                                ui.label("离开输入框后自动生效").classes("text-xs text-slate-400")
                                                ui.button(
                                                    "自动范围",
                                                    icon="restart_alt",
                                                    on_click=clear_x_range,
                                                ).props("flat dense no-caps color=grey-7")
                                            ui.number(
                                                "X 轴刻度间隔",
                                                value=self.chart_settings["x_interval"],
                                                min=0,
                                                step=10,
                                                on_change=make_chart_setting_handler("x_interval"),
                                            ).props("outlined dense clearable")
                                            ui.number(
                                                "Y 轴刻度间隔",
                                                value=self.chart_settings["y_interval"],
                                                min=0,
                                                step=0.1,
                                                on_change=make_chart_setting_handler("y_interval"),
                                            ).props("outlined dense clearable")
                                            ui.select(
                                                ["Microsoft YaHei", "Arial", "SimSun", "SimHei"],
                                                label="图表字体",
                                                value=self.chart_settings["font_family"],
                                                on_change=make_chart_setting_handler("font_family"),
                                            ).props("outlined dense options-dense")
                                            ui.number(
                                                "坐标与提示字号",
                                                value=self.chart_settings["font_size"],
                                                min=8,
                                                max=32,
                                                step=1,
                                                on_change=make_chart_setting_handler("font_size"),
                                            ).props("outlined dense")
                                            ui.number(
                                                "顶部图例字号",
                                                value=self.chart_settings["legend_font_size"],
                                                min=8,
                                                max=32,
                                                step=1,
                                                on_change=make_chart_setting_handler("legend_font_size"),
                                            ).props("outlined dense")
                                        ui.separator().classes("my-1")
                                        ui.label("已选曲线与颜色").classes("text-sm font-semibold text-slate-700")
                                        if selected_records:
                                            with ui.scroll_area().classes("w-full h-[260px]"):
                                                with ui.column().classes("w-full gap-1 pr-2"):
                                                    for index, record in enumerate(selected_records):
                                                        with ui.row().classes(
                                                            "w-full items-center justify-between gap-2 border-b border-slate-100 py-1"
                                                        ):
                                                            with ui.column().classes("min-w-0 flex-1 gap-0"):
                                                                ui.label(
                                                                    str(record.get("title", "未命名曲线"))
                                                                ).classes("text-sm font-medium text-slate-700 truncate")
                                                                ui.label(str(record.get("y_axis_name", "Y轴"))).classes(
                                                                    "text-xs text-slate-400"
                                                                )
                                                            ui.color_input(
                                                                value=_curve_color(record, index),
                                                                on_change=make_color_handler(record),
                                                                preview=True,
                                                            ).props("outlined dense").classes("w-28")
                                        else:
                                            ui.label("请先在层级树中勾选曲线").classes("text-sm text-slate-400")

                                with ui.element("div").classes("relative w-full h-full p-2 box-border overflow-hidden"):
                                    with ui.card().classes(
                                        "w-full h-full min-h-0 p-3 rounded-xl shadow-sm flex flex-col overflow-hidden"
                                    ):
                                        with ui.row().classes("w-full items-center justify-between"):
                                            ui.label("曲线对比").classes("text-lg font-bold text-slate-800")
                                            ui.badge(f"显示 {len(selected_records)} 条", color="cyan-8").props(
                                                "rounded"
                                            )
                                        display_records, fusion_status = build_display_records()
                                        fusion_status_holder["label"] = ui.label(fusion_status).classes(
                                            "text-xs text-rose-600"
                                        )
                                        if selected_records:
                                            chart_holder["chart"] = ui.echart(
                                                _chart_options(display_records, settings=self.chart_settings)
                                            ).classes("w-full flex-1 min-h-0")
                                        else:
                                            with ui.column().classes(
                                                "w-full flex-1 min-h-0 items-center justify-center gap-2"
                                            ):
                                                ui.icon("query_stats", size="56px").classes("text-slate-300")
                                                ui.label("请从左侧边栏勾选需要显示的曲线").classes("text-slate-500")
                                    # 控制侧边栏宽度与高度
                                    sidebar_with = "w-1"
                                    sidebar_hight = "top-[150px]"

                                    left_base_width = "w-[370px]" if self.left_sidebar_open else sidebar_with
                                    with (
                                        ui.element("div")
                                        .classes(
                                            f"fixed left-0 {sidebar_hight} bottom-0 z-40 {left_base_width} hover:w-[370px] "
                                            "focus-within:w-[370px] transition-[width] duration-300 delay-300 "
                                            "hover:delay-0 focus-within:delay-0 overflow-hidden group"
                                        )
                                        .on(
                                            "mouseleave",
                                            close_left_sidebar,
                                            js_handler="""(event) => {
                                            const focusedInside = event.currentTarget.contains(document.activeElement);
                                            const popupOpen = document.querySelector('.q-menu');
                                            if (!focusedInside && !popupOpen) emit();
                                        }""",
                                        )
                                        .on(
                                            "focusout",
                                            close_left_sidebar,
                                            js_handler="""(event) => {
                                            setTimeout(() => {
                                                const focusedInside = event.currentTarget.contains(document.activeElement);
                                                const popupOpen = document.querySelector('.q-menu');
                                                if (!focusedInside && !popupOpen) emit();
                                            }, 100);
                                        }""",
                                        )
                                    ):
                                        with ui.row().classes(
                                            "absolute left-0 top-0 w-[370px] h-full flex-nowrap gap-0"
                                        ):
                                            with ui.column().classes(
                                                f"{sidebar_with} h-full shrink-0 bg-cyan-500/60 text-white items-center "
                                                "justify-center p-0 shadow-lg"
                                            ):
                                                # ui.icon("tune", size="16px")
                                                ui.tooltip("筛选与选择")
                                            with ui.scroll_area().classes(
                                                "w-[350px] h-full bg-slate-100/95 backdrop-blur shadow-2xl p-2"
                                            ):
                                                with ui.column().classes("w-full gap-3 pr-1"):
                                                    render_filter_and_tree()

                                    right_base_width = "w-[370px]" if self.right_sidebar_open else sidebar_with
                                    with (
                                        ui.element("div")
                                        .classes(
                                            f"fixed right-0 {sidebar_hight} bottom-0 z-40 {right_base_width} hover:w-[370px] "
                                            "focus-within:w-[370px] transition-[width] duration-300 delay-300 "
                                            "hover:delay-0 focus-within:delay-0 overflow-hidden group"
                                        )
                                        .on(
                                            "mouseleave",
                                            close_right_sidebar,
                                            js_handler="""(event) => {
                                            const focusedInside = event.currentTarget.contains(document.activeElement);
                                            const popupOpen = document.querySelector('.q-menu');
                                            if (!focusedInside && !popupOpen) emit();
                                        }""",
                                        )
                                        .on(
                                            "focusout",
                                            close_right_sidebar,
                                            js_handler="""(event) => {
                                            setTimeout(() => {
                                                const focusedInside = event.currentTarget.contains(document.activeElement);
                                                const popupOpen = document.querySelector('.q-menu');
                                                if (!focusedInside && !popupOpen) emit();
                                            }, 100);
                                        }""",
                                        )
                                    ):
                                        with ui.row().classes(
                                            "absolute right-0 top-0 w-[370px] h-full flex-nowrap gap-0"
                                        ):
                                            with ui.scroll_area().classes(
                                                "w-[390px] h-full bg-slate-100/95 backdrop-blur shadow-2xl p-2"
                                            ):
                                                with ui.column().classes("w-full gap-3 pr-1"):
                                                    render_display_settings()
                                            with ui.column().classes(
                                                f"{sidebar_with} h-full shrink-0 bg-amber-500/60 text-white items-center "
                                                "justify-center p-0 shadow-lg"
                                            ):
                                                # ui.icon("display_settings", size="16px")
                                                ui.tooltip("显示设置")

                            render_workspace()

                        filter_panel()

    def _add_condition(self, renderer: Any) -> None:
        self.condition_rows.append({"name": "", "value": ""})
        renderer.refresh()

    def _add_condition_filter(self, renderer: Any) -> None:
        self.filter_rows.append({"name": "", "value": ""})
        renderer.refresh()

    @staticmethod
    def _remove_row(rows: list[dict[str, str]], index: int, renderer: Any) -> None:
        if 0 <= index < len(rows):
            rows.pop(index)
        renderer.refresh()

    @classmethod
    def _make_remove_handler(
        cls,
        rows: list[dict[str, str]],
        index: int,
        renderer: Any,
    ) -> Callable[[], None]:
        """创建零参数点击回调，避免 NiceGUI 把 ClickEventArguments 写入行号参数。"""

        def remove() -> None:
            cls._remove_row(rows, index, renderer)

        return remove

    def _change_filter_name(self, index: int, value: Any, renderer: Any) -> None:
        if 0 <= index < len(self.filter_rows):
            self.filter_rows[index] = {"name": str(value or ""), "value": ""}
        renderer.refresh()

    def _reset_filters(self, renderer: Any) -> None:
        self.filter_state.update({"title_query": "", "y_axis_name": ""})
        self.filter_rows[:] = [{"name": "", "value": ""}]
        renderer.refresh()
