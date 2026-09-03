import json
import argparse
from pathlib import Path

import pandas as pd


def convert_excel_to_json(excel_path, json_path, sheet_name=0):
    """
    将 Excel 文件转换为特定结构的 JSON 文件。
    - 能够正确处理日期时间对象，将其转换为字符串。
    - 能够将所有空白单元格（包括日期列的空白单元格）的值设置为空字符串 ""。
    """
    try:
        # 读取 Excel 文件，将第一行作为表头
        df = pd.read_excel(excel_path, sheet_name=sheet_name, header=0)

        # 检查 DataFrame 是否为空
        if df.empty:
            print(f"错误：Excel 文件 '{excel_path}' 或其指定的工作表为空。")
            return

        # --- 逻辑顺序调整：步骤一 ---
        # 首先，专门处理所有日期时间类型的列
        datetime_cols = df.select_dtypes(include=["datetime64[ns]"]).columns

        for col in datetime_cols:
            # 将该列中有效的日期时间值转换为字符串格式
            # 此操作会将该列中的空值（NaT）变为 NaN
            df[col] = df[col].dt.strftime("%Y-%m-%d")  # type: ignore

        # --- 逻辑顺序调整：步骤二 ---
        # 然后，在全局范围内将所有剩余的空值（NaN/None）替换为空字符串 ""
        df.fillna("", inplace=True)

        # 获取第一列的列名，并将其设置为索引
        first_column_name = df.columns[0]
        df.set_index(first_column_name, inplace=True)

        # 将处理好的 DataFrame 转换为目标字典结构
        result_dict = df.to_dict(orient="index")

        # 将字典写入 JSON 文件
        with open(json_path, "w", encoding="utf-8") as json_file:
            json.dump(result_dict, json_file, ensure_ascii=False, indent=4)

        print(f"✅ 成功！文件已从 '{excel_path}' 转换并保存至 '{json_path}'。")

    except FileNotFoundError:
        print(f"错误：找不到文件 '{excel_path}'。请检查路径是否正确。")
    except Exception as e:
        print(f"处理过程中发生了一个错误：{e}")


def main() -> int:
    """解析命令行参数并执行一次转换。"""
    parser = argparse.ArgumentParser(description="将 Excel 第一列转为 JSON 对象键")
    parser.add_argument("excel_path", type=Path, help="源 Excel 文件路径")
    parser.add_argument("json_path", type=Path, help="目标 JSON 文件路径")
    parser.add_argument("--sheet", default=0, help="工作表名称或从 0 开始的序号")
    args = parser.parse_args()
    sheet_name = int(args.sheet) if str(args.sheet).isdigit() else str(args.sheet)
    convert_excel_to_json(args.excel_path, args.json_path, sheet_name=sheet_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
