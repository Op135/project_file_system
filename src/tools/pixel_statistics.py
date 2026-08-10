# -*- encoding: utf-8 -*-
"""像素矩阵的可选分块合并、区域定位与统计分析工具。"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from nicegui import run, ui

SUPPORTED_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".csv", ".cvs"}
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class _ExcelBytesBuffer(io.BytesIO):
    """兼容 Pandas WriteExcelBuffer 协议的内存字节缓冲区。"""

    def truncate(self, size: int | None = None) -> int:
        return super().truncate(size)


class PixelStatisticsError(ValueError):
    """输入数据或分析参数不满足要求。"""


@dataclass(frozen=True)
class PixelStatisticsSettings:
    merge_enabled: bool = False
    granularity: int = 10
    force_odd_grid: bool = True
    dimension_mode: str = "actual"
    expected_rows: int = 1024
    expected_cols: int = 1280
    missing_policy: str = "strict"
    scale_pixels: float = 1.0
    scale_length_mm: float = 1.0
    region_mode: str = "rectangle"
    radius_mm: float = 100.0
    rectangle_height_mm: float = 336.0
    rectangle_width_mm: float = 596.0
    center_mode: str = "geometric"
    manual_center_row: float = 512.0
    manual_center_col: float = 725.0
    threshold_percent: float = 90.0
    matrix_uniformity_enabled: bool = False
    matrix_rows: int = 3
    matrix_cols: int = 3
    matrix_sample_side_mm: float = 1.0

    def validate(self) -> None:
        if self.granularity < 1:
            raise PixelStatisticsError("合并颗粒度必须是大于 0 的整数")
        if self.dimension_mode not in {"actual", "fixed"}:
            raise PixelStatisticsError("未知的尺寸策略")
        if self.expected_rows < 1 or self.expected_cols < 1:
            raise PixelStatisticsError("固定尺寸的行数和列数必须大于 0")
        if self.missing_policy not in {"strict", "ignore"}:
            raise PixelStatisticsError("未知的空值处理策略")
        if self.region_mode not in {"full", "circle", "rectangle"}:
            raise PixelStatisticsError("未知的统计区域")
        if self.scale_pixels <= 0 or self.scale_length_mm <= 0:
            raise PixelStatisticsError("比例尺的像素点数和对应长度必须大于 0")
        if self.radius_mm < 0:
            raise PixelStatisticsError("圆形区域半径不能小于 0")
        if self.rectangle_height_mm <= 0 or self.rectangle_width_mm <= 0:
            raise PixelStatisticsError("矩形区域的高度和宽度必须大于 0")
        if self.center_mode not in {"geometric", "maximum", "manual", "threshold"}:
            raise PixelStatisticsError("未知的中心算法")
        if not 0 < self.threshold_percent <= 100:
            raise PixelStatisticsError("最大值阈值百分比必须在 0～100 之间")
        if self.matrix_uniformity_enabled:
            if self.region_mode == "circle":
                raise PixelStatisticsError("圆形统计区域不支持矩阵均匀性统计")
            if self.matrix_rows < 1 or self.matrix_cols < 1:
                raise PixelStatisticsError("矩阵横向和竖向等分数必须大于 0")
            if self.matrix_sample_side_mm <= 0:
                raise PixelStatisticsError("矩阵中心采样边长必须大于 0")


@dataclass
class ThresholdOutline:
    """阈值中心示意图所需的主连通区域边缘。"""

    edge_points: list[tuple[float, float]]
    edge_values: list[float]
    threshold: float
    region_points: int
    min_row: int
    max_row: int
    min_col: int
    max_col: int


@dataclass
class CalculatedStatistics:
    mean: float
    minimum: float
    maximum: float
    min_max_ratio: float | None
    contrast_ratio: float | None
    relative_population_std: float | None
    relative_sample_std: float | None


@dataclass
class MatrixUniformityResult:
    rows: int
    cols: int
    sample_side_mm: float
    sample_values: list[list[float]]
    sample_count: int
    mean: float
    minimum: float
    maximum: float
    min_max_ratio: float | None
    contrast_ratio: float | None
    relative_population_std: float | None
    relative_sample_std: float | None


@dataclass
class SheetAnalysis:
    source_file: str
    sheet_name: str
    source_rows: int
    source_cols: int
    processed_rows: int
    processed_cols: int
    center_row: float
    center_col: float
    center_description: str
    region_description: str
    sample_count: int
    mean: float
    minimum: float
    maximum: float
    min_max_ratio: float | None
    contrast_ratio: float | None
    relative_population_std: float | None
    relative_sample_std: float | None
    ignored_cells: int = 0
    warning: str = ""
    processed_matrix: np.ndarray | None = None
    raw_pixels_per_mm: float = 1.0
    processed_pixels_per_mm: float = 1.0
    threshold_outline: ThresholdOutline | None = None
    matrix_uniformity: MatrixUniformityResult | None = None

    def summary_row(self, formatted: bool = False) -> dict[str, Any]:
        def value(number: float | None) -> float | str:
            if number is None or not math.isfinite(number):
                return "—" if formatted else np.nan
            return f"{number:.4f}" if formatted else round(number, 4)

        matrix = self.matrix_uniformity
        return {
            "文件": self.source_file,
            "工作表": self.sheet_name,
            "原始尺寸": f"{self.source_rows}×{self.source_cols}",
            "处理后尺寸": f"{self.processed_rows}×{self.processed_cols}",
            "原始比例尺": f"{self.raw_pixels_per_mm:g} 像素/mm",
            "处理后比例尺": f"{self.processed_pixels_per_mm:g} 网格/mm",
            "中心坐标": f"({self.center_row:.2f}, {self.center_col:.2f})",
            "中心算法": self.center_description,
            "统计区域": self.region_description,
            "有效样本数": self.sample_count,
            "平均值": value(self.mean),
            "最小值": value(self.minimum),
            "最大值": value(self.maximum),
            "最小/最大": value(self.min_max_ratio),
            "(最大-最小)/(最大+最小)": value(self.contrast_ratio),
            "相对总体标准差": value(self.relative_population_std),
            "相对样本标准差": value(self.relative_sample_std),
            "矩阵划分": f"{matrix.rows}×{matrix.cols}" if matrix else "—",
            "矩阵中心采样边长(mm)": value(matrix.sample_side_mm if matrix else None),
            "矩阵有效采样数": matrix.sample_count if matrix else "—",
            "矩阵平均值": value(matrix.mean if matrix else None),
            "矩阵最小值": value(matrix.minimum if matrix else None),
            "矩阵最大值": value(matrix.maximum if matrix else None),
            "矩阵最小/最大": value(matrix.min_max_ratio if matrix else None),
            "矩阵(最大-最小)/(最大+最小)": value(matrix.contrast_ratio if matrix else None),
            "矩阵相对总体标准差": value(matrix.relative_population_std if matrix else None),
            "矩阵相对样本标准差": value(matrix.relative_sample_std if matrix else None),
            "忽略单元格": self.ignored_cells,
            "提示": self.warning,
        }


@dataclass
class AnalysisBatch:
    results: list[SheetAnalysis]
    errors: list[str]
    settings: PixelStatisticsSettings


def dataframe_to_numeric_matrix(df: pd.DataFrame, missing_policy: str) -> tuple[np.ndarray, int]:
    """把无表头工作表转换为二维浮点矩阵，并返回被忽略的单元格数。"""
    if df.empty or df.shape[0] == 0 or df.shape[1] == 0:
        raise PixelStatisticsError("工作表为空")

    normalized = df.replace(r"^\s*$", np.nan, regex=True)
    numeric = normalized.apply(pd.to_numeric, errors="coerce")
    matrix = numeric.to_numpy(dtype=float)
    invalid = ~np.isfinite(matrix)
    invalid_count = int(invalid.sum())
    if missing_policy == "strict" and invalid_count:
        first_row, first_col = np.argwhere(invalid)[0]
        original = df.iat[int(first_row), int(first_col)]
        raise PixelStatisticsError(
            f"发现 {invalid_count} 个空白或非数字单元格，首个位置为 "
            f"R{first_row + 1}C{first_col + 1}（值：{original!r}）"
        )

    if not np.isfinite(matrix).any():
        raise PixelStatisticsError("工作表中没有有效数字")
    matrix[~np.isfinite(matrix)] = np.nan
    return matrix, invalid_count


def _odd_block_count(length: int, granularity: int, force_odd: bool) -> int:
    count = length // granularity
    if force_odd and count > 0 and count % 2 == 0:
        count -= 1
    return count


def merge_pixel_blocks(
    matrix: np.ndarray,
    granularity: int,
    force_odd_grid: bool,
    ignore_missing: bool,
) -> tuple[np.ndarray, str]:
    """居中裁剪矩阵后，以 granularity×granularity 的块均值降采样。"""
    rows, cols = matrix.shape
    block_rows = _odd_block_count(rows, granularity, force_odd_grid)
    block_cols = _odd_block_count(cols, granularity, force_odd_grid)
    if block_rows < 1 or block_cols < 1:
        raise PixelStatisticsError(f"当前数据尺寸 {rows}×{cols} 小于合并颗粒度 {granularity}")

    kept_rows = block_rows * granularity
    kept_cols = block_cols * granularity
    removed_rows = rows - kept_rows
    removed_cols = cols - kept_cols
    row_start = (removed_rows + 1) // 2
    col_start = (removed_cols + 1) // 2
    cropped = matrix[row_start : row_start + kept_rows, col_start : col_start + kept_cols]
    blocks = cropped.reshape(block_rows, granularity, block_cols, granularity)

    if ignore_missing:
        counts = np.isfinite(blocks).sum(axis=(1, 3))
        sums = np.nansum(blocks, axis=(1, 3))
        merged = np.full((block_rows, block_cols), np.nan, dtype=float)
        np.divide(sums, counts, out=merged, where=counts > 0)
    else:
        merged = blocks.mean(axis=(1, 3))

    crop_text = (
        f"颗粒度 {granularity}×{granularity}；居中裁剪上/下 "
        f"{row_start}/{removed_rows - row_start} 行、左/右 "
        f"{col_start}/{removed_cols - col_start} 列"
    )
    return merged, crop_text


def _threshold_component_center(matrix: np.ndarray, percent: float) -> tuple[float, float, str, ThresholdOutline]:
    """返回包含全局最大值的阈值连通区域的几何质心。"""
    maximum = float(np.nanmax(matrix))
    if maximum < 0:
        raise PixelStatisticsError("阈值区域中心要求数据最大值不小于 0")
    threshold = maximum * percent / 100.0
    mask = np.isfinite(matrix) & (matrix >= threshold)
    maximum_points = np.argwhere(np.isfinite(matrix) & np.isclose(matrix, maximum, rtol=1e-12, atol=1e-12))
    if not len(maximum_points):
        raise PixelStatisticsError("无法定位最大值")

    rows, cols = matrix.shape
    labels = np.zeros(mask.shape, dtype=np.int32)
    best: tuple[int, float, float, int, int, int, int, int] | None = None
    directions = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))

    component_id = 0
    for seed_row, seed_col in maximum_points:
        seed_row, seed_col = int(seed_row), int(seed_col)
        if labels[seed_row, seed_col] != 0:
            continue
        component_id += 1
        queue: deque[tuple[int, int]] = deque([(seed_row, seed_col)])
        labels[seed_row, seed_col] = component_id
        count = 0
        row_sum = 0.0
        col_sum = 0.0
        min_row = max_row = seed_row
        min_col = max_col = seed_col
        while queue:
            row, col = queue.popleft()
            count += 1
            row_sum += row
            col_sum += col
            min_row, max_row = min(min_row, row), max(max_row, row)
            min_col, max_col = min(min_col, col), max(max_col, col)
            for row_delta, col_delta in directions:
                neighbor_row = row + row_delta
                neighbor_col = col + col_delta
                if (
                    0 <= neighbor_row < rows
                    and 0 <= neighbor_col < cols
                    and mask[neighbor_row, neighbor_col]
                    and labels[neighbor_row, neighbor_col] == 0
                ):
                    labels[neighbor_row, neighbor_col] = component_id
                    queue.append((neighbor_row, neighbor_col))
        candidate = (count, row_sum, col_sum, min_row, max_row, min_col, max_col, component_id)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        raise PixelStatisticsError("最大值阈值没有形成有效区域")
    count, row_sum, col_sum, min_row, max_row, min_col, max_col, best_component_id = best
    center_row = row_sum / count
    center_col = col_sum / count
    component_mask = labels == best_component_id
    padded = np.pad(component_mask, 1, mode="constant", constant_values=False)
    interior = component_mask.copy()
    for row_delta, col_delta in directions:
        interior &= padded[
            1 + row_delta : 1 + row_delta + rows,
            1 + col_delta : 1 + col_delta + cols,
        ]
    boundary_points = np.argwhere(component_mask & ~interior)
    max_chart_points = 2000
    if len(boundary_points) > max_chart_points:
        step = math.ceil(len(boundary_points) / max_chart_points)
        boundary_points = boundary_points[::step]
    edge_points = [(float(col), float(row)) for row, col in boundary_points]
    edge_values = [float(matrix[int(row), int(col)]) for row, col in boundary_points]
    description = (
        f"最大值的 {percent:g}% 阈值连通区（阈值 {threshold:.4g}，{count} 点，"
        f"范围 R{min_row + 1}:R{max_row + 1}、C{min_col + 1}:C{max_col + 1}）"
    )
    outline = ThresholdOutline(
        edge_points=edge_points,
        edge_values=edge_values,
        threshold=threshold,
        region_points=count,
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
    )
    return center_row, center_col, description, outline


def locate_center(
    matrix: np.ndarray, settings: PixelStatisticsSettings
) -> tuple[float, float, str, ThresholdOutline | None]:
    rows, cols = matrix.shape
    if settings.center_mode == "geometric":
        return (rows - 1) / 2.0, (cols - 1) / 2.0, "数据全局中心", None
    if settings.center_mode == "maximum":
        flat_index = int(np.nanargmax(matrix))
        row, col = np.unravel_index(flat_index, matrix.shape)
        return float(row), float(col), "全局最大值位置", None
    if settings.center_mode == "threshold":
        return _threshold_component_center(matrix, settings.threshold_percent)

    row = settings.manual_center_row - 1.0
    col = settings.manual_center_col - 1.0
    if not 0 <= row < rows or not 0 <= col < cols:
        raise PixelStatisticsError(
            f"指定中心 ({settings.manual_center_row:g}, {settings.manual_center_col:g}) 超出当前数据范围 {rows}×{cols}"
        )
    return row, col, "手工指定中心", None


def _centered_window(length: int, requested: int, center: float) -> tuple[int, int, bool]:
    ideal_start = math.floor(center - (requested - 1) / 2.0)
    ideal_end = ideal_start + requested
    start = max(0, ideal_start)
    end = min(length, ideal_end)
    return start, end, start != ideal_start or end != ideal_end


def build_region_mask(
    shape: tuple[int, int],
    center_row: float,
    center_col: float,
    settings: PixelStatisticsSettings,
    pixels_per_mm: float,
) -> tuple[np.ndarray, str, str]:
    rows, cols = shape
    if settings.region_mode == "full":
        return np.ones(shape, dtype=bool), "全域", ""
    if settings.region_mode == "circle":
        row_indices, col_indices = np.ogrid[:rows, :cols]
        radius_in_grid = settings.radius_mm * pixels_per_mm
        max_radius_in_grid = min(
            center_row + 0.5,
            rows - center_row - 0.5,
            center_col + 0.5,
            cols - center_col - 0.5,
        )
        if radius_in_grid > max_radius_in_grid + 1e-9:
            raise PixelStatisticsError(
                f"圆形统计范围无法完整容纳：半径 {settings.radius_mm:g} mm "
                f"换算为 {radius_in_grid:.4g} 网格，但当前中心到最近数据边界仅 "
                f"{max_radius_in_grid:.4g} 网格"
            )
        mask = (row_indices - center_row) ** 2 + (col_indices - center_col) ** 2 <= radius_in_grid**2
        return mask, f"半径 {settings.radius_mm:g} mm（{radius_in_grid:.2f} 网格）的圆形区域", ""

    requested_rows = max(1, round(settings.rectangle_height_mm * pixels_per_mm))
    requested_cols = max(1, round(settings.rectangle_width_mm * pixels_per_mm))
    row_start, row_end, rows_clipped = _centered_window(rows, requested_rows, center_row)
    col_start, col_end, cols_clipped = _centered_window(cols, requested_cols, center_col)
    if rows_clipped or cols_clipped:
        raise PixelStatisticsError(
            f"矩形统计范围无法完整容纳：{settings.rectangle_height_mm:g}×"
            f"{settings.rectangle_width_mm:g} mm 换算为 {requested_rows}×{requested_cols} 网格，"
            f"当前数据为 {rows}×{cols} 网格，请缩小统计范围或检查比例尺"
        )
    mask = np.zeros(shape, dtype=bool)
    mask[row_start:row_end, col_start:col_end] = True
    description = (
        f"矩形 {settings.rectangle_height_mm:g}×{settings.rectangle_width_mm:g} mm，"
        f"实际 {row_end - row_start}×{col_end - col_start} 网格，"
        f"R{row_start + 1}:R{row_end}、C{col_start + 1}:C{col_end}"
    )
    return mask, description, ""


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if math.isclose(denominator, 0.0, abs_tol=1e-15):
        return None
    return numerator / denominator


def _calculate_statistics(values: np.ndarray) -> CalculatedStatistics:
    mean = float(np.mean(values))
    minimum = float(np.min(values))
    maximum = float(np.max(values))
    population_std = float(np.std(values, ddof=0))
    sample_std = float(np.std(values, ddof=1)) if values.size >= 2 else None
    return CalculatedStatistics(
        mean=mean,
        minimum=minimum,
        maximum=maximum,
        min_max_ratio=_safe_ratio(minimum, maximum),
        contrast_ratio=_safe_ratio(maximum - minimum, maximum + minimum),
        relative_population_std=_safe_ratio(population_std, mean),
        relative_sample_std=_safe_ratio(sample_std, mean) if sample_std is not None else None,
    )


def analyze_uniformity_matrix(
    matrix: np.ndarray,
    region_mask: np.ndarray,
    settings: PixelStatisticsSettings,
    pixels_per_mm: float,
) -> MatrixUniformityResult:
    """对非圆形区域等分，在每格中心取正方形均值，再统计这些均值组成的矩阵。"""
    region_points = np.argwhere(region_mask)
    if not len(region_points):
        raise PixelStatisticsError("矩阵均匀性统计区域为空")
    row_start, col_start = region_points.min(axis=0)
    row_end, col_end = region_points.max(axis=0) + 1
    region_rows = int(row_end - row_start)
    region_cols = int(col_end - col_start)
    if settings.matrix_rows > region_rows or settings.matrix_cols > region_cols:
        raise PixelStatisticsError(
            f"矩阵 {settings.matrix_rows}×{settings.matrix_cols} 超过统计区域网格尺寸 {region_rows}×{region_cols}"
        )

    sample_side = max(1, round(settings.matrix_sample_side_mm * pixels_per_mm))
    smallest_cell_rows = region_rows // settings.matrix_rows
    smallest_cell_cols = region_cols // settings.matrix_cols
    if sample_side > min(smallest_cell_rows, smallest_cell_cols):
        max_side_mm = min(smallest_cell_rows, smallest_cell_cols) / pixels_per_mm
        raise PixelStatisticsError(
            f"矩阵中心采样边长 {settings.matrix_sample_side_mm:g} mm 超过单格尺寸，"
            f"当前最大可用边长约 {max_side_mm:.4g} mm"
        )

    row_edges = np.linspace(row_start, row_end, settings.matrix_rows + 1)
    col_edges = np.linspace(col_start, col_end, settings.matrix_cols + 1)
    sample_values = np.empty((settings.matrix_rows, settings.matrix_cols), dtype=float)
    for row_index in range(settings.matrix_rows):
        for col_index in range(settings.matrix_cols):
            center_row = (row_edges[row_index] + row_edges[row_index + 1] - 1.0) / 2.0
            center_col = (col_edges[col_index] + col_edges[col_index + 1] - 1.0) / 2.0
            sample_row_start = math.floor(center_row - (sample_side - 1) / 2.0)
            sample_col_start = math.floor(center_col - (sample_side - 1) / 2.0)
            sample_row_end = sample_row_start + sample_side
            sample_col_end = sample_col_start + sample_side
            sample_mask = region_mask[sample_row_start:sample_row_end, sample_col_start:sample_col_end]
            sample = matrix[sample_row_start:sample_row_end, sample_col_start:sample_col_end]
            valid_values = sample[sample_mask & np.isfinite(sample)]
            if not valid_values.size:
                raise PixelStatisticsError(f"矩阵采样点 R{row_index + 1}C{col_index + 1} 没有有效数字")
            sample_values[row_index, col_index] = float(np.mean(valid_values))

    statistics = _calculate_statistics(sample_values.ravel())
    return MatrixUniformityResult(
        rows=settings.matrix_rows,
        cols=settings.matrix_cols,
        sample_side_mm=settings.matrix_sample_side_mm,
        sample_values=sample_values.tolist(),
        sample_count=sample_values.size,
        mean=statistics.mean,
        minimum=statistics.minimum,
        maximum=statistics.maximum,
        min_max_ratio=statistics.min_max_ratio,
        contrast_ratio=statistics.contrast_ratio,
        relative_population_std=statistics.relative_population_std,
        relative_sample_std=statistics.relative_sample_std,
    )


def analyze_matrix(
    matrix: np.ndarray,
    source_file: str,
    sheet_name: str,
    settings: PixelStatisticsSettings,
    ignored_cells: int = 0,
) -> SheetAnalysis:
    settings.validate()
    if matrix.ndim != 2:
        raise PixelStatisticsError("数据必须是二维矩阵")
    source_rows, source_cols = matrix.shape
    if settings.dimension_mode == "fixed" and (source_rows, source_cols) != (
        settings.expected_rows,
        settings.expected_cols,
    ):
        raise PixelStatisticsError(
            f"工作表尺寸为 {source_rows}×{source_cols}，与固定尺寸 "
            f"{settings.expected_rows}×{settings.expected_cols} 不一致"
        )

    processed = matrix.copy()
    warnings: list[str] = []
    if settings.merge_enabled:
        processed, crop_text = merge_pixel_blocks(
            processed,
            settings.granularity,
            settings.force_odd_grid,
            settings.missing_policy == "ignore",
        )
        warnings.append(crop_text)

    raw_pixels_per_mm = settings.scale_pixels / settings.scale_length_mm
    processed_pixels_per_mm = raw_pixels_per_mm / settings.granularity if settings.merge_enabled else raw_pixels_per_mm
    center_row, center_col, center_description, threshold_outline = locate_center(processed, settings)
    region_mask, region_description, region_warning = build_region_mask(
        processed.shape,
        center_row,
        center_col,
        settings,
        processed_pixels_per_mm,
    )
    if region_warning:
        warnings.append(region_warning)
    valid_values = processed[region_mask & np.isfinite(processed)]
    if not valid_values.size:
        raise PixelStatisticsError("所选统计区域没有有效数字")

    statistics = _calculate_statistics(valid_values)
    matrix_uniformity = None
    if settings.matrix_uniformity_enabled:
        matrix_uniformity = analyze_uniformity_matrix(
            processed,
            region_mask,
            settings,
            processed_pixels_per_mm,
        )
    return SheetAnalysis(
        source_file=source_file,
        sheet_name=sheet_name,
        source_rows=source_rows,
        source_cols=source_cols,
        processed_rows=processed.shape[0],
        processed_cols=processed.shape[1],
        center_row=center_row + 1.0,
        center_col=center_col + 1.0,
        center_description=center_description,
        region_description=region_description,
        sample_count=int(valid_values.size),
        mean=statistics.mean,
        minimum=statistics.minimum,
        maximum=statistics.maximum,
        min_max_ratio=statistics.min_max_ratio,
        contrast_ratio=statistics.contrast_ratio,
        relative_population_std=statistics.relative_population_std,
        relative_sample_std=statistics.relative_sample_std,
        ignored_cells=ignored_cells,
        warning="；".join(warnings),
        processed_matrix=processed if settings.merge_enabled else None,
        raw_pixels_per_mm=raw_pixels_per_mm,
        processed_pixels_per_mm=processed_pixels_per_mm,
        threshold_outline=threshold_outline,
        matrix_uniformity=matrix_uniformity,
    )


def analyze_uploaded_files(files: dict[str, bytes], settings: PixelStatisticsSettings) -> AnalysisBatch:
    settings.validate()
    results: list[SheetAnalysis] = []
    errors: list[str] = []
    for filename, content in files.items():
        try:
            if Path(filename).suffix.lower() in {".csv", ".cvs"}:
                workbook = {"数据": _read_csv_dataframe(content)}
            else:
                workbook = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
        except Exception as exc:
            errors.append(f"{filename}：无法读取表格（{exc}）")
            continue
        for sheet_name, dataframe in workbook.items():
            try:
                matrix, ignored_cells = dataframe_to_numeric_matrix(dataframe, settings.missing_policy)
                results.append(analyze_matrix(matrix, filename, str(sheet_name), settings, ignored_cells))
            except Exception as exc:
                errors.append(f"{filename} / {sheet_name}：{exc}")
    return AnalysisBatch(results=results, errors=errors, settings=settings)


def _read_csv_dataframe(content: bytes) -> pd.DataFrame:
    """读取常见 UTF-8/GBK CSV，并自动识别逗号、分号或制表符。"""
    failures: list[str] = []
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError as exc:
            failures.append(f"{encoding}: {exc}")
            continue

        try:
            return pd.read_csv(io.StringIO(text), header=None, sep=None, engine="python")
        except Exception as inferred_exc:
            # 单列 CSV 可能无法推断分隔符，退回标准逗号读取。
            try:
                return pd.read_csv(io.StringIO(text), header=None, sep=",")
            except Exception as comma_exc:
                failures.append(f"{encoding}: {inferred_exc}; {comma_exc}")
    raise PixelStatisticsError("CSV 编码或分隔符无法识别：" + " | ".join(failures))


def _safe_sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "_", name).strip() or "数据"
    base = base[:31]
    candidate = base
    suffix = 1
    while candidate in used:
        suffix_text = f"_{suffix}"
        candidate = f"{base[: 31 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def build_excel_report(batch: AnalysisBatch) -> bytes:
    output = _ExcelBytesBuffer()
    summary = pd.DataFrame([result.summary_row() for result in batch.results])
    settings_rows = [{"参数": key, "值": value} for key, value in asdict(batch.settings).items()]
    if batch.errors:
        settings_rows.append({"参数": "处理错误", "值": "\n".join(batch.errors)})

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="统计汇总", index=False)
        pd.DataFrame(settings_rows).to_excel(writer, sheet_name="处理参数", index=False)
        used_names = {"统计汇总", "处理参数"}
        for result in batch.results:
            stem = Path(result.source_file).stem
            if result.processed_matrix is not None:
                sheet_name = _safe_sheet_name(f"{stem}_{result.sheet_name}", used_names)
                pd.DataFrame(result.processed_matrix).to_excel(writer, sheet_name=sheet_name, index=False, header=False)
            if result.matrix_uniformity is not None:
                matrix_sheet_name = _safe_sheet_name(f"矩阵_{stem}_{result.sheet_name}", used_names)
                pd.DataFrame(result.matrix_uniformity.sample_values).to_excel(
                    writer,
                    sheet_name=matrix_sheet_name,
                    index=False,
                    header=False,
                )
    return output.getvalue()


def _threshold_outline_chart_options(result: SheetAnalysis) -> dict[str, Any]:
    outline = result.threshold_outline
    if outline is None:
        return {}
    pixels_per_mm = result.processed_pixels_per_mm
    edge_data = [
        [
            round(col / pixels_per_mm, 6),
            round(row / pixels_per_mm, 6),
            round(point_value, 6),
        ]
        for (col, row), point_value in zip(outline.edge_points, outline.edge_values)
    ]
    center_data = [
        [
            round((result.center_col - 1.0) / pixels_per_mm, 6),
            round((result.center_row - 1.0) / pixels_per_mm, 6),
        ]
    ]
    full_width_mm = result.processed_cols / pixels_per_mm
    full_height_mm = result.processed_rows / pixels_per_mm
    max_grid_size = 480
    if full_width_mm >= full_height_mm:
        grid_width = max_grid_size
        grid_height = max(1, round(max_grid_size * full_height_mm / full_width_mm))
    else:
        grid_height = max_grid_size
        grid_width = max(1, round(max_grid_size * full_width_mm / full_height_mm))
    axis_label_formatter = "value => String(Number(Number(value).toFixed(2)))"
    tooltip_formatter = """
        params => {
            const value = Array.isArray(params.value) ? params.value : [];
            const format = number => String(Number(Number(number).toFixed(4)));
            const lines = [
                params.marker + params.seriesName,
                'X：' + format(value[0]) + ' mm',
                'Y：' + format(value[1]) + ' mm',
            ];
            if (params.seriesName === '阈值区域边缘') {
                lines.push('边缘点数值：' + format(value[2]));
                lines.push('判定阈值：' + THRESHOLD_VALUE);
            }
            return lines.join('<br/>');
        }
    """.replace("THRESHOLD_VALUE", json.dumps(round(outline.threshold, 6)))
    return {
        "animation": False,
        # 显示整张数据，并按其物理长宽比设置绘图区，保证横纵方向毫米单位等比例。
        "grid": {"width": grid_width, "height": grid_height, "left": "center", "top": 72},
        "legend": {"top": 8, "data": ["阈值区域边缘", "计算中心"]},
        "tooltip": {"trigger": "item", ":formatter": tooltip_formatter},
        "xAxis": {
            "type": "value",
            "name": "列方向距离（mm）",
            "nameLocation": "middle",
            "nameGap": 35,
            "scale": True,
            "min": 0,
            "max": full_width_mm,
            "axisLabel": {":formatter": axis_label_formatter},
        },
        "yAxis": {
            "type": "value",
            "name": "行方向距离（mm）",
            "nameLocation": "middle",
            "nameGap": 50,
            "inverse": True,
            "scale": True,
            "min": 0,
            "max": full_height_mm,
            "axisLabel": {":formatter": axis_label_formatter},
        },
        "series": [
            {
                "name": "阈值区域边缘",
                "type": "scatter",
                "data": edge_data,
                "encode": {"x": 0, "y": 1},
                "symbolSize": 4,
                "itemStyle": {"color": "#2563eb", "opacity": 0.8},
            },
            {
                "name": "计算中心",
                "type": "scatter",
                "data": center_data,
                "encode": {"x": 0, "y": 1},
                "symbol": "cross",
                "symbolSize": 18,
                "itemStyle": {"color": "#dc2626"},
                "label": {
                    "show": True,
                    "position": "top",
                    "formatter": f"中心 ({center_data[0][0]:g}, {center_data[0][1]:g}) mm",
                    "color": "#b91c1c",
                },
            },
        ],
    }


class PixelStatisticsTool:
    """像素矩阵统计分析的 NiceGUI 工具界面。"""

    def __init__(self) -> None:
        self.uploaded_files: dict[str, bytes] = {}
        self.batch: AnalysisBatch | None = None
        self.upload_control: Any = None
        self.analyze_button: Any = None
        self.export_button: Any = None

    def show(self, parent_dialog: ui.dialog) -> None:
        with ui.column().classes("w-full min-h-screen bg-slate-50 absolute inset-0 p-3 md:p-5 overflow-auto"):
            with ui.row().classes("w-full justify-between items-center mb-2"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("analytics", size="md").classes("text-blue-600")
                    with ui.column().classes("gap-0"):
                        ui.label("像素数据统计分析").classes("text-xl font-bold text-gray-800")
                        ui.label("可选分块平均、自动定位中心、区域统计与 Excel 导出").classes("text-xs text-gray-500")
                ui.button("退出工具", on_click=parent_dialog.close).props("outline color=negative icon=close size=sm")

            input_layout = ui.element("div").classes(
                "w-full grid grid-cols-1 xl:grid-cols-[minmax(340px,1fr)_minmax(760px,3fr)] gap-3 items-stretch"
            )

            with ui.card().classes(
                "pixel-statistics-upload-card w-full h-full min-h-0 overflow-hidden p-4 border shadow-sm flex flex-col"
            ) as upload_card:
                ui.label("1. 上传数据").classes("font-bold text-gray-700")
                self.upload_control = (
                    ui.upload(
                        on_upload=self._handle_upload,
                        multiple=True,
                        auto_upload=True,
                        label="选择或添加文件",
                    )
                    .classes("w-full pixel-statistics-upload")
                    .props('accept=".xlsx,.xlsm,.xls,.csv,.cvs" max-files="20" max-file-size="52428800"')
                )
                ui.add_css(
                    """
                    .pixel-statistics-upload .q-uploader__list {
                        display: none !important;
                    }
                    .pixel-statistics-upload .q-uploader__file-status {
                        display: none !important;
                    }
                    .pixel-statistics-upload .q-uploader__subtitle {
                        display: none !important;
                    }
                    @media (min-width: 1280px) {
                        .pixel-statistics-upload-card {
                            contain: size;
                        }
                    }
                    """
                )
                ui.label("支持 Excel、CSV（含 UTF-8/GBK）；按无表头二维数值矩阵读取，单文件上限 50 MB。").classes(
                    "text-xs text-gray-500"
                )
                with ui.scroll_area().classes("w-full flex-1 min-h-0"):
                    self.render_file_list()

            with ui.card().classes("w-full h-full p-4 border shadow-sm") as settings_card:
                ui.label("2. 常用分析设置").classes("font-bold text-gray-700")
                common_card_classes = "w-full h-full min-h-36 gap-2 rounded-lg border bg-slate-50 p-3"
                with ui.element("div").classes(
                    "w-full grid grid-cols-1 sm:grid-cols-2 2xl:grid-cols-5 gap-3 items-stretch"
                ):
                    with ui.column().classes(common_card_classes):
                        ui.label("比例尺").classes("text-sm font-semibold text-gray-700")
                        with ui.element("div").classes("w-full grid grid-cols-2 gap-2"):
                            self.scale_pixels_input = (
                                ui.number("原始像素点", value=1, min=0.000001, step=1)
                                .props("outlined dense")
                                .classes("w-full")
                            )
                            self.scale_length_input = (
                                ui.number("对应长度 mm", value=1, min=0.000001, step=0.1)
                                .props("outlined dense")
                                .classes("w-full")
                            )
                        ui.label("例：100 像素对应 5 mm").classes("text-xs text-gray-400")

                    with ui.column().classes(common_card_classes):
                        ui.label("中心定位").classes("text-sm font-semibold text-gray-700")
                        self.center_mode_input = (
                            ui.select(
                                {
                                    "geometric": "数据全局中心",
                                    "maximum": "全局最大值位置",
                                    "threshold": "最大值百分比区域中心",
                                    "manual": "手工指定中心",
                                },
                                value="geometric",
                                label="中心算法",
                            )
                            .props("outlined dense")
                            .classes("w-full")
                        )
                        self.threshold_percent_input = (
                            ui.number("最大值阈值（%）", value=10, min=0.1, max=100, step=0.1)
                            .props("outlined dense")
                            .classes("w-40 max-w-full")
                            .bind_visibility_from(self.center_mode_input, "value", lambda value: value == "threshold")
                        )
                        with (
                            ui.element("div")
                            .classes("w-full grid grid-cols-2 gap-2")
                            .bind_visibility_from(self.center_mode_input, "value", lambda value: value == "manual")
                        ):
                            self.manual_row_input = (
                                ui.number("中心行", value=512, min=1).props("outlined dense").classes("w-full")
                            )
                            self.manual_col_input = (
                                ui.number("中心列", value=725, min=1).props("outlined dense").classes("w-full")
                            )

                    with ui.column().classes(common_card_classes):
                        ui.label("统计范围").classes("text-sm font-semibold text-gray-700")
                        self.region_mode_input = (
                            ui.select(
                                {"full": "全域", "circle": "圆形区域", "rectangle": "矩形区域"},
                                value="circle",
                                label="范围形状",
                            )
                            .props("outlined dense")
                            .classes("w-full")
                        )
                        self.radius_input = (
                            ui.number("圆形半径（mm）", value=100, min=0)
                            .props("outlined dense")
                            .classes("w-40 max-w-full")
                            .bind_visibility_from(self.region_mode_input, "value", lambda value: value == "circle")
                        )
                        with (
                            ui.element("div")
                            .classes("w-full grid grid-cols-2 gap-2")
                            .bind_visibility_from(self.region_mode_input, "value", lambda value: value == "rectangle")
                        ):
                            self.rectangle_rows_input = (
                                ui.number("高度 mm", value=336, min=0.000001, step=0.1)
                                .props("outlined dense")
                                .classes("w-full")
                            )
                            self.rectangle_cols_input = (
                                ui.number("宽度 mm", value=596, min=0.000001, step=0.1)
                                .props("outlined dense")
                                .classes("w-full")
                            )

                    with (
                        ui.column()
                        .classes(common_card_classes)
                        .bind_visibility_from(self.region_mode_input, "value", lambda value: value != "circle")
                    ):
                        ui.label("矩阵均匀性").classes("text-sm font-semibold text-gray-700")
                        self.matrix_uniformity_input = ui.checkbox("启用矩阵采样", value=False).props("dense")
                        with (
                            ui.element("div")
                            .classes("w-full grid grid-cols-2 gap-2")
                            .bind_visibility_from(self.matrix_uniformity_input, "value")
                        ):
                            self.matrix_cols_input = (
                                ui.number("横向等分", value=3, min=1, step=1).props("outlined dense").classes("w-full")
                            )
                            self.matrix_rows_input = (
                                ui.number("竖向等分", value=3, min=1, step=1).props("outlined dense").classes("w-full")
                            )
                        self.matrix_sample_side_input = (
                            ui.number("中心采样边长（mm）", value=1, min=0.000001, step=0.1)
                            .props("outlined dense")
                            .classes("w-full")
                            .bind_visibility_from(self.matrix_uniformity_input, "value")
                        )
                        ui.label("每格中心方块先求平均，再统计采样矩阵").classes(
                            "text-xs text-gray-400"
                        ).bind_visibility_from(self.matrix_uniformity_input, "value")

                    with ui.column().classes(common_card_classes):
                        ui.label("颗粒度").classes("text-sm font-semibold text-gray-700")
                        self.merge_input = ui.checkbox("统计前进行单元格合并", value=False).props("dense")
                        self.granularity_input = (
                            ui.number("合并颗粒度", value=10, min=1, step=1)
                            .props("outlined dense")
                            .classes("w-40 max-w-full")
                            .bind_visibility_from(self.merge_input, "value")
                        )

                with ui.expansion("高阶设置（通常无需修改）", icon="tune", value=False).classes(
                    "w-full mt-2 border rounded-lg bg-white"
                ):
                    with ui.element("div").classes(
                        "w-full grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 p-3 items-start"
                    ):
                        self.dimension_mode_input = (
                            ui.select(
                                {"actual": "按表格实际尺寸", "fixed": "严格指定尺寸"},
                                value="actual",
                                label="统计范围策略",
                            )
                            .props("outlined dense")
                            .classes("w-56 max-w-full")
                        )
                        self.expected_rows_input = (
                            ui.number("固定行数", value=1024, min=1, step=1)
                            .props("outlined dense")
                            .classes("w-40 max-w-full")
                            .bind_visibility_from(self.dimension_mode_input, "value", lambda value: value == "fixed")
                        )
                        self.expected_cols_input = (
                            ui.number("固定列数", value=1280, min=1, step=1)
                            .props("outlined dense")
                            .classes("w-40 max-w-full")
                            .bind_visibility_from(self.dimension_mode_input, "value", lambda value: value == "fixed")
                        )
                        self.missing_policy_input = (
                            ui.select(
                                {"strict": "严格校验（推荐）", "ignore": "忽略空白/非数字"},
                                value="strict",
                                label="异常单元格",
                            )
                            .props("outlined dense")
                            .classes("w-56 max-w-full")
                        )
                        self.odd_grid_input = ui.checkbox("合并后保持奇数行列", value=True).bind_visibility_from(
                            self.merge_input, "value"
                        )

            upload_card.move(input_layout)
            settings_card.move(input_layout)

            with ui.row().classes("w-full justify-end gap-3"):
                ui.button("清空", on_click=self._reset).props("outline color=grey icon=restart_alt")
                self.analyze_button = ui.button("开始计算", on_click=self._analyze).props(
                    "color=primary icon=play_arrow"
                )
                self.export_button = ui.button("导出 Excel", on_click=self._export).props(
                    "color=positive icon=file_download"
                )
                self.export_button.disable()

            self.render_results()

    @ui.refreshable_method
    def render_file_list(self) -> None:
        if not self.uploaded_files:
            ui.label("尚未上传文件").classes("text-sm text-gray-400")
            return
        total_bytes = sum(len(content) for content in self.uploaded_files.values())
        ui.label(
            f"当前 {len(self.uploaded_files)} 个文件，共 {total_bytes / 1024 / 1024:.2f} MB"
        ).classes("text-xs text-gray-500")
        with ui.row().classes("w-full gap-2 flex-wrap content-start"):
            for filename, content in self.uploaded_files.items():
                with ui.chip(icon="description").props("outline color=primary"):
                    ui.label(f"{filename}（{len(content) / 1024 / 1024:.2f} MB）")
                    ui.icon("close", size="xs").classes("cursor-pointer").on(
                        "click", lambda _=None, name=filename: self._remove_file(name)
                    )

    @ui.refreshable_method
    def render_results(self) -> None:
        if self.batch is None:
            return
        with ui.card().classes("w-full p-4 border shadow-sm"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label("3. 统计结果").classes("font-bold text-gray-700")
                ui.label(f"成功 {len(self.batch.results)} 个工作表，失败 {len(self.batch.errors)} 个").classes(
                    "text-sm text-gray-500"
                )
            if self.batch.results:
                all_rows = [result.summary_row(formatted=True) for result in self.batch.results]
                primary_fields = [
                    "文件",
                    "工作表",
                    "有效样本数",
                    "平均值",
                    "最小值",
                    "最大值",
                    "最小/最大",
                    "(最大-最小)/(最大+最小)",
                    "相对总体标准差",
                    "相对样本标准差",
                ]
                primary_rows = [{field: row[field] for field in primary_fields} for row in all_rows]
                primary_columns = [
                    {"name": field, "label": field, "field": field, "align": "center", "sortable": True}
                    for field in primary_fields
                ]
                ui.table(
                    columns=primary_columns,
                    rows=primary_rows,
                    pagination={"rowsPerPage": 10},
                ).classes("w-full").props("dense flat bordered wrap-cells")

                matrix_rows = [row for result, row in zip(self.batch.results, all_rows) if result.matrix_uniformity]
                if matrix_rows:
                    ui.label("矩阵均匀性统计结果").classes("mt-3 text-sm font-semibold text-blue-700")
                    matrix_fields = [
                        "文件",
                        "工作表",
                        "矩阵划分",
                        "矩阵中心采样边长(mm)",
                        "矩阵有效采样数",
                        "矩阵平均值",
                        "矩阵最小值",
                        "矩阵最大值",
                        "矩阵最小/最大",
                        "矩阵(最大-最小)/(最大+最小)",
                        "矩阵相对总体标准差",
                        "矩阵相对样本标准差",
                    ]
                    matrix_table_rows = [{field: row[field] for field in matrix_fields} for row in matrix_rows]
                    matrix_columns = [
                        {"name": field, "label": field, "field": field, "align": "center", "sortable": True}
                        for field in matrix_fields
                    ]
                    ui.table(
                        columns=matrix_columns,
                        rows=matrix_table_rows,
                        pagination={"rowsPerPage": 10},
                    ).classes("w-full").props("dense flat bordered wrap-cells")
                detail_fields = [
                    "文件",
                    "工作表",
                    "原始尺寸",
                    "处理后尺寸",
                    "原始比例尺",
                    "处理后比例尺",
                    "中心坐标",
                    "中心算法",
                    "统计区域",
                    "忽略单元格",
                    "提示",
                ]
                with ui.expansion("查看处理、定位与范围明细", icon="manage_search", value=False).classes(
                    "w-full mt-2 border rounded-lg bg-slate-50"
                ):
                    detail_rows = [{field: row[field] for field in detail_fields} for row in all_rows]
                    detail_columns = [
                        {"name": field, "label": field, "field": field, "align": "center", "sortable": True}
                        for field in detail_fields
                    ]
                    ui.table(
                        columns=detail_columns,
                        rows=detail_rows,
                        pagination={"rowsPerPage": 10},
                    ).classes("w-full").props("dense flat bordered wrap-cells")
            if self.batch.errors:
                with ui.expansion(f"查看 {len(self.batch.errors)} 条处理失败信息", icon="warning").classes(
                    "w-full bg-red-50 text-red-800"
                ):
                    for message in self.batch.errors:
                        ui.label(message).classes("text-sm")

        outline_results = [result for result in self.batch.results if result.threshold_outline is not None]
        if outline_results:
            with ui.expansion(
                "4. 阈值区域边缘与中心示意图",
                icon="center_focus_strong",
                value=False,
            ).classes("w-full border rounded-lg bg-white shadow-sm"):
                ui.label(
                    "蓝色点表示最大值百分比主连通区域的边缘，红色十字表示区域几何中心；"
                    "坐标为相对表格起点的毫米距离，横纵方向按 1:1 比例显示。"
                ).classes("text-xs text-gray-500")
                with ui.element("div").classes("w-full grid grid-cols-1 xl:grid-cols-2 gap-4"):
                    for result in outline_results:
                        outline = result.threshold_outline
                        if outline is None:
                            continue
                        with ui.card().classes("w-full p-3 bg-slate-50 border"):
                            ui.label(f"{result.source_file} / {result.sheet_name}").classes(
                                "font-semibold text-gray-700"
                            )
                            ui.label(
                                f"阈值 {outline.threshold:.4g}，区域 {outline.region_points} 点，"
                                f"中心 R{result.center_row:.2f} / C{result.center_col:.2f}"
                            ).classes("text-xs text-gray-500")
                            with ui.element("div").classes("w-full overflow-x-auto"):
                                with ui.element("div").style(
                                    "width: 640px; height: 640px; min-width: 640px; min-height: 640px; "
                                    "margin: 0 auto; flex: 0 0 640px;"
                                ):
                                    ui.echart(_threshold_outline_chart_options(result)).style(
                                        "width: 640px; height: 640px; min-width: 640px; min-height: 640px;"
                                    )

    async def _handle_upload(self, event: Any) -> None:
        try:
            filename = str(event.file.name)
            extension = Path(filename).suffix.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise PixelStatisticsError("仅支持 Excel 和 CSV 文件")
            read_result = event.file.read()
            content = await read_result if asyncio.iscoroutine(read_result) else read_result
            if len(content) > MAX_UPLOAD_BYTES:
                raise PixelStatisticsError("单个文件不能超过 50 MB")
            self.uploaded_files[filename] = content
            self.batch = None
            if self.export_button is not None:
                self.export_button.disable()
            self.render_file_list.refresh()
            self.render_results.refresh()
            ui.notify(f"已载入 {filename}", type="positive")
        except Exception as exc:
            ui.notify(f"上传失败：{exc}", type="negative")

    def _remove_file(self, filename: str) -> None:
        self.uploaded_files.pop(filename, None)
        if self.upload_control is not None:
            encoded_name = json.dumps(filename, ensure_ascii=False)
            self.upload_control.client.run_javascript(
                f"""
                const wrapper = getElement({self.upload_control.id});
                const uploader = wrapper?.$refs?.qRef;
                const file = uploader?.files?.find(item => item.name === {encoded_name});
                if (file) uploader.removeFile(file);
                """
            )
        self.batch = None
        if self.export_button is not None:
            self.export_button.disable()
        self.render_file_list.refresh()
        self.render_results.refresh()

    def _settings_from_ui(self) -> PixelStatisticsSettings:
        def integer(element: Any, label: str) -> int:
            value = float(element.value)
            if not value.is_integer():
                raise PixelStatisticsError(f"{label}必须是整数")
            return int(value)

        region_mode = str(self.region_mode_input.value)
        return PixelStatisticsSettings(
            merge_enabled=bool(self.merge_input.value),
            granularity=integer(self.granularity_input, "合并颗粒度"),
            force_odd_grid=bool(self.odd_grid_input.value),
            dimension_mode=str(self.dimension_mode_input.value),
            expected_rows=integer(self.expected_rows_input, "固定行数"),
            expected_cols=integer(self.expected_cols_input, "固定列数"),
            missing_policy=str(self.missing_policy_input.value),
            scale_pixels=float(self.scale_pixels_input.value),
            scale_length_mm=float(self.scale_length_input.value),
            region_mode=region_mode,
            radius_mm=float(self.radius_input.value),
            rectangle_height_mm=float(self.rectangle_rows_input.value),
            rectangle_width_mm=float(self.rectangle_cols_input.value),
            center_mode=str(self.center_mode_input.value),
            manual_center_row=float(self.manual_row_input.value),
            manual_center_col=float(self.manual_col_input.value),
            threshold_percent=float(self.threshold_percent_input.value),
            matrix_uniformity_enabled=(bool(self.matrix_uniformity_input.value) and region_mode != "circle"),
            matrix_rows=integer(self.matrix_rows_input, "矩阵竖向等分数"),
            matrix_cols=integer(self.matrix_cols_input, "矩阵横向等分数"),
            matrix_sample_side_mm=float(self.matrix_sample_side_input.value),
        )

    async def _analyze(self) -> None:
        if not self.uploaded_files:
            ui.notify("请先上传 Excel 文件", type="warning")
            return
        try:
            settings = self._settings_from_ui()
            settings.validate()
        except Exception as exc:
            ui.notify(f"参数错误：{exc}", type="negative")
            return

        self.analyze_button.disable()
        self.export_button.disable()
        ui.notify("正在读取并计算，请稍候…", type="ongoing", timeout=1500)
        try:
            self.batch = await run.io_bound(analyze_uploaded_files, dict(self.uploaded_files), settings)
            self.render_results.refresh()
            if self.batch.results:
                self.export_button.enable()
                ui.notify(f"已完成 {len(self.batch.results)} 个工作表的统计", type="positive")
            else:
                ui.notify("没有工作表成功完成统计，请查看失败信息", type="negative")
        except Exception as exc:
            ui.notify(f"统计失败：{exc}", type="negative")
        finally:
            self.analyze_button.enable()

    async def _export(self) -> None:
        if self.batch is None or not self.batch.results:
            ui.notify("暂无可导出的统计结果", type="warning")
            return
        try:
            content = await run.io_bound(build_excel_report, self.batch)
            filename = f"像素数据统计结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            ui.download(content, filename=filename)
            ui.notify("统计结果已生成", type="positive")
        except Exception as exc:
            ui.notify(f"导出失败：{exc}", type="negative")

    def _reset(self) -> None:
        self.uploaded_files.clear()
        if self.upload_control is not None:
            self.upload_control.reset()
        self.batch = None
        if self.export_button is not None:
            self.export_button.disable()
        self.render_file_list.refresh()
        self.render_results.refresh()
        ui.notify("已清空上传文件和统计结果", type="info")
