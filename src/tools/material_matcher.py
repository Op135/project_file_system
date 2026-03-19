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
# 1. 全局解析规则管控区（符号提纯）
# ==========================================
# \s匹配任何空白字符，[\]等匹配各类中英文括号、逗号等。用于将人类书写的冗余标点全部替换为空格
SEPARATORS_REGEX = r"[\s,\;、\|_\(\)（）\[\]【】\/\\]+"
# 匹配纯英文、数字以及特定符号（如小数点、乘号、正负号），作为工程规格的专属提取规则
SPEC_TOKEN_REGEX = r"[A-Z0-9\.\-\*\%\+\×]+"
# \u4e00-\u9fa5 是汉字的Unicode编码范围，用于专门提取中文字符串
ZH_TOKEN_REGEX = r"[\u4e00-\u9fa5]+"

# re.compile: 预编译正则表达式对象，比每次直接调用re.match速度更快，适合高频循环调用
TOKEN_PATTERN = re.compile(f"{SPEC_TOKEN_REGEX}|{ZH_TOKEN_REGEX}")
NUM_PATTERN = re.compile(r"\d")  # 专门用于检测字符串中是否包含至少一个数字
ZH_PATTERN = re.compile(ZH_TOKEN_REGEX)

# 针对PCB电路板的专属提纯规则（匹配类似“绿油白字”、“哑黑”等特征）
PCB_PATTERN = re.compile(r"[黑白红绿蓝黄紫亚哑]色?油[黑白红绿蓝黄紫亚哑]色?字")


class MaterialMatcherTool:
    """智能物料匹配工具类 —— 跨列聚合、冗余精度洗牌与详尽注释版"""

    def __init__(self):
        # ==========================================
        # 2. 匹配分数阈值管控区
        # ==========================================
        self.SCORE_GREEN = 85.0  # 自动变绿（无需人工确认）的最低分数线
        self.SCORE_YELLOW = 40.0  # 变黄（进入人工候选池）的最低分数线
        self.SCORE_MIN = 20.0  # 绝对底线，低于此分的ERP物料连进入候选项的资格都没有
        self.SCORE_CONFLICT = 5.0  # 防呆机制：如果最高分和第二名分差小于5分，哪怕过85也会被强制降级为黄灯让你确认
        self.SCORE_MEMORY_BOOST = 100.0  # 若命中历史知识库，直接加100分保送第一

        # Optional[pd.DataFrame] 表示该变量可以是 Pandas 的 DataFrame 对象，也可以是 None (初始状态)
        self.non_elec_df: Optional[pd.DataFrame] = None
        self.elec_df: Optional[pd.DataFrame] = None
        self.erp_data: Optional[pd.DataFrame] = None

        self.erp_search_pool: List[Dict[str, Any]] = []
        self.unified_bom_pool: List[Dict[str, Any]] = []
        self.match_results: List[Dict[str, Any]] = []

        # 以下变量将通过 nicegui 的 .bind_xxxx_from() 机制与UI元素双向绑定
        # 当在代码中修改这些变量的值时，前端浏览器UI会自动更新，无需手动刷新
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
        # ui.column / ui.row: nicegui 的基础布局组件，对应前端的 flex-col / flex-row
        # .classes(): 接收 Tailwind CSS 框架的工具类名，用于快速定义样式（如 w-full 宽100%, p-4 内边距等）
        with ui.column().classes(
            "w-full min-h-screen bg-slate-50 absolute inset-0 max-w-screen-2xl mx-auto p-4 md:p-6 lg:p-8 overflow-y-auto"
        ):
            with ui.row().classes("w-full justify-between items-center mb-6"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("inventory_2", size="md").classes("text-blue-600")
                    ui.label("智能物料清单请购匹配系统").classes("text-2xl font-bold text-gray-800")
                # .props(): 透传 Quasar 框架 (nicegui 的底层Vue组件库) 的属性，如 outline, color, icon 等
                ui.button("退出工具", on_click=parent_dialog.close).props("outline color=negative icon=close size=md")

            # ⚙️ 全局等效词库配置 (ui.expansion 为折叠面板组件)
            with ui.expansion("⚙️ 全局等效词库配置 (双向自动容错)", icon="translate").classes(
                "w-full mb-6 bg-white shadow-sm border rounded-lg"
            ):
                with ui.row().classes("w-full items-center gap-4 p-4"):
                    source_input = ui.input("词汇 A (如: 螺丝)").props("dense outlined")
                    target_input = ui.input("词汇 B (如: 螺钉)").props("dense outlined")

                    async def add_syn():
                        s, t = source_input.value, target_input.value
                        if s and t:
                            s, t = s.strip().upper(), t.strip().upper()
                            if s != t:
                                syns = db_storage.get_deep_item(["bom_synonyms"], {})
                                syns[s] = t
                                await db_storage.atomic_deep_update(["bom_synonyms"], lambda _: syns)
                                self.render_synonyms_list.refresh()  # 刷新被 @ui.refreshable 装饰的局部UI
                                source_input.value = ""
                                target_input.value = ""
                                ui.notify(f"已添加无向等效规则: {s} ⇋ {t}", type="positive")  # 弹出右下角提示框

                    ui.button("添加等效规则", on_click=add_syn).props("color=primary size=sm")

                self.render_synonyms_list()  # type: ignore

            # bind_visibility_from: 只要 self.show_upload 变为 False，这个卡片就会立刻在界面上消失
            with (
                ui.card().bind_visibility_from(self, "show_upload").classes("w-full p-6 mb-6 bg-white shadow-sm border")
            ):
                ui.label("第一步：载入核心数据源 (任一物料清单 + 库存数据即可)").classes(
                    "text-lg font-bold mb-4 text-gray-700"
                )

                with ui.row().classes("w-full grid grid-cols-1 md:grid-cols-3 gap-4"):
                    with ui.column().classes("border rounded-lg p-4 bg-gray-50 hover:bg-gray-100 transition-colors"):
                        ui.label("1. 非电子物料清单").classes("font-bold text-sm")
                        # ui.upload: 文件上传组件。on_upload事件触发时，调用_handle_upload处理文件数据
                        ui.upload(on_upload=lambda e: self._handle_upload(e, "non_elec"), auto_upload=True).classes(
                            "w-full"
                        ).props('accept=".csv, .xlsx, .xlsm" max-files="1" flat')
                        ui.label().bind_text_from(self, "status_non_elec").classes(
                            "text-xs text-blue-600 font-bold mt-2"
                        )

                    with ui.column().classes("border rounded-lg p-4 bg-gray-50 hover:bg-gray-100 transition-colors"):
                        ui.label("2. 电子物料清单").classes("font-bold text-sm")
                        ui.upload(on_upload=lambda e: self._handle_upload(e, "elec"), auto_upload=True).classes(
                            "w-full"
                        ).props('accept=".csv, .xlsx" max-files="1" flat')
                        ui.label().bind_text_from(self, "status_elec").classes("text-xs text-blue-600 font-bold mt-2")

                    with ui.column().classes("border rounded-lg p-4 bg-gray-50 hover:bg-gray-100 transition-colors"):
                        ui.label("3. 企业库存数据表").classes("font-bold text-sm")
                        ui.upload(on_upload=lambda e: self._handle_upload(e, "erp"), auto_upload=True).classes(
                            "w-full"
                        ).props('accept=".csv, .xlsx" max-files="1" flat')
                        ui.label().bind_text_from(self, "status_erp").classes("text-xs text-blue-600 font-bold mt-2")

                with ui.row().classes("w-full justify-end mt-6"):
                    # bind_enabled_from: 根据 can_start 的布尔值决定按钮是否可以点击（置灰禁用防呆）
                    ui.button("开始合并与智能匹配", on_click=self._process_and_match).bind_enabled_from(
                        self, "can_start"
                    ).props("color=primary icon=play_arrow size=md")

            with (
                ui.column()
                .bind_visibility_from(self, "is_calculating")
                .classes("w-full items-center justify-center py-20 gap-4")
            ):
                ui.spinner("cube", size="4em", color="primary")  # 展示加载动画
                ui.label("后台正在执行漏斗过滤与跨列聚合寻优...").classes(
                    "text-xl font-bold text-gray-600 animate-pulse"
                )
                # ui.linear_progress: 进度条，0.0 到 1.0 的范围
                ui.linear_progress(value=0.0).bind_value_from(self, "calc_progress").classes("w-1/2 mt-4").props(
                    "rounded size=20px color=blue-400"
                )
                ui.label().bind_text_from(self, "calc_progress_text").classes("text-sm text-gray-500 font-mono")

            with ui.row().bind_visibility_from(self, "show_result").classes("w-full justify-between items-end mb-4"):
                ui.label().bind_text_from(self, "summary_text").classes("text-base text-gray-700 font-bold")

                with ui.row().classes("items-center gap-4"):
                    ui.button("导出请购单", on_click=self._export_excel).bind_enabled_from(self, "can_export").props(
                        "color=positive icon=file_download"
                    )
                    ui.button("重新上传数据", on_click=self._reset_and_show_upload).props(
                        "flat color=primary icon=refresh"
                    )

            self.render_result_grid()  # type: ignore

    @ui.refreshable  # 这个装饰器极大地提升了性能，只有调用 .refresh() 时，此函数包裹的UI才会被重新销毁重建
    def render_synonyms_list(self):
        syns = db_storage.get_deep_item(["bom_synonyms"], {})
        with ui.scroll_area().classes("w-full max-h-32 border-t p-4 bg-gray-50"):
            if not syns:
                ui.label("暂无等效词规则。添加后系统会自动生成双向容错特征，并采用得分最高的方案匹配。").classes(
                    "text-gray-400 text-xs"
                )
            else:
                with ui.row().classes("gap-2"):
                    for k, v in syns.items():

                        async def remove_syn(e, key=k):
                            if not e.value:  # chip的关闭事件触发时 e.value 为 False
                                curr_syns = db_storage.get_deep_item(["bom_synonyms"], {})
                                if key in curr_syns:
                                    del curr_syns[key]
                                    await db_storage.atomic_deep_update(["bom_synonyms"], lambda _: curr_syns)
                                    self.render_synonyms_list.refresh()

                        # ui.chip: 胶囊标签组件，常用于展示关键词或标签
                        ui.chip(f"{k} ⇋ {v}", removable=True, on_value_change=remove_syn).props(
                            "color=primary outline size=sm"
                        )

    def _fission_text(self, text: str) -> List[str]:
        """等效词多维裂变引擎 (Synonym Fission)"""
        if not text:
            return [text]
        syns = db_storage.get_deep_item(["bom_synonyms"], {})
        if not syns:
            return [text]

        # 1. 构建双向映射规则，实现图的无向连通
        bidirectional_rules = []
        for k, v in syns.items():
            bidirectional_rules.append((k, v))
            bidirectional_rules.append((v, k))

        # 2. 广度优先搜索 (BFS) 穷举所有变体
        variants = {text}
        queue = [text]
        max_limit = 8  # 限制裂变上限，防止因特殊配置陷入死循环或指数爆炸

        while queue and len(variants) < max_limit:
            # pop(0): 从队列头部取出一个元素进行处理，保证了BFS的层级遍历特性
            curr = queue.pop(0)
            for k, v in bidirectional_rules:
                if k in curr:
                    new_text = curr.replace(k, v)
                    if new_text not in variants:
                        # 只有当这个变体是新出现的，且不超过总数限制时，才加入集合和队列继续进行后续的裂变尝试
                        variants.add(new_text)
                        # 只有当这个变体是新出现的，且不超过总数限制时，才加入队列继续进行后续的裂变尝试
                        queue.append(new_text)

        return list(variants)

    @staticmethod
    def _clean_str_display(text: Any) -> str:
        """为UI显示而清洗（主要将各类乱七八糟的符号替换为空格）"""
        # re.sub: 正则替换。将匹配到 SEPARATORS_REGEX 的字符串全替换为一个空格 " "
        return re.sub(SEPARATORS_REGEX, " ", str(text).upper()).strip()

    @staticmethod
    def _clean_str_calc(text: str) -> str:
        """为底层算法计算而进行的深度清洗（强制归一化）"""
        # 1. 统一乘号规范：使用(?<=\d)等零宽断言(lookaround)，仅当 x/X 夹在两个数字中间时，才把它当作乘号替换为 *
        clean_text = re.sub(r"(?<=\d)\s*[X×x]\s*(?=\d)", "*", text, flags=re.IGNORECASE)
        # 2. 终极消除冗余精度：如 75.0KR 变 75KR, 1.0% 变 1%, 5.0 变 5
        clean_text = re.sub(r"(?<=\d)\.0+(?=[A-Za-z%_]|\b)", "", clean_text)
        # 3. 封装清洗：利用捕获组 \1，不管前后带什么字母（如 0603R，1206C，甚至 C0805R），统统斩断，只保留核心数字 (0603)
        clean_text = re.sub(
            r"[A-Za-z]*(0201|0402|0603|0805|1206|1210|2010|2512)[A-Za-z]*", r"\1", clean_text, flags=re.IGNORECASE
        )
        return clean_text

    def _smart_read_dataframe(self, buffer: io.BytesIO, filename: str) -> pd.DataFrame:
        """
        利用 Pandas 智能读取表格。
        由于现实中的表格经常有各类表头（Title）、说明行，数据并不一定从第一行开始。
        此函数会向下探测最多30行，寻找包含指定目标关键字最多的一行，认定为真实表头。
        """
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
            # iloc[i]: pandas 用于基于行号/列号的纯整数位置索引提取数据
            for i in range(min(30, len(df_raw))):
                # pd.notna(): pandas 检查值是否不是空值 (如 NaN)
                row_values = [str(val) for val in df_raw.iloc[i].values if pd.notna(val)]
                score = sum(1 for word in target_keywords if any(word.lower() in val.lower() for val in row_values))
                if score > max_score:
                    max_score, header_row_idx = score, i
                if max_score >= 3:  # 如果这一行命中了至少3个关键字，基本可以断定这就是表头，直接跳出循环
                    break

            if max_score > 0:
                # 重新定义列名，遇到空列名则塞入 Unnamed_j 防呆
                new_columns = [
                    str(val).strip() if pd.notna(val) else f"Unnamed_{j}"
                    for j, val in enumerate(df_raw.iloc[header_row_idx])
                ]
                df_clean = df_raw.iloc[header_row_idx + 1 :].copy()  # 截取表头之后的所有行作为真实数据
                df_clean.columns = pd.Index(new_columns)

                # 剔除全部是空白或无效值（如0, nan）的幽灵空行
                def is_ghost_row(row):
                    for val in row.values:
                        if pd.notna(val) and str(val).strip().lower() not in ["", "0", "0.0", "nan", "none"]:
                            return False
                    return True

                # df.apply(..., axis=1): 对DataFrame的每一行执行指定函数；~ 是按位取反（保留非幽灵行）
                return df_clean[~df_clean.apply(is_ghost_row, axis=1)].reset_index(drop=True), max_score
            return df_raw, 0

        buffer.seek(0)  # 将文件指针拨回流的开头，防止后续读取读不到东西
        if filename.lower().endswith(".csv"):
            try:
                # pd.read_csv: 默认按UTF-8读取CSV，header=None表示暂不解析表头，全当数据读
                df_raw = pd.read_csv(buffer, header=None, encoding="utf-8")
            except UnicodeDecodeError:
                buffer.seek(0)
                # 遇到中文Windows系统的老CSV，捕获报错并退回到 gbk 编码读取
                df_raw = pd.read_csv(buffer, header=None, encoding="gbk")
            df_clean, _ = process_df(df_raw)
            return df_clean
        else:
            # pd.read_excel: sheet_name=None 会一次性读取Excel里所有的Sheet，返回字典 {sheet_name: df}
            sheets_dict = pd.read_excel(buffer, sheet_name=None, header=None)
            best_df, global_max_score = None, -1
            for sheet_name, df_raw in sheets_dict.items():
                if df_raw.empty:
                    continue
                df_clean, score = process_df(df_raw)
                # 在所有Sheet里，挑一个得分最高、行数最多的Sheet作为最终使用的表格
                if score > global_max_score or (
                    score == global_max_score and best_df is not None and len(df_clean) > len(best_df)
                ):
                    global_max_score, best_df = score, df_clean
            if best_df is not None:
                return best_df
            buffer.seek(0)
            return pd.read_excel(buffer)

    def _check_ready(self):
        # 只要存在一份BOM表且存在ERP表，就能启动计算
        self.can_start = (self.non_elec_df is not None or self.elec_df is not None) and self.erp_data is not None

    def _check_export_status(self):
        # 遍历判定：必须每一条需求物料都被标记为 "直接请购"(忽略库存) 或 匹配状态=2(已选定映射) 才能导出
        self.can_export = bool(self.match_results) and all(
            r.get("is_direct", False) or r.get("status", 0) == 2 for r in self.match_results
        )

    def _export_excel(self):
        """利用 Pandas 生成导出所需的 Excel 表结构及文件流"""
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
        df = pd.DataFrame(export_data)  # 将字典列表转化为 Pandas 二维表

        # 为了让导出的Excel看起来整洁，强制排序：🔴需请购 在最前，⚪在中间，🟢满足在最后
        df["_sort_status"] = df["状态"].map({"🔴 需请购": 0, "⚪ 直接请购": 1, "🟢 库存满足": 2})
        # sort_values: 按照多列优先级排序；drop: 排完序后把这列内部辅助排序字段丢弃
        df = df.sort_values(by=["_sort_status", "商品分类", "映射企业品号"]).drop(columns=["_sort_status"])

        output = io.BytesIO()
        df.to_excel(
            output, index=False, sheet_name="智能核算请购单"
        )  # 将Pandas表写入内存字节流，index=False去掉丑陋的自带行号
        output.seek(0)

        # nicegui 内置文件下载触发器
        ui.download(output.getvalue(), filename=f"智能物料请购单_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx")
        ui.notify("请购单导出成功！", type="positive")

    async def _handle_upload(self, e: Any, target: str):
        """异步处理前端文件上传事件，防止大型表格上传卡死UI主线程"""
        try:
            # 兼容处理：检查返回的是否为协程(coroutine)，如果是则 await 获取实际的二进制流数据
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
        """状态机复位函数，用于清理内存并返回上传界面"""
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
        """高容错的数字提取器：能处理带逗号的千分位（如1,000）、带中文等夹杂杂质的数据"""
        try:
            # 抓取包含可能的负号、逗号和浮点数的数字串，替换逗号后强转float
            match = re.search(r"-?[\d,]+\.?\d*", str(val).strip())
            return float(match.group(0).replace(",", "")) if match else default
        except Exception:
            return default

    def _find_col(self, columns, keywords, excludes=None) -> Optional[str]:
        """模糊列名匹配器：遍历DataFrame的所有列头，只要包含keywords里面的任何词，就被征用"""
        for col in columns:
            col_str = str(col).lower()
            # 比如排除了 "原理图"，以防把原理图封装误当成实际封装
            if excludes and any(ex.lower() in col_str for ex in excludes):
                continue
            if any(k.lower() in col_str for k in keywords):
                return str(col)
        return None

    def _get_val(self, row, col_name, default=""):
        # 安全的数据提取，防止取到 pandas 中的缺失值 (pd.notna) 时程序崩溃
        return str(row[col_name]).strip() if col_name and col_name in row and pd.notna(row[col_name]) else default

    def _build_erp_pool(self, df: pd.DataFrame):
        """构建ERP特征向量化预编译检索池：提前将ERP数据清洗、正则分词，避免在比对循环中重复做这些耗时操作"""
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

        # to_dict("records"): 将DataFrame按行转换成一个个独立的字典对象构成的列表，极大提高遍历速度
        for row in df.to_dict(orient="records"):
            code, name, spec = self._get_val(row, code_col), self._get_val(row, name_col), self._get_val(row, spec_col)
            if not name and not spec:
                continue
            # 双重清洗：先做一轮为显示优化的清洗（保留更多细节），再做一轮为计算优化的清洗（极限归一化）
            erp_name_orig = self._clean_str_calc(self._clean_str_display(name))
            erp_spec_orig = self._clean_str_calc(self._clean_str_display(spec))
            # 直接基于原版文本提取 Token
            e_tokens_orig = set(TOKEN_PATTERN.findall(f"{erp_name_orig} {erp_spec_orig}"))

            display_desc = " ".join(filter(None, [self._clean_str_display(name), self._clean_str_display(spec)]))
            stock = self._safe_float(row.get(stock_col) if stock_col else 0.0)

            # 剔除掉原先冗长的 e_n_syn 等所有包含 syn 的键值对
            self.erp_search_pool.append(
                {
                    "code": code,
                    "erp_name_orig": erp_name_orig,
                    "erp_spec_orig": erp_spec_orig,
                    "e_f_orig": "",
                    "e_num_orig": {t for t in e_tokens_orig if NUM_PATTERN.search(t)},
                    "e_zh_orig": {t for t in e_tokens_orig if ZH_PATTERN.search(t)},
                    "search_str": display_desc,
                    "display": f"[{code}] {display_desc} (库存:{stock})",
                    "stock": stock,
                    "category": self._get_val(row, cat_col, "未分类"),
                    "unit": self._get_val(row, unit_col, "PCS"),
                }
            )

    def _extract_demands(self, df: pd.DataFrame, source_name: str) -> List[Dict]:
        """从BOM表中提取真实需求信息，做一些定制化业务处理（如PCB特征截留）"""
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

            # 如果用户传的是电子BOM，但系统在规格里嗅探到了“绿油白字”等PCB特征
            if source_name == "电子物料清单" and PCB_PATTERN.search(spec):
                # 尝试抓取位号（PCB名字可能写在位号里）
                pcb_name = self._get_val(row, desig_col) or self._get_val(row, comment_col) or name
                if pcb_name:
                    name = pcb_name
                # 动态降维：强制将该行按非电子BOM的权重进行核算（弱化封装权重，看重名字）
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
            # 业务定制：如果是电子BOM但又有PCB特征，则不论数量列怎么填，都默认当作1来处理，确保至少能匹配上库存里的一行PCB数据，避免被漏掉
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
        """
        最核心的打分引擎引擎：基础分生成 与 严苛特征防漏惩罚。
        """

        def pair_sim(s1: str, s2: str) -> float:
            if not s1 and not s2:
                return 100.0
            if not s1 or not s2:
                return 0.0

            # 【第一重保险】：无视空格的整体回退判定
            # difflib.SequenceMatcher: 基于Gestalt模式匹配算法。
            # 这里先将两边字符串剔除空格后，计算最长连续匹配片段的长度占总长度的百分比
            s1_nospace = s1.replace(" ", "")
            s2_nospace = s2.replace(" ", "")
            holistic_score = difflib.SequenceMatcher(None, s1_nospace, s2_nospace).ratio() * 100.0

            # 【第二重保险】：单向词元覆盖寻优
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
                # 用BOM词元的长度作为权重(weight)进行加权，越长的关键参数匹配上了贡献越大
                total_score += max_score * weight

            token_score = (total_score / total_weight) * 100.0 if total_weight > 0 else 0.0

            return max(holistic_score, token_score)

        # 数据源策略分流：不同类型的物料，关注维度和权重截然不同
        if source == "非电子物料清单":
            w_n, w_s, w_f = 0.7, 0.3, 0.0
            # 动态权重补偿：缺啥补啥，保持算力100%投入
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

        # 【核心优化】：跨列聚合寻优
        # 不再纠结于ERP填的列是不是对齐的，直接把ERP的名称、规格、封装揉成一团给BOM各列去撞击匹配
        e_comb = f"{e_n} {e_s} {e_f}".strip()

        final_name_score = pair_sim(b_n, e_comb) if w_n > 0 else 0.0
        final_spec_score = pair_sim(b_s, e_comb) if w_s > 0 else 0.0
        final_footprint_score = pair_sim(b_f, e_comb) if w_f > 0 else 0.0

        weighted_score = final_name_score * w_n + final_spec_score * w_s + final_footprint_score * w_f

        # 【核心优化】：全局聚合无视空格回退 (Global Holistic Fallback)
        b_comb_nospace = f"{b_n}{b_s}{b_f}".replace(" ", "")
        e_comb_nospace = e_comb.replace(" ", "")
        global_holistic_score = difflib.SequenceMatcher(None, b_comb_nospace, e_comb_nospace).ratio() * 100.0

        # 取各列加权交叉比对分 与 暴力去空聚合兜底分 的最高值，完美解决分列与合并填写的差异导致被降维惩罚
        base_score = max(weighted_score, global_holistic_score)

        # 【严苛的防呆拦截机制】：只要BOM里的数字特征在ERP池子里没找到对应，直接触发一票否决式的降分乘数
        if b_num:
            matched_nums = 0
            for nb in b_num:
                matched = False
                for ne in e_num:
                    if nb == ne:
                        matched = True
                        break

                    # 精度符号绝对隔离：如果词元含 % 必须完全相等（杜绝提取出 1%，却错误命中了 ERP中的 1/8W 里的1）
                    if "%" in nb or "%" in ne:
                        continue

                    # 数字纯净度校验：如果发生包含（如 22 包含在 220 里面），剥除公共部分后，残余的字符串里绝不能含有数字！
                    # 即拦截用 22 去误匹配 220，但允许用 22R 去匹配 22 (R不含数字)
                    if nb in ne and not any(c.isdigit() for c in ne.replace(nb, "", 1)):
                        matched = True
                        break
                    if ne in nb and not any(c.isdigit() for c in nb.replace(ne, "", 1)):
                        matched = True
                        break

                if matched:
                    matched_nums += 1

            num_ratio = matched_nums / len(b_num)
            # 一旦没找到关键数字，直接归0（触发底线拦截）；或者平方衰减分数值（大幅拉低至黄灯区）
            if num_ratio == 0:
                return {"score": 0.0, "w_n": w_n, "w_s": w_s, "w_f": w_f}
            base_score *= num_ratio**2

        return {"score": base_score, "w_n": w_n, "w_s": w_s, "w_f": w_f}

    def _match_single_item(
        self, bom_code: str, bom_name: str, bom_spec: str, bom_footprint: str, bom_qty: float, source: str
    ) -> Dict[str, Any]:
        bom_name_ui, bom_spec_ui, bom_footprint_ui = (
            self._clean_str_display(bom_name),
            self._clean_str_display(bom_spec),
            self._clean_str_display(bom_footprint),
        )
        # UI 展示的物料描述：直接拼接清洗后非空的名称、规格、封装，保持最丰富的细节供用户参考
        bom_desc_display = " ".join(filter(None, [bom_name_ui, bom_spec_ui, bom_footprint_ui]))

        bom_name_orig, bom_spec_orig, bom_footprint_orig = (
            self._clean_str_calc(bom_name_ui),
            self._clean_str_calc(bom_spec_ui),
            self._clean_str_calc(bom_footprint_ui),
        )

        # 1. 外部生成 BOM 端的裂变组合矩阵
        bom_name_variants = self._fission_text(bom_name_orig)
        bom_spec_variants = self._fission_text(bom_spec_orig)
        bom_footprint_variants = self._fission_text(bom_footprint_orig)
        # 2. 内部生成 BOM 端的变体池：基于上述裂变组合矩阵，构造一个包含所有可能变体的列表
        # （每个变体都保留原版的数字和中文特征集，避免在后续比对中重复计算）
        bom_variant_matrix = []
        for vn in bom_name_variants:
            for vs in bom_spec_variants:
                for vf in bom_footprint_variants:
                    if vn == bom_name_orig and vs == bom_spec_orig and vf == bom_footprint_orig:
                        continue  # 原版已经提取过了，无需放入变体池

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

        # 为 UI 展示和基础计算准备 Token
        text_for_tokens_ui = (
            f"{bom_name_ui} {bom_spec_ui}" if source == "非电子物料清单" else f"{bom_spec_ui} {bom_footprint_ui}"
        )
        text_for_tokens_orig = (
            f"{bom_name_orig} {bom_spec_orig}"
            if source == "非电子物料清单"
            else f"{bom_spec_orig} {bom_footprint_orig}"
        )

        ui_tokens_all = set(TOKEN_PATTERN.findall(text_for_tokens_ui))
        ui_num_tokens = {t for t in ui_tokens_all if NUM_PATTERN.search(t)}
        ui_zh_tokens = {t for t in ui_tokens_all if ZH_PATTERN.search(t)}

        orig_tokens_all = set(TOKEN_PATTERN.findall(text_for_tokens_orig))
        orig_num = {t for t in orig_tokens_all if NUM_PATTERN.search(t)}
        orig_zh = {t for t in orig_tokens_all if ZH_PATTERN.search(t)}

        # 前缀索引预过滤 (启发式剪枝加速机制)
        filtered_erp_pool = []
        bom_code_str = str(bom_code).strip()
        # 电子料直接在 ERP 料号 101 开头的池子里找，非电子料如果有传料号，也必须开头吻合才算，极大节省比对性能
        for erp_item in self.erp_search_pool:
            e_code = str(erp_item["code"]).strip()
            if source == "电子物料清单":
                if not e_code.startswith("101"):
                    continue  # 电子料直接在 ERP 料号 101 开头的池子里找，节省 90% 性能
            else:
                # 非电子料，如果有传料号，也必须开头吻合才算
                if bom_code_str and not e_code.startswith(bom_code_str):
                    continue
            filtered_erp_pool.append(erp_item)

        history_dict = db_storage.get_deep_item(["bom_erp_mapping", bom_desc_display], {})
        history_list = [
            {"erp_code": k, "hit_count": v.get("hit_count", 0)} for k, v in history_dict.items() if isinstance(v, dict)
        ]
        # 提取历史匹配次数最多的 ERP Code 作为强记忆参考
        top_history_code = (
            sorted(history_list, key=lambda x: x["hit_count"], reverse=True)[0]["erp_code"] if history_list else None
        )

        best_score, best_match, candidates = 0.0, None, []
        debug_weights = {"w_n": 0.0, "w_s": 0.0, "w_f": 0.0}

        # 核心比对循环开始
        for erp_item in filtered_erp_pool:
            # 第一轨：原汁原味比对（基准分）
            res_orig = self._calculate_similarity(
                bom_name_orig,
                bom_spec_orig,
                bom_footprint_orig,
                erp_item["e_n_orig"],
                erp_item["e_s_orig"],
                erp_item["e_f_orig"],
                orig_num,
                orig_zh,
                erp_item["e_num_orig"],
                erp_item["e_zh_orig"],
                source,
            )

            best_res = res_orig
            used_syn = False

            # 第二轨：裂变矩阵降维打击 (仅当原版分未达绿灯且存在有效变体时才激活计算)
            if best_res["score"] < self.SCORE_GREEN and bom_variant_matrix:
                for var in bom_variant_matrix:
                    temp_res = self._calculate_similarity(
                        var["n"],
                        var["s"],
                        var["f"],
                        erp_item["e_n_orig"],
                        erp_item["e_s_orig"],
                        erp_item["e_f_orig"],
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
                            break  # 触线熔断机制：只要有一条变体过了绿灯线，立刻停止遍历，极致节约性能

            score = best_res["score"]
            debug_weights = {"w_n": best_res["w_n"], "w_s": best_res["w_s"], "w_f": best_res["w_f"]}
            # 历史记忆加分：如果ERP项正好是历史上该BOM描述匹配过的那个ERP Code，则给予分数提升，增加绿灯概率
            is_history = top_history_code in (erp_item["code"], erp_item["search_str"])
            if is_history:
                score += self.SCORE_MEMORY_BOOST

            if score >= self.SCORE_MIN:
                candidates.append(
                    {**erp_item, "score": min(score, 100.0), "is_history": is_history, "used_syn": used_syn}
                )

        candidates.sort(key=lambda x: x["score"], reverse=True)  # 结果倒序排列，分数最高排第一
        is_memorized, is_synonym_boosted = False, False
        # 如果有候选项，取分数最高的那个作为最佳匹配，并提取它的历史记忆和同义词加成状态，供后续评级系统使用
        if candidates:
            best_match, best_score = candidates[0], candidates[0]["score"]
            is_memorized, is_synonym_boosted = best_match.get("is_history", False), best_match.get("used_syn", False)

        # 【评级系统】：只有分数大于 85、且不能和第二名侯选项分差太近（冲突防呆），才能自动确认为2（绿灯）。
        status = (
            2
            if best_score >= self.SCORE_GREEN
            and (len(candidates) == 1 or best_score - candidates[1]["score"] >= self.SCORE_CONFLICT or is_memorized)
            else (1 if best_score >= self.SCORE_YELLOW else 0)
        )
        is_always_ignored = db_storage.get_deep_item(["bom_erp_ignored", bom_desc_display], False)

        return {
            "source": source,  # 是电子BOM还是非电子BOM，影响后续的展示和核算逻辑
            "bom_code": bom_code_str,  # BOM里提取的料号，作为重要的启发式索引特征
            "bom_name": bom_name,  # BOM里提取的原始名称，供UI展示用
            "bom_spec": bom_spec,  # BOM里提取的原始规格，供UI展示用
            "bom_footprint": bom_footprint,  # BOM里提取的原始封装，供UI展示用
            "bom_desc": bom_desc_display,  # BOM里提取的综合描述（名称+规格+封装），供UI展示用
            "bom_qty": bom_qty,  # BOM里提取的数量，供核算用
            "ui_num_tokens": list(ui_num_tokens),  # 供UI展示用的数字特征列表，帮助用户快速定位关键参数
            "ui_zh_tokens": list(ui_zh_tokens),  # 供UI展示用的中文特征列表，帮助用户快速定位关键参数
            "ui_footprint_str": bom_footprint_ui,  # 供UI展示用的封装字符串，电子料用户尤其关心
            "debug_weights": debug_weights,  # 供调试用的权重分布，帮助开发者理解最终得分是如何计算出来的
            "is_always_ignored": is_always_ignored,  # 供评级系统使用的永久忽略标记，如果用户之前标记过这个BOM描述无论如何都不匹配了，就直接在这里传递这个状态，后端和前端都能据此做出相应的处理（如自动降级为红灯，或者在UI上显示一个特殊的“已忽略”标签）
            "is_direct": is_always_ignored,  # 供UI展示用的直接映射标记，如果这个BOM描述被永久忽略了，那么在UI上直接显示为红灯且不允许用户修改（因为已经明确了这个物料不匹配任何ERP项了）
            "is_memorized": is_memorized,  # 供评级系统使用的历史记忆标记，如果这个匹配项正好是历史上该BOM描述匹配过的那个ERP Code，则为True，评级系统可以据此给予分数提升，增加绿灯概率
            "is_synonym_boosted": is_synonym_boosted,  # 供评级系统使用的同义词加成标记，如果这个匹配项是通过BOM变体矩阵中的某个变体才匹配上的，则为True，评级系统可以据此给予分数提升，增加绿灯概率
            "status": status,  # 最终的匹配状态：2=绿灯（自动确认），1=黄灯（需人工确认），0=红灯（未匹配）
            "best_match": best_match,  # 最佳匹配的ERP项的完整信息字典，供UI展示用
            "candidates": candidates[:5],  # 侯选池只扔出去前5个以节省前端渲染性能
        }

    async def _process_and_match(self):
        """核心线程池控制中心，利用多线程避免长时间密集计算阻塞了UI的响应"""
        if self.erp_data is None or (self.non_elec_df is None and self.elec_df is None):
            ui.notify("请至少上传一份物料清单数据与库存数据！", type="warning")
            return

        self.show_upload, self.show_result, self.is_calculating, self.calc_progress = False, False, True, 0.0
        self.calc_progress_text = "正在初始化与预编译检索池特征..."
        await asyncio.sleep(0.1)  # 强制释放一瞬间的线程执行权，让界面UI能够先渲染出旋转的 Spinner 加载动画

        self._build_erp_pool(self.erp_data)
        pool1 = self._extract_demands(self.non_elec_df, "非电子物料清单") if self.non_elec_df is not None else []
        pool2 = self._extract_demands(self.elec_df, "电子物料清单") if self.elec_df is not None else []

        merged_dict = {}
        # 将相同规格型号的物料在处理前直接进行归口聚合（数量相加），避免多次重复计算同样的特征
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
            self.render_result_grid.refresh()  # type: ignore
            return

        results, loop = [], asyncio.get_running_loop()  # 获取当前asyncio的事件循环
        for i, item in enumerate(self.unified_bom_pool):
            # loop.run_in_executor(None, func): 将CPU密集型的 _match_single_item 计算放入默认的 ThreadPoolExecutor（后台线程池）中跑
            # 这样就不会卡死负责网络和界面渲染的主协程事件循环
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
            # 每隔3条记录或者结束时，将当前进度回调更新给 UI 进度条
            if i % 3 == 0 or i == total_items - 1:
                self.calc_progress, self.calc_progress_text = (
                    (i + 1) / total_items,
                    f"后台多线程疾速核算：已完成 {i + 1} / {total_items} 项",
                )
                await asyncio.sleep(0.001)

        self.match_results = results
        gc, yc, rc = (sum(1 for r in results if r["status"] == s and not r.get("is_direct", False)) for s in (2, 1, 0))
        self.summary_text = (
            f"共清洗合并 {len(results)} 款物料 | 🟢 自动确认: {gc} | 🟡 需人工确认: {yc} | 🔴 未匹配: {rc}"
        )

        self.is_calculating, self.show_result = False, True
        self._check_export_status()
        self.render_result_grid.refresh()  # type: ignore

    @ui.refreshable
    def render_result_grid(self):
        """核心前端卡片列表的渲染，由 @ui.refreshable 动态托管生命周期"""
        if not self.match_results:
            with ui.column().classes("w-full items-center justify-center py-20 text-gray-400"):
                ui.icon("sentiment_dissatisfied", size="4em")
                ui.label("未能提取到有效数据").classes("mt-4 text-lg")
            return

        # grid-cols-1 md:grid-cols-2 lg:grid-cols-3... 是 Tailwind 的响应式栅格系统
        # 在手机(md前)上单列，在巨大屏幕(2xl)上可以显示5列
        with ui.row().classes(
            "w-full grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5 gap-4"
        ):
            for res in self.match_results:
                self._render_material_card(res)

    def _render_material_card(self, res: Dict[str, Any]):
        """生成单张物料的详情与控制UI卡片"""
        is_direct, is_always_ignored = res.get("is_direct", False), res.get("is_always_ignored", False)

        if is_always_ignored:
            status_class, icon_name, status_text = (
                "bg-gray-100 border-gray-300 opacity-80 grayscale",
                "do_not_disturb_on",
                "已永远忽略 (直接请购)",
            )
        elif is_direct:
            status_class, icon_name, status_text = (
                "bg-gray-100 border-gray-300 opacity-80 grayscale",
                "block",
                "本次已忽略 (直接请购)",
            )
        else:
            status_class = {
                0: "bg-red-50 border-red-300",
                1: "bg-yellow-50 border-yellow-300",
                2: "bg-green-50 border-green-300",
            }[res["status"]]
            icon_name = {0: "error", 1: "warning", 2: "check_circle"}[res["status"]]
            status_text = f"【{res['source']}】智能分析"

        with ui.card().classes(
            f"w-full border shadow-sm {status_class} hover:shadow-md transition-all duration-300 flex flex-col justify-between"
        ):
            with ui.column().classes("w-full flex-grow"):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(status_text).classes("text-xs text-gray-500 font-bold")
                    ui.icon(icon_name, color="black").classes("text-lg opacity-50")

                ui.label(res["bom_desc"]).classes(
                    "text-sm font-bold break-words line-clamp-2 mt-1"
                )  # line-clamp-2: 最多显示两行文字，超出则打省略号
                ui.label(f"核算需求总量: {res['bom_qty']}").classes("text-xs text-gray-600 mb-2")

                with ui.column().classes("w-full bg-gray-200/60 p-2 rounded gap-0 mb-2"):
                    ui.label("🛠️ 算法判定依据").classes("text-[10px] text-gray-500 font-bold")
                    dw = res.get("debug_weights", {"w_n": 0, "w_s": 0, "w_f": 0})

                    if res["source"] == "非电子物料清单":
                        ui.label(f"算力分配: 品名 {dw['w_n'] * 100:.0f}% / 规格 {dw['w_s'] * 100:.0f}%").classes(
                            "text-[10px] text-gray-700 font-bold"
                        )

                        n_toks = ", ".join(res.get("ui_num_tokens", [])) if res.get("ui_num_tokens") else "无"
                        z_toks = ", ".join(res.get("ui_zh_tokens", [])) if res.get("ui_zh_tokens") else "无"
                        ui.label(f"全局提取参数: [{n_toks}]").classes("text-[10px] text-blue-600 break-all")
                        ui.label(f"全局提取品名: [{z_toks}]").classes("text-[10px] text-purple-600 break-all")
                    else:
                        ui.label(f"算力分配: 规格 {dw['w_s'] * 100:.0f}% / 封装 {dw['w_f'] * 100:.0f}%").classes(
                            "text-[10px] text-gray-700 font-bold"
                        )

                        n_toks = ", ".join(res.get("ui_num_tokens", [])) if res.get("ui_num_tokens") else "无"
                        ui.label(f"全局提取参数: [{n_toks}]").classes("text-[10px] text-blue-600 break-all")

                        f_str = res.get("ui_footprint_str", "").strip()
                        if f_str:
                            ui.label(f"独立封装靶向: [{f_str}]").classes(
                                "text-[10px] text-teal-600 font-bold break-all"
                            )

                    if res.get("best_match"):
                        score_text = f"最高通过分数: {res['best_match']['score']:.1f}%" + (
                            " (🔄等效容错)" if res.get("is_synonym_boosted") else ""
                        )
                        score_color = (
                            "text-green-600"
                            if res["best_match"]["score"] >= self.SCORE_GREEN
                            else (
                                "text-yellow-600" if res["best_match"]["score"] >= self.SCORE_YELLOW else "text-red-500"
                            )
                        )
                        ui.label(score_text).classes(f"text-[10px] {score_color} font-bold mt-1")
                        ui.label(f"命中目标库: {res['best_match']['search_str']}").classes(
                            "text-[10px] text-gray-500 break-all"
                        )
                    else:
                        ui.label("最高通过分数: 0.0% (无合格项)").classes("text-[10px] text-red-500 font-bold mt-1")

                ui.separator()

            # ... 下方是针对不同状态 (0: 未找到, 1: 需要确认, 2: 自动匹配, is_direct: 忽略请购) 挂载的对应按钮回调函数 ...
            with ui.column().classes("w-full mt-auto"):
                if is_direct:
                    ui.label(
                        "该物料已被设置为永远忽略，默认不匹配库存。"
                        if is_always_ignored
                        else "无需匹配库存，本次将按需求总量全额请购。"
                    ).classes("text-xs text-gray-600 mt-2 font-bold")

                    async def toggle_off(r=res, always=is_always_ignored):
                        r["is_direct"] = False
                        if always:
                            r["is_always_ignored"] = False
                            await db_storage.atomic_deep_update(["bom_erp_ignored", r["bom_desc"]], lambda _: False)
                            ui.notify("已取消永远忽略，恢复智能匹配算法", type="info")
                        self._check_export_status()
                        self.render_result_grid.refresh()  # type: ignore

                    ui.button(
                        "取消永远忽略，恢复匹配" if is_always_ignored else "恢复智能匹配", on_click=toggle_off
                    ).classes("w-full mt-2").props("size=sm outline color=gray")
                else:
                    if res["status"] == 2:
                        match = res["best_match"]
                        ui.label("✅ 历史知识库匹配" if res.get("is_memorized") else "自动匹配结果").classes(
                            "text-xs text-green-700 mt-2 font-bold"
                        )
                        ui.label(match["display"]).classes("text-xs break-words")
                        self._render_procurement_advice(res["bom_qty"], match["stock"])

                        if res.get("is_memorized"):

                            async def on_unbind(r=res):
                                await db_storage.del_deep_item(
                                    ["bom_erp_mapping", r["bom_desc"], match["code"] or match["search_str"]]
                                )
                                ui.notify("已从知识库中移除该映射规则", type="info")
                                r.update(
                                    self._match_single_item(
                                        r.get("bom_code", ""),
                                        r["bom_name"],
                                        r["bom_spec"],
                                        r["bom_footprint"],
                                        r["bom_qty"],
                                        r["source"],
                                    )
                                )
                                self._check_export_status()
                                self.render_result_grid.refresh()  # type: ignore

                            ui.button("解除绑定并重算", on_click=on_unbind).classes("w-full mt-2").props(
                                "size=sm color=negative flat"
                            )

                    elif res["status"] == 1:
                        ui.label(f"推荐选项 (最高匹配 {len(res['candidates'])} 项需确认)").classes(
                            "text-xs text-yellow-700 mt-2 font-bold"
                        )
                        # ui.select: 下拉选择框组件
                        dropdown = (
                            ui.select({i: c["display"] for i, c in enumerate(res["candidates"])}, value=0)
                            .classes("w-full text-xs bg-white")
                            .props("dense options-dense outlined")
                        )

                        async def on_confirm(r=res, drop=dropdown, cands=res["candidates"]):
                            if drop.value is not None:
                                sel = cands[drop.value]
                                await self._update_memory_dict(r["bom_desc"], sel["code"] or sel["search_str"])
                                ui.notify("成功将规则写入中心知识库", type="positive")
                                r.update({"status": 2, "best_match": sel, "is_memorized": True})
                                self._check_export_status()
                                self.render_result_grid.refresh()  # type: ignore

                        ui.button("确认映射并记忆", on_click=on_confirm).classes("w-full mt-2").props(
                            "size=sm color=warning text-black outline"
                        )

                    elif res["status"] == 0:
                        ui.label("未找到精准匹配项").classes("text-xs text-red-700 mt-2 font-bold")
                        manual_input = (
                            ui.input("输入准确的企业品号").classes("w-full text-xs bg-white").props("dense outlined")
                        )

                        async def on_bind(r=res, inp=manual_input):
                            code = inp.value.strip()
                            if code:
                                # next(generator, None): 在迭代器中寻找符合条件的第一个项，找不到则返回None
                                erp_item = next((item for item in self.erp_search_pool if item["code"] == code), None)
                                if not erp_item:
                                    ui.notify("当前导入的库存表中未找到该品号！", type="negative")
                                    return
                                await self._update_memory_dict(r["bom_desc"], code)
                                ui.notify("强制绑定生效，规则已被系统学习", type="positive")
                                r.update({"status": 2, "best_match": erp_item, "is_memorized": True})
                                self._check_export_status()
                                self.render_result_grid.refresh()  # type: ignore

                        ui.button("绑定并记忆", on_click=on_bind).classes("w-full mt-2").props(
                            "size=sm color=negative outline"
                        )

                    with ui.row().classes("w-full mt-2 gap-2"):

                        def toggle_on(r=res):
                            r["is_direct"] = True
                            self._check_export_status()
                            self.render_result_grid.refresh()  # type: ignore

                        ui.button("本次请购忽略", on_click=toggle_on).classes("flex-1").props("size=sm flat color=gray")

                        async def toggle_always_ignore_on(r=res):
                            r.update({"is_always_ignored": True, "is_direct": True})
                            await db_storage.atomic_deep_update(["bom_erp_ignored", r["bom_desc"]], lambda _: True)
                            ui.notify("已将该物料加入永远忽略白名单", type="info")
                            self._check_export_status()
                            self.render_result_grid.refresh()  # type: ignore

                        ui.button("永远忽略", on_click=toggle_always_ignore_on).classes("flex-1").props(
                            "size=sm outline color=gray"
                        )

    async def _update_memory_dict(self, bom_desc: str, erp_code: str):
        """将成功映射的结构以热更新的方式写入到底层数据库中"""

        def increment_weight(current_val):
            if not isinstance(current_val, dict):
                current_val = {"hit_count": 0, "last_used": None}
            current_val["hit_count"] += 1
            # isoformat: 生成国际标准时间的文本表现形式 (如 2026-03-18T10:42:00)
            current_val["last_used"] = datetime.now().isoformat()
            return current_val

        await db_storage.atomic_deep_update(["bom_erp_mapping", bom_desc, erp_code], increment_weight)

    def _render_procurement_advice(self, need_qty: float, stock_qty: float):
        """渲染采购缺口意见"""
        if stock_qty >= need_qty:
            ui.label(f"结余: {stock_qty - need_qty} (无需请购)").classes("text-xs text-green-600 font-bold mt-1")
        else:
            ui.label(f"🚨 缺口: {need_qty - stock_qty} (建议采购)").classes(
                "text-xs text-red-600 font-bold mt-1 bg-red-100 px-1 rounded"
            )
