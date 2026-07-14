# -*- encoding: utf-8 -*-
"""光谱与光源色度指标的解析、计算和比较。"""

from __future__ import annotations

import base64
import csv
import importlib
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO, StringIO
from itertools import combinations
from typing import Any

import numpy as np

colour: Any | None = None


CALCULATION_START_NM = 360
CALCULATION_END_NM = 780
MIN_REQUIRED_START_NM = 380
MAX_SPECTRA_COUNT = 12
CRI_CHROMATICITY_DISTANCE_LIMIT = 5.4e-3
COORDINATE_SYSTEMS = {
    "xy": "CIE 1931 xy",
    "uv": "CIE 1960 UCS uv",
    "upvp": "CIE 1976 UCS u′v′",
    "XYZ": "CIE XYZ",
}
STANDARD_ILLUMINANTS = {
    "A": "标准 A 光源（钨丝灯）",
    "D50": "CIE D50（日光 5000 K）",
    "D55": "CIE D55（日光 5500 K）",
    "D65": "CIE D65（日光 6500 K）",
    "D75": "CIE D75（日光 7500 K）",
    "E": "CIE E（等能白光）",
    "FL2": "CIE FL2（冷白荧光灯）",
    "FL11": "CIE FL11（三基色荧光灯）",
    "LED-B3": "CIE LED-B3（蓝光激发 LED）",
    "LED-B5": "CIE LED-B5（蓝光激发 LED）",
    "LED-BH1": "CIE LED-BH1（混合型 LED）",
    "LED-RGB1": "CIE LED-RGB1（RGB LED）",
}


class SpectralAnalysisError(ValueError):
    """表示输入数据或色度计算不满足要求。"""


@dataclass(frozen=True)
class SpectrumInput:
    """一条待计算的相对光谱功率分布。"""

    name: str
    wavelengths: tuple[float, ...]
    values: tuple[float, ...]


@dataclass(frozen=True)
class CRIColorSampleResult:
    """一个 CRI 测试色样在测试源与参考源下的色坐标结果。"""

    index: int
    score: float
    test_xy: tuple[float, float]
    reference_xy: tuple[float, float]
    test_upvp: tuple[float, float]
    reference_upvp: tuple[float, float]


@dataclass(frozen=True)
class SpectrumResult:
    """一条光谱的色度、显色和绘图结果。"""

    name: str
    wavelengths: tuple[float, ...]
    values: tuple[float, ...]
    normalized_values: tuple[float, ...]
    XYZ: tuple[float, float, float]
    xy: tuple[float, float]
    uv: tuple[float, float]
    upvp: tuple[float, float]
    cct: float | None
    duv: float | None
    cri_reference_distance: float | None
    reference_name: str | None
    reference_xy: tuple[float, float] | None
    reference_uv: tuple[float, float] | None
    reference_upvp: tuple[float, float] | None
    ra: float | None
    ri: tuple[tuple[int, float], ...]
    cri_samples: tuple[CRIColorSampleResult, ...]
    rf: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ChromaticityResult:
    """一个输入色坐标转换后的统一结果。"""

    name: str
    XYZ: tuple[float, float, float]
    xy: tuple[float, float]
    uv: tuple[float, float]
    upvp: tuple[float, float]
    cct: float | None
    duv: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ChromaticityDistance:
    """两个色坐标点之间的坐标距离。"""

    first_name: str
    second_name: str
    delta_xy: float
    delta_upvp: float


def _require_colour() -> Any:
    """延迟导入 Colour，避免工具页启动时加载整套标准数据。"""

    global colour
    if colour is None:
        try:
            colour = importlib.import_module("colour")
        except ImportError as exc:  # pragma: no cover - 仅在部署环境漏装依赖时触发
            raise SpectralAnalysisError("缺少 colour-science 0.4.7，请先安装该计算依赖") from exc
    return colour


def _split_row(line: str) -> list[str]:
    """按 Excel 粘贴、CSV、分号或空白分隔一行数据。"""

    if "\t" in line:
        return [cell.strip() for cell in line.split("\t")]
    if "," in line:
        return [cell.strip() for cell in next(csv.reader(StringIO(line)))]
    if ";" in line:
        return [cell.strip() for cell in line.split(";")]
    return [cell.strip() for cell in re.split(r"\s+", line.strip())]


def _finite_float(value: str, *, row_number: int, field_name: str) -> float:
    """把单元格转换为有限浮点数。"""

    try:
        numeric = float(value)
    except ValueError as exc:
        raise SpectralAnalysisError(f"第 {row_number} 行的{field_name}不是数字：{value}") from exc
    if not math.isfinite(numeric):
        raise SpectralAnalysisError(f"第 {row_number} 行的{field_name}必须是有限数字")
    return numeric


def _is_finite_number(value: str) -> bool:
    try:
        return math.isfinite(float(value))
    except ValueError:
        return False


def _unique_names(raw_names: list[str]) -> list[str]:
    """校验并返回不重复的光谱名称。"""

    names = [name.strip() or f"光谱{index + 1}" for index, name in enumerate(raw_names)]
    folded = [name.casefold() for name in names]
    if len(set(folded)) != len(folded):
        raise SpectralAnalysisError("光谱名称不能重复")
    return names


def parse_spectral_text(raw_text: str) -> list[SpectrumInput]:
    """解析共享波长列加一列或多列光谱值的粘贴文本。"""

    numbered_rows = [
        (line_number, _split_row(line))
        for line_number, line in enumerate(str(raw_text or "").splitlines(), start=1)
        if line.strip()
    ]
    if not numbered_rows:
        raise SpectralAnalysisError("请粘贴波长和至少一列光谱数据")

    first_row_number, first_cells = numbered_rows[0]
    has_header = not first_cells or not _is_finite_number(first_cells[0])
    if has_header:
        if len(first_cells) < 2:
            raise SpectralAnalysisError("表头至少需要包含波长列和一列光谱名称")
        names = _unique_names(first_cells[1:])
        data_rows = numbered_rows[1:]
    else:
        if len(first_cells) < 2:
            raise SpectralAnalysisError(f"第 {first_row_number} 行至少需要两列数据")
        names = [f"光谱{index + 1}" for index in range(len(first_cells) - 1)]
        data_rows = numbered_rows

    if not data_rows:
        raise SpectralAnalysisError("表头下方没有光谱数据")
    if len(names) > MAX_SPECTRA_COUNT:
        raise SpectralAnalysisError(f"一次最多比较 {MAX_SPECTRA_COUNT} 条光谱")

    expected_columns = len(names) + 1
    wavelengths: list[float] = []
    value_columns: list[list[float]] = [[] for _ in names]
    for row_number, cells in data_rows:
        if len(cells) != expected_columns or any(cell == "" for cell in cells):
            raise SpectralAnalysisError(f"第 {row_number} 行应恰好包含 {expected_columns} 个非空单元格")
        wavelength = _finite_float(cells[0], row_number=row_number, field_name="波长")
        if wavelength <= 0:
            raise SpectralAnalysisError(f"第 {row_number} 行的波长必须大于 0")
        wavelengths.append(wavelength)
        for index, cell in enumerate(cells[1:]):
            value = _finite_float(cell, row_number=row_number, field_name=f"{names[index]}光谱值")
            if value < 0:
                raise SpectralAnalysisError(f"第 {row_number} 行的光谱值不能为负数")
            value_columns[index].append(value)

    if len(wavelengths) < 2:
        raise SpectralAnalysisError("每条光谱至少需要两个波长点")
    if len(set(wavelengths)) != len(wavelengths):
        raise SpectralAnalysisError("波长列存在重复值")

    order = np.argsort(np.asarray(wavelengths, dtype=float))
    sorted_wavelengths = tuple(float(wavelengths[index]) for index in order)
    spectra: list[SpectrumInput] = []
    for name, values in zip(names, value_columns):
        sorted_values = tuple(float(values[index]) for index in order)
        if not any(value > 0 for value in sorted_values):
            raise SpectralAnalysisError(f"{name} 的光谱值不能全部为 0")
        spectra.append(SpectrumInput(name, sorted_wavelengths, sorted_values))
    return spectra


def _xy_to_uv(xy: tuple[float, float]) -> tuple[float, float]:
    x, y = xy
    denominator = -2 * x + 12 * y + 3
    if abs(denominator) < 1e-12:
        raise SpectralAnalysisError("色坐标无法转换到 CIE 1960 UCS")
    return 4 * x / denominator, 6 * y / denominator


def _uv_to_xy(uv: tuple[float, float]) -> tuple[float, float]:
    u, v = uv
    denominator = 2 * u - 8 * v + 4
    if abs(denominator) < 1e-12:
        raise SpectralAnalysisError("色坐标无法转换到 CIE 1931 xy")
    return 3 * u / denominator, 2 * v / denominator


def _normalized_XYZ_from_xy(xy: tuple[float, float]) -> tuple[float, float, float]:
    x, y = xy
    if y <= 0:
        raise SpectralAnalysisError("y 坐标必须大于 0 才能生成 XYZ")
    return 100 * x / y, 100.0, 100 * (1 - x - y) / y


def _validate_xy(xy: tuple[float, float]) -> None:
    x, y = xy
    if x < 0 or y <= 0 or x > 1 or y > 1 or x + y > 1 + 1e-9:
        raise SpectralAnalysisError(f"无效的 CIE xy 坐标：({x:.6g}, {y:.6g})")


def _cct_duv_from_uv(uv: tuple[float, float]) -> tuple[float | None, float | None, list[str]]:
    """由 CIE 1960 uv 计算 Robertson CCT 和有符号 Duv。"""

    colour_module = _require_colour()
    warnings: list[str] = []
    try:
        values = np.asarray(colour_module.uv_to_CCT(uv, method="Robertson 1968"), dtype=float)
        cct, duv = float(values[0]), float(values[1])
    except Exception:
        return None, None, ["该色坐标无法计算有效的相关色温"]
    if not math.isfinite(cct) or not math.isfinite(duv) or cct <= 0:
        return None, None, ["该色坐标无法计算有效的相关色温"]
    if abs(duv) > 0.05:
        warnings.append("该点距离普朗克轨迹较远，CCT 的工程意义有限")
    return cct, duv, warnings


def _coordinate_result(
    name: str,
    values: tuple[float, ...],
    coordinate_system: str,
) -> ChromaticityResult:
    """把一种输入坐标转换为统一的 XYZ、xy、uv 与 u′v′。"""

    if coordinate_system == "xy":
        xy = (values[0], values[1])
        _validate_xy(xy)
        XYZ = _normalized_XYZ_from_xy(xy)
        uv = _xy_to_uv(xy)
    elif coordinate_system == "uv":
        uv = (values[0], values[1])
        xy = _uv_to_xy(uv)
        _validate_xy(xy)
        XYZ = _normalized_XYZ_from_xy(xy)
    elif coordinate_system == "upvp":
        uv = (values[0], values[1] * 2 / 3)
        xy = _uv_to_xy(uv)
        _validate_xy(xy)
        XYZ = _normalized_XYZ_from_xy(xy)
    elif coordinate_system == "XYZ":
        X, Y, Z = values
        if min(X, Y, Z) < 0 or X + Y + Z <= 0 or Y <= 0:
            raise SpectralAnalysisError("XYZ 必须为非负数，且 Y 和三刺激值总和必须大于 0")
        scale = 100 / Y
        XYZ = (X * scale, 100.0, Z * scale)
        total = sum(XYZ)
        xy = (XYZ[0] / total, XYZ[1] / total)
        _validate_xy(xy)
        uv = _xy_to_uv(xy)
    else:
        raise SpectralAnalysisError("不支持的色坐标类型")

    upvp = (uv[0], uv[1] * 1.5)
    cct, duv, warnings = _cct_duv_from_uv(uv)
    return ChromaticityResult(name, XYZ, xy, uv, upvp, cct, duv, tuple(warnings))


def parse_chromaticity_text(raw_text: str, coordinate_system: str) -> list[ChromaticityResult]:
    """解析带可选名称列的多个色坐标。"""

    if coordinate_system not in COORDINATE_SYSTEMS:
        raise SpectralAnalysisError("请选择有效的色坐标类型")
    dimension = 3 if coordinate_system == "XYZ" else 2
    numbered_rows = [
        (line_number, _split_row(line))
        for line_number, line in enumerate(str(raw_text or "").splitlines(), start=1)
        if line.strip()
    ]
    if not numbered_rows:
        raise SpectralAnalysisError("请粘贴至少一个色坐标")

    _, first_cells = numbered_rows[0]
    coordinate_cells = first_cells[-dimension:] if len(first_cells) >= dimension else []
    if len(coordinate_cells) != dimension or not all(_is_finite_number(cell) for cell in coordinate_cells):
        numbered_rows = numbered_rows[1:]
    if not numbered_rows:
        raise SpectralAnalysisError("表头下方没有色坐标数据")

    results: list[ChromaticityResult] = []
    used_names: set[str] = set()
    for item_index, (row_number, cells) in enumerate(numbered_rows, start=1):
        if len(cells) == dimension and all(_is_finite_number(cell) for cell in cells):
            name = f"坐标{item_index}"
            numeric_cells = cells
        elif len(cells) == dimension + 1 and all(_is_finite_number(cell) for cell in cells[-dimension:]):
            name = cells[0].strip() or f"坐标{item_index}"
            numeric_cells = cells[-dimension:]
        else:
            raise SpectralAnalysisError(
                f"第 {row_number} 行应包含可选名称和 {dimension} 个坐标值"
            )
        folded_name = name.casefold()
        if folded_name in used_names:
            raise SpectralAnalysisError("色坐标名称不能重复")
        used_names.add(folded_name)
        numeric_values = tuple(
            _finite_float(cell, row_number=row_number, field_name="坐标") for cell in numeric_cells
        )
        try:
            results.append(_coordinate_result(name, numeric_values, coordinate_system))
        except SpectralAnalysisError as exc:
            raise SpectralAnalysisError(f"第 {row_number} 行：{exc}") from exc
    return results


def pairwise_chromaticity_distances(
    results: list[ChromaticityResult] | tuple[ChromaticityResult, ...],
) -> list[ChromaticityDistance]:
    """计算所有色坐标点之间的 xy 和 u′v′ 欧氏距离。"""

    distances: list[ChromaticityDistance] = []
    for first, second in combinations(results, 2):
        delta_xy = math.dist(first.xy, second.xy)
        delta_upvp = math.dist(first.upvp, second.upvp)
        distances.append(ChromaticityDistance(first.name, second.name, delta_xy, delta_upvp))
    return distances


def _cri_reference_sd(cct: float) -> tuple[str, Any]:
    """生成与 CRI 规则一致的同色温标准参考光源。"""

    colour_module = _require_colour()
    shape = colour_module.SpectralShape(CALCULATION_START_NM, CALCULATION_END_NM, 1)
    if cct < 5000:
        reference = colour_module.sd_blackbody(cct, shape)
        reference_name = f"黑体参考源（{cct:.0f} K）"
    else:
        reference_xy = colour_module.temperature.CCT_to_xy_CIE_D(cct)
        reference = colour_module.sd_CIE_illuminant_D_series(reference_xy).align(shape)
        reference_name = f"CIE 日光参考源（{cct:.0f} K）"
    return reference_name, reference


def _sd_chromaticity(sd: Any) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """计算光谱的 xy、CIE 1960 uv 与 CIE 1976 u′v′。"""

    colour_module = _require_colour()
    xy_values = np.asarray(colour_module.XYZ_to_xy(colour_module.sd_to_XYZ(sd)), dtype=float)
    xy = (float(xy_values[0]), float(xy_values[1]))
    uv = _xy_to_uv(xy)
    return xy, uv, (uv[0], uv[1] * 1.5)


def _reference_chromaticity(
    sd: Any,
    cct: float,
) -> tuple[
    float,
    str,
    tuple[float, float],
    tuple[float, float],
    tuple[float, float],
]:
    """返回 CRI 参考源信息及测试源与参考源的 CIE 1960 uv 距离。"""

    reference_name, reference = _cri_reference_sd(cct)
    _, test_uv, _ = _sd_chromaticity(sd)
    reference_xy, reference_uv, reference_upvp = _sd_chromaticity(reference)
    distance = float(np.linalg.norm(np.asarray(test_uv) - np.asarray(reference_uv)))
    return distance, reference_name, reference_xy, reference_uv, reference_upvp


def _cri_sample_results(
    cri_specification: Any,
) -> tuple[CRIColorSampleResult, ...]:
    """提取 R1–R15 色样在测试源与参考源下的实际色坐标。"""

    colour_module = _require_colour()
    test_data, reference_data = cri_specification.colorimetry_data
    scores = cri_specification.Q_as
    results: list[CRIColorSampleResult] = []
    for index, (test_item, reference_item) in enumerate(zip(test_data, reference_data), start=1):
        test_xy_values = np.asarray(colour_module.XYZ_to_xy(test_item.XYZ), dtype=float)
        reference_xy_values = np.asarray(colour_module.XYZ_to_xy(reference_item.XYZ), dtype=float)
        test_xy = (float(test_xy_values[0]), float(test_xy_values[1]))
        reference_xy = (float(reference_xy_values[0]), float(reference_xy_values[1]))
        test_uv = _xy_to_uv(test_xy)
        reference_sample_uv = _xy_to_uv(reference_xy)
        results.append(
            CRIColorSampleResult(
                index=index,
                score=float(scores[index].Q_a),
                test_xy=test_xy,
                reference_xy=reference_xy,
                test_upvp=(test_uv[0], test_uv[1] * 1.5),
                reference_upvp=(reference_sample_uv[0], reference_sample_uv[1] * 1.5),
            )
        )
    return tuple(results)


def analyze_spectrum(spectrum: SpectrumInput) -> SpectrumResult:
    """计算单条光谱的色坐标、CCT、CRI 和 CIE Rf。"""

    colour_module = _require_colour()
    wavelengths = np.asarray(spectrum.wavelengths, dtype=float)
    values = np.asarray(spectrum.values, dtype=float)
    if wavelengths[0] > MIN_REQUIRED_START_NM or wavelengths[-1] < CALCULATION_END_NM:
        raise SpectralAnalysisError(
            f"{spectrum.name} 的波长范围至少应覆盖 {MIN_REQUIRED_START_NM}–{CALCULATION_END_NM} nm"
        )

    warnings: list[str] = []
    if wavelengths[0] > CALCULATION_START_NM:
        warnings.append(
            f"输入从 {wavelengths[0]:.0f} nm 开始，较短波段按 0 补齐到 {CALCULATION_START_NM} nm"
        )
    maximum_gap = float(np.max(np.diff(wavelengths)))
    if maximum_gap > 5:
        warnings.append(f"最大波长间隔为 {maximum_gap:g} nm，窄带光谱可能产生插值误差")

    calculation_grid = np.arange(CALCULATION_START_NM, CALCULATION_END_NM + 1, dtype=float)
    aligned_values = np.interp(calculation_grid, wavelengths, values, left=0.0, right=0.0)
    sd = colour_module.SpectralDistribution(
        dict(zip(calculation_grid.tolist(), aligned_values.tolist())),
        name=spectrum.name,
    )

    XYZ_raw = np.asarray(colour_module.sd_to_XYZ(sd), dtype=float)
    if not np.all(np.isfinite(XYZ_raw)) or XYZ_raw[1] <= 0:
        raise SpectralAnalysisError(f"{spectrum.name} 无法得到有效的三刺激值")
    XYZ_normalized = XYZ_raw / XYZ_raw[1] * 100
    xy_values = np.asarray(colour_module.XYZ_to_xy(XYZ_raw), dtype=float)
    xy = (float(xy_values[0]), float(xy_values[1]))
    uv = _xy_to_uv(xy)
    upvp = (uv[0], uv[1] * 1.5)
    cct, duv, cct_warnings = _cct_duv_from_uv(uv)
    warnings.extend(cct_warnings)

    reference_distance: float | None = None
    reference_name: str | None = None
    reference_xy: tuple[float, float] | None = None
    reference_uv: tuple[float, float] | None = None
    reference_upvp: tuple[float, float] | None = None
    ra: float | None = None
    ri: tuple[tuple[int, float], ...] = ()
    cri_samples: tuple[CRIColorSampleResult, ...] = ()
    rf: float | None = None
    if cct is not None and 1000 <= cct <= 25000:
        try:
            (
                reference_distance,
                reference_name,
                reference_xy,
                reference_uv,
                reference_upvp,
            ) = _reference_chromaticity(sd, cct)
            if reference_distance > CRI_CHROMATICITY_DISTANCE_LIMIT:
                warnings.append(
                    "测试光源与同色温 CRI 参考光源的 uv 距离超过 0.0054，显色指数仅供参考"
                )
            cri_specification = colour_module.colour_rendering_index(
                sd,
                additional_data=True,
                method="CIE 2024",
            )
            ra = float(cri_specification.Q_a)
            ri = tuple(
                (int(index), float(item.Q_a))
                for index, item in sorted(cri_specification.Q_as.items())
            )
            cri_samples = _cri_sample_results(cri_specification)
        except Exception as exc:
            warnings.append(f"CRI 计算失败：{exc}")
        try:
            rf = float(colour_module.colour_fidelity_index(sd, method="CIE 2017"))
        except Exception as exc:
            warnings.append(f"CIE Rf 计算失败：{exc}")
    else:
        warnings.append("CCT 超出 1000–25000 K，未计算 CRI 和 CIE Rf")

    maximum = float(np.max(values))
    normalized_values = tuple(float(value / maximum) for value in values)
    return SpectrumResult(
        name=spectrum.name,
        wavelengths=tuple(float(value) for value in wavelengths),
        values=tuple(float(value) for value in values),
        normalized_values=normalized_values,
        XYZ=(
            float(XYZ_normalized[0]),
            float(XYZ_normalized[1]),
            float(XYZ_normalized[2]),
        ),
        xy=xy,
        uv=uv,
        upvp=upvp,
        cct=cct,
        duv=duv,
        cri_reference_distance=reference_distance,
        reference_name=reference_name,
        reference_xy=reference_xy,
        reference_uv=reference_uv,
        reference_upvp=reference_upvp,
        ra=ra,
        ri=ri,
        cri_samples=cri_samples,
        rf=rf,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def analyze_spectral_text(raw_text: str) -> list[SpectrumResult]:
    """解析并计算一段多光谱粘贴文本。"""

    return [analyze_spectrum(spectrum) for spectrum in parse_spectral_text(raw_text)]


def spectral_example_text() -> str:
    """生成 D65 与标准 A 光源的可粘贴对比示例。"""

    colour_module = _require_colour()
    shape = colour_module.SpectralShape(CALCULATION_START_NM, CALCULATION_END_NM, 5)
    d65 = colour_module.SDS_ILLUMINANTS["D65"].copy().align(shape)
    illuminant_a = colour_module.SDS_ILLUMINANTS["A"].copy().align(shape)
    lines = ["波长(nm)\tD65\t标准A光源"]
    for wavelength, d65_value, a_value in zip(d65.wavelengths, d65.values, illuminant_a.values):
        lines.append(f"{wavelength:.0f}\t{d65_value:.8g}\t{a_value:.8g}")
    return "\n".join(lines)


@lru_cache(maxsize=len(STANDARD_ILLUMINANTS))
def analyze_standard_illuminant(illuminant_key: str) -> SpectrumResult:
    """计算一个内置 CIE 标准光源并缓存结果。"""

    if illuminant_key not in STANDARD_ILLUMINANTS:
        raise SpectralAnalysisError("不支持的内置标准光源")
    colour_module = _require_colour()
    shape = colour_module.SpectralShape(CALCULATION_START_NM, CALCULATION_END_NM, 5)
    sd = colour_module.SDS_ILLUMINANTS[illuminant_key].copy().align(shape)
    spectrum = SpectrumInput(
        name=STANDARD_ILLUMINANTS[illuminant_key],
        wavelengths=tuple(float(value) for value in sd.wavelengths),
        values=tuple(float(value) for value in sd.values),
    )
    return analyze_spectrum(spectrum)


@lru_cache(maxsize=32)
def analyze_cct_reference(cct: float) -> SpectrumResult:
    """按给定相关色温生成 CRI 规则对应的等色温标准光源。"""

    if not math.isfinite(cct) or not 1000 <= cct <= 25000:
        raise SpectralAnalysisError("等色温标准光源要求 CCT 位于 1000–25000 K")
    reference_name, sd = _cri_reference_sd(float(cct))
    spectrum = SpectrumInput(
        name=reference_name,
        wavelengths=tuple(float(value) for value in sd.wavelengths),
        values=tuple(float(value) for value in sd.values),
    )
    return analyze_spectrum(spectrum)


@lru_cache(maxsize=2)
def chromaticity_loci(coordinate_system: str) -> tuple[
    tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]
]:
    """生成 CIE 光谱轨迹和普朗克轨迹坐标。"""

    if coordinate_system not in {"xy", "upvp"}:
        raise SpectralAnalysisError("轨迹图仅支持 xy 或 u′v′")
    colour_module = _require_colour()
    spectral_points: list[tuple[float, float]] = []
    for wavelength in range(380, 781):
        XYZ = colour_module.wavelength_to_XYZ(wavelength)
        xy_values = colour_module.XYZ_to_xy(XYZ)
        xy = (float(xy_values[0]), float(xy_values[1]))
        uv = _xy_to_uv(xy)
        spectral_points.append(xy if coordinate_system == "xy" else (uv[0], uv[1] * 1.5))
    spectral_points.append(spectral_points[0])

    planckian_points: list[tuple[float, float]] = []
    for cct in np.geomspace(1000, 25000, 240):
        uv_values = np.asarray(
            colour_module.temperature.CCT_to_uv_Planck1900(float(cct)),
            dtype=float,
        )
        uv = (float(uv_values[0]), float(uv_values[1]))
        planckian_points.append(_uv_to_xy(uv) if coordinate_system == "xy" else (uv[0], uv[1] * 1.5))
    return tuple(spectral_points), tuple(planckian_points)


@lru_cache(maxsize=2)
def chromaticity_background_image(coordinate_system: str) -> str:
    """生成光谱轨迹内部连续渐变的透明 PNG 色度背景。"""

    if coordinate_system not in {"xy", "upvp"}:
        raise SpectralAnalysisError("色度背景仅支持 xy 或 u′v′")
    from PIL import Image, ImageDraw

    if coordinate_system == "xy":
        x_max, y_max = 0.8, 0.9
        width, height = 640, 720
    else:
        x_max, y_max = 0.7, 0.65
        width, height = 630, 585

    horizontal = np.linspace(0, x_max, width, dtype=float)
    vertical = np.linspace(y_max, 0, height, dtype=float)
    first, second = np.meshgrid(horizontal, vertical)
    if coordinate_system == "xy":
        x_values, y_values = first, second
    else:
        u_values = first
        v_values = second * 2 / 3
        denominator = 2 * u_values - 8 * v_values + 4
        x_values = 3 * u_values / denominator
        y_values = 2 * v_values / denominator

    safe_y = np.maximum(y_values, 1e-8)
    XYZ = np.stack(
        [x_values / safe_y, np.ones_like(safe_y), (1 - x_values - y_values) / safe_y],
        axis=-1,
    )
    conversion_matrix = np.asarray(
        [
            [3.2406, -1.5372, -0.4986],
            [-0.9689, 1.8758, 0.0415],
            [0.0557, -0.2040, 1.0570],
        ],
        dtype=float,
    )
    linear_rgb = np.clip(XYZ @ conversion_matrix.T, 0.0, None)
    maximum = np.max(linear_rgb, axis=-1, keepdims=True)
    linear_rgb = np.divide(linear_rgb, maximum, out=np.zeros_like(linear_rgb), where=maximum > 0)
    srgb = np.where(
        linear_rgb <= 0.0031308,
        12.92 * linear_rgb,
        1.055 * np.power(linear_rgb, 1 / 2.4) - 0.055,
    )
    rgb = np.rint((0.08 + 0.92 * np.clip(srgb, 0.0, 1.0)) * 255).astype(np.uint8)

    mask = Image.new("L", (width, height), 0)
    polygon = [
        (
            round(point[0] / x_max * (width - 1)),
            round((1 - point[1] / y_max) * (height - 1)),
        )
        for point in chromaticity_loci(coordinate_system)[0][:-1]
    ]
    ImageDraw.Draw(mask).polygon(polygon, fill=235)
    rgba = np.dstack([rgb, np.asarray(mask, dtype=np.uint8)])
    image = Image.fromarray(rgba, mode="RGBA")
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
