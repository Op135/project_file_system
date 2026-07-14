# -*- encoding: utf-8 -*-
"""面向光学工程师的光谱、色度与显色对比工具。"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from nicegui import run, ui

from .spectral_analysis import (
    COORDINATE_SYSTEMS,
    STANDARD_ILLUMINANTS,
    ChromaticityResult,
    SpectralAnalysisError,
    SpectrumResult,
    analyze_cct_reference,
    analyze_spectral_text,
    analyze_standard_illuminant,
    chromaticity_background_image,
    chromaticity_loci,
    parse_chromaticity_text,
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


def _render_cie_chart(options: dict[str, Any], viewport_offset: int = 245) -> None:
    """以正方形容器渲染支持原生二维缩放和平移的 CIE 图。"""

    (
        ui.echart(options)
        .classes("mx-auto")
        .style(
            f"width: min(100%, calc(100vh - {viewport_offset}px)); "
            "aspect-ratio: 1 / 1; min-width: 680px; min-height: 680px; cursor: grab;"
        )
    )


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
            const data = params.data || {{}};
            const value = data.value || params.value || [];
            const title = data.title || data.name || params.name || params.seriesName;
            let html = `<b>${{title}}</b>`;
            html += `<br/>{first_axis}: ${{Number(value[0]).toFixed(6)}}`;
            html += `<br/>{second_axis}: ${{Number(value[1]).toFixed(6)}}`;
            {cri_line}
            return html;
        }}
    """


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


def _default_series_styles(results: list[SpectrumResult]) -> dict[str, dict[str, str]]:
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


def _spectrum_summary_rows(results: list[SpectrumResult]) -> list[dict[str, Any]]:
    """生成综合指标表数据。"""

    return [
        {
            "name": item.name,
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


def _spectrum_chart_options(
    results: list[SpectrumResult],
    normalized: bool = True,
    reference_result: SpectrumResult | None = None,
    series_styles: dict[str, dict[str, str]] | None = None,
    x_axis_interval: float = 50.0,
    y_axis_interval: float = 0.0,
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
    return {
        "animation": False,
        "color": CHART_COLORS,
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": _legend_options(
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
        ),
        "grid": {"left": 72, "right": 40, "top": 110, "bottom": 78},
        "toolbox": {
            "right": 10,
            "top": 5,
            "feature": {"saveAsImage": {}, "dataZoom": {}, "restore": {}},
        },
        "xAxis": {
            "type": "value",
            "name": "波长 (nm)",
            "nameLocation": "middle",
            "nameGap": 34,
            "min": 380,
            "max": 780,
            **({"splitNumber": x_split_number} if x_split_number is not None else {}),
        },
        "yAxis": {
            "type": "value",
            "name": "相对强度" if normalized else "输入值",
            "min": 0,
            **({"splitNumber": y_split_number} if y_split_number is not None else {}),
        },
        "dataZoom": [
            {"type": "inside", "filterMode": "none"},
            {"type": "slider", "height": 20, "bottom": 24, "filterMode": "none"},
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


def _chromaticity_chart_options(
    spectrum_results: list[SpectrumResult] | None = None,
    coordinate_results: list[ChromaticityResult] | None = None,
    *,
    coordinate_system: str = "xy",
    series_styles: dict[str, dict[str, str]] | None = None,
    axis_interval: float = 0.1,
) -> dict[str, Any]:
    """生成带颜色背景的轨迹、光谱点与手工坐标联合色度图。"""

    spectrum_results = spectrum_results or []
    coordinate_results = coordinate_results or []
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
            "z": 3,
            "lineStyle": {"color": "#111827", "width": 2.5, "type": "solid"},
            "data": [list(point) for point in planckian_locus],
        },
    ]
    all_results: list[SpectrumResult | ChromaticityResult] = [*spectrum_results, *coordinate_results]
    for index, item in enumerate(all_results):
        point = item.xy if is_xy else item.upvp
        is_spectrum = isinstance(item, SpectrumResult)
        symbol, color = _series_style(item.name, series_styles, index)
        if not is_spectrum:
            symbol = "triangle"
        series.append(
            {
                "name": item.name,
                "type": "scatter",
                "symbol": symbol,
                "symbolSize": 13,
                "z": 5,
                "itemStyle": {
                    "color": color,
                    "borderColor": "#ffffff",
                    "borderWidth": 2,
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
    return {
        "animation": False,
        "tooltip": {
            "trigger": "item",
            "confine": True,
            ":formatter": _chromaticity_tooltip(coordinate_system),
        },
        "legend": _legend_options(
            [
                "光谱轨迹",
                "普朗克轨迹",
                *[
                    {
                        "name": item.name,
                        "icon": (
                            _series_style(item.name, series_styles, index)[0]
                            if isinstance(item, SpectrumResult)
                            else "triangle"
                        ),
                    }
                    for index, item in enumerate(all_results)
                ],
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
            "type": "value",
            "name": "x" if is_xy else "u′",
            "nameLocation": "middle",
            "nameGap": 32,
            "min": 0,
            "max": axis_max,
            **({"splitNumber": split_number} if split_number is not None else {}),
        },
        "yAxis": {
            "type": "value",
            "name": "y" if is_xy else "v′",
            "min": 0,
            "max": axis_max,
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
            "z": 3,
            "lineStyle": {"color": "#111827", "width": 2.5, "type": "solid"},
            "data": [list(point) for point in planckian_locus],
        },
    ]
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
                "symbolSize": 18,
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
                    "symbolSize": 11,
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
        "tooltip": {
            "trigger": "item",
            "confine": True,
            ":formatter": _chromaticity_tooltip(coordinate_system, include_cri=True),
        },
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
            "type": "value",
            "name": "x" if is_xy else "u′",
            "nameLocation": "middle",
            "nameGap": 32,
            "min": 0,
            "max": axis_max,
            **({"splitNumber": split_number} if split_number is not None else {}),
        },
        "yAxis": {
            "type": "value",
            "name": "y" if is_xy else "v′",
            "min": 0,
            "max": axis_max,
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
        }
        self.cri_state: dict[str, Any] = {
            "source_a": "",
            "source_b": "standard:D65",
        }
        self.chart_state: dict[str, Any] = {
            "spectrum_x_interval": 50.0,
            "spectrum_y_interval": 0.0,
            "xy_interval": 0.1,
            "upvp_interval": 0.1,
        }
        self.series_styles: dict[str, dict[str, str]] = {}
        self.spectrum_results: list[SpectrumResult] = []
        self.coordinate_results: list[ChromaticityResult] = []
        self.cri_comparison_results: tuple[SpectrumResult, SpectrumResult] | None = None
        self.spectrum_reference_result: SpectrumResult | None = None

    def show(self, dialog: ui.dialog) -> None:
        """渲染光谱分析工具界面。"""

        if self.spectrum_results and not self.series_styles:
            self.series_styles = _default_series_styles(self.spectrum_results)

        @ui.refreshable
        def render_spectrum_chart() -> None:
            normalized = bool(self.spectral_state.get("normalized", True))
            ui.echart(
                _spectrum_chart_options(
                    self.spectrum_results,
                    normalized,
                    self.spectrum_reference_result,
                    self.series_styles,
                    _nonnegative_number(self.chart_state.get("spectrum_x_interval"), 50.0),
                    _nonnegative_number(self.chart_state.get("spectrum_y_interval"), 0.0),
                )
            ).classes("w-full h-[calc(100vh-245px)] min-h-[680px]")

        @ui.refreshable
        def render_chromaticity_view() -> None:
            ui.label(
                "光谱点与手工坐标已叠加；背景色仅为屏幕近似效果。滚轮等比例缩放，按住左键可任意方向平移。"
            ).classes("text-xs text-slate-500 mb-2")
            cie_tabs = ui.tabs().classes("w-full text-blue-700")
            with cie_tabs:
                cie_xy_tab = ui.tab("CIE 1931 xy")
                cie_upvp_tab = ui.tab("CIE 1976 u′v′")
            with ui.tab_panels(cie_tabs, value=cie_xy_tab).classes("w-full"):
                with ui.tab_panel(cie_xy_tab).classes("p-0"):
                    _render_cie_chart(
                        _chromaticity_chart_options(
                            spectrum_results=self.spectrum_results,
                            coordinate_results=self.coordinate_results,
                            coordinate_system="xy",
                            series_styles=self.series_styles,
                            axis_interval=_nonnegative_number(self.chart_state.get("xy_interval"), 0.1),
                        )
                    )
                with ui.tab_panel(cie_upvp_tab).classes("p-0"):
                    _render_cie_chart(
                        _chromaticity_chart_options(
                            spectrum_results=self.spectrum_results,
                            coordinate_results=self.coordinate_results,
                            coordinate_system="upvp",
                            series_styles=self.series_styles,
                            axis_interval=_nonnegative_number(self.chart_state.get("upvp_interval"), 0.1),
                        )
                    )

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
                        ),
                        viewport_offset=300,
                    )

        @ui.refreshable
        def render_spectrum_results() -> None:
            if not self.spectrum_results:
                with ui.column().classes("w-full h-[520px] items-center justify-center text-slate-400 gap-3"):
                    ui.icon("query_stats", size="64px")
                    ui.label("粘贴光谱后点击“联合计算”").classes("text-lg")
                    ui.label("支持共享波长列和最多 12 列光谱值").classes("text-sm")
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
                    columns = [
                        {"name": "name", "label": "光谱", "field": "name", "align": "left"},
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
                    ui.label("XYZ 已按 Y=100 归一化；Ra 仍取 R1–R8 平均，R15 标注为 JIS 扩展。").classes(
                        "text-xs text-slate-500 mt-2"
                    )
                    if self.coordinate_results:
                        ui.label("手工输入色坐标").classes("text-base font-bold text-slate-700 mt-4")
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
                    with ui.row().classes("w-full items-center justify-between gap-3"):
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

        async def calculate_spectra() -> None:
            calculate_button.props("loading disable")
            waiting_notification = ui.notification(
                "正在计算光谱、色坐标与显色指数，请稍候……",
                type="ongoing",
                spinner=True,
                timeout=None,
                position="top",
            )
            data_text = str(self.spectral_state.get("data_text") or "")
            coordinate_system = _option_text(
                self.coordinate_state.get("system"),
                set(COORDINATE_SYSTEMS),
                "xy",
            )
            coordinate_text = str(self.coordinate_state.get("data_text") or "").strip()
            try:
                coordinates = parse_chromaticity_text(coordinate_text, coordinate_system) if coordinate_text else []
                results = await run.cpu_bound(analyze_spectral_text, data_text)
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
                await load_cri_comparison_sources()
                render_spectrum_results.refresh()
                input_expansion.set_value(False)
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
            spectral_textarea.update()
            render_spectrum_results.refresh()

        def clear_coordinates() -> None:
            self.coordinate_state["data_text"] = ""
            self.coordinate_results = []
            combined_coordinate_textarea.update()
            render_spectrum_results.refresh()

        with ui.column().classes("w-full h-full p-0 gap-0 bg-slate-50"):
            with ui.row().classes("w-full bg-white px-5 py-3 border-b items-center justify-between shadow-sm"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("science", size="34px").classes("text-blue-700")
                    with ui.column().classes("gap-0"):
                        ui.label("光谱色度与显色分析").classes("text-xl font-bold text-slate-800")
                        ui.label("CCT · Duv · CIE CRI · CIE Rf · 多光谱/色坐标对比").classes("text-xs text-slate-500")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round").tooltip("关闭")

            with ui.scroll_area().classes("w-full h-[calc(100vh-68px)]"):
                with ui.column().classes("w-full max-w-[1900px] mx-auto p-3 gap-3"):
                    with ui.expansion("数据录入", icon="input", value=True).classes(
                        "w-full bg-white rounded-xl shadow-sm border border-slate-200"
                    ) as input_expansion:
                        with ui.grid().classes("w-full grid-cols-1 xl:grid-cols-2 gap-4 p-3"):
                            with ui.column().classes("w-full gap-2"):
                                ui.label("光谱数据").classes("text-lg font-bold text-slate-800")
                                ui.label("首列为波长，后续每列为一条光谱；支持 Excel、CSV 和空白分隔。").classes(
                                    "text-xs text-slate-500"
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
                                    .props("outlined rows=12 input-style='font-family: monospace; white-space: pre'")
                                    .classes("w-full")
                                )
                                ui.label("显色计算至少覆盖 380–780 nm；曲线图仅显示该可见光范围。").classes(
                                    "text-xs text-amber-700"
                                )
                            with ui.column().classes("w-full gap-2"):
                                ui.label("叠加具体色坐标（可选）").classes("text-lg font-bold text-slate-800")
                                ui.select(
                                    COORDINATE_SYSTEMS,
                                    label="输入坐标类型",
                                ).bind_value(self.coordinate_state, "system").props(
                                    "outlined dense options-dense"
                                ).classes("w-full max-w-sm")
                                combined_coordinate_textarea = (
                                    ui.textarea(
                                        "名称 / 坐标值",
                                        placeholder=("名称\tx\ty\n目标白点\t0.3127\t0.3290\n实测色点\t0.3200\t0.3350"),
                                    )
                                    .bind_value(self.coordinate_state, "data_text")
                                    .props("outlined rows=9 input-style='font-family: monospace; white-space: pre'")
                                    .classes("w-full")
                                )
                                ui.label("这些点会直接叠加到光谱结果的同一张 CIE 图中。").classes(
                                    "text-xs text-blue-800"
                                )
                        with ui.row().classes("w-full px-3 pb-3 gap-2 justify-end"):
                            ui.button("载入示例", icon="lightbulb", on_click=load_spectral_example).props(
                                "outline no-caps"
                            )
                            ui.button("清空光谱", icon="delete_outline", on_click=clear_spectra).props(
                                "flat no-caps color=grey-7"
                            )
                            ui.button("清空坐标", icon="location_off", on_click=clear_coordinates).props(
                                "flat no-caps color=grey-7"
                            )
                            calculate_button = ui.button(
                                "联合计算",
                                icon="calculate",
                                on_click=calculate_spectra,
                            ).props("unelevated no-caps color=blue-8")

                    with ui.card().classes("w-full p-3 rounded-xl shadow-sm"):
                        render_spectrum_results()
