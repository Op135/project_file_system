import asyncio  # Python内置的异步I/O库，用于在不阻塞UI主线程的情况下执行后台任务
import difflib  # Python内置库，提供计算文本差异和相似度的类和方法
import io  # Python内置的内存流I/O操作库，用于在内存中读写文件（如生成Excel文件流）
import re  # Python内置的正则表达式库，用于进行复杂的字符串模式匹配和替换
from datetime import datetime  # 用于获取和格式化当前时间
from typing import Any, Dict, List, Optional, Set  # 用于静态类型提示，提升代码可读性和IDE检查能力

import pandas as pd  # 强大的第三方数据分析处理库，此处主要用于读取Excel/CSV表格和清洗结构化数据
from nicegui import ui  # 基于Vue和FastAPI的Python第三方UI框架，采用声明式的上下文管理器构建界面

from .. import db_storage  # 本地项目的数据库/存储操作模块

# ==========================================
# 🚀 核心功能开关区
# ==========================================
# 控制是否在界面上插入“品名/规格/封装”三个提取关键词列（用于核对算法拆词准确性）
SHOW_EXTRACTED_KEYWORDS = True

# ==========================================
# 1. 全局解析规则管控区（符号提纯）
# ==========================================
SEPARATORS_REGEX = r"[\s,\;、\|_\(\)（）\[\]【】\/\\]+"
SPEC_TOKEN_REGEX = r"[A-Za-z0-9\.\-\*\%\+\×]+"
ZH_TOKEN_REGEX = r"[\u4e00-\u9fa5]+"

# 增加 re.IGNORECASE 标志，确保匹配过程对大小写绝对免疫
TOKEN_PATTERN = re.compile(f"{SPEC_TOKEN_REGEX}|{ZH_TOKEN_REGEX}", flags=re.IGNORECASE)
NUM_PATTERN = re.compile(r"\d")
ZH_PATTERN = re.compile(ZH_TOKEN_REGEX)
PCB_PATTERN = re.compile(r"[黑白红绿蓝黄紫亚哑]色?油[黑白红绿蓝黄紫亚哑]色?字")


class MaterialMatcherTool:
    """智能物料匹配工具类 —— 跨列聚合、无序高亮、免疫拦截与原生数据展示版"""

    def __init__(self):
        # ==========================================
        # 2. 匹配分数阈值管控区
        # ==========================================
        self.SCORE_GREEN = 85.0
        self.SCORE_YELLOW = 40.0
        self.SCORE_MIN = 20.0
        self.SCORE_CONFLICT = 5.0
        self.SCORE_MEMORY_BOOST = 100.0

        self.non_elec_df: Optional[pd.DataFrame] = None
        self.elec_df: Optional[pd.DataFrame] = None
        self.erp_data: Optional[pd.DataFrame] = None

        self.erp_search_pool: List[Dict[str, Any]] = []
        self.unified_bom_pool: List[Dict[str, Any]] = []
        self.match_results: List[Dict[str, Any]] = []

        self.status_non_elec: str = "等待上传..."
        self.status_elec: str = "等待上传..."
        self.status_erp: str = "等待上传..."
        self.show_upload: bool = True
        self.is_calculating: bool = False
        self.show_result: bool = False
        self.can_start: bool = False
        self.can_export: bool = False
        self.summary_text: str = "请在上方上传需要核算的物料清单（至少一份）与企业库存数据..."
        self.calc_progress: float = 0.0
        self.calc_progress_text: str = "准备就绪..."

    def show(self, parent_dialog: ui.dialog):
        """构建工具的主界面 UI"""
        with ui.column().classes(
            "w-full min-h-screen bg-slate-50 absolute inset-0 p-2 md:p-4 lg:p-6 overflow-hidden flex flex-col"
        ):
            with ui.row().classes("w-full justify-between items-center mb-2 shrink-0"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("inventory_2", size="md").classes("text-blue-600")
                    ui.label("智能物料清单请购匹配系统").classes("text-xl font-bold text-gray-800")
                ui.button("退出工具", on_click=parent_dialog.close).props("outline color=negative icon=close size=sm")

            with ui.expansion("⚙️ 全局等效词库配置 (双向自动容错)", icon="translate").classes(
                "w-full mb-3 bg-white shadow-sm border rounded-lg shrink-0"
            ):
                with ui.row().classes("w-full items-center gap-4 p-3"):
                    source_input = ui.input("词汇 A (如: 螺丝)").props("dense outlined")
                    target_input = ui.input("词汇 B (如: 螺钉)").props("dense outlined")

                    async def add_syn():
                        s, t = source_input.value, target_input.value
                        if s and t:
                            s, t = s.strip().upper(), t.strip().upper()
                            if s != t:

                                def safe_add(curr):
                                    if not isinstance(curr, dict):
                                        curr = {}
                                    curr[s] = t
                                    return curr

                                await db_storage.atomic_deep_update(["bom_synonyms"], safe_add)
                                self.render_synonyms_list.refresh()
                                source_input.value = ""
                                target_input.value = ""
                                ui.notify(f"已添加无向等效规则: {s} ⇋ {t}", type="positive")

                    ui.button("添加等效规则", on_click=add_syn).props("color=primary size=sm")

                self.render_synonyms_list()  # type: ignore

            with (
                ui.card()
                .bind_visibility_from(self, "show_upload")
                .classes("w-full p-4 mb-3 bg-white shadow-sm border shrink-0")
            ):
                ui.label("第一步：载入核心数据源 (任一物料清单 + 库存数据即可)").classes(
                    "text-base font-bold mb-3 text-gray-700"
                )

                with ui.row().classes("w-full grid grid-cols-1 md:grid-cols-3 gap-4"):
                    with ui.column().classes("border rounded-lg p-3 bg-gray-50 hover:bg-gray-100 transition-colors"):
                        ui.label("1. 非电子物料清单").classes("font-bold text-xs")
                        ui.upload(on_upload=lambda e: self._handle_upload(e, "non_elec"), auto_upload=True).classes(
                            "w-full"
                        ).props('accept=".csv, .xlsx, .xlsm" max-files="1" flat')
                        ui.label().bind_text_from(self, "status_non_elec").classes(
                            "text-xs text-blue-600 font-bold mt-1"
                        )

                    with ui.column().classes("border rounded-lg p-3 bg-gray-50 hover:bg-gray-100 transition-colors"):
                        ui.label("2. 电子物料清单").classes("font-bold text-xs")
                        ui.upload(on_upload=lambda e: self._handle_upload(e, "elec"), auto_upload=True).classes(
                            "w-full"
                        ).props('accept=".csv, .xlsx" max-files="1" flat')
                        ui.label().bind_text_from(self, "status_elec").classes("text-xs text-blue-600 font-bold mt-1")

                    with ui.column().classes("border rounded-lg p-3 bg-gray-50 hover:bg-gray-100 transition-colors"):
                        ui.label("3. 企业库存数据表").classes("font-bold text-xs")
                        ui.upload(on_upload=lambda e: self._handle_upload(e, "erp"), auto_upload=True).classes(
                            "w-full"
                        ).props('accept=".csv, .xlsx" max-files="1" flat')
                        ui.label().bind_text_from(self, "status_erp").classes("text-xs text-blue-600 font-bold mt-1")

                with ui.row().classes("w-full justify-end mt-3"):
                    ui.button("开始合并与智能匹配", on_click=self._process_and_match).bind_enabled_from(
                        self, "can_start"
                    ).props("color=primary icon=play_arrow size=sm")

            with (
                ui.column()
                .bind_visibility_from(self, "is_calculating")
                .classes("w-full items-center justify-center py-10 gap-4 shrink-0")
            ):
                ui.spinner("cube", size="4em", color="primary")
                ui.label("后台正在执行漏斗过滤与跨列聚合寻优...").classes(
                    "text-lg font-bold text-gray-600 animate-pulse"
                )
                ui.linear_progress(value=0.0).bind_value_from(self, "calc_progress").classes("w-1/2 mt-4").props(
                    "rounded size=20px color=blue-400"
                )
                ui.label().bind_text_from(self, "calc_progress_text").classes("text-xs text-gray-500 font-mono")

            with (
                ui.row()
                .bind_visibility_from(self, "show_result")
                .classes("w-full justify-between items-end mb-2 shrink-0")
            ):
                ui.label().bind_text_from(self, "summary_text").classes("text-sm text-gray-700 font-bold")

                with ui.row().classes("items-center gap-3"):
                    ui.button("导出请购单", on_click=self._export_excel).bind_enabled_from(self, "can_export").props(
                        "color=positive icon=file_download size=sm"
                    )
                    ui.button("重新上传数据", on_click=self._reset_and_show_upload).props(
                        "flat color=primary icon=refresh size=sm"
                    )

            with ui.column().classes("w-full flex-grow overflow-hidden"):
                self.render_result_grid()  # type: ignore

    @ui.refreshable
    def render_synonyms_list(self):
        syns = db_storage.get_deep_item(["bom_synonyms"], {})
        with ui.scroll_area().classes("w-full max-h-48 border-t p-3 bg-gray-50"):
            if not syns:
                ui.label("暂无等效词规则。添加后系统会自动生成双向容错特征，并采用得分最高的方案匹配。").classes(
                    "text-gray-400 text-xs"
                )
            else:
                with ui.element("div").classes("w-full flex flex-wrap gap-2 items-center"):
                    for k, v in syns.items():

                        async def remove_syn(e, key=k):
                            if not e.value:

                                def safe_del(curr):
                                    if isinstance(curr, dict) and key in curr:
                                        del curr[key]
                                    return curr

                                await db_storage.atomic_deep_update(["bom_synonyms"], safe_del)
                                self.render_synonyms_list.refresh()

                        ui.chip(f"{k} ⇋ {v}", removable=True, on_value_change=remove_syn).props(
                            "color=primary outline size=sm"
                        )

    def _fission_text(self, text: str) -> List[str]:
        if not text:
            return [text]
        syns = db_storage.get_deep_item(["bom_synonyms"], {})
        if not syns:
            return [text]

        bidirectional_rules = []
        for k, v in syns.items():
            bidirectional_rules.append((k, v))
            bidirectional_rules.append((v, k))

        variants = {text}
        queue = [text]
        max_limit = 8

        while queue and len(variants) < max_limit:
            curr = queue.pop(0)
            for k, v in bidirectional_rules:
                if k in curr:
                    new_text = curr.replace(k, v)
                    if new_text not in variants:
                        variants.add(new_text)
                        queue.append(new_text)

        return list(variants)

    @staticmethod
    def _clean_str_display(text: Any) -> str:
        return re.sub(SEPARATORS_REGEX, " ", str(text).upper()).strip()

    @staticmethod
    def _clean_str_calc(text: str) -> str:
        clean_text = re.sub(r"(?<=\d)\s*[X×x]\s*(?=\d)", "*", text, flags=re.IGNORECASE)
        clean_text = re.sub(r"(?<=\d)\.0+(?=[A-Za-z%_]|\b)", "", clean_text)
        clean_text = re.sub(
            r"[A-Za-z]*(0201|0402|0603|0805|1206|1210|2010|2512)[A-Za-z]*", r"\1", clean_text, flags=re.IGNORECASE
        )
        return clean_text

    def _smart_read_dataframe(self, buffer: io.BytesIO, filename: str) -> pd.DataFrame:
        target_keywords = {
            "名称",
            "规格",
            "型号",
            "Comment",
            "描述",
            "Description",
            "封装",
            "Footprint",
            "数量",
            "Quantity",
            "库存",
            "元件标号",
            "Designator",
            "品号",
            "编号",
            "编码",
            "用量",
        }

        def process_df(df_raw):
            header_row_idx, max_score = 0, 0
            for i in range(min(30, len(df_raw))):
                row_values = [str(val) for val in df_raw.iloc[i].values if pd.notna(val)]
                score = sum(1 for word in target_keywords if any(word.lower() in val.lower() for val in row_values))
                if score > max_score:
                    max_score, header_row_idx = score, i
                if max_score >= 3:
                    break

            if max_score > 0:
                new_columns = [
                    str(val).strip() if pd.notna(val) else f"Unnamed_{j}"
                    for j, val in enumerate(df_raw.iloc[header_row_idx])
                ]
                df_clean = df_raw.iloc[header_row_idx + 1 :].copy()
                df_clean.columns = pd.Index(new_columns)

                def is_ghost_row(row):
                    for val in row.values:
                        if pd.notna(val) and str(val).strip().lower() not in ["", "0", "0.0", "nan", "none"]:
                            return False
                    return True

                return df_clean[~df_clean.apply(is_ghost_row, axis=1)].reset_index(drop=True), max_score
            return df_raw, 0

        buffer.seek(0)
        if filename.lower().endswith(".csv"):
            try:
                df_raw = pd.read_csv(buffer, header=None, encoding="utf-8")
            except UnicodeDecodeError:
                buffer.seek(0)
                df_raw = pd.read_csv(buffer, header=None, encoding="gbk")
            df_clean, _ = process_df(df_raw)
            return df_clean
        else:
            sheets_dict = pd.read_excel(buffer, sheet_name=None, header=None)
            best_df, global_max_score = None, -1
            for sheet_name, df_raw in sheets_dict.items():
                if df_raw.empty:
                    continue
                df_clean, score = process_df(df_raw)
                if score > global_max_score or (
                    score == global_max_score and best_df is not None and len(df_clean) > len(best_df)
                ):
                    global_max_score, best_df = score, df_clean
            if best_df is not None:
                return best_df
            buffer.seek(0)
            return pd.read_excel(buffer)

    def _check_ready(self):
        self.can_start = (self.non_elec_df is not None or self.elec_df is not None) and self.erp_data is not None

    def _check_export_status(self):
        self.can_export = bool(self.match_results) and all(
            r.get("is_direct", False) or r.get("status", 0) == 2 for r in self.match_results
        )

    def _update_summary(self):
        gc, yc, rc = (
            sum(1 for r in self.match_results if r["status"] == s and not r.get("is_direct", False)) for s in (2, 1, 0)
        )
        self.summary_text = (
            f"共清洗合并 {len(self.match_results)} 款物料 | 🟢 自动确认: {gc} | 🟡 需人工确认: {yc} | 🔴 未匹配: {rc}"
        )

    def _export_excel(self):
        export_data = []
        for r in self.match_results:
            is_direct, match = r.get("is_direct", False), r.get("best_match")
            export_data.append(
                {
                    "状态": "⚪ 直接请购"
                    if is_direct
                    else (
                        "🔴 需请购"
                        if max(0.0, r["bom_qty"] - (match["stock"] if match else 0.0)) > 0
                        else "🟢 库存满足"
                    ),
                    "商品分类": "直接请购物料" if is_direct else (match["category"] if match else "未分类"),
                    "核算物料描述 (名称/规格/封装)": r["bom_desc"],
                    "物料清单提取料号": r.get("bom_code", ""),
                    "映射企业品号": match["code"] if match else "无",
                    "核算总需求量": r["bom_qty"],
                    "当前可用库存": match["stock"] if match and not is_direct else "-",
                    "实际需请购量": r["bom_qty"]
                    if is_direct
                    else max(0.0, r["bom_qty"] - (match["stock"] if match else 0.0)),
                    "单位": match["unit"] if match else "PCS",
                }
            )
        df = pd.DataFrame(export_data)

        df["_sort_status"] = df["状态"].map({"🔴 需请购": 0, "⚪ 直接请购": 1, "🟢 库存满足": 2})
        df = df.sort_values(by=["_sort_status", "商品分类", "映射企业品号"]).drop(columns=["_sort_status"])

        output = io.BytesIO()
        df.to_excel(output, index=False, sheet_name="智能核算请购单")
        output.seek(0)

        ui.download(output.getvalue(), filename=f"智能物料请购单_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
        ui.notify("请购单导出成功！", type="positive")

    async def _handle_upload(self, e: Any, target: str):
        try:
            file_bytes = await e.file.read() if asyncio.iscoroutine(e.file.read()) else e.file.read()
            df = self._smart_read_dataframe(io.BytesIO(file_bytes), e.file.name)

            if target == "non_elec":
                self.non_elec_df, self.status_non_elec = df, f"成功提取真实物料 {len(df)} 行"
            elif target == "elec":
                self.elec_df, self.status_elec = df, f"成功提取真实物料 {len(df)} 行"
            elif target == "erp":
                self.erp_data, self.status_erp = df, f"成功载入库存档案 {len(df)} 行"
            self._check_ready()
        except Exception as ex:
            ui.notify(f"文件读取失败，请检查格式: {str(ex)}", type="negative")

    def _reset_and_show_upload(self):
        self.show_upload, self.show_result, self.is_calculating, self.can_export, self.calc_progress = (
            True,
            False,
            False,
            False,
            0.0,
        )
        self.match_results, self.summary_text = [], "请重置数据或点击开始匹配..."
        self.render_result_grid.refresh()  # type: ignore

    def _safe_float(self, val: Any, default: float = 0.0) -> float:
        try:
            match = re.search(r"-?[\d,]+\.?\d*", str(val).strip())
            return float(match.group(0).replace(",", "")) if match else default
        except Exception:
            return default

    def _find_col(self, columns, keywords, excludes=None) -> Optional[str]:
        for col in columns:
            col_str = str(col).lower()
            if excludes and any(ex.lower() in col_str for ex in excludes):
                continue
            if any(k.lower() in col_str for k in keywords):
                return str(col)
        return None

    def _get_val(self, row, col_name, default=""):
        return str(row[col_name]).strip() if col_name and col_name in row and pd.notna(row[col_name]) else default

    def _build_erp_pool(self, df: pd.DataFrame):
        self.erp_search_pool = []
        code_col, name_col, spec_col, stock_col, cat_col, unit_col = (
            self._find_col(df.columns, keys)
            for keys in [
                ["品号", "料号"],
                ["名称", "品名"],
                ["规格", "型号", "comment"],
                ["可用量", "数量", "quantity"],
                ["商品分类"],
                ["单位"],
            ]
        )

        for row in df.to_dict(orient="records"):
            code, name, spec = self._get_val(row, code_col), self._get_val(row, name_col), self._get_val(row, spec_col)
            if not name and not spec:
                continue

            raw_desc = " ".join(filter(None, [str(name), str(spec)])).strip()

            erp_name_orig = self._clean_str_calc(self._clean_str_display(name))
            erp_spec_orig = self._clean_str_calc(self._clean_str_display(spec))
            e_tokens_orig = set(TOKEN_PATTERN.findall(f"{erp_name_orig} {erp_spec_orig}"))

            stock = self._safe_float(row.get(stock_col) if stock_col else 0.0)

            self.erp_search_pool.append(
                {
                    "code": code,
                    "erp_name_orig": erp_name_orig,
                    "erp_spec_orig": erp_spec_orig,
                    "erp_footprint_orig": "",
                    "e_num_orig": {t for t in e_tokens_orig if NUM_PATTERN.search(t)},
                    "e_zh_orig": {t for t in e_tokens_orig if ZH_PATTERN.search(t)},
                    "search_str": raw_desc,
                    "display": f"[{code}] {raw_desc} (库存:{stock})",
                    "stock": stock,
                    "category": self._get_val(row, cat_col, "未分类"),
                    "unit": self._get_val(row, unit_col, "PCS"),
                }
            )

    def _extract_demands(self, df: pd.DataFrame, source_name: str) -> List[Dict]:
        demands = []
        code_col, name_col, spec_col, qty_col, footprint_col, desig_col, comment_col = (
            self._find_col(df.columns, keys, ex)
            for keys, ex in [
                (["料号", "品号"], None),
                (["名称", "品名"], None),
                (["规格", "型号", "comment"], None),
                (["数量", "用量", "quantity"], None),
                (["封装", "footprint"], ["原理图"]),
                (["元件标号", "designator"], None),
                (["comment"], None),
            ]
        )

        for row in df.to_dict(orient="records"):
            code, name, spec, footprint = (
                self._get_val(row, code_col),
                self._get_val(row, name_col),
                self._get_val(row, spec_col),
                self._get_val(row, footprint_col),
            )
            source_for_item = source_name

            if source_name == "电子物料清单" and PCB_PATTERN.search(spec):
                pcb_name = self._get_val(row, desig_col) or self._get_val(row, comment_col) or name
                if pcb_name:
                    name = pcb_name
                source_for_item = "非电子物料清单"

            if not name and not spec:
                name = self._get_val(
                    row, self._find_col(df.columns, ["描述/型号", "描述", "明细", "项目", "description", "comment"])
                )

            name = "" if name.lower() in ["0", "0.0", "nan", "none"] else name
            spec = "" if spec.lower() in ["0", "0.0", "nan", "none"] else spec
            footprint = "" if footprint.lower() in ["0", "0.0", "nan", "none"] else footprint
            code = "" if code.lower() in ["0", "0.0", "nan", "none"] else code

            if not name and not spec:
                continue

            qty = self._safe_float(row.get(qty_col) if qty_col else 0.0)
            if qty > 0:
                demands.append(
                    {
                        "code": code,
                        "name": name,
                        "spec": spec,
                        "footprint": footprint,
                        "qty": qty,
                        "source": source_for_item,
                    }
                )
        return demands

    def _calculate_similarity(
        self,
        b_n: str,
        b_s: str,
        b_f: str,
        e_n: str,
        e_s: str,
        e_f: str,
        b_num: Set[str],
        b_zh: Set[str],
        e_num: Set[str],
        e_zh: Set[str],
        source: str,
    ) -> Dict[str, Any]:

        def pair_sim(s1: str, s2: str) -> float:
            if not s1 and not s2:
                return 100.0
            if not s1 or not s2:
                return 0.0

            s1_nospace = s1.replace(" ", "")
            s2_nospace = s2.replace(" ", "")
            holistic_score = difflib.SequenceMatcher(None, s1_nospace, s2_nospace).ratio() * 100.0

            t1_list = s1.split()
            t2_list = s2.split()
            if not t1_list or not t2_list:
                return max(holistic_score, difflib.SequenceMatcher(None, s1, s2).ratio() * 100.0)

            total_weight, total_score = 0, 0.0
            for w1 in t1_list:
                weight = len(w1)
                total_weight += weight
                max_score = 0.0
                for w2 in t2_list:
                    if w1 == w2:
                        max_score = 1.0
                        break
                    score = difflib.SequenceMatcher(None, w1, w2).ratio()
                    if score > max_score:
                        max_score = score
                total_score += max_score * weight

            token_score = (total_score / total_weight) * 100.0 if total_weight > 0 else 0.0

            return max(holistic_score, token_score)

        if source == "非电子物料清单":
            w_n, w_s, w_f = 0.7, 0.3, 0.0
            if not b_s:
                w_n, w_s = w_n + w_s, 0.0
            if not b_n:
                w_s, w_n = w_s + w_n, 0.0
        else:
            w_n, w_s, w_f = 0.0, 0.7, 0.3
            if not b_f:
                w_s, w_f = w_s + w_f, 0.0
            if not b_s:
                w_f, w_s = w_f + w_s, 0.0

        e_comb = f"{e_n} {e_s} {e_f}".strip()

        final_name_score = pair_sim(b_n, e_comb) if w_n > 0 else 0.0
        final_spec_score = pair_sim(b_s, e_comb) if w_s > 0 else 0.0
        final_footprint_score = pair_sim(b_f, e_comb) if w_f > 0 else 0.0

        weighted_score = final_name_score * w_n + final_spec_score * w_s + final_footprint_score * w_f

        b_comb_nospace = f"{b_n}{b_s}{b_f}".replace(" ", "")
        e_comb_nospace = e_comb.replace(" ", "")
        global_holistic_score = difflib.SequenceMatcher(None, b_comb_nospace, e_comb_nospace).ratio() * 100.0

        base_score = max(weighted_score, global_holistic_score)

        if b_num:
            matched_nums = 0
            for nb in b_num:
                matched = False
                for ne in e_num:
                    if nb == ne:
                        matched = True
                        break

                    if "%" in nb or "%" in ne:
                        continue

                    if nb in ne and not any(c.isdigit() for c in ne.replace(nb, "", 1)):
                        matched = True
                        break
                    if ne in nb and not any(c.isdigit() for c in nb.replace(ne, "", 1)):
                        matched = True
                        break

                if matched:
                    matched_nums += 1

            num_ratio = matched_nums / len(b_num)
            if num_ratio == 0:
                return {"score": 0.0, "w_n": w_n, "w_s": w_s, "w_f": w_f}
            base_score *= num_ratio**2

        return {"score": base_score, "w_n": w_n, "w_s": w_s, "w_f": w_f}

    def _match_single_item(
        self, bom_code: str, bom_name: str, bom_spec: str, bom_footprint: str, bom_qty: float, source: str
    ) -> Dict[str, Any]:

        bom_desc_raw = " ".join(filter(None, [str(bom_name), str(bom_spec), str(bom_footprint)])).strip()

        bom_name_orig, bom_spec_orig, bom_footprint_orig = (
            self._clean_str_calc(self._clean_str_display(bom_name)),
            self._clean_str_calc(self._clean_str_display(bom_spec)),
            self._clean_str_calc(self._clean_str_display(bom_footprint)),
        )

        bom_name_variants = self._fission_text(bom_name_orig)
        bom_spec_variants = self._fission_text(bom_spec_orig)
        bom_footprint_variants = self._fission_text(bom_footprint_orig)

        bom_variant_matrix = []
        for vn in bom_name_variants:
            for vs in bom_spec_variants:
                for vf in bom_footprint_variants:
                    if vn == bom_name_orig and vs == bom_spec_orig and vf == bom_footprint_orig:
                        continue

                    v_text = f"{vn} {vs}" if source == "非电子物料清单" else f"{vs} {vf}"
                    v_tokens = set(TOKEN_PATTERN.findall(v_text))
                    bom_variant_matrix.append(
                        {
                            "n": vn,
                            "s": vs,
                            "f": vf,
                            "num": {t for t in v_tokens if NUM_PATTERN.search(t)},
                            "zh": {t for t in v_tokens if ZH_PATTERN.search(t)},
                        }
                    )

        expanded_text_pool = " ".join(bom_name_variants + bom_spec_variants + bom_footprint_variants)
        expanded_source_tokens = set(t.upper() for t in TOKEN_PATTERN.findall(expanded_text_pool))

        # 💡 核心修复1：提前拉取知识库记忆
        history_dict = db_storage.get_deep_item(["bom_erp_mapping", bom_desc_raw], {})
        history_list = [
            {"erp_code": k, "hit_count": v.get("hit_count", 0)} for k, v in history_dict.items() if isinstance(v, dict)
        ]
        top_history_code = (
            sorted(history_list, key=lambda x: x["hit_count"], reverse=True)[0]["erp_code"] if history_list else None
        )

        filtered_erp_pool = []
        bom_code_str = str(bom_code).strip()
        for erp_item in self.erp_search_pool:
            e_code = str(erp_item["code"]).strip()

            # 💡 核心修复2：免死金牌，只要是历史绑定的料号，无视前缀过滤规则，直接保送进比对候选池
            if top_history_code and top_history_code in (e_code, erp_item["search_str"]):
                filtered_erp_pool.append(erp_item)
                continue

            if source == "电子物料清单":
                if not e_code.startswith("101"):
                    continue
            else:
                if bom_code_str and not e_code.startswith(bom_code_str):
                    continue
            filtered_erp_pool.append(erp_item)

        orig_tokens_all = set(
            TOKEN_PATTERN.findall(
                f"{bom_name_orig} {bom_spec_orig}"
                if source == "非电子物料清单"
                else f"{bom_spec_orig} {bom_footprint_orig}"
            )
        )
        orig_num = {t for t in orig_tokens_all if NUM_PATTERN.search(t)}
        orig_zh = {t for t in orig_tokens_all if ZH_PATTERN.search(t)}

        best_score, best_match, candidates = 0.0, None, []
        debug_weights = {"w_n": 0.0, "w_s": 0.0, "w_f": 0.0}

        for erp_item in filtered_erp_pool:
            res_orig = self._calculate_similarity(
                bom_name_orig,
                bom_spec_orig,
                bom_footprint_orig,
                erp_item["erp_name_orig"],
                erp_item["erp_spec_orig"],
                erp_item["erp_footprint_orig"],
                orig_num,
                orig_zh,
                erp_item["e_num_orig"],
                erp_item["e_zh_orig"],
                source,
            )

            best_res = res_orig
            used_syn = False

            if best_res["score"] < self.SCORE_GREEN and bom_variant_matrix:
                for var in bom_variant_matrix:
                    temp_res = self._calculate_similarity(
                        var["n"],
                        var["s"],
                        var["f"],
                        erp_item["erp_name_orig"],
                        erp_item["erp_spec_orig"],
                        erp_item["erp_footprint_orig"],
                        var["num"],
                        var["zh"],
                        erp_item["e_num_orig"],
                        erp_item["e_zh_orig"],
                        source,
                    )
                    if temp_res["score"] > best_res["score"]:
                        best_res = temp_res
                        used_syn = True
                        if best_res["score"] >= self.SCORE_GREEN:
                            break

            score = best_res["score"]
            debug_weights = {"w_n": best_res["w_n"], "w_s": best_res["w_s"], "w_f": best_res["w_f"]}
            is_history = top_history_code in (erp_item["code"], erp_item["search_str"])
            if is_history:
                score += self.SCORE_MEMORY_BOOST

            if score >= self.SCORE_MIN:
                candidates.append(
                    {**erp_item, "score": min(score, 100.0), "is_history": is_history, "used_syn": used_syn}
                )

        # 💡 核心修复3：同分时，历史记忆项具有绝对优先权（排在前面）
        candidates.sort(key=lambda x: (x["score"], x.get("is_history", False)), reverse=True)
        is_memorized, is_synonym_boosted = False, False
        if candidates:
            best_match, best_score = candidates[0], candidates[0]["score"]
            is_memorized, is_synonym_boosted = best_match.get("is_history", False), best_match.get("used_syn", False)

        status = (
            2
            if best_score >= self.SCORE_GREEN
            and (len(candidates) == 1 or best_score - candidates[1]["score"] >= self.SCORE_CONFLICT or is_memorized)
            else (1 if best_score >= self.SCORE_YELLOW else 0)
        )
        is_always_ignored = db_storage.get_deep_item(["bom_erp_ignored", bom_desc_raw], False)

        return {
            "source": source,
            "bom_code": bom_code_str,
            "bom_name": bom_name,
            "bom_spec": bom_spec,
            "bom_footprint": bom_footprint,
            "bom_desc": bom_desc_raw,
            "bom_qty": bom_qty,
            "kw_name": TOKEN_PATTERN.findall(bom_name_orig),
            "kw_spec": TOKEN_PATTERN.findall(bom_spec_orig),
            "kw_footprint": TOKEN_PATTERN.findall(bom_footprint_orig),
            "expanded_source_tokens": expanded_source_tokens,
            "debug_weights": debug_weights,
            "is_always_ignored": is_always_ignored,
            "is_direct": is_always_ignored,
            "is_memorized": is_memorized,
            "is_synonym_boosted": is_synonym_boosted,
            "status": status,
            "best_match": best_match,
            "candidates": candidates[:5],
        }

    async def _process_and_match(self):
        if self.erp_data is None or (self.non_elec_df is None and self.elec_df is None):
            ui.notify("请至少上传一份物料清单数据与库存数据！", type="warning")
            return

        self.show_upload, self.show_result, self.is_calculating, self.calc_progress = False, False, True, 0.0
        self.calc_progress_text = "正在初始化与预编译检索池特征..."
        await asyncio.sleep(0.1)

        self._build_erp_pool(self.erp_data)
        pool1 = self._extract_demands(self.non_elec_df, "非电子物料清单") if self.non_elec_df is not None else []
        pool2 = self._extract_demands(self.elec_df, "电子物料清单") if self.elec_df is not None else []

        merged_dict = {}
        for item in pool1 + pool2:
            key = f"{item['source']}_{item['code']}_{item['name']}_{item['spec']}_{item['footprint']}".upper().strip()
            if key in merged_dict:
                merged_dict[key]["qty"] += item["qty"]
            else:
                merged_dict[key] = item

        self.unified_bom_pool = list(merged_dict.values())
        total_items = len(self.unified_bom_pool)

        if total_items == 0:
            self.is_calculating, self.show_result, self.summary_text = (
                False,
                True,
                "🚨 未能从表格中提取到有效的物料需求，请检查表格是否为空！",
            )
            self._check_export_status()
            return

        results, loop = [], asyncio.get_running_loop()
        for i, item in enumerate(self.unified_bom_pool):
            res = await loop.run_in_executor(
                None,
                self._match_single_item,
                item["code"],
                item["name"],
                item["spec"],
                item["footprint"],
                item["qty"],
                item["source"],
            )
            results.append(res)
            if i % 3 == 0 or i == total_items - 1:
                self.calc_progress, self.calc_progress_text = (
                    (i + 1) / total_items,
                    f"后台多线程疾速核算：已完成 {i + 1} / {total_items} 项",
                )
                await asyncio.sleep(0.001)

        self.match_results = results
        self._update_summary()

        self.is_calculating, self.show_result = False, True
        self._check_export_status()
        self.render_result_grid.refresh()  # type: ignore

    # ==========================================
    # 核心渲染引擎
    # ==========================================
    def _generate_diff_html(self, expanded_source_tokens: Set[str], target: str) -> str:
        if not target:
            return "<span class='text-gray-400'>-</span>"

        html = []
        last_end = 0

        for match in TOKEN_PATTERN.finditer(target):
            start, end = match.span()
            if start > last_end:
                sep = target[last_end:start]
                html.append(f"<span class='text-gray-400'>{sep}</span>")

            raw_token = match.group()
            cleaned_token_soul = self._clean_str_calc(self._clean_str_display(raw_token)).upper()

            if cleaned_token_soul in expanded_source_tokens:
                html.append(f"<span class='text-green-700 font-bold bg-green-100 px-[2px] rounded'>{raw_token}</span>")
            else:
                html.append(f"<span class='text-gray-400'>{raw_token}</span>")

            last_end = end

        if last_end < len(target):
            html.append(f"<span class='text-gray-400'>{target[last_end:]}</span>")

        return "".join(html)

    def _generate_tokens_html(self, tokens: List[str], color_theme: str) -> str:
        if not tokens:
            return "<span class='text-gray-400'>-</span>"
        html = []
        for t in tokens:
            html.append(
                f"<span class='inline-block px-[4px] py-[2px] mb-1 mr-1 text-[10px] font-bold tracking-tight rounded bg-{color_theme}-50 text-{color_theme}-600 border border-{color_theme}-200 shadow-sm'>{t}</span>"
            )
        return "".join(html)

    def _build_row_container(self, res: Dict[str, Any]):
        container = ui.element("div").classes("w-full")

        def refresh_row():
            container.clear()
            self._update_summary()
            self._check_export_status()
            with container:
                self._render_table_row(res, refresh_row)

        refresh_row()

    @ui.refreshable
    def render_result_grid(self):
        if not self.match_results:
            with ui.column().classes("w-full h-full items-center justify-center text-gray-400"):
                ui.icon("sentiment_dissatisfied", size="4em")
                ui.label("未能提取到有效数据").classes("mt-4 text-lg")
            return

        with ui.scroll_area().classes("w-full h-full bg-white rounded shadow-sm border"):
            min_width_class = "min-w-[1400px]" if SHOW_EXTRACTED_KEYWORDS else "min-w-[1100px]"

            with ui.column().classes(f"w-full {min_width_class}"):
                with ui.row().classes(
                    "w-full bg-slate-100 border-b p-2 items-center font-bold text-gray-700 text-sm flex-nowrap sticky top-0 z-10"
                ):
                    ui.label("匹配状态").classes("w-20 text-center shrink-0")
                    ui.label("物料清单描述 (源数据)").classes("w-[22%] px-2 shrink-0")

                    if SHOW_EXTRACTED_KEYWORDS:
                        ui.label("提取品名 Token").classes("w-28 px-1 shrink-0 text-xs text-blue-700")
                        ui.label("提取规格 Token").classes("w-32 px-1 shrink-0 text-xs text-purple-700")
                        ui.label("提取封装 Token").classes("w-20 px-1 shrink-0 text-xs text-teal-700")

                    ui.label("知识库目标比对 (绿色为命中参数)").classes("flex-1 px-2 shrink-0")
                    ui.label("需求/库存").classes("w-20 text-center shrink-0")
                    ui.label("最终决策与操作").classes("w-[360px] pl-2 shrink-0")

                with ui.column().classes("w-full divide-y divide-gray-100"):
                    for res in self.match_results:
                        self._build_row_container(res)

    def _render_table_row(self, res: Dict[str, Any], refresh_row: Any):
        is_direct, is_always_ignored = res.get("is_direct", False), res.get("is_always_ignored", False)

        if is_always_ignored:
            row_bg, icon_name, icon_color, status_text = (
                "bg-gray-50 opacity-70",
                "do_not_disturb_on",
                "text-gray-400",
                "永不请购",
            )
        elif is_direct:
            row_bg, icon_name, icon_color, status_text = ("bg-gray-50 opacity-70", "block", "text-gray-400", "本次忽略")
        else:
            row_bg = {0: "hover:bg-red-50/40", 1: "hover:bg-yellow-50/40", 2: "hover:bg-green-50/40"}[res["status"]]
            icon_name = {0: "error", 1: "warning", 2: "check_circle"}[res["status"]]
            icon_color = {0: "text-red-500", 1: "text-yellow-500", 2: "text-green-500"}[res["status"]]
            status_text = {0: "未匹配", 1: "待确认", 2: "已确认"}[res["status"]]

        with ui.row().classes(f"w-full py-2 px-2 items-start flex-nowrap transition-colors {row_bg}"):
            with ui.row().classes("w-20 items-center justify-center gap-1 shrink-0 pt-1"):
                ui.icon(icon_name).classes(f"text-lg {icon_color}")
                ui.label(status_text).classes(f"text-xs font-bold {icon_color}")

            with ui.column().classes("w-[22%] px-2 shrink-0"):
                ui.label(res["bom_desc"]).classes("text-sm font-bold break-words line-clamp-2")
                ui.label(f"[{res['source']}]").classes("text-[10px] text-gray-400")

            if SHOW_EXTRACTED_KEYWORDS:
                with ui.column().classes("w-28 px-1 shrink-0"):
                    ui.html(self._generate_tokens_html(res.get("kw_name", []), "blue"), sanitize=False).classes(
                        "break-words w-full leading-tight"
                    )
                with ui.column().classes("w-32 px-1 shrink-0"):
                    ui.html(self._generate_tokens_html(res.get("kw_spec", []), "purple"), sanitize=False).classes(
                        "break-words w-full leading-tight"
                    )
                with ui.column().classes("w-20 px-1 shrink-0"):
                    ui.html(self._generate_tokens_html(res.get("kw_footprint", []), "teal"), sanitize=False).classes(
                        "break-words w-full leading-tight"
                    )

            with ui.column().classes("flex-1 px-2 shrink-0"):
                if is_direct:
                    ui.label("不参与库存计算").classes("text-xs text-gray-400")
                else:
                    if res.get("best_match"):
                        target_str = res["best_match"]["search_str"]
                        diff_html_str = self._generate_diff_html(res.get("expanded_source_tokens", set()), target_str)
                        ui.html(diff_html_str, sanitize=False).classes("text-xs break-words line-clamp-2")

                        with ui.row().classes("items-center gap-2 mt-[2px]"):
                            score_text = f"匹配度: {res['best_match']['score']:.1f}%"
                            ui.label(score_text).classes(
                                f"text-[10px] font-bold {'text-green-600' if res['best_match']['score'] >= self.SCORE_GREEN else 'text-yellow-600'}"
                            )

                            if res.get("is_synonym_boosted"):
                                ui.label("🔄 等效词触发").classes(
                                    "text-[9px] font-bold text-indigo-600 bg-indigo-50 border border-indigo-200 px-1 rounded"
                                )

                            if res.get("is_memorized"):
                                ui.label("🧠 知识库记忆").classes(
                                    "text-[9px] font-bold text-orange-600 bg-orange-50 border border-orange-200 px-1 rounded"
                                )
                    else:
                        ui.label("0.0% (无合格项)").classes("text-xs text-red-500 font-bold")

            with ui.column().classes("w-20 items-center px-1 shrink-0 pt-1"):
                ui.label(f"{res['bom_qty']}").classes("text-sm text-gray-800 font-bold")
                if res["status"] == 2 and not is_direct:
                    stock = res["best_match"]["stock"]
                    if stock >= res["bom_qty"]:
                        ui.label(f"库:{stock} (足)").classes("text-[10px] text-green-600 font-bold")
                    else:
                        ui.label(f"缺:{res['bom_qty'] - stock}").classes(
                            "text-[10px] text-white bg-red-500 px-1 rounded"
                        )

            with ui.row().classes("w-[360px] items-start gap-2 pl-2 flex-nowrap shrink-0"):
                if is_direct:

                    async def toggle_off(r=res, always=is_always_ignored):
                        r["is_direct"] = False
                        if always:
                            r["is_always_ignored"] = False
                            await db_storage.atomic_deep_update(["bom_erp_ignored", r["bom_desc"]], lambda _: False)
                            ui.notify("已取消永远忽略", type="info")
                        refresh_row()

                    ui.button("取消忽略，恢复匹配", on_click=toggle_off).props("size=sm outline color=gray")
                    ui.space()
                else:
                    if res["status"] == 2:

                        async def on_manual_correct(r=res):
                            if r.get("is_memorized"):
                                await db_storage.del_deep_item(
                                    [
                                        "bom_erp_mapping",
                                        r["bom_desc"],
                                        r["best_match"]["code"] or r["best_match"]["search_str"],
                                    ]
                                )
                                r["is_memorized"] = False
                                new_state = self._match_single_item(
                                    r.get("bom_code", ""),
                                    r.get("bom_name", ""),
                                    r.get("bom_spec", ""),
                                    r.get("bom_footprint", ""),
                                    r.get("bom_qty", 0.0),
                                    r.get("source", ""),
                                )
                                r.update(new_state)
                            else:
                                r["status"] = 0
                                r["best_match"] = None
                            refresh_row()

                        btn_text = "解绑映射" if res.get("is_memorized") else "手动纠错"
                        ui.button(btn_text, on_click=on_manual_correct).props("size=sm color=warning outline")

                    elif res["status"] == 1:
                        dropdown = (
                            ui.select({i: c["display"] for i, c in enumerate(res["candidates"])}, value=0)
                            .classes("w-[140px] text-xs bg-white")
                            .props("dense outlined")
                        )

                        async def on_confirm(r=res, drop=dropdown, cands=res["candidates"]):
                            if drop.value is not None:
                                sel = cands[drop.value]
                                await self._update_memory_dict(r["bom_desc"], sel["code"] or sel["search_str"])
                                r.update({"status": 2, "best_match": sel, "is_memorized": True})
                                refresh_row()

                        ui.button("确认映射", on_click=on_confirm).props("size=sm color=warning text-black outline")

                    elif res["status"] == 0:
                        manual_input = (
                            ui.input("请输入确切品号").classes("w-28 text-xs bg-white").props("dense outlined")
                        )

                        async def on_bind(r=res, inp=manual_input):
                            code = inp.value.strip()
                            if code:
                                erp_item = next((item for item in self.erp_search_pool if item["code"] == code), None)
                                if not erp_item:
                                    ui.notify("未找到该品号！", type="negative")
                                    return
                                await self._update_memory_dict(r["bom_desc"], code)

                                bind_item = erp_item.copy()
                                bind_item["score"] = 100.0  # 补丁：赋予100分防止UI渲染错误

                                r.update({"status": 2, "best_match": bind_item, "is_memorized": True})
                                refresh_row()

                        ui.button("强制绑定", on_click=on_bind).props("size=sm color=negative outline")

                    ui.space()

                    def toggle_on(r=res):
                        r["is_direct"] = True
                        refresh_row()

                    ui.button("本次忽略", on_click=toggle_on).props("size=sm flat color=gray")

                    async def toggle_always_ignore_on(r=res):
                        r.update({"is_always_ignored": True, "is_direct": True})
                        await db_storage.atomic_deep_update(["bom_erp_ignored", r["bom_desc"]], lambda _: True)
                        refresh_row()

                    ui.button("永远忽略", on_click=toggle_always_ignore_on).props("size=sm outline color=gray")

    async def _update_memory_dict(self, bom_desc: str, erp_code: str):
        def increment_weight(current_val):
            if not isinstance(current_val, dict):
                current_val = {"hit_count": 0, "last_used": None}
            current_val["hit_count"] += 1
            current_val["last_used"] = datetime.now().isoformat()
            return current_val

        await db_storage.atomic_deep_update(["bom_erp_mapping", bom_desc, erp_code], increment_weight)
