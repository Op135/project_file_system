# -*- encoding: utf-8 -*-
"""面向光学工程师的光谱、色度与显色对比工具。"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from nicegui import run, ui

from .spectral_analysis import (
    COORDINATE_SYSTEMS,
    STANDARD_ILLUMINANTS,
    ChromaticityResult,
    PowerLimitedMixResult,
    SpectralAnalysisError,
    SpectrumChromaticityResult,
    SpectrumInput,
    SpectrumResult,
    ThreeSpectrumMixSolution,
    analyze_cct_reference,
    analyze_spectral_text,
    analyze_standard_illuminant,
    analyze_standard_illuminant_chromaticity,
    calculate_power_limited_mix,
    chromaticity_background_image,
    chromaticity_isotherms,
    chromaticity_loci,
    macadam_ellipse_points,
    mix_spectra_by_peak_ratio,
    parse_chromaticity_text,
    solve_three_spectrum_mix,
    spectral_example_text,
)

logger = logging.getLogger(__name__)

CHART_COLORS = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#db2777",
    "#4f46e5",
    "#65a30d",
    "#0f766e",
    "#b45309",
    "#7e22ce",
]
SYMBOL_OPTIONS = {
    "circle": "圆形",
    "rect": "方形",
    "roundRect": "圆角方形",
    "triangle": "三角形",
    "diamond": "菱形",
    "pin": "图钉",
    "arrow": "箭头",
}
GROUP_KEYWORDS = (
    "模式",
    "混合",
    "目标",
    "单色",
    "混光",
    "光源",
    "模组",
    "导光束",
    "光纤",
    "准直",
    "汇聚",
    "焦平面",
    "入",
    "出",
    "镜子",
    "反射",
    "透射",
    "光",
)
SDCM_OPTIONS = {
    1: "1 SDCM",
    3: "3 SDCM",
    5: "5 SDCM",
    7: "7 SDCM",
}


def _sdcm_key(result_kind: str, name: str) -> str:
    """生成不同来源色坐标互不冲突的 SDCM 状态键。"""

    return _chromaticity_result_key(result_kind, name)


def _chromaticity_result_key(result_kind: str, name: str) -> str:
    """生成色度图结果点在交互状态中的唯一键。"""

    return f"{result_kind}:{name}"


def _sdcm_orders(value: Any) -> tuple[int, ...]:
    """把界面单值或多选值规范为有序且不重复的 SDCM 阶数。"""

    raw_values = value if isinstance(value, (list, tuple, set)) else (value,)
    normalized: set[int] = set()
    for raw_value in raw_values:
        try:
            order = int(raw_value)
        except (TypeError, ValueError):
            continue
        if order in SDCM_OPTIONS:
            normalized.add(order)
    return tuple(order for order in SDCM_OPTIONS if order in normalized)


def _sdcm_ellipse_series(
    item: SpectrumResult | ChromaticityResult | SpectrumChromaticityResult,
    result_kind: str,
    coordinate_system: str,
    order: int,
    color: str,
    *,
    visible: bool = True,
) -> dict[str, Any]:
    """生成一条可按稳定 ID 原位更新的 SDCM 椭圆系列。"""

    return {
        "id": f"sdcm:{result_kind}:{item.name}:{order}",
        "name": f"{item.name} · {order} SDCM",
        "type": "line",
        "showSymbol": False,
        "silent": True,
        "z": 4,
        "tooltip": {"show": False},
        "lineStyle": {
            "color": color,
            "width": 2,
            "type": "dashed",
            "opacity": 0.95,
        },
        "endLabel": {
            "show": visible,
            "formatter": f"{order} SDCM",
            "color": color,
            "fontSize": 11,
            "fontWeight": 700,
            "backgroundColor": "rgba(255,255,255,0.88)",
            "borderRadius": 3,
            "padding": [2, 4],
            "distance": 4,
        },
        "labelLayout": {"hideOverlap": False},
        "data": (
            [list(point) for point in macadam_ellipse_points(item.xy, order, coordinate_system)]
            if visible
            else []
        ),
    }


def _coordinate_connection_series(
    source: SpectrumResult | ChromaticityResult | SpectrumChromaticityResult,
    source_kind: str,
    target: SpectrumResult | ChromaticityResult | SpectrumChromaticityResult,
    coordinate_system: str,
    color: str,
    *,
    visible: bool = True,
) -> dict[str, Any]:
    """生成一条从光源坐标点指向目标坐标点的稳定连线系列。"""

    source_point = source.xy if coordinate_system == "xy" else source.upvp
    target_point = target.xy if coordinate_system == "xy" else target.upvp
    return {
        "id": f"coordinate-connection:{_chromaticity_result_key(source_kind, source.name)}",
        "name": f"{source.name} → {target.name}",
        "type": "line",
        "showSymbol": False,
        "silent": True,
        "z": 4,
        "tooltip": {"show": False},
        "lineStyle": {
            "color": color,
            "width": 1.8,
            "type": "solid",
            "opacity": 0.85,
        },
        "data": [list(source_point), list(target_point)] if visible else [],
    }


def _legend_options(names: list[Any] | None = None) -> dict[str, Any]:
    """生成可自动换行且避开右侧工具栏的图例配置。"""

    options: dict[str, Any] = {
        "type": "plain",
        "left": 76,
        "right": 150,
        "top": 8,
        "itemGap": 14,
        "itemWidth": 18,
        "itemHeight": 10,
    }
    if names is not None:
        options["data"] = names
    return options


def _compact_legend_options(names: list[Any]) -> dict[str, Any]:
    """生成单行滚动图例，避免紧凑图表的图例覆盖绘图区。"""

    options = _legend_options(names)
    options.update(
        {
            "type": "scroll",
            "left": 58,
            "right": 116,
            "top": 4,
            "height": 28,
            "pageIconSize": 12,
            "pageButtonItemGap": 6,
        }
    )
    return options


def _side_legend_options(names: list[Any]) -> dict[str, Any]:
    """生成位于 CIE 绘图区右侧的纵向图例。"""

    return {
        "type": "plain",
        "orient": "vertical",
        "left": "76%",
        "right": 4,
        "top": "16%",
        "bottom": "8%",
        "itemGap": 11,
        "itemWidth": 18,
        "itemHeight": 10,
        "tooltip": {"show": True},
        "textStyle": {
            "fontSize": 12,
            "width": 160,
            "overflow": "breakAll",
            "lineHeight": 17,
        },
        "data": names,
    }


def _cie_data_zoom() -> list[dict[str, Any]]:
    """生成同时控制横纵坐标的滚轮缩放与拖拽平移配置。"""

    common = {
        "type": "inside",
        "filterMode": "none",
        "zoomOnMouseWheel": True,
        "moveOnMouseMove": True,
        "moveOnMouseWheel": False,
        "preventDefaultMouseMove": True,
    }
    return [
        {**common, "id": "cie_zoom_x", "xAxisIndex": [0]},
        {**common, "id": "cie_zoom_y", "yAxisIndex": [0]},
    ]


def _axis_split_number(full_range: float, target_interval: float) -> int | None:
    """把全视图目标间隔换算为刻度段数，使缩放后刻度可以自动重算。"""

    if full_range <= 0 or target_interval <= 0:
        return None
    return max(2, min(40, round(full_range / target_interval)))


def _chromaticity_background_series(coordinate_system: str) -> dict[str, Any]:
    """生成与坐标轴精确对齐的连续色度背景图层。"""

    x_max, y_max = (0.8, 0.9) if coordinate_system == "xy" else (0.7, 0.65)
    image_url = chromaticity_background_image(coordinate_system)
    render_item = f"""
        function(params, api) {{
            const topLeft = api.coord([0, {y_max}]);
            const bottomRight = api.coord([{x_max}, 0]);
            return {{
                type: 'image',
                style: {{
                    image: '{image_url}',
                    x: topLeft[0],
                    y: topLeft[1],
                    width: bottomRight[0] - topLeft[0],
                    height: bottomRight[1] - topLeft[1]
                }}
            }};
        }}
    """
    return {
        "name": "色度背景",
        "type": "custom",
        "coordinateSystem": "cartesian2d",
        "silent": True,
        "z": 0,
        "tooltip": {"show": False},
        ":renderItem": render_item,
        "data": [[0, 0]],
    }


def _cie_interaction_setup_js(chart_id: int, coordinate_system: str) -> str:
    """生成像素级十字线与空白区域单击坐标事件的客户端初始化脚本。"""

    first_axis, second_axis = ("x", "y") if coordinate_system == "xy" else ("u′", "v′")
    return f"""
        () => {{
            const component = getElement({chart_id});
            if (!component?.chart || component._cieInteractionBound) return;
            component._cieInteractionBound = true;
            const chart = component.chart;
            const root = component.$el;
            root.style.position = 'relative';

            const createOverlay = (key, cssText) => {{
                const element = document.createElement('div');
                element.dataset.cieOverlay = key;
                element.style.cssText = cssText;
                root.appendChild(element);
                return element;
            }};
            const commonLine = 'position:absolute;display:none;pointer-events:none;z-index:40;'
                + 'background:rgba(71,85,105,0.8);';
            const vertical = createOverlay('vertical', commonLine + 'width:1px;');
            const horizontal = createOverlay('horizontal', commonLine + 'height:1px;');
            const commonLabel = 'position:absolute;display:none;pointer-events:none;z-index:41;'
                + 'padding:4px 7px;border-radius:4px;background:rgba(30,41,59,0.9);'
                + 'color:white;font:600 13px sans-serif;white-space:nowrap;';
            const firstLabel = createOverlay('first-label', commonLabel + 'transform:translateX(-50%);');
            const secondLabel = createOverlay('second-label', commonLabel + 'transform:translateY(-50%);');
            const overlays = [vertical, horizontal, firstLabel, secondLabel];
            const hide = () => overlays.forEach(element => element.style.display = 'none');

            let pendingEvent = null;
            let framePending = false;
            chart.getZr().on('mousemove', event => {{
                pendingEvent = event;
                if (framePending) return;
                framePending = true;
                requestAnimationFrame(() => {{
                    framePending = false;
                    const current = pendingEvent;
                    if (!current) return;
                    const pixel = [current.offsetX, current.offsetY];
                    if (!chart.containPixel({{gridIndex: 0}}, pixel)) {{
                        hide();
                        return;
                    }}
                    const value = chart.convertFromPixel({{gridIndex: 0}}, pixel);
                    const grid = chart.getModel().getComponent('grid', 0).coordinateSystem.getRect();
                    vertical.style.display = 'block';
                    vertical.style.left = `${{pixel[0]}}px`;
                    vertical.style.top = `${{grid.y}}px`;
                    vertical.style.height = `${{grid.height}}px`;
                    horizontal.style.display = 'block';
                    horizontal.style.left = `${{grid.x}}px`;
                    horizontal.style.top = `${{pixel[1]}}px`;
                    horizontal.style.width = `${{grid.width}}px`;
                    firstLabel.style.display = 'block';
                    firstLabel.style.left = `${{pixel[0]}}px`;
                    firstLabel.style.top = `${{grid.y + grid.height - 28}}px`;
                    firstLabel.textContent = '{first_axis}: ' + Number(value[0]).toFixed(6);
                    secondLabel.style.display = 'block';
                    secondLabel.style.left = `${{grid.x + 5}}px`;
                    secondLabel.style.top = `${{pixel[1]}}px`;
                    secondLabel.textContent = '{second_axis}: ' + Number(value[1]).toFixed(6);
                }});
            }});
            chart.getZr().on('globalout', hide);
            chart.getZr().on('click', event => {{
                const pixel = [event.offsetX, event.offsetY];
                if (!chart.containPixel({{gridIndex: 0}}, pixel)) return;
                const value = chart.convertFromPixel({{gridIndex: 0}}, pixel);
                emit({{first: Number(value[0]), second: Number(value[1])}});
            }});
        }}
    """


def _cie_clicked_point_series(
    first: float,
    second: float,
    coordinate_system: str,
) -> dict[str, Any]:
    """生成单击坐标标记，并在接近普朗克轨迹时附加 CCT。"""

    first_axis, second_axis = ("x", "y") if coordinate_system == "xy" else ("u′", "v′")
    label_lines = [f"{first_axis}: {first:.6f}", f"{second_axis}: {second:.6f}"]
    try:
        result = parse_chromaticity_text(
            f"单击坐标\t{first:.12g}\t{second:.12g}",
            coordinate_system,
        )[0]
    except SpectralAnalysisError:
        result = None
    if (
        result is not None
        and result.cct is not None
        and result.duv is not None
        and abs(result.duv) <= 0.05
    ):
        label_lines.append(f"CCT: {result.cct:.0f} K")
    return {
        "id": "cie-click-marker",
        "name": "单击坐标",
        "type": "scatter",
        "symbol": "pin",
        "symbolSize": 24,
        "silent": True,
        "z": 50,
        "itemStyle": {
            "color": "#ef4444",
            "borderColor": "#ffffff",
            "borderWidth": 1.5,
        },
        "label": {
            "show": True,
            "formatter": "\n".join(label_lines),
            "position": "top",
            "distance": 8,
            "color": "#1e293b",
            "fontSize": 12,
            "fontWeight": 600,
            "lineHeight": 18,
            "backgroundColor": "rgba(255,255,255,0.94)",
            "borderColor": "#cbd5e1",
            "borderWidth": 1,
            "borderRadius": 5,
            "padding": [5, 7],
        },
        "data": [[first, second]],
    }


def _render_cie_chart(options: dict[str, Any], viewport_offset: int = 245) -> Any:
    """以正方形容器渲染支持缩放、平移和智能十字指示的 CIE 图。"""

    chart = (
        ui.echart(options)
        .classes("mx-auto")
        .style(
            f"width: min(100%, calc(100vh - {viewport_offset}px)); "
            "aspect-ratio: 1 / 1; min-width: 680px; min-height: 680px; cursor: grab;"
        )
    )
    coordinate_system = "xy" if options.get("xAxis", {}).get("name") == "x" else "upvp"
    _bind_cie_pointer_events(chart, coordinate_system)
    return chart


def _bind_cie_pointer_events(chart: Any, coordinate_system: str) -> None:
    """绑定像素级十字线，以及单击坐标的 CCT 计算和固定标记。"""

    async def handle_chart_click(event: Any) -> None:
        raw = getattr(event, "args", None)
        if not isinstance(raw, dict):
            return
        first_raw = raw.get("first")
        second_raw = raw.get("second")
        if not isinstance(first_raw, (str, int, float)) or not isinstance(
            second_raw,
            (str, int, float),
        ):
            return
        try:
            first = float(first_raw)
            second = float(second_raw)
        except (TypeError, ValueError):
            return
        if not math.isfinite(first) or not math.isfinite(second):
            return
        marker_series = await run.cpu_bound(
            _cie_clicked_point_series,
            first,
            second,
            coordinate_system,
        )
        chart.run_chart_method(
            "setOption",
            {"series": [marker_series]},
            {"notMerge": False, "lazyUpdate": False},
        )

    chart.on(
        "chart:finished",
        handler=handle_chart_click,
        js_handler=_cie_interaction_setup_js(chart.id, coordinate_system),
    )


def _render_fitted_cie_chart(options: dict[str, Any]) -> None:
    """在现有卡片剩余空间内渲染自适应 CIE 图。"""

    with ui.element("div").classes("w-full flex-1 min-h-0 flex items-center justify-center"):
        chart = (
            ui.echart(options)
            .classes("h-full max-w-full max-h-full")
            .style("width: auto; aspect-ratio: 1 / 1; cursor: grab;")
        )
        coordinate_system = "xy" if options.get("xAxis", {}).get("name") == "x" else "upvp"
        _bind_cie_pointer_events(chart, coordinate_system)


def _chromaticity_tooltip(coordinate_system: str, include_cri: bool = False) -> str:
    """生成明确标注坐标轴与显色指数的悬停提示。"""

    first_axis, second_axis = ("x", "y") if coordinate_system == "xy" else ("u′", "v′")
    cri_line = (
        """
        if (data.ri !== undefined && data.ri !== null) {
            html += `<br/>${data.sample}: ${Number(data.ri).toFixed(2)}`;
        }
    """
        if include_cri
        else ""
    )
    return f"""
        function(params) {{
            const items = Array.isArray(params) ? params : [params];
            const pointItems = items.filter(item =>
                item && item.seriesType === 'scatter' && item.data && item.data.value
            );
            if (pointItems.length === 0) return '';
            return pointItems.map(param => {{
                const data = param.data || {{}};
                const value = data.value || param.value || [];
                const title = data.title || data.name || param.name || param.seriesName;
                let html = `<b>${{title}}</b>`;
                html += `<br/>{first_axis}: ${{Number(value[0]).toFixed(6)}}`;
                html += `<br/>{second_axis}: ${{Number(value[1]).toFixed(6)}}`;
                {cri_line}
                return html;
            }}).join('<br/>');
        }}
    """


def _cie_tooltip_options(coordinate_system: str, include_cri: bool = False) -> dict[str, Any]:
    """生成数据点悬停详情；连续十字线由独立客户端覆盖层负责。"""

    return {
        "trigger": "item",
        "triggerOn": "mousemove",
        "confine": True,
        "transitionDuration": 0,
        ":formatter": _chromaticity_tooltip(coordinate_system, include_cri),
    }


def _cie_split_line() -> dict[str, Any]:
    """生成层级稳定且不抢占色度背景的浅色坐标网格线。"""

    return {
        "show": True,
        "lineStyle": {
            "color": "rgba(71, 85, 105, 0.16)",
            "width": 1,
            "type": "solid",
        },
    }


def _cie_axis_text_options() -> dict[str, Any]:
    """统一放大 CIE 坐标轴名称和刻度字体。"""

    return {
        "axisLabel": {
            "fontSize": 15,
            "fontWeight": 500,
            "color": "#334155",
            "margin": 10,
        },
        "nameTextStyle": {
            "fontSize": 18,
            "fontWeight": 600,
            "color": "#1e293b",
        },
    }


def _option_text(value: object, allowed: set[str] | None = None, default: str = "") -> str:
    """把 NiceGUI 选项值收窄为可用于业务逻辑的字符串。"""

    text = str(value or "").strip()
    if allowed is not None and text not in allowed:
        return default
    return text


def _spectrum_group_key(name: str) -> str:
    """从光谱标题提取稳定分组关键字。"""

    source_name = str(name or "")
    upper_name = source_name.upper()
    configured_keywords = sorted(
        (str(keyword).strip() for keyword in GROUP_KEYWORDS if str(keyword).strip()),
        key=len,
        reverse=True,
    )
    for keyword in configured_keywords:
        if keyword.upper() in upper_name:
            return keyword.upper()

    simplified = re.sub(r"模式|光源|光谱|样品|测试|方案|通道|数据", " ", source_name)
    tokens = re.findall(r"[A-Za-z]+|[\u4e00-\u9fff]{2,}", simplified)
    if tokens:
        return tokens[0].upper()
    return (source_name or "未分组").upper()


def _default_series_styles(
    results: Sequence[SpectrumResult | SpectrumChromaticityResult],
) -> dict[str, dict[str, str]]:
    """按标题关键字为同组光谱分配一致图标形状。"""

    group_symbols: dict[str, str] = {}
    symbols = list(SYMBOL_OPTIONS)
    styles: dict[str, dict[str, str]] = {}
    for index, item in enumerate(results):
        group = _spectrum_group_key(item.name)
        if group not in group_symbols:
            group_symbols[group] = symbols[len(group_symbols) % len(symbols)]
        styles[item.name] = {
            "group": group,
            "symbol": group_symbols[group],
            "color": CHART_COLORS[index % len(CHART_COLORS)],
        }
    return styles


def _series_style(
    name: str,
    styles: dict[str, dict[str, str]] | None,
    fallback_index: int,
) -> tuple[str, str]:
    """读取并校验某条光谱的图标形状和颜色。"""

    configured = (styles or {}).get(name, {})
    default_symbol = list(SYMBOL_OPTIONS)[fallback_index % len(SYMBOL_OPTIONS)]
    symbol = _option_text(configured.get("symbol"), set(SYMBOL_OPTIONS), default_symbol)
    color = str(configured.get("color") or CHART_COLORS[fallback_index % len(CHART_COLORS)])
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        color = CHART_COLORS[fallback_index % len(CHART_COLORS)]
    return symbol, color


def _metric(value: float | None, digits: int = 3) -> str:
    """格式化可能缺失的计算指标。"""

    return "—" if value is None else f"{value:.{digits}f}"


def _nonnegative_number(value: object, default: float) -> float:
    """把坐标轴设置收窄为有限非负数。"""

    if not isinstance(value, (str, int, float)):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) and number >= 0 else default


MixingSpectrumResult = SpectrumResult | SpectrumChromaticityResult


def _spectrum_input_from_result(result: MixingSpectrumResult) -> SpectrumInput:
    """把原始或中间混合结果转换为可继续参与混合的光谱输入。"""

    return SpectrumInput(result.name, result.wavelengths, result.values)


def _mixing_nodes_and_active_ids(
    source_results: list[SpectrumResult],
    steps: list[dict[str, Any]],
) -> tuple[dict[str, MixingSpectrumResult], list[str]]:
    """按组合步骤重建多层混光节点，并返回当前未被消耗的节点。"""

    nodes, active_ids, _ = _mixing_graph_details(source_results, steps)
    return nodes, active_ids


def _mixing_graph_details(
    source_results: list[SpectrumResult],
    steps: list[dict[str, Any]],
) -> tuple[
    dict[str, MixingSpectrumResult],
    list[str],
    dict[str, tuple[float, ...]],
]:
    """重建混光节点，并跟踪每个节点对原始峰值归一化光谱的贡献。"""

    nodes: dict[str, MixingSpectrumResult] = {f"source:{index}": result for index, result in enumerate(source_results)}
    active_ids = list(nodes)
    source_count = len(source_results)
    source_coefficients: dict[str, tuple[float, ...]] = {
        f"source:{index}": tuple(1.0 if index == source_index else 0.0 for source_index in range(source_count))
        for index in range(source_count)
    }
    for index, step in enumerate(steps, start=1):
        step_id = str(step.get("id") or f"mix:{index}")
        first_id = str(step.get("first_id") or "")
        second_id = str(step.get("second_id") or "")
        if first_id == second_id or first_id not in active_ids or second_id not in active_ids:
            raise SpectralAnalysisError(f"第 {index} 个混合步骤引用了无效或已消耗的光谱")
        ratio_value = step.get("ratio", 50.0)
        ratio_percent = _nonnegative_number(ratio_value, 50.0)
        if ratio_percent > 100:
            raise SpectralAnalysisError("峰值比例必须位于 0–100%")
        first = nodes[first_id]
        second = nodes[second_id]
        mixed = mix_spectra_by_peak_ratio(
            _spectrum_input_from_result(first),
            _spectrum_input_from_result(second),
            ratio_percent / 100,
            name=str(step.get("name") or f"第 {index} 层混合"),
        )
        nodes[step_id] = mixed
        first_coefficients = source_coefficients[first_id]
        second_coefficients = source_coefficients[second_id]
        first_ratio = ratio_percent / 100
        normalization_factor = mixed.normalization_factor
        if normalization_factor <= 0:
            raise SpectralAnalysisError(f"第 {index} 个混合步骤无法得到有效的归一化系数")
        source_coefficients[step_id] = tuple(
            (first_ratio * first_coefficient + (1 - first_ratio) * second_coefficient) / normalization_factor
            for first_coefficient, second_coefficient in zip(
                first_coefficients,
                second_coefficients,
            )
        )
        active_ids.remove(first_id)
        active_ids.remove(second_id)
        active_ids.append(step_id)
    return nodes, active_ids, source_coefficients


def _mixing_node_options(
    nodes: dict[str, MixingSpectrumResult],
    active_ids: list[str],
) -> dict[str, str]:
    """生成当前仍可参与下一层组合的光谱选项。"""

    return {
        node_id: (f"原始 · {nodes[node_id].name}" if node_id.startswith("source:") else nodes[node_id].name)
        for node_id in active_ids
    }


def _spectrum_summary_rows(results: list[SpectrumResult]) -> list[dict[str, Any]]:
    """生成综合指标表数据。"""

    return [
        {
            "name": item.name,
            "peak_wavelength": f"{item.peak_wavelength:.1f}",
            "dominant_wavelength": _dominant_wavelength_metric(item),
            "cct": "—" if item.cct is None else f"{item.cct:.0f}",
            "duv": _metric(item.duv, 6),
            "x": _metric(item.xy[0], 5),
            "y": _metric(item.xy[1], 5),
            "up": _metric(item.upvp[0], 5),
            "vp": _metric(item.upvp[1], 5),
            "ra": _metric(item.ra, 2),
            "r9": _metric(dict(item.ri).get(9), 2),
            "r15": _metric(dict(item.ri).get(15), 2),
            "rf": _metric(item.rf, 2),
            "dc": _metric(item.cri_reference_distance, 6),
        }
        for item in results
    ]


def _dominant_wavelength_metric(result: MixingSpectrumResult) -> str:
    """格式化主波长；紫边方向明确标注为补色波长。"""

    if result.dominant_wavelength is not None:
        return f"{result.dominant_wavelength:.1f} nm"
    if result.complementary_wavelength is not None:
        return f"—（补色 {result.complementary_wavelength:.1f} nm）"
    return "—"


def _spectrum_chart_options(
    results: Sequence[MixingSpectrumResult],
    normalized: bool = True,
    reference_result: SpectrumResult | None = None,
    series_styles: dict[str, dict[str, str]] | None = None,
    x_axis_interval: float = 20.0,
    y_axis_interval: float = 0.0,
    hidden_series_names: set[str] | None = None,
    compact_layout: bool = False,
) -> dict[str, Any]:
    """生成多光谱叠加折线图配置。"""

    series = []
    plot_results = [*results, *([reference_result] if reference_result is not None else [])]
    for index, item in enumerate(plot_results):
        plot_values = item.normalized_values if normalized else item.values
        is_reference = reference_result is not None and item is reference_result
        symbol, color = _series_style(item.name, series_styles, index)
        if is_reference:
            symbol, color = "diamond", "#111827"
        series.append(
            {
                "name": item.name,
                "type": "line",
                "symbol": symbol,
                "showSymbol": False,
                "smooth": False,
                "lineStyle": {
                    "width": 3 if is_reference else 2,
                    "type": "dashed" if is_reference else "solid",
                    "color": color,
                },
                "itemStyle": {"color": color},
                "data": [
                    [wavelength, value]
                    for wavelength, value in zip(item.wavelengths, plot_values)
                    if 380 <= wavelength <= 780
                ],
            }
        )
    visible_y_max = max(
        (point[1] for item in series for point in item["data"]),
        default=1.0,
    )
    x_split_number = _axis_split_number(780 - 380, x_axis_interval)
    y_split_number = _axis_split_number(visible_y_max, y_axis_interval)
    legend = _legend_options(
        [
            {
                "name": item.name,
                "icon": (
                    "diamond"
                    if reference_result is not None and item is reference_result
                    else _series_style(item.name, series_styles, index)[0]
                ),
            }
            for index, item in enumerate(plot_results)
        ]
    )
    if compact_layout:
        legend = _compact_legend_options(legend["data"])
    if hidden_series_names:
        legend["selected"] = {item.name: item.name not in hidden_series_names for item in plot_results}
    return {
        "animation": False,
        "color": CHART_COLORS,
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": legend,
        "grid": {
            "left": 88,
            "right": 40,
            "top": 45 if compact_layout else 110,
            "bottom": 108,
        },
        "toolbox": {
            "right": 10,
            "top": 5,
            "feature": {"saveAsImage": {}, "dataZoom": {}, "restore": {}},
        },
        "xAxis": {
            "type": "value",
            "name": "波长 (nm)",
            "nameLocation": "middle",
            "nameGap": 42,
            "min": 380,
            "max": 780,
            **({"splitNumber": x_split_number} if x_split_number is not None else {}),
        },
        "yAxis": {
            "type": "value",
            "name": "相对强度" if normalized else "输入值",
            "nameLocation": "middle",
            "nameGap": 55,
            "nameRotate": 90,
            "min": 0,
            **({"splitNumber": y_split_number} if y_split_number is not None else {}),
        },
        "dataZoom": [
            {"type": "inside", "filterMode": "none"},
            {"type": "slider", "height": 20, "bottom": 12, "filterMode": "none"},
        ],
        "series": series,
    }


def _cri_value_rows(results: list[SpectrumResult]) -> list[dict[str, Any]]:
    """生成 Ra、R1–R15 与 CIE Rf 的直接数值表。"""

    rows: list[dict[str, Any]] = []
    for item in results:
        ri = dict(item.ri)
        rows.append(
            {
                "name": item.name,
                "ra": _metric(item.ra, 2),
                **{f"r{sample}": _metric(ri.get(sample), 2) for sample in range(1, 16)},
                "rf": _metric(item.rf, 2),
            }
        )
    return rows


def _comparison_source_options(results: list[SpectrumResult]) -> dict[str, str]:
    """合并用户输入光谱和内置标准光源选项。"""

    options = {f"input:{index}": f"输入光谱 · {item.name}" for index, item in enumerate(results)}
    options.update({f"standard:{key}": f"内置标准 · {label}" for key, label in STANDARD_ILLUMINANTS.items()})
    options.update(
        {
            f"reference:{index}": (f"等色温标准 · 按 {item.name} 的 CCT（{item.cct:.0f} K）")
            for index, item in enumerate(results)
            if item.cct is not None and 1000 <= item.cct <= 25000
        }
    )
    return options


def _spectrum_reference_options(results: list[SpectrumResult]) -> dict[str, str]:
    """生成光谱曲线可叠加的等色温标准源选项。"""

    return {
        "none": "不显示等色温标准源",
        **{
            f"reference:{index}": f"按 {item.name} 的 CCT 匹配（{item.cct:.0f} K）"
            for index, item in enumerate(results)
            if item.cct is not None and 1000 <= item.cct <= 25000
        },
    }


def _isotherm_series(coordinate_system: str) -> list[dict[str, Any]]:
    """生成带温度标注且不占用图例空间的等色温线系列。"""

    series: list[dict[str, Any]] = []
    for cct, start, end in chromaticity_isotherms(coordinate_system):
        series.append(
            {
                "name": f"{cct} K 等色温线",
                "type": "line",
                "showSymbol": False,
                "silent": True,
                "z": 3,
                "tooltip": {"show": False},
                "lineStyle": {
                    "color": "#64748b",
                    "width": 1.2,
                    "type": "dashed",
                    "opacity": 0.9,
                },
                "endLabel": {
                    "show": True,
                    "formatter": f"{cct} K",
                    "color": "#475569",
                    "fontSize": 10,
                    "fontWeight": 600,
                    "distance": 4,
                },
                "labelLayout": {
                    "moveOverlap": "shiftY",
                    "hideOverlap": False,
                },
                "data": [list(start), list(end)],
            }
        )
    return series


def _chromaticity_chart_options(
    spectrum_results: Sequence[SpectrumResult] | None = None,
    coordinate_results: Sequence[ChromaticityResult | SpectrumChromaticityResult] | None = None,
    standard_illuminant_results: Sequence[ChromaticityResult] | None = None,
    *,
    coordinate_system: str = "xy",
    series_styles: dict[str, dict[str, str]] | None = None,
    sdcm_orders: Mapping[str, Sequence[int] | int] | None = None,
    connection_target: str | None = None,
    connection_sources: Sequence[str] | None = None,
    axis_interval: float = 0.1,
    show_isotherms: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    """生成带颜色背景的轨迹、光谱点、标准光源与手工坐标联合色度图。"""

    spectrum_results = spectrum_results or []
    coordinate_results = coordinate_results or []
    standard_illuminant_results = standard_illuminant_results or []
    sdcm_orders = sdcm_orders or {}
    connection_sources = connection_sources or []
    spectral_locus, planckian_locus = chromaticity_loci(coordinate_system)
    is_xy = coordinate_system == "xy"
    series: list[dict[str, Any]] = [
        _chromaticity_background_series(coordinate_system),
        {
            "name": "光谱轨迹",
            "type": "line",
            "showSymbol": False,
            "silent": True,
            "z": 2,
            "lineStyle": {"color": "#475569", "width": 2},
            "data": [list(point) for point in spectral_locus],
        },
        {
            "name": "普朗克轨迹",
            "type": "line",
            "showSymbol": False,
            "silent": True,
            "z": 4,
            "lineStyle": {"color": "#111827", "width": 2.5, "type": "solid"},
            "data": [list(point) for point in planckian_locus],
        },
    ]
    if show_isotherms:
        series.extend(_isotherm_series(coordinate_system))
    all_results: list[tuple[SpectrumResult | ChromaticityResult | SpectrumChromaticityResult, str]] = [
        *((item, "spectrum") for item in spectrum_results),
        *(
            (
                item,
                "spectrum" if isinstance(item, SpectrumChromaticityResult) else "coordinate",
            )
            for item in coordinate_results
        ),
        *((item, "standard") for item in standard_illuminant_results),
    ]
    keyed_results = {
        _chromaticity_result_key(result_kind, item.name): (item, result_kind, index)
        for index, (item, result_kind) in enumerate(all_results)
    }
    target_entry = keyed_results.get(connection_target or "")
    if target_entry is not None:
        target_item = target_entry[0]
        for source_key in connection_sources:
            source_entry = keyed_results.get(str(source_key))
            if source_entry is None or str(source_key) == connection_target:
                continue
            source_item, source_kind, source_index = source_entry
            _, source_color = _series_style(source_item.name, series_styles, source_index)
            series.append(
                _coordinate_connection_series(
                    source_item,
                    source_kind,
                    target_item,
                    coordinate_system,
                    source_color,
                )
            )
    for index, (item, result_kind) in enumerate(all_results):
        point = item.xy if is_xy else item.upvp
        symbol, color = _series_style(item.name, series_styles, index)
        if result_kind == "coordinate":
            symbol = "triangle"
        elif result_kind == "standard":
            symbol = "diamond"
        for sdcm in _sdcm_orders(sdcm_orders.get(_sdcm_key(result_kind, item.name), ())):
            series.append(
                _sdcm_ellipse_series(
                    item,
                    result_kind,
                    coordinate_system,
                    sdcm,
                    color,
                )
            )
        series.append(
            {
                "name": item.name,
                "type": "scatter",
                "symbol": symbol,
                "symbolSize": 8,
                "z": 5,
                "itemStyle": {
                    "color": color,
                    "borderColor": "#ffffff",
                    "borderWidth": 1,
                },
                "label": {"show": False},
                "data": [
                    {
                        "name": item.name,
                        "title": item.name,
                        "value": [point[0], point[1]],
                    }
                ],
            }
        )
    axis_max = 0.9 if is_xy else 0.7
    split_number = _axis_split_number(axis_max, axis_interval)
    legend_items: list[Any] = [
        "光谱轨迹",
        "普朗克轨迹",
        *[
            {
                "name": item.name,
                "icon": (
                    "triangle"
                    if result_kind == "coordinate"
                    else ("diamond" if result_kind == "standard" else _series_style(item.name, series_styles, index)[0])
                ),
            }
            for index, (item, result_kind) in enumerate(all_results)
        ],
    ]
    return {
        "animation": False,
        "tooltip": _cie_tooltip_options(coordinate_system),
        "legend": (_side_legend_options(legend_items) if compact else _legend_options(legend_items)),
        "grid": (
            {"left": "5%", "top": "15%", "width": "70%", "height": "70%"}
            if compact
            else {"left": 100, "right": 100, "top": 100, "bottom": 100}
        ),
        "toolbox": {
            "right": 10,
            "top": 5,
            "feature": {"saveAsImage": {}, "restore": {}},
        },
        "dataZoom": _cie_data_zoom(),
        "xAxis": {
            "z": 1,
            "type": "value",
            "name": "x" if is_xy else "u′",
            "nameLocation": "middle",
            "nameGap": 32,
            "min": 0,
            "max": axis_max,
            "splitLine": _cie_split_line(),
            **_cie_axis_text_options(),
            **({"splitNumber": split_number} if split_number is not None else {}),
        },
        "yAxis": {
            "z": 1,
            "type": "value",
            "name": "y" if is_xy else "v′",
            "min": 0,
            "max": axis_max,
            "splitLine": _cie_split_line(),
            **_cie_axis_text_options(),
            **({"splitNumber": split_number} if split_number is not None else {}),
        },
        "series": series,
    }


def _cri_pair_chromaticity_chart_options(
    first_result: SpectrumResult,
    second_result: SpectrumResult,
    *,
    coordinate_system: str = "xy",
    series_styles: dict[str, dict[str, str]] | None = None,
    axis_interval: float = 0.1,
    show_isotherms: bool = False,
) -> dict[str, Any]:
    """生成任意两个光源的白点及 R1–R15 实际色样坐标对比图。"""

    is_xy = coordinate_system == "xy"
    spectral_locus, planckian_locus = chromaticity_loci(coordinate_system)
    series: list[dict[str, Any]] = [
        _chromaticity_background_series(coordinate_system),
        {
            "name": "光谱轨迹",
            "type": "line",
            "showSymbol": False,
            "silent": True,
            "z": 2,
            "lineStyle": {"color": "#475569", "width": 2},
            "data": [list(point) for point in spectral_locus],
        },
        {
            "name": "普朗克轨迹",
            "type": "line",
            "showSymbol": False,
            "silent": True,
            "z": 4,
            "lineStyle": {"color": "#111827", "width": 2.5, "type": "solid"},
            "data": [list(point) for point in planckian_locus],
        },
    ]
    if show_isotherms:
        series.extend(_isotherm_series(coordinate_system))
    first_samples = {item.index: item for item in first_result.cri_samples}
    second_samples = {item.index: item for item in second_result.cri_samples}
    for index in sorted(set(first_samples) & set(second_samples)):
        first_item = first_samples[index]
        second_item = second_samples[index]
        first_point = first_item.test_xy if is_xy else first_item.test_upvp
        second_point = second_item.test_xy if is_xy else second_item.test_upvp
        series.append(
            {
                "name": f"R{index} 对应关系",
                "type": "line",
                "showSymbol": False,
                "silent": True,
                "z": 4,
                "tooltip": {"show": False},
                "lineStyle": {
                    "color": CHART_COLORS[(index - 1) % len(CHART_COLORS)],
                    "width": 1.2,
                    "opacity": 0.55,
                },
                "data": [list(first_point), list(second_point)],
            }
        )

    for result_index, result in enumerate((first_result, second_result)):
        symbol, color = _series_style(result.name, series_styles, result_index)
        white_point = result.xy if is_xy else result.upvp
        data: list[dict[str, Any]] = [
            {
                "name": "光源白点",
                "title": f"{result.name} · 光源白点",
                "value": [white_point[0], white_point[1]],
                "symbol": "diamond",
                "symbolSize": 14,
            }
        ]
        for item in result.cri_samples:
            point = item.test_xy if is_xy else item.test_upvp
            data.append(
                {
                    "name": f"R{item.index}",
                    "title": f"{result.name} · R{item.index}",
                    "sample": f"R{item.index}",
                    "ri": item.score,
                    "value": [point[0], point[1]],
                    "symbol": symbol,
                    "symbolSize": 8,
                }
            )
        series.append(
            {
                "name": result.name,
                "type": "scatter",
                "z": 6,
                "label": {"show": False},
                "itemStyle": {"color": color, "borderColor": "#ffffff", "borderWidth": 1.5},
                "data": data,
            }
        )
    axis_max = 0.9 if is_xy else 0.7
    split_number = _axis_split_number(axis_max, axis_interval)
    return {
        "animation": False,
        "tooltip": _cie_tooltip_options(coordinate_system, include_cri=True),
        "legend": _legend_options(
            [
                "光谱轨迹",
                "普朗克轨迹",
                {
                    "name": first_result.name,
                    "icon": _series_style(first_result.name, series_styles, 0)[0],
                },
                {
                    "name": second_result.name,
                    "icon": _series_style(second_result.name, series_styles, 1)[0],
                },
            ]
        ),
        "grid": {"left": 125, "right": 125, "top": 125, "bottom": 125},
        "toolbox": {
            "right": 10,
            "top": 5,
            "feature": {"saveAsImage": {}, "restore": {}},
        },
        "dataZoom": _cie_data_zoom(),
        "xAxis": {
            "z": 1,
            "type": "value",
            "name": "x" if is_xy else "u′",
            "nameLocation": "middle",
            "nameGap": 32,
            "min": 0,
            "max": axis_max,
            "splitLine": _cie_split_line(),
            **_cie_axis_text_options(),
            **({"splitNumber": split_number} if split_number is not None else {}),
        },
        "yAxis": {
            "z": 1,
            "type": "value",
            "name": "y" if is_xy else "v′",
            "min": 0,
            "max": axis_max,
            "splitLine": _cie_split_line(),
            **_cie_axis_text_options(),
            **({"splitNumber": split_number} if split_number is not None else {}),
        },
        "series": series,
    }


def _coordinate_summary_rows(results: list[ChromaticityResult]) -> list[dict[str, Any]]:
    """生成色坐标转换结果表。"""

    return [
        {
            "name": item.name,
            "X": _metric(item.XYZ[0], 4),
            "Y": _metric(item.XYZ[1], 4),
            "Z": _metric(item.XYZ[2], 4),
            "x": _metric(item.xy[0], 6),
            "y": _metric(item.xy[1], 6),
            "u": _metric(item.uv[0], 6),
            "v": _metric(item.uv[1], 6),
            "up": _metric(item.upvp[0], 6),
            "vp": _metric(item.upvp[1], 6),
            "cct": "—" if item.cct is None else f"{item.cct:.0f}",
            "duv": _metric(item.duv, 6),
        }
        for item in results
    ]


class SpectralAnalyzerTool:
    """在全屏弹窗中提供光谱分析和色坐标对比。"""

    def __init__(self) -> None:
        self.spectral_state: dict[str, Any] = {
            "data_text": "",
            "normalized": True,
            "reference_source": "none",
        }
        self.coordinate_state: dict[str, Any] = {
            "data_text": "",
            "system": "xy",
            "standard_illuminants": [],
            "show_isotherms": False,
        }
        self.cri_state: dict[str, Any] = {
            "source_a": "",
            "source_b": "standard:D65",
        }
        self.mixing_state: dict[str, Any] = {
            "source_a": "",
            "source_b": "",
            "target_x": 0.3127,
            "target_y": 0.3290,
        }
        self.chart_state: dict[str, Any] = {
            "spectrum_x_interval": 20.0,
            "spectrum_y_interval": 0.0,
            "xy_interval": 0.1,
            "upvp_interval": 0.1,
        }
        self.coordinate_connection_state: dict[str, Any] = {
            "target": "",
            "sources": [],
        }
        self.series_styles: dict[str, dict[str, str]] = {}
        self.sdcm_orders: dict[str, list[int]] = {}
        self.spectrum_results: list[SpectrumResult] = []
        self.coordinate_results: list[ChromaticityResult] = []
        self.standard_illuminant_results: list[ChromaticityResult] = []
        self.cri_comparison_results: tuple[SpectrumResult, SpectrumResult] | None = None
        self.spectrum_reference_result: SpectrumResult | None = None
        self.mixing_steps: list[dict[str, Any]] = []
        self.mixing_solution: ThreeSpectrumMixSolution | None = None
        self.mixing_power_limits: dict[str, float] = {}
        self.mixing_power_result: PowerLimitedMixResult | None = None

    def show(self, dialog: ui.dialog) -> None:
        """渲染光谱分析工具界面。"""

        if self.spectrum_results and not self.series_styles:
            self.series_styles = _default_series_styles(self.spectrum_results)
        mixing_step_labels: dict[str, tuple[Any, Any, Any, Any]] = {}
        mixing_solve_task: asyncio.Task[None] | None = None
        mixing_solve_generation = 0
        mixing_solve_pending = False
        mixing_solve_error = ""
        cie_charts: dict[str, Any] = {}
        active_analysis_cie_system = "xy"

        @ui.refreshable
        def render_spectrum_chart() -> None:
            normalized = bool(self.spectral_state.get("normalized", True))
            ui.echart(
                _spectrum_chart_options(
                    self.spectrum_results,
                    normalized,
                    self.spectrum_reference_result,
                    self.series_styles,
                    _nonnegative_number(self.chart_state.get("spectrum_x_interval"), 20.0),
                    _nonnegative_number(self.chart_state.get("spectrum_y_interval"), 0.0),
                )
            ).classes("w-full h-[calc(100vh-545px)] min-h-[480px]")

        def analysis_chromaticity_options(coordinate_system: str) -> dict[str, Any]:
            """按当前分析状态生成一张色度图配置。"""

            interval_key = "xy_interval" if coordinate_system == "xy" else "upvp_interval"
            return _chromaticity_chart_options(
                spectrum_results=self.spectrum_results,
                coordinate_results=self.coordinate_results,
                standard_illuminant_results=self.standard_illuminant_results,
                coordinate_system=coordinate_system,
                series_styles=self.series_styles,
                sdcm_orders=self.sdcm_orders,
                connection_target=str(self.coordinate_connection_state.get("target") or ""),
                connection_sources=(
                    self.coordinate_connection_state.get("sources")
                    if isinstance(self.coordinate_connection_state.get("sources"), list)
                    else []
                ),
                axis_interval=_nonnegative_number(self.chart_state.get(interval_key), 0.1),
                show_isotherms=bool(self.coordinate_state.get("show_isotherms", False)),
            )

        def analysis_chromaticity_items() -> list[
            tuple[SpectrumResult | ChromaticityResult, str]
        ]:
            """返回分析色度图中全部结果点及其来源类型。"""

            return [
                *((item, "spectrum") for item in self.spectrum_results),
                *((item, "coordinate") for item in self.coordinate_results),
                *((item, "standard") for item in self.standard_illuminant_results),
            ]

        def update_sdcm_chart_series(coordinate_system: str | None = None) -> None:
            """只合并更新椭圆系列，保留 ECharts 当前缩放和平移状态。"""

            items = analysis_chromaticity_items()
            systems = (coordinate_system or active_analysis_cie_system,)
            for current_system in systems:
                chart = cie_charts.get(current_system)
                if chart is None:
                    continue
                ellipse_series: list[dict[str, Any]] = []
                for index, (item, result_kind) in enumerate(items):
                    _, color = _series_style(item.name, self.series_styles, index)
                    selected = set(
                        _sdcm_orders(
                            self.sdcm_orders.get(_sdcm_key(result_kind, item.name), ())
                        )
                    )
                    ellipse_series.extend(
                        _sdcm_ellipse_series(
                            item,
                            result_kind,
                            current_system,
                            order,
                            color,
                            visible=order in selected,
                        )
                        for order in SDCM_OPTIONS
                    )
                chart.run_chart_method(
                    "setOption",
                    {"series": ellipse_series},
                    {"notMerge": False, "lazyUpdate": False},
                )

        def update_sdcm_orders(event: Any, key: str) -> None:
            """保存单个色坐标的多选 SDCM 阶数并原位更新椭圆。"""

            self.sdcm_orders[key] = list(_sdcm_orders(getattr(event, "value", ())))
            update_sdcm_chart_series()

        def connection_point_options() -> dict[str, str]:
            """生成目标点与光源点选择器共用的坐标点选项。"""

            source_labels = {
                "spectrum": "光谱",
                "coordinate": "手工坐标",
                "standard": "标准光源",
            }
            return {
                _chromaticity_result_key(result_kind, item.name): (
                    f"{source_labels[result_kind]} · {item.name}"
                )
                for item, result_kind in analysis_chromaticity_items()
            }

        def normalized_connection_sources(allowed_keys: set[str], target_key: str) -> list[str]:
            """过滤无效、重复以及与目标点相同的光源点键。"""

            raw_sources = self.coordinate_connection_state.get("sources", [])
            if not isinstance(raw_sources, (list, tuple, set)):
                return []
            return list(
                dict.fromkeys(
                    str(key)
                    for key in raw_sources
                    if str(key) in allowed_keys and str(key) != target_key
                )
            )

        def update_coordinate_connection_chart_series(
            coordinate_system: str | None = None,
        ) -> None:
            """只更新坐标连线系列，保留 CIE 图当前视口。"""

            items = analysis_chromaticity_items()
            keyed_items = {
                _chromaticity_result_key(result_kind, item.name): (item, result_kind, index)
                for index, (item, result_kind) in enumerate(items)
            }
            target_key = str(self.coordinate_connection_state.get("target") or "")
            target_entry = keyed_items.get(target_key)
            selected_sources = set(normalized_connection_sources(set(keyed_items), target_key))
            systems = (coordinate_system or active_analysis_cie_system,)
            for current_system in systems:
                chart = cie_charts.get(current_system)
                if chart is None:
                    continue
                connection_series: list[dict[str, Any]] = []
                for source_key, (source_item, source_kind, source_index) in keyed_items.items():
                    if target_entry is None:
                        connection_series.append(
                            {
                                "id": f"coordinate-connection:{source_key}",
                                "data": [],
                            }
                        )
                        continue
                    _, color = _series_style(source_item.name, self.series_styles, source_index)
                    connection_series.append(
                        _coordinate_connection_series(
                            source_item,
                            source_kind,
                            target_entry[0],
                            current_system,
                            color,
                            visible=source_key in selected_sources and source_key != target_key,
                        )
                    )
                chart.run_chart_method(
                    "setOption",
                    {"series": connection_series},
                    {"notMerge": False, "lazyUpdate": False},
                )

        async def synchronize_analysis_cie_tab(event: Any) -> None:
            """Tab 首次挂载后按最新状态补刷椭圆和坐标连线。"""

            nonlocal active_analysis_cie_system
            raw_value = getattr(event, "value", "")
            selected_system = (
                raw_value
                if isinstance(raw_value, str) and raw_value in {"xy", "upvp"}
                else "xy"
            )
            active_analysis_cie_system = selected_system
            await asyncio.sleep(0.05)
            update_sdcm_chart_series(selected_system)
            update_coordinate_connection_chart_series(selected_system)

        def update_connection_target(event: Any) -> None:
            """更新目标坐标点并移除与其重复的光源点。"""

            options = connection_point_options()
            raw_target = str(getattr(event, "value", "") or "")
            target_key = raw_target if raw_target in options else ""
            self.coordinate_connection_state["target"] = target_key
            self.coordinate_connection_state["sources"] = normalized_connection_sources(
                set(options),
                target_key,
            )
            render_coordinate_connection_controls.refresh()
            update_coordinate_connection_chart_series()

        def update_connection_sources(event: Any) -> None:
            """更新需要连接到目标点的多个光源坐标点。"""

            raw_sources = getattr(event, "value", [])
            self.coordinate_connection_state["sources"] = (
                list(raw_sources) if isinstance(raw_sources, (list, tuple, set)) else []
            )
            options = connection_point_options()
            target_key = str(self.coordinate_connection_state.get("target") or "")
            self.coordinate_connection_state["sources"] = normalized_connection_sources(
                set(options),
                target_key,
            )
            update_coordinate_connection_chart_series()

        @ui.refreshable
        def render_coordinate_connection_controls() -> None:
            """渲染目标坐标点与多光源坐标点的连线设置。"""

            options = connection_point_options()
            if not options:
                return
            allowed_keys = set(options)
            target_key = str(self.coordinate_connection_state.get("target") or "")
            if target_key not in allowed_keys:
                target_key = ""
                self.coordinate_connection_state["target"] = ""
            sources = normalized_connection_sources(allowed_keys, target_key)
            self.coordinate_connection_state["sources"] = sources
            source_options = {
                key: label for key, label in options.items() if key != target_key
            }
            with ui.card().classes("w-full p-3 bg-slate-50 border border-slate-200"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("polyline").classes("text-blue-700")
                    ui.label("坐标点连线").classes("font-bold text-slate-800")
                ui.select(
                    options,
                    value=target_key or None,
                    label="目标坐标点",
                    on_change=update_connection_target,
                ).props("outlined dense options-dense clearable").classes("w-full")
                source_select = ui.select(
                    source_options,
                    value=sources,
                    label="光源坐标点（可多选）",
                    multiple=True,
                    on_change=update_connection_sources,
                ).props("outlined dense options-dense use-chips clearable").classes("w-full")
                if not target_key:
                    source_select.props("disable")

        def render_sdcm_controls() -> None:
            """渲染每个分析结果点各自独立的 MacAdam 椭圆选项。"""

            items: list[tuple[SpectrumResult | ChromaticityResult, str, str]] = [
                *((item, "spectrum", "光谱") for item in self.spectrum_results),
                *((item, "coordinate", "手工坐标") for item in self.coordinate_results),
                *((item, "standard", "标准光源") for item in self.standard_illuminant_results),
            ]
            if not items:
                return
            with ui.card().classes("w-full p-3 bg-slate-50 border border-slate-200"):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.icon("radio_button_unchecked").classes("text-blue-700")
                    ui.label("麦克亚当椭圆（SDCM）").classes("font-bold text-slate-800")
                ui.label(
                    "每个点可同时选择多个阶数；清空选项即不显示。"
                ).classes("text-xs text-slate-500")
                with ui.column().classes(
                    "w-full gap-2 max-h-[calc(100vh-360px)] min-h-48 overflow-y-auto pr-1"
                ):
                    for item, result_kind, source_label in items:
                        key = _sdcm_key(result_kind, item.name)
                        ui.select(
                            SDCM_OPTIONS,
                            value=list(_sdcm_orders(self.sdcm_orders.get(key, ()))),
                            label=f"{source_label} · {item.name}",
                            multiple=True,
                            on_change=lambda event, state_key=key: update_sdcm_orders(event, state_key),
                        ).props("outlined dense options-dense use-chips clearable").classes("w-full")

        @ui.refreshable
        def render_chromaticity_view() -> None:
            nonlocal active_analysis_cie_system
            active_analysis_cie_system = "xy"
            with ui.row().classes("w-full flex-nowrap items-start gap-3"):
                with ui.column().classes("flex-1 min-w-0 gap-0"):
                    ui.label(
                        "光谱点、手工坐标与所选标准光源已叠加；十字线实时显示鼠标坐标，单击可固定坐标与有效 CCT。滚轮等比例缩放，按住左键可任意方向平移。"
                    ).classes("text-xs text-slate-500 mb-2")
                    cie_tabs = ui.tabs().classes("w-full text-blue-700")
                    with cie_tabs:
                        cie_xy_tab = ui.tab("xy", label="CIE 1931 xy")
                        cie_upvp_tab = ui.tab("upvp", label="CIE 1976 u′v′")
                    with ui.tab_panels(
                        cie_tabs,
                        value=cie_xy_tab,
                        on_change=synchronize_analysis_cie_tab,
                    ).classes("w-full"):
                        with ui.tab_panel(cie_xy_tab).classes("p-0"):
                            cie_charts["xy"] = _render_cie_chart(analysis_chromaticity_options("xy"))
                        with ui.tab_panel(cie_upvp_tab).classes("p-0"):
                            cie_charts["upvp"] = _render_cie_chart(analysis_chromaticity_options("upvp"))
                with ui.column().classes("w-80 shrink-0 gap-3 sticky top-2"):
                    render_coordinate_connection_controls()
                    render_sdcm_controls()

        def refresh_chart_appearance(_=None) -> None:
            """刷新所有受图标、颜色和坐标轴间隔影响的图表。"""

            render_spectrum_chart.refresh()
            render_chromaticity_view.refresh()
            render_cri_comparison.refresh()

        def update_series_color(event: Any, spectrum_name: str) -> None:
            """保存用户手动选择的单条光谱颜色。"""

            color = str(getattr(event, "value", "") or "")
            if re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                self.series_styles.setdefault(spectrum_name, {})["color"] = color
            refresh_chart_appearance()

        def reset_series_styles(_=None) -> None:
            """恢复按标题关键字自动分组的图标和默认颜色。"""

            self.series_styles = _default_series_styles(self.spectrum_results)
            render_spectrum_results.refresh()

        @ui.refreshable
        def render_cri_comparison() -> None:
            if self.cri_comparison_results is None:
                ui.label("请选择两个可计算 CRI 的光源").classes("text-sm text-slate-500")
                return
            first_result, second_result = self.cri_comparison_results
            cri_columns = [
                {"name": "name", "label": "光源", "field": "name", "align": "left"},
                {"name": "ra", "label": "Ra", "field": "ra", "align": "right"},
                *[
                    {
                        "name": f"r{sample}",
                        "label": f"R{sample}",
                        "field": f"r{sample}",
                        "align": "right",
                    }
                    for sample in range(1, 16)
                ],
                {"name": "rf", "label": "CIE Rf", "field": "rf", "align": "right"},
            ]
            ui.table(
                columns=cri_columns,
                rows=_cri_value_rows([first_result, second_result]),
            ).props("dense flat bordered wrap-cells").classes("w-full")
            ui.label(
                "图中显示两个光源照射 R1–R15 色样后的实际色坐标；同编号连线表示位置差异。滚轮缩放，拖拽平移。"
            ).classes("text-xs text-slate-500 mt-2")
            comparison_tabs = ui.tabs().classes("w-full text-blue-700")
            with comparison_tabs:
                comparison_xy_tab = ui.tab("CIE 1931 xy")
                comparison_upvp_tab = ui.tab("CIE 1976 u′v′")
            with ui.tab_panels(comparison_tabs, value=comparison_xy_tab).classes("w-full"):
                with ui.tab_panel(comparison_xy_tab).classes("p-0"):
                    _render_cie_chart(
                        _cri_pair_chromaticity_chart_options(
                            first_result,
                            second_result,
                            coordinate_system="xy",
                            series_styles=self.series_styles,
                            axis_interval=_nonnegative_number(self.chart_state.get("xy_interval"), 0.1),
                            show_isotherms=bool(self.coordinate_state.get("show_isotherms", False)),
                        ),
                        viewport_offset=300,
                    )
                with ui.tab_panel(comparison_upvp_tab).classes("p-0"):
                    _render_cie_chart(
                        _cri_pair_chromaticity_chart_options(
                            first_result,
                            second_result,
                            coordinate_system="upvp",
                            series_styles=self.series_styles,
                            axis_interval=_nonnegative_number(self.chart_state.get("upvp_interval"), 0.1),
                            show_isotherms=bool(self.coordinate_state.get("show_isotherms", False)),
                        ),
                        viewport_offset=300,
                    )

        @ui.refreshable
        def render_spectrum_results() -> None:
            if not self.spectrum_results and not self.coordinate_results:
                with ui.column().classes("w-full h-[520px] items-center justify-center text-slate-400 gap-3"):
                    ui.icon("query_stats", size="64px")
                    ui.label("输入光谱或色坐标后点击“联合计算”").classes("text-lg")
                    ui.label("两类数据可以只填写其中一种，也可以同时填写").classes("text-sm")
                return

            warning_items = [(result.name, warning) for result in self.spectrum_results for warning in result.warnings]
            if warning_items:
                with ui.expansion("计算提示与适用性警告", icon="warning", value=False).classes(
                    "w-full bg-amber-50 text-amber-900 rounded-lg border border-amber-200"
                ):
                    for name, warning in warning_items:
                        ui.label(f"{name}：{warning}").classes("text-sm")

            with ui.expansion("图表样式与坐标轴设置", icon="tune", value=False).classes(
                "w-full bg-slate-50 rounded-lg border border-slate-200"
            ):
                with ui.grid().classes("w-full grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 p-2"):
                    ui.number(
                        "光谱 X 轴目标间隔 (nm)",
                        min=0,
                        step=5,
                    ).bind_value(self.chart_state, "spectrum_x_interval").props("outlined dense").on_value_change(
                        refresh_chart_appearance
                    )
                    ui.number(
                        "光谱 Y 轴目标间隔（0=自动）",
                        min=0,
                        step=0.05,
                    ).bind_value(self.chart_state, "spectrum_y_interval").props("outlined dense").on_value_change(
                        refresh_chart_appearance
                    )
                    ui.number(
                        "CIE xy 轴目标间隔",
                        min=0.01,
                        max=0.5,
                        step=0.01,
                    ).bind_value(self.chart_state, "xy_interval").props("outlined dense").on_value_change(
                        refresh_chart_appearance
                    )
                    ui.number(
                        "CIE u′v′ 轴目标间隔",
                        min=0.01,
                        max=0.5,
                        step=0.01,
                    ).bind_value(self.chart_state, "upvp_interval").props("outlined dense").on_value_change(
                        refresh_chart_appearance
                    )
                with ui.row().classes("w-full items-center justify-between px-2"):
                    ui.label(
                        "目标间隔用于确定全视图刻度密度，缩放后会自动细分；同一自动分组关键字默认使用相同图标。"
                    ).classes("text-xs text-slate-500")
                    ui.button(
                        "恢复自动样式",
                        icon="restart_alt",
                        on_click=reset_series_styles,
                    ).props("flat dense no-caps")
                for item in self.spectrum_results:
                    style = self.series_styles.setdefault(
                        item.name,
                        _default_series_styles([item])[item.name],
                    )
                    with ui.row().classes("w-full items-center gap-3 px-2 py-1"):
                        ui.label(item.name).classes("w-56 truncate font-medium").tooltip(item.name)
                        ui.label(f"自动分组：{style.get('group', '未分组')}").classes("w-48 text-xs text-slate-500")
                        ui.select(
                            SYMBOL_OPTIONS,
                            label="图标形状",
                        ).bind_value(style, "symbol").props("outlined dense options-dense").classes(
                            "w-44"
                        ).on_value_change(refresh_chart_appearance)
                        ui.color_input(
                            "颜色",
                            value=str(style.get("color") or "#2563eb"),
                            preview=True,
                            on_change=lambda event, spectrum_name=item.name: update_series_color(event, spectrum_name),
                        )

            result_tabs = ui.tabs().classes("w-full text-blue-700")
            with result_tabs:
                summary_tab = ui.tab("综合指标", icon="table_chart")
                spectrum_tab = ui.tab("光谱曲线", icon="show_chart")
                chromaticity_tab = ui.tab("色坐标", icon="scatter_plot")
                cri_tab = ui.tab("显色指数", icon="fact_check")

            with ui.tab_panels(result_tabs, value=summary_tab).classes("w-full bg-white"):
                with ui.tab_panel(summary_tab).classes("p-2"):
                    if self.spectrum_results:
                        columns = [
                            {"name": "name", "label": "光谱", "field": "name", "align": "left"},
                            {
                                "name": "peak_wavelength",
                                "label": "峰值波长(nm)",
                                "field": "peak_wavelength",
                                "align": "right",
                            },
                            {
                                "name": "dominant_wavelength",
                                "label": "主波长 λd",
                                "field": "dominant_wavelength",
                                "align": "right",
                            },
                            {"name": "cct", "label": "CCT(K)", "field": "cct", "align": "right"},
                            {"name": "duv", "label": "Duv", "field": "duv", "align": "right"},
                            {"name": "x", "label": "x", "field": "x", "align": "right"},
                            {"name": "y", "label": "y", "field": "y", "align": "right"},
                            {"name": "up", "label": "u′", "field": "up", "align": "right"},
                            {"name": "vp", "label": "v′", "field": "vp", "align": "right"},
                            {"name": "ra", "label": "Ra", "field": "ra", "align": "right"},
                            {"name": "r9", "label": "R9", "field": "r9", "align": "right"},
                            {"name": "r15", "label": "R15(JIS)", "field": "r15", "align": "right"},
                            {"name": "rf", "label": "CIE Rf", "field": "rf", "align": "right"},
                            {"name": "dc", "label": "CRI Δuv", "field": "dc", "align": "right"},
                        ]
                        ui.table(columns=columns, rows=_spectrum_summary_rows(self.spectrum_results)).props(
                            "dense flat bordered wrap-cells"
                        ).classes("w-full")
                        ui.label(
                            "峰值波长取光谱最大值所在采样点；主波长以等能白点 E 为参考，紫边方向显示补色波长。"
                            "XYZ 已按 Y=100 归一化；Ra 仍取 R1–R8 平均，R15 标注为 JIS 扩展。"
                        ).classes("text-xs text-slate-500 mt-2")
                    if self.coordinate_results:
                        ui.label("手工输入色坐标").classes(
                            "text-base font-bold text-slate-700" + (" mt-4" if self.spectrum_results else "")
                        )
                        coordinate_columns = [
                            {"name": "name", "label": "名称", "field": "name", "align": "left"},
                            *[
                                {"name": key, "label": label, "field": key, "align": "right"}
                                for key, label in [
                                    ("x", "x"),
                                    ("y", "y"),
                                    ("u", "u"),
                                    ("v", "v"),
                                    ("up", "u′"),
                                    ("vp", "v′"),
                                    ("cct", "CCT(K)"),
                                    ("duv", "Duv"),
                                ]
                            ],
                        ]
                        ui.table(
                            columns=coordinate_columns,
                            rows=_coordinate_summary_rows(self.coordinate_results),
                        ).props("dense flat bordered wrap-cells").classes("w-full")

                with ui.tab_panel(spectrum_tab).classes("p-2"):
                    reference_options = _spectrum_reference_options(self.spectrum_results)
                    with ui.row().classes("w-full px-10 items-center justify-between gap-3"):
                        ui.select(
                            reference_options,
                            label="叠加等色温标准源",
                        ).bind_value(self.spectral_state, "reference_source").props(
                            "outlined dense options-dense"
                        ).classes("w-full max-w-[460px]").on_value_change(update_spectrum_reference)
                        ui.switch("峰值归一化显示").bind_value(self.spectral_state, "normalized").on_value_change(
                            lambda _=None: render_spectrum_chart.refresh()
                        )
                    render_spectrum_chart()

                with ui.tab_panel(chromaticity_tab).classes("p-2"):
                    with ui.row().classes("w-full items-center justify-between gap-3 mb-2"):
                        ui.select(
                            STANDARD_ILLUMINANTS,
                            label="加入内置标准光源坐标点",
                            multiple=True,
                        ).bind_value(self.coordinate_state, "standard_illuminants").props(
                            "outlined dense options-dense use-chips clearable"
                        ).classes("w-full max-w-[720px]").on_value_change(update_chromaticity_illuminants)
                        ui.switch("显示等色温线").bind_value(self.coordinate_state, "show_isotherms").on_value_change(
                            refresh_chart_appearance
                        )
                    render_chromaticity_view()

                with ui.tab_panel(cri_tab).classes("p-2"):
                    source_options = _comparison_source_options(self.spectrum_results)
                    with ui.card().classes("w-full p-3 bg-slate-50"):
                        ui.label("选择任意两个光源，对比 R1–R15 色样坐标").classes("text-base font-bold text-slate-800")
                        with ui.row().classes("w-full items-center gap-3"):
                            ui.select(
                                source_options,
                                label="光源 A",
                            ).bind_value(self.cri_state, "source_a").props("outlined dense options-dense").classes(
                                "w-full max-w-[430px]"
                            ).on_value_change(update_cri_sources)
                            ui.select(
                                source_options,
                                label="光源 B",
                            ).bind_value(self.cri_state, "source_b").props("outlined dense options-dense").classes(
                                "w-full max-w-[430px]"
                            ).on_value_change(update_cri_sources)
                        ui.label("内置光源包含 A、D50/D55/D65/D75、E、常用荧光灯与 LED 标准光谱。").classes(
                            "text-xs text-slate-500"
                        )
                        render_cri_comparison()

        async def load_cri_comparison_sources() -> None:
            """解析两个选择项，并异步载入可能需要的标准光源。"""

            options = _comparison_source_options(self.spectrum_results)
            allowed = set(options)
            if not allowed:
                self.cri_comparison_results = None
                return
            default_a = "input:0" if self.spectrum_results else next(iter(allowed))
            default_b = "input:1" if len(self.spectrum_results) > 1 else "standard:D65"
            source_a = _option_text(self.cri_state.get("source_a"), allowed, default_a)
            source_b = _option_text(self.cri_state.get("source_b"), allowed, default_b)
            if source_a == source_b:
                source_b = next((item for item in allowed if item != source_a), source_b)
            self.cri_state["source_a"] = source_a
            self.cri_state["source_b"] = source_b

            async def resolve(source_id: str) -> SpectrumResult:
                if source_id.startswith("input:"):
                    index = int(source_id.split(":", 1)[1])
                    return self.spectrum_results[index]
                if source_id.startswith("reference:"):
                    index = int(source_id.split(":", 1)[1])
                    cct = self.spectrum_results[index].cct
                    if cct is None:
                        raise SpectralAnalysisError("所选光谱没有有效 CCT，无法生成等色温标准源")
                    return await run.cpu_bound(analyze_cct_reference, cct)
                illuminant_key = source_id.split(":", 1)[1]
                return await run.cpu_bound(analyze_standard_illuminant, illuminant_key)

            first_result = await resolve(source_a)
            second_result = await resolve(source_b)
            self.cri_comparison_results = (first_result, second_result)

        async def update_cri_sources(_=None) -> None:
            """响应光源选择变化并只刷新显色对比区域。"""

            try:
                await load_cri_comparison_sources()
            except Exception as exc:
                logger.error("载入显色对比光源失败", exc_info=True)
                ui.notify(f"载入对比光源失败：{exc}", type="negative")
                return
            render_cri_comparison.refresh()

        async def update_chromaticity_illuminants(_=None) -> None:
            """响应内置标准光源多选变化并刷新色坐标图。"""

            raw_selected = self.coordinate_state.get("standard_illuminants", [])
            if isinstance(raw_selected, (list, tuple, set)):
                selected = tuple(str(key) for key in raw_selected if str(key) in STANDARD_ILLUMINANTS)
            else:
                selected = ()
            self.coordinate_state["standard_illuminants"] = list(selected)
            try:
                results = [await run.cpu_bound(analyze_standard_illuminant_chromaticity, key) for key in selected]
            except Exception as exc:
                logger.error("载入色坐标图内置标准光源失败", exc_info=True)
                ui.notify(f"载入内置标准光源失败：{exc}", type="negative")
                self.standard_illuminant_results = []
                render_chromaticity_view.refresh()
                return
            current_selection = self.coordinate_state.get("standard_illuminants", [])
            if isinstance(current_selection, list) and tuple(current_selection) == selected:
                self.standard_illuminant_results = results
                render_chromaticity_view.refresh()

        async def update_spectrum_reference(_=None) -> None:
            """按所选光谱的 CCT 异步生成曲线叠加参考源。"""

            options = _spectrum_reference_options(self.spectrum_results)
            selected = _option_text(
                self.spectral_state.get("reference_source"),
                set(options),
                "none",
            )
            self.spectral_state["reference_source"] = selected
            if selected == "none":
                self.spectrum_reference_result = None
                render_spectrum_chart.refresh()
                return
            try:
                index = int(selected.split(":", 1)[1])
                cct = self.spectrum_results[index].cct
                if cct is None:
                    raise SpectralAnalysisError("所选光谱没有有效 CCT")
                self.spectrum_reference_result = await run.cpu_bound(analyze_cct_reference, cct)
            except Exception as exc:
                logger.error("生成等色温标准光源失败", exc_info=True)
                ui.notify(f"生成等色温标准源失败：{exc}", type="negative")
                return
            render_spectrum_chart.refresh()

        def current_mixing_graph() -> tuple[dict[str, MixingSpectrumResult], list[str]]:
            """返回按当前滑块比例重建后的全部混光节点与活动节点。"""

            return _mixing_nodes_and_active_ids(self.spectrum_results, self.mixing_steps)

        def current_mixing_graph_details() -> tuple[
            dict[str, MixingSpectrumResult],
            list[str],
            dict[str, tuple[float, ...]],
        ]:
            """返回混光节点、活动节点及其原始光谱峰值贡献。"""

            return _mixing_graph_details(self.spectrum_results, self.mixing_steps)

        def clear_mixing_solution() -> None:
            """同时清除目标配比结果及其绝对光功率结果。"""

            nonlocal mixing_solve_task, mixing_solve_generation
            nonlocal mixing_solve_pending, mixing_solve_error
            mixing_solve_generation += 1
            if mixing_solve_task is not None:
                mixing_solve_task.cancel()
                mixing_solve_task = None
            mixing_solve_pending = False
            mixing_solve_error = ""
            self.mixing_solution = None
            self.mixing_power_result = None

        def mixing_step_summary_parts(
            step: dict[str, Any],
            nodes: dict[str, MixingSpectrumResult],
        ) -> tuple[str, str, str, str]:
            """格式化单个组合步骤的配比、色坐标及混合后波长指标。"""

            ratio = _nonnegative_number(step.get("ratio"), 50.0)
            first = nodes[str(step["first_id"])]
            second = nodes[str(step["second_id"])]
            result = nodes[str(step["id"])]
            return (
                f"{first.name} {ratio:.0f}%",
                f"xy = ({result.xy[0]:.6f}, {result.xy[1]:.6f})",
                f"{second.name} {100 - ratio:.0f}%",
                f"混合后：峰值 {result.peak_wavelength:.1f} nm；主波长 {_dominant_wavelength_metric(result)}",
            )

        @ui.refreshable
        def render_mixing_solution() -> None:
            try:
                nodes, active_ids = current_mixing_graph()
            except SpectralAnalysisError as exc:
                ui.label(str(exc)).classes("text-sm text-rose-600")
                return
            if len(active_ids) != 3:
                ui.label(f"当前剩余 {len(active_ids)} 条光谱；请继续组合，直至恰好剩余 3 条。").classes(
                    "text-sm text-slate-500"
                )
                return
            with ui.element("div").classes("w-full min-h-[28px]"):
                if mixing_solve_pending:
                    with ui.row().classes("w-full items-center gap-2 text-blue-700"):
                        ui.spinner(size="20px")
                        ui.label("参数已变化，正在自动更新目标配比与光参数……").classes("text-sm")
                elif mixing_solve_error:
                    ui.label(f"本次自动求解失败：{mixing_solve_error}；下方保留上次有效结果。").classes(
                        "w-full text-sm text-amber-700"
                    )
            if self.mixing_solution is None:
                if not mixing_solve_pending:
                    ui.label("修改目标色坐标后会自动计算最终配比和光参数。").classes("text-sm text-blue-700")
                ui.element("div").classes("w-full min-h-[320px]")
                return
            solution = self.mixing_solution
            ratio_rows = [
                {
                    "name": nodes[node_id].name,
                    "ratio": f"{ratio * 100:.4f}%",
                }
                for node_id, ratio in zip(active_ids, solution.peak_ratios)
            ]
            ui.table(
                columns=[
                    {"name": "name", "label": "最终光谱", "field": "name", "align": "left"},
                    {"name": "ratio", "label": "峰值配比", "field": "ratio", "align": "right"},
                ],
                rows=ratio_rows,
            ).props("dense flat bordered").classes("w-full")
            result = solution.result
            power_result = self.mixing_power_result
            if power_result is None:
                ui.label("绝对光功率结果不可用，请重新执行目标配比求解。").classes("text-sm text-amber-700")
                return
            metrics = [
                ("目标混合 xy", f"{result.xy[0]:.6f}, {result.xy[1]:.6f}"),
                ("混合光谱峰值波长", f"{result.peak_wavelength:.1f} nm"),
                ("混合光谱主波长", _dominant_wavelength_metric(result)),
                ("CCT", "—" if result.cct is None else f"{result.cct:.0f} K"),
                ("Duv", _metric(result.duv, 6)),
                ("混合光功率", f"{power_result.radiant_power:.6g} W"),
                ("光通量", f"{power_result.luminous_flux:.6g} lm"),
                ("光视效能", f"{power_result.luminous_efficacy:.3f} lm/W"),
                ("CRI Ra", _metric(result.ra, 2)),
                ("R9", _metric(dict(result.ri).get(9), 2)),
                ("CIE Rf", _metric(result.rf, 2)),
            ]
            with ui.grid().classes("w-full grid-cols-2 xl:grid-cols-3 gap-2 mt-2"):
                for label, value in metrics:
                    with ui.card().classes("w-full p-1 gap-0 bg-slate-50 shadow-none border"):
                        ui.label(label).classes("text-xs text-slate-500")
                        ui.label(value).classes("text-base font-bold text-slate-800")

            limiting_indices = set(power_result.limiting_source_indices)
            power_rows = []
            for index, (source, source_power) in enumerate(zip(self.spectrum_results, power_result.source_powers)):
                limit = _nonnegative_number(
                    self.mixing_power_limits.get(f"source:{index}"),
                    1.0,
                )
                power_rows.append(
                    {
                        "name": source.name,
                        "power": f"{source_power:.6g}",
                        "limit": f"{limit:.6g}",
                        "usage": f"{source_power / limit * 100:.2f}%" if limit > 0 else "—",
                        "status": "达到上限" if index in limiting_indices else "",
                    }
                )
            ui.table(
                columns=[
                    {"name": "name", "label": "原始光谱", "field": "name", "align": "left"},
                    {"name": "power", "label": "实际功率 (W)", "field": "power", "align": "right"},
                    {"name": "limit", "label": "上限 (W)", "field": "limit", "align": "right"},
                    {"name": "usage", "label": "利用率", "field": "usage", "align": "right"},
                    {"name": "status", "label": "限制状态", "field": "status", "align": "center"},
                ],
                rows=power_rows,
            ).props("dense flat bordered").classes("w-full mt-2")
            ui.label(
                "波长指标由最终混合光谱重新计算，主波长以等能白点 E 为参考；"
                "按 360–780 nm 辐射功率上限整体放大到首个光谱达到上限，光通量采用 CIE 1924 明视觉函数计算。"
            ).classes("text-xs text-blue-700 mt-1")

        @ui.refreshable
        def render_mixing_charts() -> None:
            try:
                nodes, active_ids = current_mixing_graph()
            except SpectralAnalysisError as exc:
                ui.label(str(exc)).classes("text-sm text-rose-600")
                return
            active_results = [nodes[node_id] for node_id in active_ids]
            spectrum_results = [item for item in active_results if isinstance(item, SpectrumResult)]
            coordinate_results: list[Any] = [
                item for item in active_results if isinstance(item, SpectrumChromaticityResult)
            ]
            plot_results: list[Any] = list(active_results)
            hidden_spectrum_names: set[str] = set()
            if self.mixing_solution is not None:
                spectrum_results.append(self.mixing_solution.result)
                plot_results.append(self.mixing_solution.result)
                hidden_spectrum_names = {item.name for item in active_results}
            mixing_series_styles = _default_series_styles(
                [*self.spectrum_results, *plot_results]
            )
            for name in set(mixing_series_styles) & set(self.series_styles):
                mixing_series_styles[name].update(self.series_styles[name])
            target_x = _nonnegative_number(self.mixing_state.get("target_x"), -1.0)
            target_y = _nonnegative_number(self.mixing_state.get("target_y"), -1.0)
            if target_x > 0 and target_y > 0 and target_x + target_y < 1:
                try:
                    coordinate_results.extend(parse_chromaticity_text(f"目标色坐标\t{target_x}\t{target_y}", "xy"))
                except SpectralAnalysisError:
                    pass
            with ui.grid().classes("w-full h-full grid-cols-2 grid-rows-2 gap-2 min-h-0"):
                with ui.card().classes("w-full h-full p-2 rounded-xl shadow-sm min-h-0"):
                    ui.label("CIE 1931 xy 实时色坐标").classes("text-lg font-bold text-slate-800")
                    _render_fitted_cie_chart(
                        _chromaticity_chart_options(
                            spectrum_results=spectrum_results,
                            coordinate_results=coordinate_results,
                            coordinate_system="xy",
                            series_styles=mixing_series_styles,
                            axis_interval=_nonnegative_number(self.chart_state.get("xy_interval"), 0.1),
                            show_isotherms=bool(self.coordinate_state.get("show_isotherms", False)),
                            compact=True,
                        )
                    )
                with ui.card().classes("w-full h-full p-2 rounded-xl shadow-sm min-h-0"):
                    ui.label("CIE 1976 u′v′ 实时色坐标").classes("text-lg font-bold text-slate-800")
                    _render_fitted_cie_chart(
                        _chromaticity_chart_options(
                            spectrum_results=spectrum_results,
                            coordinate_results=coordinate_results,
                            coordinate_system="upvp",
                            series_styles=mixing_series_styles,
                            axis_interval=_nonnegative_number(
                                self.chart_state.get("upvp_interval"),
                                0.1,
                            ),
                            show_isotherms=bool(self.coordinate_state.get("show_isotherms", False)),
                            compact=True,
                        )
                    )
                with ui.card().classes("w-full h-full col-span-2 p-2 rounded-xl shadow-sm min-h-0"):
                    ui.label("实时光谱").classes("text-lg font-bold text-slate-800")
                    ui.echart(
                        _spectrum_chart_options(
                            plot_results,
                            normalized=True,
                            series_styles=mixing_series_styles,
                            x_axis_interval=_nonnegative_number(self.chart_state.get("spectrum_x_interval"), 20.0),
                            y_axis_interval=_nonnegative_number(self.chart_state.get("spectrum_y_interval"), 0.0),
                            hidden_series_names=hidden_spectrum_names,
                            compact_layout=True,
                        )
                    ).classes("w-full flex-1 min-h-0")

        def refresh_mixing_outputs() -> None:
            """刷新最终指标与右侧实时图，不重建正在拖动的滑块。"""

            render_mixing_solution.refresh()
            render_mixing_charts.refresh()

        def schedule_dynamic_mixing_solve(delay: float = 0.25) -> None:
            """防抖调度自动求解，并保留上次有效结果直到新结果完成。"""

            nonlocal mixing_solve_task, mixing_solve_generation
            nonlocal mixing_solve_pending, mixing_solve_error
            render_mixing_charts.refresh()
            try:
                _, active_ids = current_mixing_graph()
            except SpectralAnalysisError as exc:
                mixing_solve_pending = False
                mixing_solve_error = str(exc)
                refresh_mixing_outputs()
                return
            if len(active_ids) != 3:
                return
            mixing_solve_generation += 1
            generation = mixing_solve_generation
            if mixing_solve_task is not None:
                mixing_solve_task.cancel()
            mixing_solve_pending = True
            mixing_solve_error = ""
            render_mixing_solution.refresh()

            async def run_scheduled_solve() -> None:
                await asyncio.sleep(delay)
                await solve_target_mixing(generation)

            mixing_solve_task = asyncio.create_task(run_scheduled_solve())

        def update_mixing_target(_=None) -> None:
            """目标坐标变化时实时移动目标点并自动重新求解。"""

            schedule_dynamic_mixing_solve()

        def make_mixing_ratio_handler(step_id: str):
            """为单个混合步骤创建不会被事件参数覆盖的比例更新回调。"""

            def handle_ratio_change(event: Any) -> None:
                step = next((item for item in self.mixing_steps if item.get("id") == step_id), None)
                if step is None:
                    return
                step["ratio"] = min(100.0, _nonnegative_number(getattr(event, "value", None), 50.0))
                try:
                    nodes, _ = current_mixing_graph()
                except SpectralAnalysisError as exc:
                    ui.notify(str(exc), type="warning")
                    return
                labels = mixing_step_labels.get(step_id)
                if labels is not None:
                    for label, text in zip(labels, mixing_step_summary_parts(step, nodes)):
                        label.set_text(text)
                schedule_dynamic_mixing_solve()

            return handle_ratio_change

        def make_power_limit_handler(source_id: str):
            """为原始光谱功率上限创建类型安全的更新回调。"""

            def handle_power_limit_change(event: Any) -> None:
                value = _nonnegative_number(getattr(event, "value", None), 0.0)
                if value <= 0:
                    ui.notify("光功率上限必须大于 0 W", type="warning")
                    return
                self.mixing_power_limits[source_id] = value
                schedule_dynamic_mixing_solve()

            return handle_power_limit_change

        def add_mixing_step() -> None:
            """消耗两个当前活动节点并生成下一层混合节点。"""

            try:
                nodes, active_ids = current_mixing_graph()
            except SpectralAnalysisError as exc:
                ui.notify(str(exc), type="warning")
                return
            if len(active_ids) <= 3:
                ui.notify("已剩余三条光谱，请直接进行目标色坐标求解", type="info")
                return
            allowed = set(active_ids)
            first_id = _option_text(self.mixing_state.get("source_a"), allowed)
            second_id = _option_text(self.mixing_state.get("source_b"), allowed)
            if not first_id or not second_id or first_id == second_id:
                ui.notify("请选择两条不同的当前光谱", type="warning")
                return
            step_number = len(self.mixing_steps) + 1
            step_id = f"mix:{step_number}"
            self.mixing_steps.append(
                {
                    "id": step_id,
                    "name": f"混合 {step_number} · {nodes[first_id].name} + {nodes[second_id].name}",
                    "first_id": first_id,
                    "second_id": second_id,
                    "ratio": 50.0,
                }
            )
            clear_mixing_solution()
            _, next_active_ids = current_mixing_graph()
            self.mixing_state["source_a"] = next_active_ids[0] if next_active_ids else ""
            self.mixing_state["source_b"] = next_active_ids[1] if len(next_active_ids) > 1 else ""
            render_mixing_workspace.refresh()
            if len(next_active_ids) == 3:
                schedule_dynamic_mixing_solve()

        def undo_mixing_step() -> None:
            """撤销最近一层组合。"""

            if not self.mixing_steps:
                return
            self.mixing_steps.pop()
            clear_mixing_solution()
            render_mixing_workspace.refresh()

        def reset_mixing_steps() -> None:
            """恢复到全部导入光谱均未组合的状态。"""

            self.mixing_steps.clear()
            clear_mixing_solution()
            if self.spectrum_results:
                self.mixing_state["source_a"] = "source:0"
                self.mixing_state["source_b"] = "source:1" if len(self.spectrum_results) > 1 else ""
            render_mixing_workspace.refresh()

        async def solve_target_mixing(generation: int) -> None:
            """对最终三条活动光谱自动反求目标 xy 配比，并丢弃过期计算。"""

            nonlocal mixing_solve_task, mixing_solve_pending, mixing_solve_error
            try:
                nodes, active_ids, source_coefficients = current_mixing_graph_details()
                if len(active_ids) != 3:
                    raise SpectralAnalysisError("必须先组合到恰好剩余三条光谱")
                target_x = _nonnegative_number(self.mixing_state.get("target_x"), -1.0)
                target_y = _nonnegative_number(self.mixing_state.get("target_y"), -1.0)
                if target_x <= 0 or target_y <= 0 or target_x + target_y >= 1:
                    raise SpectralAnalysisError("目标 xy 色坐标无效")
                spectra = (
                    _spectrum_input_from_result(nodes[active_ids[0]]),
                    _spectrum_input_from_result(nodes[active_ids[1]]),
                    _spectrum_input_from_result(nodes[active_ids[2]]),
                )
                solution = await run.cpu_bound(
                    solve_three_spectrum_mix,
                    spectra,
                    (target_x, target_y),
                )
                final_source_coefficients = tuple(
                    sum(
                        ratio * source_coefficients[node_id][source_index]
                        for node_id, ratio in zip(active_ids, solution.peak_ratios)
                    )
                    for source_index in range(len(self.spectrum_results))
                )
                source_inputs = tuple(_spectrum_input_from_result(result) for result in self.spectrum_results)
                power_limits = tuple(
                    _nonnegative_number(
                        self.mixing_power_limits.get(f"source:{index}"),
                        1.0,
                    )
                    for index in range(len(self.spectrum_results))
                )
                power_result = await run.cpu_bound(
                    calculate_power_limited_mix,
                    source_inputs,
                    final_source_coefficients,
                    power_limits,
                )
            except SpectralAnalysisError as exc:
                if generation != mixing_solve_generation:
                    return
                mixing_solve_pending = False
                mixing_solve_error = str(exc)
                mixing_solve_task = None
                refresh_mixing_outputs()
                return
            except Exception as exc:
                if generation != mixing_solve_generation:
                    return
                logger.error("三光谱目标配比自动求解失败", exc_info=True)
                mixing_solve_pending = False
                mixing_solve_error = str(exc)
                mixing_solve_task = None
                refresh_mixing_outputs()
                return
            if generation != mixing_solve_generation:
                return
            self.mixing_solution = solution
            self.mixing_power_result = power_result
            mixing_solve_pending = False
            mixing_solve_error = ""
            mixing_solve_task = None
            refresh_mixing_outputs()

        @ui.refreshable
        def render_mixing_workspace() -> None:
            mixing_step_labels.clear()
            if len(self.spectrum_results) < 3:
                with ui.column().classes("w-full h-[520px] items-center justify-center gap-3 text-slate-400"):
                    ui.icon("device_hub", size="64px")
                    ui.label("请先在“数据录入”中导入并计算至少三条光谱").classes("text-lg")
                return
            try:
                nodes, active_ids = current_mixing_graph()
            except SpectralAnalysisError as exc:
                ui.label(str(exc)).classes("text-sm text-rose-600")
                return
            options = _mixing_node_options(nodes, active_ids)
            allowed = set(options)
            if self.mixing_state.get("source_a") not in allowed:
                self.mixing_state["source_a"] = active_ids[0]
            if self.mixing_state.get("source_b") not in allowed or (
                self.mixing_state.get("source_b") == self.mixing_state.get("source_a")
            ):
                self.mixing_state["source_b"] = next(
                    (node_id for node_id in active_ids if node_id != self.mixing_state.get("source_a")),
                    "",
                )

            for index in range(len(self.spectrum_results)):
                self.mixing_power_limits.setdefault(f"source:{index}", 1.0)

            splitter = ui.splitter(value=30, limits=(28, 52)).classes("w-full h-full min-h-0")
            with splitter.before:
                with ui.scroll_area().classes("w-full h-full"):
                    with ui.column().classes("w-full p-2 gap-2"):
                        with ui.card().classes("w-full p-3 rounded-xl shadow-sm"):
                            ui.label("原始光谱光功率上限").classes("text-lg font-bold text-slate-800")
                            ui.label("单位为 W，表示各光谱在 360–780 nm 范围内可提供的最大辐射功率。").classes(
                                "text-xs text-slate-500"
                            )
                            with ui.grid().classes("w-full grid-cols-1 xl:grid-cols-5 gap-2 mt-1"):
                                for index, result in enumerate(self.spectrum_results):
                                    source_id = f"source:{index}"
                                    ui.number(
                                        result.name,
                                        value=self.mixing_power_limits[source_id],
                                        min=0.000001,
                                        step=0.1,
                                        on_change=make_power_limit_handler(source_id),
                                    ).props("outlined dense suffix=W input-class=text-right").classes("w-full")

                        with ui.card().classes("w-full p-3 rounded-xl shadow-sm"):
                            with ui.row().classes("w-full items-center justify-between gap-2"):
                                ui.label("逐层峰值配比组合").classes("text-lg font-bold text-slate-800")
                                ui.badge(f"剩余 {len(active_ids)} 条", color="blue-8").props("rounded")
                            ui.label("选择两条当前光谱后直接加入；新层初始为 50:50，可在加入后继续拖动调整。").classes(
                                "text-xs text-slate-500"
                            )
                            with ui.row().classes("w-full items-center justify-between gap-2"):
                                ui.select(
                                    options,
                                    label="光谱 A",
                                ).bind_value(self.mixing_state, "source_a").props(
                                    "outlined dense options-dense"
                                ).classes("w-full")
                                ui.select(
                                    options,
                                    label="光谱 B",
                                ).bind_value(self.mixing_state, "source_b").props(
                                    "outlined dense options-dense"
                                ).classes("w-full")
                            with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                                ui.button(
                                    "加入组合层",
                                    icon="add_link",
                                    on_click=add_mixing_step,
                                ).props(
                                    "unelevated no-caps color=blue-8" + (" disable" if len(active_ids) <= 3 else "")
                                )
                                ui.button(
                                    "撤销上一层",
                                    icon="undo",
                                    on_click=undo_mixing_step,
                                ).props("flat no-caps" + (" disable" if not self.mixing_steps else ""))
                                ui.button(
                                    "重置组合",
                                    icon="restart_alt",
                                    on_click=reset_mixing_steps,
                                ).props("flat no-caps color=grey-7")
                            with ui.row().classes("w-full gap-1 flex-wrap"):
                                for node_id in active_ids:
                                    ui.badge(options[node_id], color="blue-grey-6").props("outline")

                        if self.mixing_steps:
                            with ui.card().classes("w-full p-3 rounded-xl shadow-sm"):
                                ui.label("组合层").classes("text-lg font-bold text-slate-800")
                                for index, step in enumerate(self.mixing_steps, start=1):
                                    with ui.card().classes("w-full p-2 gap-1 bg-slate-50 shadow-none border"):
                                        ui.label(f"第 {index} 层 · {step['name']}").classes(
                                            "font-semibold text-slate-700"
                                        )
                                        summary_parts = mixing_step_summary_parts(step, nodes)
                                        with ui.grid().classes("w-full grid-cols-3 items-center gap-2"):
                                            first_label = ui.label(summary_parts[0]).classes(
                                                "w-full min-w-0 text-center text-sm text-blue-800"
                                            )
                                            coordinate_label = ui.label(summary_parts[1]).classes(
                                                "w-full min-w-0 text-center text-sm text-blue-800"
                                            )
                                            second_label = ui.label(summary_parts[2]).classes(
                                                "w-full min-w-0 text-center text-sm text-blue-800"
                                            )
                                        wavelength_label = ui.label(summary_parts[3]).classes(
                                            "w-full text-center text-xs text-slate-600"
                                        )
                                        mixing_step_labels[str(step["id"])] = (
                                            first_label,
                                            coordinate_label,
                                            second_label,
                                            wavelength_label,
                                        )
                                        ui.slider(
                                            min=0,
                                            max=100,
                                            step=1,
                                            value=_nonnegative_number(step.get("ratio"), 50.0),
                                            on_change=make_mixing_ratio_handler(str(step["id"])),
                                        )

                        with ui.card().classes("w-full min-h-[520px] p-3 rounded-xl shadow-sm"):
                            ui.label("最终三光谱目标求解").classes("text-lg font-bold text-slate-800")
                            ui.label("活动节点剩余三条时，任何比例、坐标或功率上限变化都会自动重新求解。").classes(
                                "text-xs text-slate-500"
                            )
                            with ui.row().classes("w-full items-center gap-2 flex-wrap mt-1"):
                                ui.number(
                                    "目标 x",
                                    min=0.000001,
                                    max=0.999999,
                                    step=0.0001,
                                ).bind_value(self.mixing_state, "target_x").props(
                                    "outlined dense input-class=text-right"
                                ).classes("w-36").on_value_change(update_mixing_target)
                                ui.number(
                                    "目标 y",
                                    min=0.000001,
                                    max=0.999999,
                                    step=0.0001,
                                ).bind_value(self.mixing_state, "target_y").props(
                                    "outlined dense input-class=text-right"
                                ).classes("w-36").on_value_change(update_mixing_target)
                            render_mixing_solution()

            with splitter.after:
                with ui.column().classes("w-full h-full min-h-0 p-2"):
                    render_mixing_charts()

        async def calculate_spectra() -> None:
            calculate_button.props("loading disable")
            waiting_notification = ui.notification(
                "正在计算光谱、色坐标与显色指数，请稍候……",
                type="ongoing",
                spinner=True,
                timeout=None,
                position="top",
            )
            data_text = str(self.spectral_state.get("data_text") or "").strip()
            coordinate_system = _option_text(
                self.coordinate_state.get("system"),
                set(COORDINATE_SYSTEMS),
                "xy",
            )
            coordinate_text = str(self.coordinate_state.get("data_text") or "").strip()
            previous_power_limits = {
                result.name: _nonnegative_number(
                    self.mixing_power_limits.get(f"source:{index}"),
                    1.0,
                )
                for index, result in enumerate(self.spectrum_results)
            }
            try:
                if not data_text and not coordinate_text:
                    raise SpectralAnalysisError("请至少输入一组光谱数据或一组具体色坐标")
                coordinates = parse_chromaticity_text(coordinate_text, coordinate_system) if coordinate_text else []
                results = await run.cpu_bound(analyze_spectral_text, data_text) if data_text else []
                self.spectrum_results = results
                self.coordinate_results = coordinates
                previous_styles = self.series_styles
                self.series_styles = _default_series_styles(results)
                for name in set(previous_styles) & set(self.series_styles):
                    self.series_styles[name].update(previous_styles[name])
                self.spectral_state["reference_source"] = "none"
                self.spectrum_reference_result = None
                self.cri_state["source_a"] = "input:0" if results else "standard:D50"
                self.cri_state["source_b"] = "input:1" if len(results) > 1 else "standard:D65"
                self.mixing_steps.clear()
                clear_mixing_solution()
                self.mixing_power_limits = {
                    f"source:{index}": previous_power_limits.get(result.name, 1.0)
                    for index, result in enumerate(results)
                }
                self.mixing_state["source_a"] = "source:0" if results else ""
                self.mixing_state["source_b"] = "source:1" if len(results) > 1 else ""
                await load_cri_comparison_sources()
                render_spectrum_results.refresh()
                render_mixing_workspace.refresh()
                if len(results) == 3:
                    schedule_dynamic_mixing_solve()
                workspace_tabs.set_value(analysis_tab)
                ui.notify(
                    f"已完成 {len(results)} 条光谱与 {len(coordinates)} 个手工色坐标的联合计算",
                    type="positive",
                )
            except SpectralAnalysisError as exc:
                ui.notify(str(exc), type="warning")
            except Exception as exc:
                logger.error("光谱分析失败", exc_info=True)
                ui.notify(f"光谱分析失败：{exc}", type="negative")
            finally:
                waiting_notification.dismiss()
                calculate_button.props(remove="loading disable")

        def load_spectral_example() -> None:
            try:
                self.spectral_state["data_text"] = spectral_example_text()
            except SpectralAnalysisError as exc:
                ui.notify(str(exc), type="negative")
                return
            spectral_textarea.update()
            ui.notify("已载入 D65 与标准 A 光源示例", type="info")

        def clear_spectra() -> None:
            self.spectral_state["data_text"] = ""
            self.spectral_state["reference_source"] = "none"
            self.spectrum_results = []
            self.spectrum_reference_result = None
            self.cri_comparison_results = None
            self.mixing_steps.clear()
            clear_mixing_solution()
            self.mixing_power_limits.clear()
            spectral_textarea.update()
            render_spectrum_results.refresh()
            render_mixing_workspace.refresh()

        def clear_coordinates() -> None:
            self.coordinate_state["data_text"] = ""
            self.coordinate_results = []
            combined_coordinate_textarea.update()
            render_spectrum_results.refresh()

        with ui.column().classes("w-full h-full p-0 gap-0 bg-slate-50"):
            with ui.row().classes("w-full bg-white px-5 py-3 border-b items-center justify-between shadow-sm"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("science", size="34px").classes("text-blue-700")
                    ui.label("光谱色度与显色分析").classes("text-xl font-bold text-slate-800")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round").tooltip("关闭")

            workspace_tabs = ui.tabs().classes("w-full bg-white text-slate-600")
            with workspace_tabs:
                input_tab = ui.tab("数据录入", icon="edit_note")
                analysis_tab = ui.tab("分析结果", icon="query_stats")
                mixing_tab = ui.tab("光谱混合", icon="device_hub")

            with ui.tab_panels(workspace_tabs, value=input_tab).classes("w-full flex-1 min-h-0 bg-slate-50"):
                with ui.tab_panel(input_tab).classes("p-0"):
                    with ui.scroll_area().classes("w-full h-[calc(100vh-140px)]"):
                        with ui.column().classes("w-full max-w-[1800px] mx-auto p-2 gap-3"):
                            with ui.grid().classes("w-full grid-cols-1 lg:grid-cols-12 gap-2 items-stretch"):
                                with ui.card().classes("lg:col-span-7 w-full h-full p-4 rounded-xl shadow-sm"):
                                    ui.label("1. 光谱数据（可选）").classes("text-lg font-bold text-slate-800")
                                    ui.label("首列为波长，后续每列为一条光谱；支持 Excel、CSV 和空白分隔。").classes(
                                        "text-xs text-slate-500 mb-1"
                                    )
                                    spectral_textarea = (
                                        ui.textarea(
                                            "波长 / 光谱值",
                                            placeholder=(
                                                "波长(nm)\t样品A\t样品B\n"
                                                "360\t0.01\t0.02\n"
                                                "361\t0.02\t0.03\n"
                                                "...\n780\t0.01\t0.01"
                                            ),
                                        )
                                        .bind_value(self.spectral_state, "data_text")
                                        .props(
                                            "outlined rows=15 input-style='font-family: monospace; white-space: pre'"
                                        )
                                        .classes("w-full")
                                    )
                                    ui.label("显色计算至少覆盖 380–780 nm；曲线图仅显示该可见光范围。").classes(
                                        "text-xs text-amber-700"
                                    )

                                with ui.card().classes("lg:col-span-5 w-full h-full p-4 rounded-xl shadow-sm"):
                                    ui.label("2. 具体色坐标（可选）").classes("text-lg font-bold text-slate-800")
                                    ui.label("可单独计算色坐标，也可叠加到光谱结果的同一张 CIE 图中。").classes(
                                        "text-xs text-slate-500 mb-1"
                                    )
                                    ui.select(
                                        COORDINATE_SYSTEMS,
                                        label="输入坐标类型",
                                    ).bind_value(self.coordinate_state, "system").props(
                                        "outlined dense options-dense"
                                    ).classes("w-full")
                                    combined_coordinate_textarea = (
                                        ui.textarea(
                                            "名称 / 坐标值",
                                            placeholder=(
                                                "名称\tx\ty\n目标白点\t0.3127\t0.3290\n实测色点\t0.3200\t0.3350"
                                            ),
                                        )
                                        .bind_value(self.coordinate_state, "data_text")
                                        .props(
                                            "outlined rows=12 input-style='font-family: monospace; white-space: pre'"
                                        )
                                        .classes("w-full")
                                    )
                                    ui.label("光谱与色坐标至少填写其中一项。").classes("text-xs text-blue-800")

                                with ui.card().classes("lg:col-span-12 w-full p-3 rounded-xl shadow-sm"):
                                    with ui.row().classes("w-full items-center justify-between gap-3 flex-wrap"):
                                        with ui.column().classes("gap-0"):
                                            ui.label("3. 运行联合分析").classes("text-lg font-bold text-slate-800")
                                            ui.label("计算完成后将自动切换到分析结果页。").classes(
                                                "text-xs text-slate-500"
                                            )
                                        with ui.row().classes("items-center gap-2 flex-wrap"):
                                            ui.button(
                                                "载入示例", icon="lightbulb", on_click=load_spectral_example
                                            ).props("outline no-caps")
                                            ui.button("清空光谱", icon="delete_outline", on_click=clear_spectra).props(
                                                "flat no-caps color=grey-7"
                                            )
                                            ui.button(
                                                "清空坐标", icon="location_off", on_click=clear_coordinates
                                            ).props("flat no-caps color=grey-7")
                                            calculate_button = ui.button(
                                                "联合计算",
                                                icon="calculate",
                                                on_click=calculate_spectra,
                                            ).props("unelevated no-caps color=blue-8")

                with ui.tab_panel(analysis_tab).classes("p-0"):
                    with ui.scroll_area().classes("w-full h-[calc(100vh-140px)]"):
                        with ui.column().classes("w-full max-w-[1900px] mx-auto p-2 gap-3"):
                            with ui.card().classes("w-full p-3 rounded-xl shadow-sm"):
                                render_spectrum_results()

                with ui.tab_panel(mixing_tab).classes("p-0 h-[calc(100vh-140px)] min-h-0"):
                    render_mixing_workspace()
