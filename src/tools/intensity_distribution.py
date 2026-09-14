"""垂直测量平面照度到远场等效光强的换算与剖面分析。"""
from __future__ import annotations

import io
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from nicegui import run, ui
from src.custom_ui import CustomUploadRemovedEventArguments, custom_upload

from src.tools.pixel_statistics import (
    MAX_UPLOAD_BYTES, SUPPORTED_EXTENSIONS, PixelStatisticsSettings,
    _ExcelBytesBuffer, _read_csv_dataframe, _safe_sheet_name,
    dataframe_to_numeric_matrix, locate_center, merge_pixel_blocks,
)


@dataclass(frozen=True)
class IntensitySettings:
    scale_pixels: float = 1.0
    scale_length_mm: float = 1.0
    distance_mm: float = 1000.0
    lux_factor: float | None = 1.0
    irradiance_factor: float | None = None
    center_mode: str = "threshold"
    center_threshold_percent: float = 10.0
    manual_row: float = 1.0
    manual_col: float = 1.0
    target_percent: float = 50.0
    merge_enabled: bool = False
    granularity: int = 10

    @property
    def radiometric(self) -> bool:
        return self.irradiance_factor is not None

    @property
    def conversion_factor(self) -> float:
        factor = self.irradiance_factor if self.radiometric else self.lux_factor
        if factor is None:
            raise ValueError("必须填写且只能填写一种数据换算系数")
        return factor

    @property
    def output_quantity(self) -> str:
        return "辐射强度" if self.radiometric else "光强"

    @property
    def output_unit(self) -> str:
        return "mW/sr" if self.radiometric else "cd"

    def validate(self) -> None:
        for label, value in (
            ("原始像素点数", self.scale_pixels), ("比例尺长度", self.scale_length_mm),
            ("测量距离", self.distance_mm),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label}必须为有限正数")
        factors = (self.lux_factor, self.irradiance_factor)
        if sum(value is not None for value in factors) != 1:
            raise ValueError("lx 与 mW/cm² 换算系数必须且只能填写一个")
        factor = self.conversion_factor
        if not math.isfinite(factor) or factor <= 0:
            raise ValueError("数据换算系数必须为有限正数")
        if not 0 < self.target_percent <= 100:
            raise ValueError("目标强度百分比必须大于 0 且不超过 100")
        if not 0 < self.center_threshold_percent <= 100:
            raise ValueError("定位阈值百分比必须大于 0 且不超过 100")
        if self.center_mode not in {"geometric", "maximum", "manual", "threshold"}:
            raise ValueError("未知的中心算法")
        if isinstance(self.granularity, bool) or not isinstance(self.granularity, int) or self.granularity < 1:
            raise ValueError("单元格合并颗粒度必须是大于 0 的整数")
        if not all(math.isfinite(v) for v in (self.manual_row, self.manual_col)):
            raise ValueError("中心行列必须为有限数值")


@dataclass
class IntensityProfile:
    name: str
    angles: np.ndarray
    values: np.ndarray
    negative_angle: float | None
    positive_angle: float | None

    @property
    def width(self) -> float | None:
        if self.negative_angle is None or self.positive_angle is None:
            return None
        return self.positive_angle - self.negative_angle


@dataclass
class IntensityResult:
    filename: str
    sheet: str
    center_row: float
    center_col: float
    center_description: str
    center_intensity: float
    target_intensity: float
    intensity: np.ndarray
    horizontal: IntensityProfile
    vertical: IntensityProfile
    source_rows: int
    source_cols: int
    output_quantity: str = "光强"
    output_unit: str = "cd"
    merge_description: str = "未合并"

    @property
    def target_label(self) -> str:
        return f"目标{self.output_quantity}"

    def summary(self, target_percents: tuple[float, ...] | None = None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "文件": self.filename, "工作表": self.sheet,
            "原始尺寸": f"{self.source_rows}×{self.source_cols}",
            "处理后尺寸": f"{self.intensity.shape[0]}×{self.intensity.shape[1]}",
            "单元格处理": self.merge_description,
            "中心行": self.center_row + 1, "中心列": self.center_col + 1,
            f"中心{self.output_quantity}({self.output_unit})": self.center_intensity,
        }
        if target_percents is None:
            target_percents = (self.target_intensity / self.center_intensity * 100,)
        for percent in target_percents:
            target = self.center_intensity * percent / 100
            prefix = f"目标{percent:g}%"
            row[f"{prefix}{self.output_quantity}({self.output_unit})"] = target
            for profile in (self.horizontal, self.vertical):
                negative, positive = profile_crossings(profile, target)
                row[f"{prefix}{profile.name}负向角度(°)"] = negative
                row[f"{prefix}{profile.name}正向角度(°)"] = positive
                row[f"{prefix}{profile.name}夹角(°)"] = (
                    None if negative is None or positive is None else positive - negative
                )
        return row


@dataclass
class IntensityBatch:
    settings: IntensitySettings
    results: list[IntensityResult]
    errors: list[str]


def first_crossing(angles: np.ndarray, values: np.ndarray, target: float) -> float | None:
    """输入从中心向外排列；只返回第一次向下穿越，不跨越测量边界。"""
    if not len(values):
        return None
    if values[0] == target:
        return float(angles[0])
    for index in range(1, len(values)):
        before, after = float(values[index - 1]), float(values[index])
        if before >= target and after <= target:
            fraction = (before - target) / (before - after) if before != after else 0.0
            return float(angles[index - 1] + fraction * (angles[index] - angles[index - 1]))
    return None


def profile_crossings(profile: IntensityProfile, target: float) -> tuple[float | None, float | None]:
    """基于已计算的完整剖面快速重算指定强度的两侧首次下降交点。"""
    zero = int(np.searchsorted(profile.angles, 0.0))
    return (
        first_crossing(profile.angles[:zero + 1][::-1], profile.values[:zero + 1][::-1], target),
        first_crossing(profile.angles[zero:], profile.values[zero:], target),
    )


def _validated_target_percents(values: tuple[float, ...]) -> tuple[float, ...]:
    raw = tuple(dict.fromkeys(float(value) for value in values))
    if any(not math.isfinite(value) or value < 0 or value > 100 for value in raw):
        raise ValueError("目标强度百分比只能为 0，或大于 0 且不超过 100")
    return tuple(value for value in raw if value > 0)


def target_angle_rows(result: IntensityResult,
                      target_percents: tuple[float, ...]) -> list[dict[str, Any]]:
    """不重算强度矩阵，仅基于完整剖面生成多个目标比例的角度结果。"""
    rows: list[dict[str, Any]] = []
    for percent in _validated_target_percents(target_percents):
        target = result.center_intensity * percent / 100
        row: dict[str, Any] = {
            "目标比例(%)": percent,
            f"目标{result.output_quantity}({result.output_unit})": target,
        }
        for profile in (result.horizontal, result.vertical):
            negative, positive = profile_crossings(profile, target)
            row[f"{profile.name}负向角度(°)"] = negative
            row[f"{profile.name}正向角度(°)"] = positive
            row[f"{profile.name}夹角(°)"] = (
                None if negative is None or positive is None else positive - negative
            )
        rows.append(row)
    return rows


def make_profile(name: str, illuminance: np.ndarray, center: float,
                 center_lux: float, settings: IntensitySettings, target: float) -> IntensityProfile:
    positions = np.arange(len(illuminance), dtype=float)
    # 亚像素中心在两个剖面中都需保留一个真实的零角度采样点。
    positions = np.unique(np.append(positions, center))
    lux = np.interp(positions, np.arange(len(illuminance)), illuminance)
    zero = int(np.searchsorted(positions, center))
    lux[zero] = center_lux
    offsets_m = (positions - center) * settings.scale_length_mm / settings.scale_pixels / 1000
    distance_m = settings.distance_mm / 1000
    radians = np.arctan2(offsets_m, distance_m)
    angles = np.degrees(radians)
    values = lux * distance_m**2 / np.cos(radians)**3
    return IntensityProfile(
        name, angles, values,
        first_crossing(angles[:zero + 1][::-1], values[:zero + 1][::-1], target),
        first_crossing(angles[zero:], values[zero:], target),
    )


def analyze_intensity(matrix: np.ndarray, filename: str, sheet: str,
                      settings: IntensitySettings) -> IntensityResult:
    settings.validate()
    if matrix.ndim != 2 or min(matrix.shape) < 2:
        raise ValueError("水平和垂直剖面要求至少 2 行、2 列数据")
    if not np.isfinite(matrix).all() or (matrix < 0).any():
        raise ValueError("照度数据必须为有限非负数，不能含空白或非数字")
    source_rows, source_cols = matrix.shape
    processed = matrix
    merge_description = "未合并"
    effective_scale_pixels = settings.scale_pixels
    if settings.merge_enabled:
        processed, merge_description = merge_pixel_blocks(
            matrix,
            settings.granularity,
            False,
            False,
        )
        effective_scale_pixels /= settings.granularity
        if min(processed.shape) < 2:
            raise ValueError(
                f"按 {settings.granularity}×{settings.granularity} 合并后仅剩 "
                f"{processed.shape[0]}×{processed.shape[1]} 网格，无法提取水平和垂直剖面"
            )
    processed_settings = replace(
        settings,
        scale_pixels=effective_scale_pixels,
        merge_enabled=False,
    )
    center_settings = PixelStatisticsSettings(
        center_mode=settings.center_mode, threshold_percent=settings.center_threshold_percent,
        manual_center_row=settings.manual_row, manual_center_col=settings.manual_col,
        region_mode="full",
    )
    row, col, description, _ = locate_center(processed, center_settings)
    # 辐照度由 mW/cm² 换算到 mW/m² 后，所得辐射强度单位为 mW/sr。
    plane_values = processed * settings.conversion_factor * (10_000 if settings.radiometric else 1)
    r0, c0 = math.floor(row), math.floor(col)
    r1, c1 = min(r0 + 1, processed.shape[0] - 1), min(c0 + 1, processed.shape[1] - 1)
    horizontal_plane = (plane_values[r0, :] * (1 - (row - r0))
                        + plane_values[r1, :] * (row - r0))
    vertical_plane = (plane_values[:, c0] * (1 - (col - c0))
                      + plane_values[:, c1] * (col - c0))
    center_plane = float(np.interp(col, np.arange(processed.shape[1]), horizontal_plane))
    distance_m = settings.distance_mm / 1000
    center_intensity = center_plane * distance_m**2
    if not math.isfinite(center_intensity) or center_intensity <= 0:
        raise ValueError(f"中心{settings.output_quantity}必须为有限正数，请检查中心、换算系数和距离")
    target = center_intensity * settings.target_percent / 100
    step_m = processed_settings.scale_length_mm / processed_settings.scale_pixels / 1000
    y = (np.arange(processed.shape[0]) - row)[:, None] * step_m
    x = (np.arange(processed.shape[1]) - col)[None, :] * step_m
    # 平面照度关系：E = I cos(theta)/r²，r² = d²+x²+y²，cos(theta)=d/r。
    intensity = plane_values * np.power(distance_m**2 + x*x + y*y, 1.5) / distance_m
    horizontal = make_profile("水平", horizontal_plane, col, center_plane, processed_settings, target)
    vertical = make_profile("垂直", vertical_plane, row, center_plane, processed_settings, target)
    if not all(np.isfinite(a).all() for a in (intensity, horizontal.values, vertical.values)):
        raise ValueError("换算结果溢出，请检查比例尺、距离与照度系数")
    return IntensityResult(
        filename,
        sheet,
        row,
        col,
        description,
        center_intensity,
        target,
        intensity,
        horizontal,
        vertical,
        source_rows,
        source_cols,
        settings.output_quantity,
        settings.output_unit,
        merge_description,
    )


def analyze_files(files: dict[str, bytes], settings: IntensitySettings) -> IntensityBatch:
    settings.validate()
    batch = IntensityBatch(settings, [], [])
    for filename, content in files.items():
        try:
            suffix = Path(filename).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS or len(content) > MAX_UPLOAD_BYTES:
                raise ValueError("文件格式不支持或超过 50 MB")
            workbook = ({"数据": _read_csv_dataframe(content)} if suffix in {".csv", ".cvs"}
                        else pd.read_excel(io.BytesIO(content), sheet_name=None, header=None))
        except Exception as exc:
            batch.errors.append(f"{filename}：{exc}")
            continue
        for sheet, frame in workbook.items():
            try:
                matrix, _ = dataframe_to_numeric_matrix(frame, "strict")
                batch.results.append(analyze_intensity(matrix, filename, str(sheet), settings))
            except Exception as exc:
                batch.errors.append(f"{filename} / {sheet}：{exc}")
    return batch


def distribution_chart(result: IntensityResult, polar: bool = False,
                       normalized: bool = False,
                       target_percents: tuple[float, ...] | None = None) -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    angle_span = max(float(np.max(np.abs(p.angles))) for p in (result.horizontal, result.vertical))
    angle_span = max(angle_span, 0.001)
    value_scale = 100 / result.center_intensity if normalized else 1.0
    value_unit = "%" if normalized else result.output_unit
    value_axis_name = "相对中心(%)" if normalized else f"{result.output_quantity}({result.output_unit})"
    if target_percents is None:
        target_percents = (result.target_intensity / result.center_intensity * 100,)
    target_percents = _validated_target_percents(target_percents)

    def coordinate(angle: float, value: float) -> list[float]:
        return [value, angle] if polar else [angle, value]

    profile_colors = {"水平": "#2563eb", "垂直": "#ea580c"}
    profile_keys = {"水平": "horizontal", "垂直": "vertical"}
    for profile, color in ((result.horizontal, "#2563eb"), (result.vertical, "#ea580c")):
        # 图形抽样控制绘制开销，交点计算使用完整采样点。
        indices = np.unique(np.linspace(0, len(profile.angles) - 1,
                                        min(800, len(profile.angles)), dtype=int))
        indices = np.unique(np.append(indices, int(np.argmin(np.abs(profile.angles)))))
        data = [coordinate(float(profile.angles[i]), float(profile.values[i] * value_scale))
                for i in indices]
        series.append({
            "id": f"profile-{profile_keys[profile.name]}",
            "name": profile.name, "type": "line", "showSymbol": False, "smooth": False,
            "coordinateSystem": "polar" if polar else "cartesian2d",
            "data": data, "itemStyle": {"color": color}, "lineStyle": {"color": color},
        })

    target_colors = ("#64748b", "#16a34a", "#7c3aed", "#0891b2")
    target_labels: list[str] = []
    for target_index, percent in enumerate(target_percents):
        target_value = result.center_intensity * percent / 100 * value_scale
        target_label = f"目标{result.output_quantity} {percent:g}%"
        target_labels.append(target_label)
        target_color = target_colors[target_index % len(target_colors)]
        line_type = "dashed" if target_index % 2 == 0 else "dotted"
        for profile in (result.horizontal, result.vertical):
            profile_key = profile_keys[profile.name]
            negative, positive = profile_crossings(
                profile, result.center_intensity * percent / 100
            )
            crossings = [value for value in (negative, positive) if value is not None]
            label_offset = -12 if profile.name == "水平" else 12
            series.append({
                "id": f"target-{target_index}-{profile_key}-points",
                "name": target_label, "type": "scatter", "symbolSize": 9,
                "coordinateSystem": "polar" if polar else "cartesian2d",
                "data": [{
                    "value": coordinate(crossing, target_value),
                    "label": {
                        "formatter": f"{percent:g}% {profile.name} {crossing:.2f}°",
                        "position": "left" if crossing < 0 else "right",
                        "offset": [0, label_offset],
                    },
                } for crossing in crossings],
                "label": {"show": True, "distance": 8, "fontSize": 11,
                          "backgroundColor": "rgba(255,255,255,0.78)", "padding": 2},
                "itemStyle": {"color": profile_colors[profile.name]},
                "tooltip": {"show": False},
            })
            for crossing_index, crossing in enumerate(crossings):
                series.append({
                    "id": f"target-{target_index}-{profile_key}-angle-{crossing_index}",
                    "name": target_label, "type": "line", "showSymbol": False, "silent": True,
                    "coordinateSystem": "polar" if polar else "cartesian2d",
                    "data": [coordinate(crossing, 0.0), coordinate(crossing, target_value)],
                    "lineStyle": {"type": line_type, "width": 1,
                                  "color": profile_colors[profile.name], "opacity": 0.65},
                    "tooltip": {"show": False},
                })
        series.append({
            "id": f"target-{target_index}-level",
            "name": target_label, "type": "line", "showSymbol": False, "silent": True,
            "coordinateSystem": "polar" if polar else "cartesian2d",
            "data": [[target_value, float(a)] if polar else [float(a), target_value]
                     for a in np.linspace(-90 if polar else -angle_span,
                                          90 if polar else angle_span, 181)],
            "lineStyle": {"type": line_type, "color": target_color, "width": 1.5},
            "tooltip": {"show": False},
        })
    options: dict[str, Any] = {
        "animation": False,
        "legend": {"top": 5, "data": ["水平", "垂直", *target_labels]},
        "tooltip": {"show": False},
        "series": series,
    }
    if polar:
        options.update({
            "title": {"subtext": value_axis_name, "left": "center", "top": 34,
                      "subtextStyle": {"fontSize": 12, "color": "#6b7280"}},
            "polar": {"center": ["50%", "95%"], "radius": "150%"},
            "angleAxis": {"type": "value", "min": -90, "max": 90,
                          "startAngle": 180, "endAngle": 0, "clockwise": True,
                          "interval": 30, "axisLabel": {"formatter": "{value}°",
                                                        "hideOverlap": True}},
            "radiusAxis": {"type": "value", "min": 0, "splitNumber": 5,
                           "axisLabel": {"hideOverlap": True, "fontSize": 10}},
        })
    else:
        options.update({
            "grid": {"left": 80, "right": 30, "top": 65, "bottom": 65},
            "xAxis": {"type": "value", "min": -angle_span, "max": angle_span, "name": "角度(°)",
                      "nameLocation": "middle", "nameGap": 30,
                      "axisLabel": {":formatter": "v => String(Number(v.toFixed(3)))"}},
            "yAxis": {"type": "value", "min": 0, "name": value_axis_name},
        })
    return options


def _distribution_interaction_setup_js(chart_id: int, polar: bool,
                                       value_unit: str, hover_mode: str) -> str:
    """生成曲线/标记图例联动及笛卡尔曲线吸附辅助线的客户端脚本。"""
    if hover_mode not in {"angle_to_strength", "strength_to_angle"}:
        raise ValueError("未知的悬停取点模式")
    cartesian_setup = ""
    if not polar:
        cartesian_setup = f"""
            root.style.position = 'relative';
            const queryMode = '{hover_mode}';
            const hoverProfiles = {{
                horizontal: {{name: '水平', color: '#2563eb'}},
                vertical: {{name: '垂直', color: '#ea580c'}},
            }};
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:35;';
            root.appendChild(svg);
            const hoverBox = document.createElement('div');
            hoverBox.style.cssText = 'position:absolute;display:none;pointer-events:none;z-index:40;'
                + 'min-width:190px;padding:8px 10px;border:1px solid #cbd5e1;border-radius:6px;'
                + 'background:rgba(255,255,255,0.96);box-shadow:0 3px 12px rgba(15,23,42,0.2);'
                + 'color:#334155;font:12px sans-serif;line-height:1.65;white-space:nowrap;';
            root.appendChild(hoverBox);
            const hideHover = () => {{
                svg.replaceChildren();
                hoverBox.style.display = 'none';
            }};
            refreshHover = hideHover;
            const addLine = (x1, y1, x2, y2, color) => {{
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                for (const [name, value] of Object.entries({{x1, y1, x2, y2}})) line.setAttribute(name, value);
                line.setAttribute('stroke', color);
                line.setAttribute('stroke-width', '1');
                line.setAttribute('stroke-dasharray', '5 4');
                svg.appendChild(line);
            }};
            const addCircle = (x, y, color) => {{
                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', x);
                circle.setAttribute('cy', y);
                circle.setAttribute('r', '4.5');
                circle.setAttribute('fill', '#fff');
                circle.setAttribute('stroke', color);
                circle.setAttribute('stroke-width', '2.5');
                svg.appendChild(circle);
            }};
            const interpolate = (data, x) => {{
                if (!data?.length || x < Number(data[0][0]) || x > Number(data[data.length - 1][0])) return null;
                let low = 0;
                let high = data.length - 1;
                while (high - low > 1) {{
                    const middle = Math.floor((low + high) / 2);
                    if (Number(data[middle][0]) <= x) low = middle;
                    else high = middle;
                }}
                const x0 = Number(data[low][0]);
                const y0 = Number(data[low][1]);
                const x1 = Number(data[high][0]);
                const y1 = Number(data[high][1]);
                if (x1 === x0) return y0;
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0);
            }};
            const sideCrossing = (data, target, direction) => {{
                if (!data?.length) return null;
                let center = 0;
                for (let index = 1; index < data.length; index++) {{
                    if (Math.abs(Number(data[index][0])) < Math.abs(Number(data[center][0]))) center = index;
                }}
                for (let index = center; index + direction >= 0 && index + direction < data.length;
                     index += direction) {{
                    const next = index + direction;
                    const x0 = Number(data[index][0]);
                    const y0 = Number(data[index][1]);
                    const x1 = Number(data[next][0]);
                    const y1 = Number(data[next][1]);
                    if ((y0 - target) * (y1 - target) > 0) continue;
                    if (y1 === y0) return {{x: x1, y: target}};
                    return {{x: x0 + (target - y0) * (x1 - x0) / (y1 - y0), y: target}};
                }}
                return null;
            }};
            const curveData = () => {{
                const option = chart.getOption();
                const result = [];
                Object.entries(hoverProfiles).forEach(([key, profile]) => {{
                    if (legendSelected[profile.name] === false) return;
                    const curve = option.series.find(series => String(series.id) === `profile-${{key}}`);
                    const data = curve?.data?.map(item => Array.isArray(item) ? item : item.value);
                    if (data?.length) result.push({{...profile, data}});
                }});
                return result;
            }};
            const showHoverBox = (pixel, title, points) => {{
                if (!points.length) {{
                    hideHover();
                    return;
                }}
                hoverBox.innerHTML = `<div style="font-weight:700;margin-bottom:2px">${{title}}</div>`
                    + points.map(point => `<div><span style="display:inline-block;width:8px;height:8px;`
                        + `border-radius:50%;background:${{point.color}};margin-right:6px"></span>`
                        + `${{point.name}}：(${{point.x.toFixed(3)}}°, ${{point.y.toFixed(4)}} {value_unit})</div>`
                    ).join('');
                hoverBox.style.display = 'block';
                const left = Math.min(pixel[0] + 14, root.clientWidth - hoverBox.offsetWidth - 8);
                const top = Math.min(pixel[1] + 14, root.clientHeight - hoverBox.offsetHeight - 8);
                hoverBox.style.left = `${{Math.max(8, left)}}px`;
                hoverBox.style.top = `${{Math.max(8, top)}}px`;
            }};
            let pendingPointer = null;
            let pointerFrame = false;
            chart.getZr().on('mousemove', event => {{
                pendingPointer = event;
                if (pointerFrame) return;
                pointerFrame = true;
                requestAnimationFrame(() => {{
                    pointerFrame = false;
                    const current = pendingPointer;
                    if (!current) return;
                    const pixel = [current.offsetX, current.offsetY];
                    if (!chart.containPixel({{gridIndex: 0}}, pixel)) {{
                        hideHover();
                        return;
                    }}
                    const coordinate = chart.convertFromPixel({{gridIndex: 0}}, pixel);
                    svg.replaceChildren();
                    const profiles = curveData();
                    const points = [];
                    if (queryMode === 'strength_to_angle') {{
                        const target = Number(coordinate[1]);
                        const grid = chart.getModel().getComponent('grid', 0).coordinateSystem.getRect();
                        const targetPixel = chart.convertToPixel({{gridIndex: 0}}, [0, target]);
                        addLine(grid.x, targetPixel[1], grid.x + grid.width, targetPixel[1], '#64748b');
                        profiles.forEach(profile => {{
                            for (const direction of [-1, 1]) {{
                                const point = sideCrossing(profile.data, target, direction);
                                if (point && !points.some(existing => existing.name === profile.name
                                    && Math.abs(existing.x - point.x) < 1e-9)) {{
                                    points.push({{...point, name: profile.name, color: profile.color}});
                                }}
                            }}
                        }});
                        points.forEach(point => {{
                            const pointPixel = chart.convertToPixel({{gridIndex: 0}}, [point.x, point.y]);
                            const axisPixel = chart.convertToPixel({{gridIndex: 0}}, [point.x, 0]);
                            addLine(pointPixel[0], pointPixel[1], axisPixel[0], axisPixel[1], point.color);
                            addCircle(pointPixel[0], pointPixel[1], point.color);
                        }});
                        showHoverBox(pixel, `强度：${{target.toFixed(4)}} {value_unit}`, points);
                    }} else {{
                        const angle = Number(coordinate[0]);
                        const grid = chart.getModel().getComponent('grid', 0).coordinateSystem.getRect();
                        const anglePixel = chart.convertToPixel({{gridIndex: 0}}, [angle, 0]);
                        addLine(anglePixel[0], grid.y, anglePixel[0], grid.y + grid.height, '#64748b');
                        profiles.forEach(profile => {{
                            const y = interpolate(profile.data, angle);
                            if (y !== null && Number.isFinite(y)) {{
                                points.push({{x: angle, y, name: profile.name, color: profile.color}});
                            }}
                        }});
                        points.forEach(point => {{
                            const pointPixel = chart.convertToPixel({{gridIndex: 0}}, [point.x, point.y]);
                            const axisPixel = chart.convertToPixel({{gridIndex: 0}}, [0, point.y]);
                            addLine(pointPixel[0], pointPixel[1], axisPixel[0], axisPixel[1], point.color);
                            addCircle(pointPixel[0], pointPixel[1], point.color);
                        }});
                        showHoverBox(pixel, `角度：${{angle.toFixed(3)}}°`, points);
                    }}
                }});
            }});
            chart.getZr().on('globalout', hideHover);
        """
    polar_setup = ""
    if polar:
        polar_setup = f"""
            root.style.position = 'relative';
            const queryMode = '{hover_mode}';
            const hoverProfiles = {{
                horizontal: {{name: '水平', color: '#2563eb'}},
                vertical: {{name: '垂直', color: '#ea580c'}},
            }};
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:35;';
            root.appendChild(svg);
            const hoverBox = document.createElement('div');
            hoverBox.style.cssText = 'position:absolute;display:none;pointer-events:none;z-index:40;'
                + 'min-width:190px;padding:8px 10px;border:1px solid #cbd5e1;border-radius:6px;'
                + 'background:rgba(255,255,255,0.96);box-shadow:0 3px 12px rgba(15,23,42,0.2);'
                + 'color:#334155;font:12px sans-serif;line-height:1.65;white-space:nowrap;';
            root.appendChild(hoverBox);
            const hideHover = () => {{
                svg.replaceChildren();
                hoverBox.style.display = 'none';
            }};
            refreshHover = hideHover;
            const addLine = (x1, y1, x2, y2, color) => {{
                const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
                for (const [name, value] of Object.entries({{x1, y1, x2, y2}})) line.setAttribute(name, value);
                line.setAttribute('stroke', color);
                line.setAttribute('stroke-width', '1');
                line.setAttribute('stroke-dasharray', '5 4');
                svg.appendChild(line);
            }};
            const addPath = (pixels, color) => {{
                if (!pixels.length) return;
                const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
                path.setAttribute('d', pixels.map((point, index) =>
                    `${{index ? 'L' : 'M'}} ${{point[0]}} ${{point[1]}}`).join(' '));
                path.setAttribute('fill', 'none');
                path.setAttribute('stroke', color);
                path.setAttribute('stroke-width', '1');
                path.setAttribute('stroke-dasharray', '5 4');
                svg.appendChild(path);
            }};
            const addCircle = (x, y, color) => {{
                const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
                circle.setAttribute('cx', x);
                circle.setAttribute('cy', y);
                circle.setAttribute('r', '4.5');
                circle.setAttribute('fill', '#fff');
                circle.setAttribute('stroke', color);
                circle.setAttribute('stroke-width', '2.5');
                svg.appendChild(circle);
            }};
            const interpolate = (data, angle) => {{
                if (!data?.length || angle < Number(data[0][0])
                    || angle > Number(data[data.length - 1][0])) return null;
                let low = 0;
                let high = data.length - 1;
                while (high - low > 1) {{
                    const middle = Math.floor((low + high) / 2);
                    if (Number(data[middle][0]) <= angle) low = middle;
                    else high = middle;
                }}
                const x0 = Number(data[low][0]);
                const y0 = Number(data[low][1]);
                const x1 = Number(data[high][0]);
                const y1 = Number(data[high][1]);
                if (x1 === x0) return y0;
                return y0 + (y1 - y0) * (angle - x0) / (x1 - x0);
            }};
            const sideCrossing = (data, target, direction) => {{
                if (!data?.length) return null;
                let center = 0;
                for (let index = 1; index < data.length; index++) {{
                    if (Math.abs(Number(data[index][0])) < Math.abs(Number(data[center][0]))) center = index;
                }}
                for (let index = center; index + direction >= 0 && index + direction < data.length;
                     index += direction) {{
                    const next = index + direction;
                    const x0 = Number(data[index][0]);
                    const y0 = Number(data[index][1]);
                    const x1 = Number(data[next][0]);
                    const y1 = Number(data[next][1]);
                    if ((y0 - target) * (y1 - target) > 0) continue;
                    if (y1 === y0) return {{x: x1, y: target}};
                    return {{x: x0 + (target - y0) * (x1 - x0) / (y1 - y0), y: target}};
                }}
                return null;
            }};
            const curveData = () => {{
                const option = chart.getOption();
                const result = [];
                Object.entries(hoverProfiles).forEach(([key, profile]) => {{
                    if (legendSelected[profile.name] === false) return;
                    const curve = option.series.find(series => String(series.id) === `profile-${{key}}`);
                    const data = curve?.data?.map(item => {{
                        const value = Array.isArray(item) ? item : item.value;
                        return [Number(value[1]), Number(value[0])];
                    }});
                    if (data?.length) result.push({{...profile, data}});
                }});
                return result;
            }};
            const showHoverBox = (pixel, title, points) => {{
                if (!points.length) {{
                    hideHover();
                    return;
                }}
                hoverBox.innerHTML = `<div style="font-weight:700;margin-bottom:2px">${{title}}</div>`
                    + points.map(point => `<div><span style="display:inline-block;width:8px;height:8px;`
                        + `border-radius:50%;background:${{point.color}};margin-right:6px"></span>`
                        + `${{point.name}}：(${{point.x.toFixed(3)}}°, ${{point.y.toFixed(4)}} {value_unit})</div>`
                    ).join('');
                hoverBox.style.display = 'block';
                const left = Math.min(pixel[0] + 14, root.clientWidth - hoverBox.offsetWidth - 8);
                const top = Math.min(pixel[1] + 14, root.clientHeight - hoverBox.offsetHeight - 8);
                hoverBox.style.left = `${{Math.max(8, left)}}px`;
                hoverBox.style.top = `${{Math.max(8, top)}}px`;
            }};
            let pendingPointer = null;
            let pointerFrame = false;
            chart.getZr().on('mousemove', event => {{
                pendingPointer = event;
                if (pointerFrame) return;
                pointerFrame = true;
                requestAnimationFrame(() => {{
                    pointerFrame = false;
                    const current = pendingPointer;
                    if (!current) return;
                    const pixel = [current.offsetX, current.offsetY];
                    if (!chart.containPixel({{polarIndex: 0}}, pixel)) {{
                        hideHover();
                        return;
                    }}
                    const coordinate = chart.convertFromPixel({{polarIndex: 0}}, pixel);
                    const profiles = curveData();
                    const points = [];
                    svg.replaceChildren();
                    if (queryMode === 'strength_to_angle') {{
                        const target = Number(coordinate[0]);
                        const arcPixels = [];
                        for (let angle = -90; angle <= 90; angle += 3) {{
                            arcPixels.push(chart.convertToPixel({{polarIndex: 0}}, [target, angle]));
                        }}
                        addPath(arcPixels, '#64748b');
                        profiles.forEach(profile => {{
                            for (const direction of [-1, 1]) {{
                                const point = sideCrossing(profile.data, target, direction);
                                if (point && !points.some(existing => existing.name === profile.name
                                    && Math.abs(existing.x - point.x) < 1e-9)) {{
                                    points.push({{...point, name: profile.name, color: profile.color}});
                                }}
                            }}
                        }});
                        points.forEach(point => {{
                            const pointPixel = chart.convertToPixel(
                                {{polarIndex: 0}}, [point.y, point.x]);
                            const centerPixel = chart.convertToPixel(
                                {{polarIndex: 0}}, [0, point.x]);
                            addLine(centerPixel[0], centerPixel[1], pointPixel[0], pointPixel[1], point.color);
                            addCircle(pointPixel[0], pointPixel[1], point.color);
                        }});
                        showHoverBox(pixel, `强度：${{target.toFixed(4)}} {value_unit}`, points);
                    }} else {{
                        const angle = Number(coordinate[1]);
                        profiles.forEach(profile => {{
                            const strength = interpolate(profile.data, angle);
                            if (strength !== null && Number.isFinite(strength)) {{
                                points.push({{x: angle, y: strength,
                                    name: profile.name, color: profile.color}});
                            }}
                        }});
                        const maximum = Math.max(0, ...profiles.flatMap(profile =>
                            profile.data.map(point => Number(point[1]))));
                        const centerPixel = chart.convertToPixel({{polarIndex: 0}}, [0, angle]);
                        const outerPixel = chart.convertToPixel({{polarIndex: 0}}, [maximum, angle]);
                        addLine(centerPixel[0], centerPixel[1], outerPixel[0], outerPixel[1], '#64748b');
                        points.forEach(point => {{
                            const pointPixel = chart.convertToPixel(
                                {{polarIndex: 0}}, [point.y, point.x]);
                            addCircle(pointPixel[0], pointPixel[1], point.color);
                        }});
                        showHoverBox(pixel, `角度：${{angle.toFixed(3)}}°`, points);
                    }}
                }});
            }});
            chart.getZr().on('globalout', hideHover);
        """
    coordinate_setup = polar_setup if polar else cartesian_setup
    return f"""
        () => {{
            const component = getElement({chart_id});
            if (!component?.chart || component._distributionInteractionBound) return;
            component._distributionInteractionBound = true;
            const chart = component.chart;
            const root = component.$el;
            let legendSelected = {{'水平': true, '垂直': true}};
            let refreshHover = () => {{}};
            chart.on('legendselectchanged', event => {{
                legendSelected = {{...legendSelected, ...event.selected}};
                const updates = [];
                chart.getOption().series.forEach(series => {{
                    const id = String(series.id || '');
                    if (!id.startsWith('target-')) return;
                    const profile = id.includes('-horizontal-') ? '水平'
                        : id.includes('-vertical-') ? '垂直' : null;
                    if (!profile) return;
                    const visible = legendSelected[profile] !== false;
                    if (id.endsWith('-points')) {{
                        updates.push({{id, symbolSize: visible ? 9 : 0, label: {{show: visible}}}});
                    }} else if (id.includes('-angle-')) {{
                        updates.push({{id, lineStyle: {{opacity: visible ? 0.65 : 0}}}});
                    }}
                }});
                if (updates.length) chart.setOption({{series: updates}}, {{lazyUpdate: true}});
                refreshHover();
            }});
            {coordinate_setup}
        }}
    """


def _bind_distribution_chart_interactions(chart: Any, polar: bool,
                                          value_unit: str, hover_mode: str) -> None:
    """在图表首次完成渲染后绑定纯客户端交互，避免鼠标移动产生服务器请求。"""
    chart.on(
        "chart:finished",
        js_handler=_distribution_interaction_setup_js(
            chart.id, polar, value_unit, hover_mode
        ),
    )


def export_intensity(batch: IntensityBatch,
                     target_percents: tuple[float, ...] | None = None) -> bytes:
    buffer = _ExcelBytesBuffer()
    percents = _validated_target_percents(
        (batch.settings.target_percent,) if target_percents is None else target_percents
    )
    summaries: list[dict[str, Any]] = []
    for result in batch.results:
        base = result.summary(())
        angle_rows = target_angle_rows(result, percents)
        summaries.extend({**base, **angle_row} for angle_row in angle_rows)
        if not angle_rows:
            summaries.append(base)
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(summaries).to_excel(writer, sheet_name="角度汇总", index=False)
        pd.DataFrame([{"参数": k, "值": v} for k, v in asdict(batch.settings).items()]).to_excel(
            writer, sheet_name="处理参数", index=False)
        pd.DataFrame({"错误": batch.errors}).to_excel(writer, sheet_name="处理错误", index=False)
        used = {"角度汇总", "处理参数", "处理错误"}
        for index, result in enumerate(batch.results, 1):
            name = _safe_sheet_name(
                f"{index}_{result.output_quantity}_{result.output_unit}_{result.sheet}", used
            )
            pd.DataFrame(result.intensity).to_excel(writer, sheet_name=name, index=False, header=False)
            for profile in (result.horizontal, result.vertical):
                name = _safe_sheet_name(f"{index}_{profile.name}_{result.sheet}", used)
                pd.DataFrame({
                    "角度(°)": profile.angles,
                    f"{result.output_quantity}({result.output_unit})": profile.values,
                }).to_excel(
                    writer, sheet_name=name, index=False)
    return buffer.getvalue()


class IntensityDistributionTool:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.batch: IntensityBatch | None = None
        self.target_slots: tuple[float | None, float | None] = (50.0, 10.0)
        self.target_percents = (50.0, 10.0)
        self.hover_mode = "angle_to_strength"
        self.revision = 0
        self.busy = False

    def show(self, parent_dialog: ui.dialog) -> None:
        with ui.column().classes("w-full min-h-screen p-4 bg-slate-50"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label("照度/辐照度转强度分布").classes("text-xl font-bold")
                ui.button("退出工具", on_click=parent_dialog.close).props("outline")
            ui.label("垂直平面 / 远场等效换算。所选中心视为光源在平面上的垂足，距离为垂直距离；"
                     "实测距离应满足光源可近似为点光源的条件。").classes("text-sm text-gray-500")
            with ui.element("div").classes("w-full grid grid-cols-1 xl:grid-cols-[1fr_3fr] gap-4"):
                with ui.card().classes("w-full"):
                    ui.label("上传照度数据表").classes("font-bold")
                    self.uploader = custom_upload(on_upload=self._upload, on_removed=self._removed,
                        multiple=True, auto_upload=True,
                        label="添加文件", max_file_size=MAX_UPLOAD_BYTES).props(
                        'accept=".csv,.cvs,.xlsx,.xlsm,.xls"').classes("w-full intensity-upload")
                    ui.add_css(".intensity-upload .q-uploader__list {max-height:180px; overflow-y:auto;}")
                    ui.label("无表头二维数值矩阵；支持 Excel / CSV，单文件 50 MB。").classes("text-xs")
                    self.render_files()
                with ui.card().classes("w-full"):
                    ui.label("分析设置").classes("font-bold")
                    with ui.row().classes("w-full gap-3"):
                        self.pixels = ui.number("比例尺：像素点数", value=1, min=0.000001).props('step="any" outlined dense').classes("w-44")
                        self.length = ui.number("对应长度(mm)", value=1, min=0.000001).props('step="any" outlined dense').classes("w-44")
                        self.distance = ui.number("垂直测量距离(mm)", value=None, min=0.000001).props('step="any" outlined dense').classes("w-48")
                        self.lux_factor = ui.number("数据值 → lx 系数", value=None, min=0.000001).props('step="any" outlined dense').classes("w-44")
                        self.irradiance_factor = ui.number(
                            "数据值 → mW/cm² 系数", value=None, min=0.000001
                        ).props('step="any" outlined dense').classes("w-52")
                        self.merge = ui.checkbox("启用单元格合并", value=False).props("dense")
                        self.granularity = (
                            ui.number("合并颗粒度 N×N", value=10, min=1, step=1)
                            .props("outlined dense")
                            .classes("w-44")
                            .bind_visibility_from(self.merge, "value")
                        )
                        self.mode = ui.select({"geometric": "数据几何中心", "maximum": "全局最大值位置",
                            "threshold": "最大值百分比区域中心", "manual": "手工指定中心"},
                            value="threshold", label="数据中心定位").props("outlined dense").classes("w-60")
                        self.center_percent = ui.number("定位阈值(%)", value=10, min=0.000001, max=100).props("outlined dense").classes("w-40").bind_visibility_from(self.mode, "value", lambda v: v == "threshold")
                        self.row = ui.number("中心行（从1起）", value=1, min=1).props("outlined dense").classes("w-40").bind_visibility_from(self.mode, "value", lambda v: v == "manual")
                        self.col = ui.number("中心列（从1起）", value=1, min=1).props("outlined dense").classes("w-40").bind_visibility_from(self.mode, "value", lambda v: v == "manual")
                    ui.label("lx 与 mW/cm² 换算系数只能填写一个；前者输出光强 cd，后者输出辐射强度 mW/sr。"
                             "换算均采用 I = E × d(m)² / cos³θ，mW/cm² 会先换算为 mW/m²。"
                             "单元格合并按 N×N 原始单元格求平均，比例尺自动换算；"
                             "定位阈值与目标强度百分比独立；中心落在像素间时采用双线性插值。"
                             "启用合并后，手工中心按合并后的网格坐标填写。").classes("text-xs text-gray-500")
            with ui.row().classes("w-full justify-end"):
                ui.button("清空", on_click=self._clear).props("outline")
                self.calculate_button = ui.button("开始计算", on_click=self._calculate)
                self.export_button = ui.button("导出 Excel", on_click=self._export)
                self.export_button.disable()
            for control in (self.pixels, self.length, self.distance, self.merge,
                            self.granularity, self.mode,
                            self.center_percent, self.row, self.col):
                control.on_value_change(self._invalidate)
            self.lux_factor.on_value_change(
                lambda: self._factor_changed(self.lux_factor, self.irradiance_factor))
            self.irradiance_factor.on_value_change(
                lambda: self._factor_changed(self.irradiance_factor, self.lux_factor))
            self.render_results()

    def _invalidate(self) -> None:
        self.revision += 1
        self.batch = None
        self.export_button.disable()
        self.render_results.refresh()

    def _factor_changed(self, selected: Any, other: Any) -> None:
        """输入一种换算系数时清空另一种，避免产生含糊的输出单位。"""
        if selected.value is not None and other.value is not None:
            other.set_value(None)
        self._invalidate()

    @ui.refreshable_method
    def render_files(self) -> None:
        ui.label(f"当前 {len(self.files)} 个文件，共 {sum(map(len, self.files.values())) / 1024**2:.2f} MB").classes("text-xs")

    async def _upload(self, event: Any) -> None:
        name = str(event.file.name)
        try:
            if Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise ValueError("不支持的文件格式")
            content = await event.file.read()
            if len(content) > MAX_UPLOAD_BYTES:
                raise ValueError("单文件不能超过 50 MB")
            self.files[name] = content
            self._invalidate()
            self.render_files.refresh()
        except Exception as exc:
            ui.notify(f"{name}：{exc}", type="negative")

    def _removed(self, event: CustomUploadRemovedEventArguments) -> None:
        if event.clear_all:
            self.files.clear()
        else:
            for item in event.files:
                self.files.pop(str(item.get("name", "")), None)
        self._invalidate()
        self.render_files.refresh()

    def _clear(self) -> None:
        self.files.clear()
        self.uploader.reset()
        self._invalidate()
        self.render_files.refresh()

    def _settings(self) -> IntensitySettings:
        def number(element: Any, label: str) -> float:
            if element.value is None:
                raise ValueError(f"请填写{label}")
            return float(element.value)
        def optional_number(element: Any) -> float | None:
            return None if element.value is None else float(element.value)
        def integer(element: Any, label: str) -> int:
            value = number(element, label)
            if not value.is_integer():
                raise ValueError(f"{label}必须是整数")
            return int(value)
        settings = IntensitySettings(
            scale_pixels=number(self.pixels, "像素点数"), scale_length_mm=number(self.length, "比例尺长度"),
            distance_mm=number(self.distance, "测量距离"),
            lux_factor=optional_number(self.lux_factor),
            irradiance_factor=optional_number(self.irradiance_factor),
            center_mode=str(self.mode.value), center_threshold_percent=number(self.center_percent, "定位阈值"),
            manual_row=number(self.row, "中心行"), manual_col=number(self.col, "中心列"),
            target_percent=50.0,
            merge_enabled=bool(self.merge.value),
            granularity=integer(self.granularity, "合并颗粒度"),
        )
        settings.validate()
        return settings

    async def _calculate(self) -> None:
        if self.busy:
            return
        try:
            if not self.files:
                raise ValueError("请先上传数据表")
            settings = self._settings()
        except ValueError as exc:
            ui.notify(str(exc), type="warning")
            return
        self._invalidate()
        revision = self.revision
        self.busy = True
        self.calculate_button.disable()
        try:
            batch = await run.io_bound(analyze_files, dict(self.files), settings)
            if self.revision != revision:
                return
            self.batch = batch
            self.render_results.refresh()
            self.export_button.set_enabled(bool(batch.results))
        except Exception as exc:
            ui.notify(f"计算失败：{exc}", type="negative")
        finally:
            self.busy = False
            self.calculate_button.enable()

    @ui.refreshable_method
    def render_results(self) -> None:
        batch = self.batch
        if batch is None:
            return
        ui.label(f"成功 {len(batch.results)} 个工作表；失败 {len(batch.errors)} 个").classes("font-bold")
        if batch.results:
            factor_name = "mW/cm²" if batch.settings.radiometric else "lx"
            ui.label(f"本次：距离 {batch.settings.distance_mm:g} mm，数据值 → {factor_name} 系数 "
                     f"{batch.settings.conversion_factor:g}，"
                     f"单元格处理：{'按 ' + str(batch.settings.granularity) + '×' + str(batch.settings.granularity) + ' 合并' if batch.settings.merge_enabled else '未合并'}"
                     ).classes("text-sm")
            rows = [{key: "未找到" if value is None else round(value, 4) if isinstance(value, float) else value
                     for key, value in r.summary(()).items()} for r in batch.results]
            ui.table(columns=[{"name": k, "label": k, "field": k, "align": "center"} for k in rows[0]],
                     rows=rows, pagination=10).classes("w-full").props("dense flat bordered wrap-cells")
            ui.label("负向：左/上；正向：右/下。夹角 = 正向角 − 负向角。"
                     "从中心向外取首次下降交点；未找到表示当前测量范围未覆盖交点，不外推。"
                     "100% 对应中心 0°。").classes("text-xs text-gray-500")
            with ui.row().classes("w-full items-center gap-3"):
                selector = ui.select(
                    {i: f"{r.filename} / {r.sheet}" for i, r in enumerate(batch.results)},
                    value=0, label="查看工作表分布图",
                ).classes("w-96 max-w-full")
                display_mode = ui.toggle(
                    {"absolute": "绝对值", "normalized": "归一化（中心=100%）"},
                    value="absolute",
                ).props("no-caps")
                hover_mode = ui.toggle(
                    {"angle_to_strength": "按角度找强度", "strength_to_angle": "按强度找角度"},
                    value=self.hover_mode,
                ).props("no-caps")
                target_one = ui.number(
                    "目标比例 1 (%)", value=self.target_slots[0], min=0, max=100
                ).props('step="any" outlined dense').classes("w-40")
                target_two = ui.number(
                    "目标比例 2 (%)", value=self.target_slots[1], min=0, max=100
                ).props('step="any" outlined dense').classes("w-40")
            ui.label("修改目标比例只重算剖面交点，不重新换算数据矩阵。"
                     "按角度取点时两条曲线最多显示 2 点；按强度取点时从中心向两侧查找，"
                     "两条曲线最多显示 4 点。"
                     ).classes("text-xs text-gray-500")
            angle_results = ui.element("div").classes("w-full")
            charts = ui.element("div").classes("w-full grid grid-cols-1 xl:grid-cols-2 gap-4")
            def draw() -> None:
                charts.clear()
                angle_results.clear()
                if selector.value is None:
                    return
                try:
                    first = None if target_one.value in (None, "") else float(target_one.value)
                    second = None if target_two.value in (None, "") else float(target_two.value)
                    slots = (first, second)
                    if any(value is not None and (
                        not math.isfinite(value) or value < 0 or value > 100
                    ) for value in slots):
                        raise ValueError
                except (TypeError, ValueError):
                    self.export_button.disable()
                    with angle_results:
                        ui.label("目标比例只能为空、0，或大于 0 且不超过 100。"
                                 ).classes("text-sm text-red-600")
                    return
                self.target_slots = slots
                targets = tuple(value for value in slots if value is not None and value > 0)
                self.target_percents = targets
                self.hover_mode = str(hover_mode.value)
                self.export_button.enable()
                result = batch.results[int(selector.value)]
                with angle_results:
                    ui.label("实时角度结果").classes("font-bold")
                    live_rows = [{key: "未找到" if value is None
                                  else round(value, 4) if isinstance(value, float) else value
                                  for key, value in row.items()}
                                 for row in target_angle_rows(result, targets)]
                    if live_rows:
                        ui.table(
                            columns=[{"name": key, "label": key, "field": key, "align": "center"}
                                     for key in live_rows[0]],
                            rows=live_rows,
                        ).classes("w-full").props("dense flat bordered wrap-cells hide-pagination")
                    else:
                        ui.label("两个目标比例均未启用。"
                                 ).classes("text-sm text-gray-500")
                with charts:
                    normalized = display_mode.value == "normalized"
                    value_unit = "%" if normalized else result.output_unit
                    for polar, title in ((False, "笛卡尔剖面分布"), (True, "极坐标剖面分布")):
                        with ui.card().classes("w-full min-w-0"):
                            ui.label(title).classes("font-bold")
                            chart = ui.echart(distribution_chart(
                                result, polar, normalized, targets
                            )).classes("w-full h-[520px]")
                            _bind_distribution_chart_interactions(
                                chart, polar, value_unit, self.hover_mode
                            )
            selector.on_value_change(draw)
            display_mode.on_value_change(draw)
            hover_mode.on_value_change(draw)
            target_one.on_value_change(draw)
            target_two.on_value_change(draw)
            draw()
        if batch.errors:
            with ui.expansion("处理失败信息", value=True).classes("w-full text-red-700"):
                for error in batch.errors:
                    ui.label(error)

    async def _export(self) -> None:
        batch = self.batch
        if batch is None or not batch.results:
            return
        self.export_button.disable()
        try:
            content = await run.io_bound(export_intensity, batch, self.target_percents)
            ui.download(content, "强度分布分析.xlsx")
        except Exception as exc:
            ui.notify(f"导出失败：{exc}", type="negative")
        finally:
            self.export_button.set_enabled(self.batch is not None and bool(self.batch.results))
