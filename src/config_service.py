# -*- encoding: utf-8 -*-
import hashlib
import json
from datetime import datetime
from pathlib import Path

import pandas as pd


class ConfigService:
    def __init__(self):
        base_dir = Path(__file__).parent.parent
        self.excel_path = base_dir / "data" / "config.xlsx"
        self._cache = None  # 用于缓存加载的数据
        self._last_hash = None  # 用于检测文件是否修改

    # 获取文件内容哈希值用于检测修改
    def _get_file_hash(self):
        return hashlib.md5(self.excel_path.read_bytes()).hexdigest()

    # 检查文件是否被修改
    def _check_unmodified(self):
        return self._get_file_hash() == self._last_hash

    # 数据清洗与结构化处理
    def _process_data(self, df):
        # .fillna("") 将 DataFrame 中的所有缺失值替换为空字符串
        # .reset_index(drop=True) 重置 DataFrame 的索引，丢弃旧的索引，生成一个新的从 0 开始的整数索引
        df = df.fillna("").reset_index(drop=True)

        # 这里整合你提供的clean_text和数据结构逻辑
        config = {}
        # current_category = None
        # current_subcategory = None
        # 检查必须列
        required_columns = [
            "节点序号",
            "固定唯一码",
            "激活条件",
            "引导描述",
            "答案类型",
            "文件引用配置",
            "选项",
            "选项输出值",
            "选项重点值",
            "输入项数量依据",
            "输入项名称依据",
            "输入是否要求范围",
            "需求项提示",
            "选项呈现语句",
            "选项展示组",
            "选项查阅整理要求",
            "输出标签",
        ]
        missing_cols = [col for col in required_columns if col not in df.columns]
        if missing_cols:
            raise ValueError(f"Excel文件缺少必要列: {', '.join(missing_cols)}")

        node_num = 0
        option_list = []
        # 使用enumerate准确追踪行位置
        for row_index, row in enumerate(df.itertuples(), start=1):
            excel_row_num = row_index + 1  # Excel实际行号（标题行算第1行）

            # 跳过无效行（处理空值）
            if pd.isnull(self.clean_text(row.节点序号).strip()):
                print(f"警告：第{excel_row_num}行缺少节点序号，已跳过")
                continue
            temp_dic = {
                "option_content": self.clean_text(row.选项).strip(),
                "option_out": self.clean_text(row.选项输出值).strip(),
                "option_bold": self.clean_text(row.选项重点值).strip(),
                "option_show": self.clean_text(row.选项呈现语句).strip(),
                "option_label": self.clean_text(row.输出标签).strip(),
            }

            # 当前序号节点已读取过
            if self.clean_text(row.节点序号).strip() in config:
                config[self.clean_text(row.节点序号).strip()]["options"].append(temp_dic)
            # 当前序号节点未读取过
            elif not node_num == int(float(self.clean_text(row.节点序号).strip())):
                node_num = int(float(self.clean_text(row.节点序号).strip()))
                option_list = []
                option_list.append(temp_dic)
                # condition = ""
                # if "集合:" in self.clean_text(row.激活条件).strip():
                #     # json格式数据不支持集合，先按照列表形式进行保存
                #     condition = list(self.clean_text(row.激活条件).strip().lstrip("集合:").split(","))
                # else:
                # condition = self.clean_text(row.激活条件).strip()

                config[str(node_num)] = {
                    "condition": self.clean_text(row.激活条件).strip(),
                    "node_id": str(int(float(self.clean_text(row.固定唯一码).strip()))),
                    "guide_content": self.clean_text(row.引导描述).strip(),
                    "answer_type": self.clean_text(row.答案类型).strip(),
                    "input_num_accor": str(int(float(self.clean_text(row.输入项数量依据).strip())))
                    if self.clean_text(row.输入项数量依据).strip() != ""
                    else "",
                    "input_name_accor": str(int(float(self.clean_text(row.输入项名称依据).strip())))
                    if self.clean_text(row.输入项名称依据).strip() != ""
                    else "",
                    "input_tolerance": self.clean_text(row.输入是否要求范围).strip(),
                    "ref_config": self.clean_text(row.文件引用配置).strip(),
                    "option_hint": self.clean_text(row.需求项提示).strip(),
                    "option_group_id": self.clean_text(row.选项展示组).strip(),
                    "option_view": self.clean_text(row.选项查阅整理要求).strip(),
                    "user_must_out": {},
                    "option_tolerance_out": {},
                    "ref_out": "",
                    "options": option_list,
                }
            else:
                print(f"警告：第{excel_row_num}行录入失败，已跳过")
        return config

    def load_config(self, force_reload=False):
        """带缓存机制的配置加载"""
        # 配置文件没修改 且 缓存有数据 且 管理员没有点重载按钮情况下，返回缓存数据，可直接使用
        if not force_reload and self._cache and self._check_unmodified():
            return self._cache
        # 配置文件修改 或 缓存为空 或 管理员点击重载按钮情况下，重新装载更新数据
        raw_df = pd.read_excel(self.excel_path, engine="openpyxl")
        processed = self._process_data(raw_df)
        # 读取整理配置文件后，马上记录文件哈希值
        self._last_hash = self._get_file_hash()
        # 更新缓存数据
        self._cache = {
            "data": processed,
            "config_timestamp": datetime.now().isoformat(),
            "excel_version_hash": hashlib.md5(self.excel_path.read_bytes()).hexdigest()[:8],
            "entry_status": False,  # 初始化录入状态，默认没有录完
        }
        with open("config_service.json", "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=4)
        return self._cache

    @staticmethod
    def clean_text(text):
        """处理特殊字符：换行符转义、防御XSS"""
        return (
            str(text)
            .replace("\\", "\\\\")  # 先处理反斜杠
            .replace("\n", "\\n")
        )
