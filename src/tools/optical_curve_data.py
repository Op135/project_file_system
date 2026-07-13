# -*- encoding: utf-8 -*-
"""研发光学曲线工具的纯数据处理函数。"""

from __future__ import annotations

import ast
import math
import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


class CurveDataError(ValueError):
    """表示曲线录入数据无法通过业务校验。"""


_COLUMN_SEPARATOR = re.compile(r"[,，;；]+")


def _split_columns(line: str) -> list[str]:
    """优先按表格分隔符拆列，避免把表头单元格内部的空格误当成新列。"""

    if "\t" in line:
        return [part.strip() for part in line.split("\t") if part.strip()]
    if _COLUMN_SEPARATOR.search(line):
        return [part.strip() for part in _COLUMN_SEPARATOR.split(line) if part.strip()]
    return line.split()


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def parse_curve_rows(raw_text: str) -> tuple[list[float], list[float]]:
    """把从 Excel/文本粘贴的两列内容解析为按 X 升序排列的数据。"""

    points: list[tuple[float, float]] = []
    header_skipped = False

    for line_number, raw_line in enumerate(str(raw_text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        columns = _split_columns(line)
        if len(columns) != 2:
            raise CurveDataError(f"第 {line_number} 行应恰好包含两列数据")

        try:
            x_value, y_value = (float(columns[0]), float(columns[1]))
        except ValueError as exc:
            # 仅允许第一条有效内容是诸如“波长  强度”的表头。
            if not points and not header_skipped and not any(_is_number(column) for column in columns):
                header_skipped = True
                continue
            raise CurveDataError(f"第 {line_number} 行包含非数字内容") from exc

        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise CurveDataError(f"第 {line_number} 行包含无穷值或空值")
        if x_value < 0:
            raise CurveDataError(f"第 {line_number} 行的波长不能小于 0 nm")
        points.append((x_value, y_value))

    if len(points) < 2:
        raise CurveDataError("至少需要录入 2 行有效数据")

    points.sort(key=lambda point: point[0])
    duplicate_x = next(
        (points[index][0] for index in range(1, len(points)) if points[index][0] == points[index - 1][0]),
        None,
    )
    if duplicate_x is not None:
        raise CurveDataError(f"X 轴存在重复波长：{duplicate_x:g} nm")

    return [point[0] for point in points], [point[1] for point in points]


def normalize_y_values(values: Sequence[float]) -> tuple[list[float], float]:
    """按最大绝对值归一化，返回归一化序列和归一化因子。"""

    if not values:
        raise CurveDataError("Y 轴数据不能为空")

    factor = max(abs(float(value)) for value in values)
    if not math.isfinite(factor) or factor == 0:
        raise CurveDataError("Y 轴数据不能全部为 0")

    normalized = [float(value) / factor for value in values]
    return normalized, factor


def normalize_conditions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """清洗可选成立条件，并拒绝半填或同名条件。"""

    conditions: list[dict[str, str]] = []
    names_seen: set[str] = set()

    for index, row in enumerate(rows, start=1):
        name = str(row.get("name", "") or "").strip()
        value = str(row.get("value", "") or "").strip()
        if not name and not value:
            continue
        if not name or not value:
            raise CurveDataError(f"第 {index} 个成立条件需要同时填写条件名和条件值")

        folded_name = name.casefold()
        if folded_name in names_seen:
            raise CurveDataError(f"成立条件“{name}”重复，请合并后再保存")
        names_seen.add(folded_name)
        conditions.append({"name": name, "value": value})

    return conditions


def curve_matches_filters(
    record: Mapping[str, Any],
    *,
    title_query: str = "",
    y_axis_name: str = "",
    conditions: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """判断一条曲线是否同时满足标题、表征名及所有成立条件。"""

    query = str(title_query or "").strip().casefold()
    if query:
        searchable_parts = [
            str(record.get("title", "") or ""),
            str(record.get("y_axis_name", "") or ""),
        ]
        for item in record.get("conditions", []):
            if isinstance(item, Mapping):
                searchable_parts.extend(
                    [str(item.get("name", "") or ""), str(item.get("value", "") or "")]
                )
        if query not in " ".join(searchable_parts).casefold():
            return False

    selected_y_axis = str(y_axis_name or "").strip().casefold()
    if selected_y_axis and selected_y_axis != str(record.get("y_axis_name", "") or "").strip().casefold():
        return False

    record_conditions = {
        str(item.get("name", "") or "").strip().casefold(): str(item.get("value", "") or "").strip().casefold()
        for item in record.get("conditions", [])
        if isinstance(item, Mapping)
    }
    for condition in conditions:
        name = str(condition.get("name", "") or "").strip().casefold()
        value = str(condition.get("value", "") or "").strip().casefold()
        if not name and not value:
            continue
        if not name or not value or record_conditions.get(name) != value:
            return False

    return True


def _interpolate_curve(x_data: list[float], y_data: list[float], target: float) -> float:
    """在线性区间内插值，曲线自身范围外按零处理。"""

    if target < x_data[0] or target > x_data[-1]:
        return 0.0
    position = bisect_right(x_data, target)
    if position >= len(x_data):
        return y_data[-1]
    left_x, right_x = x_data[position - 1], x_data[position]
    left_y, right_y = y_data[position - 1], y_data[position]
    if target == left_x:
        return left_y
    ratio = (target - left_x) / (right_x - left_x)
    return left_y + ratio * (right_y - left_y)


def evaluate_curve_expression(
    expression: str,
    variables: Mapping[str, Mapping[str, Any]],
) -> tuple[list[float], list[float]]:
    """在曲线 X 点并集上安全计算只含基础四则运算的表达式。"""

    expression = str(expression or "").strip().replace("×", "*").replace("÷", "/")
    if not expression:
        raise CurveDataError("请输入曲线运算公式")
    try:
        parsed = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise CurveDataError("曲线运算公式格式不正确") from exc

    normalized_variables = {str(name).casefold(): record for name, record in variables.items()}
    referenced_names: list[str] = []
    for node in ast.walk(parsed):
        if isinstance(node, ast.Name):
            name = node.id.casefold()
            if name not in normalized_variables:
                raise CurveDataError(f"公式中的曲线别名“{node.id}”不存在或未被选中")
            if name not in referenced_names:
                referenced_names.append(name)
        elif isinstance(node, ast.Call | ast.Attribute | ast.Subscript | ast.Pow | ast.Mod):
            raise CurveDataError("曲线运算公式仅支持常数、曲线别名、括号和加减乘除")
    if not referenced_names:
        raise CurveDataError("曲线运算公式至少需要引用一条曲线")

    prepared: dict[str, tuple[list[float], list[float]]] = {}
    for name in referenced_names:
        record = normalized_variables[name]
        x_data = [float(value) for value in record.get("x_data", [])]
        y_data = [float(value) for value in record.get("y_data", [])]
        if len(x_data) < 2 or len(x_data) != len(y_data):
            raise CurveDataError(f"曲线别名“{name}”的数据无效或不完整")
        if any(x_data[index] >= x_data[index + 1] for index in range(len(x_data) - 1)):
            raise CurveDataError(f"曲线别名“{name}”的波长数据必须严格递增")
        prepared[name] = (x_data, y_data)

    common_x = sorted({x_value for name in referenced_names for x_value in prepared[name][0]})

    def evaluate_node(node: ast.AST, values: Mapping[str, float], x_value: float) -> float:
        if isinstance(node, ast.Expression):
            return evaluate_node(node.body, values, x_value)
        if isinstance(node, ast.Name):
            return values[node.id.casefold()]
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CurveDataError("曲线运算公式中的常数必须是数字")
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate_node(node.operand, values, x_value)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left = evaluate_node(node.left, values, x_value)
            right = evaluate_node(node.right, values, x_value)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise CurveDataError(f"曲线运算公式在 X={x_value:g} 处发生除零")
            return left / right
        raise CurveDataError("曲线运算公式仅支持常数、曲线别名、括号和加减乘除")

    result_y: list[float] = []
    for x_value in common_x:
        values = {
            name: _interpolate_curve(prepared[name][0], prepared[name][1], x_value)
            for name in referenced_names
        }
        result = evaluate_node(parsed, values, x_value)
        if not math.isfinite(result):
            raise CurveDataError(f"曲线运算公式在 X={x_value:g} 处产生无效结果")
        result_y.append(result)
    return common_x, result_y


def fuse_curve_records(records: Sequence[Mapping[str, Any]]) -> tuple[list[float], list[float]]:
    """在所有曲线 X 点的并集上插值并累加，曲线自身范围外按 0 处理。"""

    if len(records) < 2:
        raise CurveDataError("融合曲线至少需要选择 2 条曲线")

    prepared: list[tuple[list[float], list[float]]] = []
    for record in records:
        x_data = [float(value) for value in record.get("x_data", [])]
        y_data = [float(value) for value in record.get("y_data", [])]
        if len(x_data) < 2 or len(x_data) != len(y_data):
            raise CurveDataError("所选曲线存在无效或不完整的数据")
        if any(x_data[index] >= x_data[index + 1] for index in range(len(x_data) - 1)):
            raise CurveDataError("所选曲线的波长数据必须严格递增")
        prepared.append((x_data, y_data))

    common_x = sorted({x_value for x_data, _ in prepared for x_value in x_data})

    fused_y = [
        sum(_interpolate_curve(x_data, y_data, x_value) for x_data, y_data in prepared)
        for x_value in common_x
    ]
    return common_x, fused_y


def fuse_and_normalize_curve_records(
    records: Sequence[Mapping[str, Any]],
) -> tuple[list[float], list[float], float]:
    """先融合曲线，再对融合结果单独归一化。"""

    x_data, summed_y = fuse_curve_records(records)
    normalized_y, factor = normalize_y_values(summed_y)
    return x_data, normalized_y, factor
