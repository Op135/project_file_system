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
SHOW_EXTRACTED_KEYWORDS = False

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
        # 🆕 UI 统一样式与列宽常量区
        # ==========================================
        # 考虑到浏览器页面缩放，采用固定宽度与百分比结合的弹性布局
        self.COL_STATUS = "w-20"
        self.COL_DESC = "w-[22%]"
        self.COL_KW_NAME = "w-28"
        self.COL_KW_SPEC = "w-32"
        self.COL_KW_FOOTPRINT = "w-20"
        self.COL_COMPARE = "flex-1"
        self.COL_QTY = "w-20"
        self.COL_ACTION = "w-[460px]"

        # 统一操作区按钮样式字典（避免杂乱无章）
        self.BTN_PRIMARY = "color=primary size=sm shadow-sm"
        self.BTN_OUTLINE = "outline color=primary size=sm"
        self.BTN_WARNING = "outline color=warning size=sm text-black"
        self.BTN_DANGER = "outline color=negative size=sm"
        self.BTN_FLAT = "flat color=gray size=sm text-gray-600"
        # ==========================================
        # 2. 匹配分数阈值管控区
        # ==========================================
        self.SCORE_GREEN = 85.0
        self.SCORE_YELLOW = 20.0
        self.SCORE_MIN = 10.0
        self.SCORE_CONFLICT = 5.0
        self.SCORE_MEMORY_BOOST = 100.0
        # 🆕 业务逻辑状态变量
        self.assessment_sets: int = 1  # 评估套数因子
        self.calc_progress_percent: str = "0%"  # 进度条内显示的百分比
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

    def _calculate_final_purchase_qty(self, res: Dict[str, Any]) -> float:
        """
        统一的请购量收口函数。
        """
        bom_qty = res.get("bom_qty", 0.0)

        if res.get("is_direct", False):
            return bom_qty

        if res.get("status") == 2 and res.get("best_match"):
            stock = res.get("best_match", {}).get("stock", 0.0)
            safety_stock = res.get("best_match", {}).get("safety_stock", 0.0)

            # 🚀 核心计算变更：打破可用量壁垒，实际抵扣库存 = 账面可用量 + 安全存量
            effective_stock = stock + safety_stock

            return max(0.0, bom_qty - effective_stock)

        return bom_qty

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
                with ui.row().classes("w-full items-center justify-between mb-3"):
                    ui.label("第一步：载入核心数据源 (任一物料清单 + 库存数据即可)").classes(
                        "text-base font-bold text-gray-700 m-0"
                    )
                    # nicegui (第三方UI框架): 数字输入组件，限制最小值为1，绑定至套数变量
                    ui.number("评估套数", value=1, min=1, step=1).bind_value_to(self, "assessment_sets").classes(
                        "w-32"
                    ).props("dense outlined")

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

            # 👉 改造计算进度区域，实现内部百分比 + 底部双轨分子进度
            with (
                ui.column()
                .bind_visibility_from(self, "is_calculating")
                .classes("w-full items-center justify-center py-10 gap-4 shrink-0")
            ):
                ui.spinner("cube", size="4em", color="primary")
                ui.label("后台正在执行漏斗过滤与跨列聚合寻优...").classes(
                    "text-lg font-bold text-gray-600 animate-pulse"
                )

                # 使用相对定位父容器包裹进度条，打破自带组件无法内置文本的限制
                with ui.element("div").classes("w-1/2 mt-4 relative"):
                    ui.linear_progress(value=0.0).bind_value_from(self, "calc_progress").classes("w-full").props(
                        "rounded size=20px color=blue-400"
                    )
                    ui.label().bind_text_from(self, "calc_progress_percent").classes(
                        "absolute inset-0 flex items-center justify-center text-white text-xs font-bold drop-shadow-sm"
                    )

                # 底部改为显示分子进度的文字
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
        # 用于存放按 ERP 料号聚合后的数据字典
        aggregated_erp_demands = {}
        # 用于存放直购或未匹配等不需要聚合的游离物料
        other_demands = []

        for r in self.match_results:
            is_direct = r.get("is_direct", False)
            match = r.get("best_match")
            status = r.get("status", 0)

            # 原BOM信息的格式化拼接，方便溯源
            bom_trace_info = f"{r['bom_desc']} (需求:{r['bom_qty']})"
            if r.get("bom_code"):
                bom_trace_info = f"[{r['bom_code']}] {bom_trace_info}"

            # 情况A：已成功匹配 ERP 料号，且非直购，进入聚合池
            if not is_direct and status == 2 and match:
                erp_code = match.get("code", "无")

                if erp_code not in aggregated_erp_demands:
                    # 初始化该 ERP 料号的聚合数据结构
                    aggregated_erp_demands[erp_code] = {
                        "erp_code": erp_code,
                        "erp_desc": match.get("search_str", ""),
                        "category": match.get("category", "未分类"),
                        "unit": match.get("unit", "PCS"),
                        "stock": match.get("stock", 0.0),
                        "safety_stock": match.get("safety_stock", 0.0),
                        "total_bom_qty": 0.0,
                        "bom_trace_list": [],  # 记录是由哪些 BOM 物料合并而来的
                    }

                # 累加总需求量
                aggregated_erp_demands[erp_code]["total_bom_qty"] += r["bom_qty"]
                # 将当前 BOM 物料信息压入追溯列表
                aggregated_erp_demands[erp_code]["bom_trace_list"].append(bom_trace_info)

            # 情况B：直购 或 未确认/未匹配的物料，独立成行
            else:
                final_qty = r["bom_qty"]
                status_text = "⚪ 直接请购" if is_direct else "🔴 需请购(未匹配)"

                other_demands.append(
                    {
                        "状态": status_text,
                        "商品分类": "未分类",
                        "ERP料号": "无",
                        "原BOM清单物料追溯": bom_trace_info,
                        "核算总需求量": final_qty,
                        "有效库存(含安全存量)": "-",
                        "ERP物料描述": "无",
                        "实际需请购量": final_qty,
                        "单位": "PCS",
                    }
                )

        export_data = []

        # 处理聚合池中的 ERP 物料，进行统一的库存扣减计算
        for erp_code, data in aggregated_erp_demands.items():
            total_qty = data["total_bom_qty"]
            effective_stock = data["stock"] + data["safety_stock"]
            # 实际请购量 = 总需求量 - 总有效库存，若小于0则取0
            purchase_qty = max(0.0, total_qty - effective_stock)

            status_text = "🔴 需请购" if purchase_qty > 0 else "🟢 库存满足"

            export_data.append(
                {
                    "状态": status_text,
                    "商品分类": data["category"],
                    "ERP料号": erp_code,
                    "ERP物料描述": data["erp_desc"],  # 核心修复：导出 ERP 自己的标准描述
                    "原BOM清单物料追溯": " +\n".join(data["bom_trace_list"]),  # 多条BOM记录用换行符拼接
                    "核算总需求量": total_qty,
                    "有效库存(含安全存量)": effective_stock,
                    "实际需请购量": purchase_qty,
                    "单位": data["unit"],
                }
            )

        # 将两部分数据合并
        export_data.extend(other_demands)

        # pandas (强大的第三方数据分析处理库): 将组装好的字典列表转化为DataFrame表格对象
        df = pd.DataFrame(export_data)

        # 建立排序辅助列，确保需要采购的排在前面
        df["_sort_status"] = df["状态"].map(
            {"🔴 需请购": 0, "🔴 需请购(未匹配)": 1, "⚪ 直接请购": 2, "🟢 库存满足": 3}
        )
        df = df.sort_values(by=["_sort_status", "商品分类", "ERP料号"]).drop(columns=["_sort_status"])

        # io.BytesIO (Python内置的内存流I/O操作库): 在内存中创建二进制流，避免产生本地磁盘垃圾文件
        output = io.BytesIO()
        df.to_excel(output, index=False, sheet_name="智能核算请购单")
        output.seek(0)

        ui.download(output.getvalue(), filename=f"智能物料聚合请购单_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
        ui.notify("聚合请购单导出成功！", type="positive")

    async def _handle_upload(self, e: Any, target: str):
        try:
            # 获取执行结果，避免在判断语句中产生孤儿协程
            read_result = e.file.read()

            # 检查结果是否为协程对象，如果是则进行 await
            if asyncio.iscoroutine(read_result):
                file_bytes = await read_result
            else:
                file_bytes = read_result

            # io.BytesIO: Python 内置库，将字节流封装为内存文件对象，以便 pandas(第三方数据分析库) 读取
            df = self._smart_read_dataframe(io.BytesIO(file_bytes), e.file.name)

            if target == "non_elec":
                self.non_elec_df, self.status_non_elec = df, f"成功提取真实物料 {len(df)} 行"
            elif target == "elec":
                self.elec_df, self.status_elec = df, f"成功提取真实物料 {len(df)} 行"
            elif target == "erp":
                self.erp_data, self.status_erp = df, f"成功载入库存档案 {len(df)} 行"
            self._check_ready()
        except Exception as ex:
            # ui.notify: nicegui(第三方UI框架) 的消息提示组件，用于在浏览器前端弹出通知
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
        code_col, name_col, spec_col, stock_col, cat_col, unit_col, safety_stock_col = (
            self._find_col(df.columns, keys)
            for keys in [
                ["品号", "料号"],
                ["名称", "品名"],
                ["规格", "型号", "comment"],
                ["可用量", "数量", "quantity"],
                ["商品分类"],
                ["单位"],
                ["安全存量", "安全库存"],
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
            # 提取安全存量，如果表里没有这一列或数据为空，则安全地默认设为 0.0
            safety_stock = self._safe_float(row.get(safety_stock_col) if safety_stock_col else 0.0)
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
                    "safety_stock": safety_stock,
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

        # 💡 核心修复1：提前拉取映射库记忆
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
            # ui.notify: nicegui(第三方UI框架) 的消息提示组件
            ui.notify("请至少上传一份物料清单数据与库存数据！", type="warning")
            return

        self.show_upload, self.show_result, self.is_calculating, self.calc_progress = False, False, True, 0.0
        self.calc_progress_text = "正在初始化与预编译检索池特征..."
        await asyncio.sleep(0.1)

        self._build_erp_pool(self.erp_data)
        pool1 = self._extract_demands(self.non_elec_df, "非电子物料清单") if self.non_elec_df is not None else []
        pool2 = self._extract_demands(self.elec_df, "电子物料清单") if self.elec_df is not None else []

        merged_dict = {}
        # 提取最新填写的套数，兜底值为 1
        sets_multiplier = int(self.assessment_sets) if self.assessment_sets else 1

        for item in pool1 + pool2:
            key = f"{item['source']}_{item['code']}_{item['name']}_{item['spec']}_{item['footprint']}".upper().strip()

            # 🚨 核心改动：用量 = 单套用量 × 评估套数
            total_qty_for_item = item["qty"] * sets_multiplier

            if key in merged_dict:
                merged_dict[key]["qty"] += total_qty_for_item
            else:
                item["qty"] = total_qty_for_item
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

        # asyncio.get_running_loop: Python 异步库方法，获取当前事件循环，用于将计算密集型任务放入线程池
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

            # 🚨 进度条文本状态派发
            if i % 3 == 0 or i == total_items - 1:
                progress_value = (i + 1) / total_items
                self.calc_progress = round(progress_value, 4)
                # 派发内部百分比
                self.calc_progress_percent = f"{self.calc_progress * 100:.1f}%"
                # 派发底部分子形式进度
                self.calc_progress_text = f"后台多线程疾速核算：已完成 {i + 1} / {total_items}"
                # asyncio.sleep (Python内置): 主动让出 CPU 控制权，保障主线程 UI 渲染刷新
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
        container = ui.row()

        def refresh_row():
            container.clear()
            self._update_summary()
            self._check_export_status()
            with container:
                self._render_table_row(res, container, refresh_row)

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
                    ui.label("匹配状态").classes(f"{self.COL_STATUS} text-center shrink-0")
                    ui.label("物料清单描述").classes(f"{self.COL_DESC} px-2 shrink-0")

                    if SHOW_EXTRACTED_KEYWORDS:
                        ui.label("品名 Token").classes(f"{self.COL_KW_NAME} px-1 shrink-0 text-xs text-blue-700")
                        ui.label("规格 Token").classes(f"{self.COL_KW_SPEC} px-1 shrink-0 text-xs text-purple-700")
                        ui.label("封装 Token").classes(f"{self.COL_KW_FOOTPRINT} px-1 shrink-0 text-xs text-teal-700")

                    ui.label("库存目标比对").classes(f"{self.COL_COMPARE} px-2 shrink-0")
                    ui.label("需求/库存").classes(f"{self.COL_QTY} text-center shrink-0")
                    ui.label("最终决策与操作").classes(f"{self.COL_ACTION} pl-2 shrink-0")

                with ui.column().classes("w-full -space-y-3 divide-y divide-gray-100"):
                    for res in self.match_results:
                        self._build_row_container(res)

    def _render_table_row(self, res: Dict[str, Any], container, refresh_row: Any):
        is_direct, is_always_ignored = res.get("is_direct", False), res.get("is_always_ignored", False)

        if is_always_ignored:
            row_bg, icon_name, icon_color, status_text = (
                "bg-gray-50 opacity-70",
                "do_not_disturb_on",
                "text-gray-400",
                "永远直购",
            )
        elif is_direct:
            row_bg, icon_name, icon_color, status_text = ("bg-gray-50 opacity-70", "block", "text-gray-400", "本次直购")
        else:
            row_bg = {
                0: "bg-red-50/40 hover:bg-red-100/40",
                1: "bg-yellow-50/40 hover:bg-yellow-100/40",
                2: "bg-green-50/40 hover:bg-green-100/40",
            }[res["status"]]
            icon_name = {0: "error", 1: "warning", 2: "check_circle"}[res["status"]]
            icon_color = {0: "text-red-500", 1: "text-yellow-500", 2: "text-green-500"}[res["status"]]
            status_text = {0: "未匹配", 1: "待确认", 2: "已确认"}[res["status"]]

        with container.classes(f"w-full py-2 px-2 items-center flex-nowrap transition-colors {row_bg}"):
            with ui.row().classes(f"{self.COL_STATUS} items-center justify-center gap-1 shrink-0 pt-1"):
                ui.icon(icon_name).classes(f"text-lg {icon_color}")
                ui.label(status_text).classes(f"text-xs font-bold {icon_color}")

            with ui.column().classes(f"{self.COL_DESC} px-2 -space-y-3 shrink-0"):
                ui.label(res["bom_desc"]).classes("text-sm font-bold break-words line-clamp-2")
                # ui.label(f"[{res['source']}]").classes("text-[10px] text-gray-400")

            if SHOW_EXTRACTED_KEYWORDS:
                with ui.column().classes(f"{self.COL_KW_NAME} px-1 shrink-0"):
                    ui.html(self._generate_tokens_html(res.get("kw_name", []), "blue"), sanitize=False).classes(
                        "break-words w-full leading-tight"
                    )
                with ui.column().classes(f"{self.COL_KW_SPEC} px-1 shrink-0"):
                    ui.html(self._generate_tokens_html(res.get("kw_spec", []), "purple"), sanitize=False).classes(
                        "break-words w-full leading-tight"
                    )
                with ui.column().classes(f"{self.COL_KW_FOOTPRINT} px-1 shrink-0"):
                    ui.html(self._generate_tokens_html(res.get("kw_footprint", []), "teal"), sanitize=False).classes(
                        "break-words w-full leading-tight"
                    )

            with ui.column().classes(f"{self.COL_COMPARE} px-2 -space-y-3 shrink-0"):
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
                                ui.label("🧠 映射库记忆").classes(
                                    "text-[9px] font-bold text-orange-600 bg-orange-50 border border-orange-200 px-1 rounded"
                                )
                    else:
                        ui.label("0.0% (无疑似项)").classes("text-xs text-red-500 font-bold")

            # 获取统一计算的请购量
            # final_qty = self._calculate_final_purchase_qty(res)

            with ui.column().classes(f"{self.COL_QTY} items-center px-1 shrink-0 pt-1"):
                # 顶部固定显示：当前清单行的“单项需求量”
                ui.label(f"{res['bom_qty']}").classes("text-sm text-gray-800 font-bold leading-tight")

                if is_direct:
                    ui.label("直购全量").classes("text-[10px] text-gray-500 font-bold mt-1")
                elif res["status"] == 2 and res.get("best_match"):
                    # 🚀 核心优化：动态统计当前所有指向该 ERP 料号的总需求量
                    target_erp_code = res["best_match"].get("code", "")

                    # 遍历整个 match_results，累加所有状态为已确认(2)且非直购的同款 ERP 需求
                    global_demand = sum(
                        r["bom_qty"]
                        for r in self.match_results
                        if r.get("status") == 2
                        and not r.get("is_direct", False)
                        and r.get("best_match", {}).get("code") == target_erp_code
                    )

                    stock = res["best_match"]["stock"]
                    safety_stock = res["best_match"].get("safety_stock", 0.0)
                    effective_stock = stock + safety_stock

                    # 实际缺口 = 全局总需求 - 有效库存
                    global_shortage = max(0.0, global_demand - effective_stock)

                    # 如果有多行 BOM 指向同一个 ERP，增加“合并总需”的视觉提示
                    if global_demand > res["bom_qty"]:
                        ui.label(f"总需:{global_demand}").classes(
                            "text-[9px] text-blue-700 font-bold mt-1 leading-tight"
                        )

                    if global_shortage == 0:
                        ui.label(f"库:{effective_stock} (足)").classes(
                            "text-[10px] text-green-600 font-bold mt-1 leading-tight"
                        )
                    else:
                        ui.label(f"总缺:{global_shortage}").classes(
                            "text-[10px] text-white bg-red-500 px-[4px] rounded mt-1 leading-tight"
                        )

            with ui.row().classes(f"{self.COL_ACTION} items-center gap-2 pl-2 flex-nowrap shrink-0"):
                if is_direct:

                    async def toggle_off(r=res, always=is_always_ignored):
                        r["is_direct"] = False
                        if always:
                            r["is_always_ignored"] = False
                            await db_storage.atomic_deep_update(["bom_erp_ignored", r["bom_desc"]], lambda _: False)
                            ui.notify("已取消永远直购", type="info")
                        refresh_row()

                    ui.button("取消直购", on_click=toggle_off).props(self.BTN_FLAT)
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

                        btn_text = "解绑" if res.get("is_memorized") else "纠错"
                        ui.button(btn_text, on_click=on_manual_correct).props(self.BTN_WARNING)

                    elif res["status"] == 1:
                        # 检查当前是否处于“请求手动输入”的临时状态
                        if res.get("show_manual_input", False):
                            # nicegui: 渲染与状态0（未匹配）相同的输入框组件
                            manual_input = (
                                ui.input("请输入确切品号").classes("w-28 text-xs bg-white").props("dense outlined")
                            )

                            async def on_bind_yellow(r=res, inp=manual_input):
                                code = inp.value.strip()
                                if code:
                                    erp_item = next(
                                        (item for item in self.erp_search_pool if item["code"] == code), None
                                    )
                                    if not erp_item:
                                        # nicegui: 弹出负面提示通知
                                        ui.notify("未找到该品号！", type="negative")
                                        return
                                    await self._update_memory_dict(r["bom_desc"], code)

                                    bind_item = erp_item.copy()
                                    bind_item["score"] = 100.0  # 补丁：赋予100分防止UI渲染错误

                                    # 更新状态并清除手动输入标记
                                    r.update(
                                        {
                                            "status": 2,
                                            "best_match": bind_item,
                                            "is_memorized": True,
                                            "show_manual_input": False,
                                        }
                                    )
                                    refresh_row()

                            ui.button("强制绑定", on_click=on_bind_yellow).props(self.BTN_DANGER)

                            # 反悔操作：取消手动输入，切回下拉推荐列表
                            def cancel_manual(r=res):
                                r["show_manual_input"] = False
                                refresh_row()

                            ui.button("返回推荐", on_click=cancel_manual).props(self.BTN_FLAT)

                        else:
                            # 默认显示：算法推荐下拉框
                            # nicegui: 下拉选择组件，此处构建候选物料索引字典
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

                            ui.button("映射", on_click=on_confirm).props(self.BTN_PRIMARY)

                            # 切换操作：点击后进入手动输入模式
                            def switch_to_manual(r=res):
                                r["show_manual_input"] = True
                                refresh_row()

                            ui.button("指定", on_click=switch_to_manual).props(self.BTN_OUTLINE)

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

                        ui.button("强制绑定", on_click=on_bind).props(self.BTN_DANGER)

                    ui.space()

                    def toggle_on(r=res):
                        r["is_direct"] = True
                        refresh_row()

                    ui.button("本次直购", on_click=toggle_on).props(self.BTN_OUTLINE)

                    async def toggle_always_ignore_on(r=res):
                        r.update({"is_always_ignored": True, "is_direct": True})
                        await db_storage.atomic_deep_update(["bom_erp_ignored", r["bom_desc"]], lambda _: True)
                        refresh_row()

                    ui.button("永远直购", on_click=toggle_always_ignore_on).props(self.BTN_FLAT)

    async def _update_memory_dict(self, bom_desc: str, erp_code: str):
        def increment_weight(current_val):
            if not isinstance(current_val, dict):
                current_val = {"hit_count": 0, "last_used": None}
            current_val["hit_count"] += 1
            current_val["last_used"] = datetime.now().isoformat()
            return current_val

        await db_storage.atomic_deep_update(["bom_erp_mapping", bom_desc, erp_code], increment_weight)
