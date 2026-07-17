# -*- coding: utf-8 -*-
"""Zemax 优化操作数查询工具 UI。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nicegui import ui

from ..config import BASE_DIR
from .operand_data import (
    DEFAULT_SOURCE_URL,
    OperandDataError,
    load_operand_database,
    update_operand_database,
)

OPERAND_CACHE_PATH = Path(BASE_DIR) / "data" / "zemax_operands.json"

CATEGORY_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "key": "parameters",
        "label": "参数类",
        "icon": "data_object",
        "categories": (
            "Changing_System_Data",
            "Constraints_on_Lens_Data",
            "Constraints_on_Glass_Data",
            "Constraints_on_Element_Positions",
            "Constraints_on_Parameter_Data",
            "Constraints_on_TrueFreeForm_Surface_Data",
            "Thermal_Coefficient_of_Expansion_Data",
            "Multi_Configuration_Zoom_Data",
            "Constraints_on_Non_sequential_Object_Data",
        ),
    },
    {
        "key": "analysis",
        "label": "分析属性类",
        "icon": "analytics",
        "categories": (
            "First_Order_Optical_Properties",
            "Constraints_on_Lens_Properties",
            "Constraints_on_Paraxial_Ray_Data",
            "Constraints_on_Real_Ray_Data",
            "MTF_Data",
            "PSF_Strehl_Ratio_Data",
            "Foucault_Analysis_optimization_operands_by_category",
            "Aberrations_optimization_operands_by_category",
            "Ghost_Focus_Control",
            "Fiber_Coupling_Operands",
            "Relative_Illumination_Operand",
            "Encircled_Energy_optimization_operands_by_category",
            "Non_sequential_Ray_Tracing_and_Detector_Operands",
            "Constraints_on_Construction_Optics_for_Optically_Fabricated",
        ),
    },
    {
        "key": "specialized",
        "label": "专有计算类",
        "icon": "calculate",
        "categories": (
            "Gaussian_Beam_Data",
            "Gradient_Index_Control_Operands",
            "Constraints_on_Optical_Coatings_Polarization_Ray_Trace",
            "Physical_Optics_Propagation_POP_Results",
            "Best_Fit_Sphere_Data",
            "Tolerance_Sensitivity_Data",
        ),
    },
    {
        "key": "other",
        "label": "其他类",
        "icon": "tag",
        "categories": (
            "General_Math_Operands",
            "Merit_Function_Control_Operands",
            "Optimization_with_ZPL_Macros",
            "User_defined_operands_optimization_operands_by_category",
            "Obsolete_Operands",
        ),
    },
)

CATEGORY_LABELS: dict[str, tuple[str, str]] = {
    "Changing_System_Data": ("系统数据", "Changing System Data"),
    "Constraints_on_Lens_Data": ("镜头参数约束", "Constraints on Lens Data"),
    "Constraints_on_Glass_Data": ("玻璃数据约束", "Constraints on Glass Data"),
    "Constraints_on_Element_Positions": ("组件位置约束", "Constraints on Element Positions"),
    "Constraints_on_Parameter_Data": ("参数数据约束", "Constraints on Parameter Data"),
    "Constraints_on_TrueFreeForm_Surface_Data": (
        "TrueFreeForm表面数据约束",
        "Constraints on TrueFreeForm Surface Data",
    ),
    "Thermal_Coefficient_of_Expansion_Data": (
        "热膨胀系数数据",
        "Thermal Coefficient of Expansion Data",
    ),
    "Multi_Configuration_Zoom_Data": ("多重结构数据", "Multi-Configuration (Zoom) Data"),
    "Constraints_on_Non_sequential_Object_Data": (
        "非序列物体数据约束",
        "Constraints on Non-Sequential Object Data",
    ),
    "First_Order_Optical_Properties": ("一阶光学性能", "First-Order Optical Properties"),
    "Constraints_on_Lens_Properties": ("镜头属性约束", "Constraints on Lens Properties"),
    "Constraints_on_Paraxial_Ray_Data": ("近轴光线数据约束", "Constraints on Paraxial Ray Data"),
    "Constraints_on_Real_Ray_Data": ("实际光线数据约束", "Constraints on Real Ray Data"),
    "MTF_Data": ("MTF数据", "MTF Data"),
    "PSF_Strehl_Ratio_Data": ("点扩散函数/斯特列尔比数据", "PSF/Strehl Ratio Data"),
    "Foucault_Analysis_optimization_operands_by_category": ("傅科分析", "Foucault Analysis"),
    "Aberrations_optimization_operands_by_category": ("像差", "Aberrations"),
    "Ghost_Focus_Control": ("鬼像聚焦控制", "Ghost Focus Control"),
    "Fiber_Coupling_Operands": ("光纤耦合操作数", "Fiber Coupling Operands"),
    "Relative_Illumination_Operand": ("相对照度操作数", "Relative Illumination Operand"),
    "Encircled_Energy_optimization_operands_by_category": ("圈入能量", "Encircled Energy"),
    "Non_sequential_Ray_Tracing_and_Detector_Operands": (
        "非序列光线追迹和探测器",
        "Non-Sequential Ray Tracing and Detector Operands",
    ),
    "Constraints_on_Construction_Optics_for_Optically_Fabricated": (
        "光学制造全息图约束",
        "Constraints on Construction Optics for Optically Fabricated Holograms",
    ),
    "Gaussian_Beam_Data": ("高斯光束数据", "Gaussian Beam Data"),
    "Gradient_Index_Control_Operands": ("梯度折射率控制操作数", "Gradient Index Control Operands"),
    "Constraints_on_Optical_Coatings_Polarization_Ray_Trace": (
        "光学镀膜和偏振光线追迹",
        "Constraints on Optical Coatings, Polarization Ray Trace Data",
    ),
    "Physical_Optics_Propagation_POP_Results": (
        "物理光学传播（POP）结果",
        "Physical Optics Propagation (POP) Results",
    ),
    "Best_Fit_Sphere_Data": ("最佳拟合球面数据", "Best Fit Sphere Data"),
    "Tolerance_Sensitivity_Data": ("公差灵敏度数据", "Tolerance Sensitivity Data"),
    "General_Math_Operands": ("数学运算", "General Math Operands"),
    "Merit_Function_Control_Operands": ("控制评价函数操作数", "Merit Function Control Operands"),
    "Optimization_with_ZPL_Macros": ("宏（ZPL）优化操作数", "Optimization with ZPL Macros"),
    "User_defined_operands_optimization_operands_by_category": (
        "用户自定义操作数",
        "User Defined Operands",
    ),
    "Obsolete_Operands": ("废弃的操作数", "Obsolete Operands"),
}


class OperandLookupTool:
    """按光学课堂式层级浏览并全文搜索 OpticStudio 优化操作数。"""

    def __init__(self) -> None:
        self.data = load_operand_database(OPERAND_CACHE_PATH)
        self.source_url = str(self.data.get("source_url") or DEFAULT_SOURCE_URL)
        self.query = ""
        self.selected_group: str | None = None
        self.selected_category: str | None = None
        self.selected_operand: dict[str, Any] | None = None
        self.is_updating = False
        self.status_text = ""
        self.stats_text = ""

        self.search_input: Any = None
        self.source_input: Any = None
        self.update_button: Any = None
        self.update_dialog: Any = None
        self.content_scroll: Any = None
        self._select_initial_category()
        self._update_stats_text()

    def show(self, parent_dialog: ui.dialog) -> None:
        with ui.column().classes("absolute inset-0 w-full h-screen bg-white overflow-hidden gap-0"):
            self._render_top_bar(parent_dialog)
            with ui.row().classes("w-full flex-1 min-h-0 gap-0 flex-nowrap"):
                with ui.column().classes("h-full bg-white border-r border-slate-200 gap-0").style(
                    "width: 310px; min-width: 310px; display: flex;"
                ):
                    with ui.row().classes("w-full h-14 px-6 items-center border-b border-slate-100"):
                        ui.label("Zemax 操作数手册").classes("text-base font-bold text-slate-700")
                    with ui.scroll_area().classes("w-full flex-1"):
                        self.render_navigation()  # type: ignore

                with ui.column().classes("h-full gap-0 bg-white").style("flex: 1 1 auto; min-width: 0;"):
                    self.content_scroll = ui.scroll_area().classes("w-full flex-1")
                    with self.content_scroll:
                        self.render_content()  # type: ignore

        self._build_update_dialog()

    def _render_top_bar(self, parent_dialog: ui.dialog) -> None:
        with ui.row().classes(
            "w-full h-[68px] px-4 md:px-6 items-center gap-3 bg-white border-b border-slate-200 shrink-0 flex-nowrap"
        ).style("padding-right: 72px;"):
            with ui.row().classes("items-center gap-2 shrink-0"):
                with ui.element("div").classes(
                    "w-9 h-9 rounded-lg bg-blue-600 text-white flex items-center justify-center"
                ):
                    ui.icon("manage_search", size="23px")
                with ui.column().classes("gap-0"):
                    ui.label("操作数查询").classes("text-base font-bold text-slate-800")
                    ui.label().bind_text_from(self, "stats_text").classes("text-[11px] text-slate-400")

            self.search_input = (
                ui.input(placeholder="搜索操作数代码或说明…", on_change=self._on_search_change)
                .props("outlined dense clearable debounce=250")
                .classes("min-w-0")
                .style("flex: 1 1 auto; max-width: 48rem; min-width: 220px;")
            )
            self.search_input.add_slot("prepend", '<q-icon name="search" color="primary" />')

            ui.space()
            ui.button("更新资料", icon="cloud_sync", on_click=self._open_update_dialog).props(
                "flat color=primary no-caps"
            ).classes("shrink-0")
            ui.button(icon="close", on_click=parent_dialog.close).props("flat round color=grey-7").style(
                "position: fixed; top: 12px; right: 16px; z-index: 10000;"
            ).tooltip("退出工具")

    def _build_update_dialog(self) -> None:
        with ui.dialog() as self.update_dialog, ui.card().classes("w-[760px] max-w-[92vw] p-0 gap-0"):
            with ui.row().classes("w-full px-5 py-4 items-center justify-between border-b border-slate-200"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("cloud_sync", size="24px").classes("text-blue-600")
                    ui.label("更新 Ansys 操作数资料").classes("text-lg font-bold text-slate-800")
                ui.button(icon="close", on_click=self.update_dialog.close).props("flat round dense color=grey-7")

            with ui.column().classes("w-full p-5 gap-3"):
                self.source_input = (
                    ui.input("Ansys 操作数分类页网址", value=self.source_url)
                    .props("outlined clearable")
                    .classes("w-full")
                )
                ui.label(
                    "可直接粘贴登录跳转网址。系统会转换为官方公共内容地址，完整抓取成功后才覆盖本地缓存。"
                ).classes("text-xs leading-5 text-slate-500")
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    ui.label().bind_text_from(self, "status_text").classes("text-xs font-medium text-blue-600")
                    with ui.row().classes("items-center gap-2"):
                        ui.button("取消", on_click=self.update_dialog.close).props("flat color=grey-7 no-caps")
                        self.update_button = ui.button(
                            "一键更新资料", icon="download", on_click=self._update_data
                        ).props("color=primary unelevated no-caps")

    def _open_update_dialog(self) -> None:
        self.status_text = ""
        self.source_input.value = self.source_url
        self.update_dialog.open()

    def _select_initial_category(self) -> None:
        category_by_key = self._category_by_key()
        first_key = str(CATEGORY_GROUPS[0]["categories"][0])
        first = category_by_key.get(first_key)
        if first:
            self.selected_category = str(first.get("name", ""))
            self.selected_group = str(CATEGORY_GROUPS[0]["key"])
        else:
            categories = self.data.get("categories", [])
            self.selected_category = str(categories[0].get("name", "")) if categories else None
            self.selected_group = str(self._group_for_category(categories[0])["key"]) if categories else None
        self.selected_operand = None

    def _update_stats_text(self) -> None:
        category_count = int(self.data.get("category_count", 0) or 0)
        operand_count = int(self.data.get("operand_count", 0) or 0)
        self.stats_text = f"{category_count} 个分类 · {operand_count} 条说明"

    @staticmethod
    def _category_key(category: dict[str, Any]) -> str:
        return Path(urlparse(str(category.get("url", ""))).path).stem

    def _category_by_key(self) -> dict[str, dict[str, Any]]:
        return {self._category_key(category): category for category in self.data.get("categories", [])}

    @staticmethod
    def _category_title(category: dict[str, Any]) -> str:
        key = OperandLookupTool._category_key(category)
        if key in CATEGORY_LABELS:
            return CATEGORY_LABELS[key][0]
        name = str(category.get("name", "未命名分类"))
        return re.split(r"[（(]", name, maxsplit=1)[0].strip()

    @staticmethod
    def _category_english(category: dict[str, Any]) -> str:
        key = OperandLookupTool._category_key(category)
        if key in CATEGORY_LABELS:
            return CATEGORY_LABELS[key][1]
        name = str(category.get("index_name") or category.get("name", ""))
        match = re.search(r"[（(]([A-Z][\s\S]*)[）)]\s*$", name)
        if not match:
            return "Optimization Operands"
        value = match.group(1).strip()
        value = re.sub(r"^(?:分类优化操作数|分类优化操作数\)\()", "", value)
        return value.strip("()（） ")

    def _group_for_category(self, category: dict[str, Any]) -> dict[str, Any]:
        key = self._category_key(category)
        for group in CATEGORY_GROUPS:
            if key in group["categories"]:
                return group
        return CATEGORY_GROUPS[-1]

    @staticmethod
    def _group_by_key(group_key: str | None) -> dict[str, Any] | None:
        return next((group for group in CATEGORY_GROUPS if group["key"] == group_key), None)

    def _categories_for_group(self, group: dict[str, Any]) -> list[dict[str, Any]]:
        category_by_key = self._category_by_key()
        return [category_by_key[key] for key in group["categories"] if key in category_by_key]

    def _selected_category_data(self) -> dict[str, Any] | None:
        for category in self.data.get("categories", []):
            if category.get("name") == self.selected_category:
                return category
        return None

    def _make_category_handler(self, category_name: str):
        def select_category() -> None:
            self.selected_category = category_name
            self.selected_operand = None
            category = self._selected_category_data()
            self.selected_group = str(self._group_for_category(category)["key"]) if category else None
            self.query = ""
            if self.search_input is not None:
                self.search_input.value = ""
            self.render_navigation.refresh()
            self.render_content.refresh()
            self._scroll_to_top()

        return select_category

    def _make_group_handler(self, group_key: str):
        def select_group() -> None:
            self.selected_group = group_key
            self.selected_category = None
            self.selected_operand = None
            self.query = ""
            if self.search_input is not None:
                self.search_input.value = ""
            self.render_navigation.refresh()
            self.render_content.refresh()
            self._scroll_to_top()

        return select_group

    def _go_to_overview(self) -> None:
        self.selected_group = None
        self.selected_category = None
        self.selected_operand = None
        self.query = ""
        if self.search_input is not None:
            self.search_input.value = ""
        self.render_navigation.refresh()
        self.render_content.refresh()
        self._scroll_to_top()

    def _on_tree_select(self, event: Any) -> None:
        node_id = str(event.value or "")
        if node_id.startswith("group:"):
            self._make_group_handler(node_id.removeprefix("group:"))()
            return
        if node_id.startswith("category:"):
            category = self._category_by_key().get(node_id.removeprefix("category:"))
            if category:
                self._make_category_handler(str(category.get("name", "")))()

    def _make_operand_handler(self, item: dict[str, Any]):
        def select_operand() -> None:
            self.selected_operand = item
            self.render_content.refresh()
            self._scroll_to_top()

        return select_operand

    def _scroll_to_top(self) -> None:
        if self.content_scroll is not None:
            self.content_scroll.scroll_to(percent=0, duration=0.12)

    def _on_search_change(self, event: Any) -> None:
        self.query = str(event.value or "").strip()
        self.selected_operand = None
        self.render_content.refresh()
        self._scroll_to_top()

    async def _update_data(self) -> None:
        if self.is_updating:
            return
        requested_url = str(self.source_input.value or "").strip()
        self.is_updating = True
        self.status_text = "正在下载并解析全部分类，请稍候…"
        self.update_button.disable()
        self.update_button.props("loading")
        try:
            data = await asyncio.to_thread(update_operand_database, requested_url, OPERAND_CACHE_PATH)
            self.data = data
            self.source_url = str(data.get("source_url") or requested_url)
            self.source_input.value = self.source_url
            self._select_initial_category()
            self._update_stats_text()
            self.status_text = f"更新完成：{data['category_count']} 个分类，{data['operand_count']} 条说明"
            self.render_navigation.refresh()
            self.render_content.refresh()
            ui.notify("操作数说明资料已更新", type="positive")
        except OperandDataError as exc:
            self.status_text = "更新失败，原有资料未受影响"
            ui.notify(str(exc), type="negative", close_button="关闭", timeout=8000)
        except Exception as exc:  # noqa: BLE001 - UI 层统一转为用户可读错误
            self.status_text = "更新失败，原有资料未受影响"
            ui.notify(f"更新资料时发生错误：{exc}", type="negative", close_button="关闭", timeout=8000)
        finally:
            self.is_updating = False
            self.update_button.props(remove="loading")
            self.update_button.enable()

    @ui.refreshable
    def render_navigation(self) -> None:
        category_by_key = self._category_by_key()
        nodes: list[dict[str, Any]] = []
        expanded_nodes: list[str] = []
        selected_node: str | None = None
        for group in CATEGORY_GROUPS:
            group_key = str(group["key"])
            group_node_id = f"group:{group_key}"
            children = []
            for category_key in group["categories"]:
                category = category_by_key.get(category_key)
                if not category:
                    continue
                category_node_id = f"category:{category_key}"
                children.append(
                    {
                        "id": category_node_id,
                        "label": f"{self._category_title(category)}  ·  {len(category.get('operands', []))}",
                        "icon": "article",
                    }
                )
                if category.get("name") == self.selected_category and not self.query:
                    selected_node = category_node_id
                    expanded_nodes.append(group_node_id)
            if self.selected_group == group_key and self.selected_category is None and not self.query:
                selected_node = group_node_id
                expanded_nodes.append(group_node_id)
            nodes.append(
                {
                    "id": group_node_id,
                    "label": group["label"],
                    "icon": group["icon"],
                    "children": children,
                }
            )

        tree = (
            ui.tree(nodes, on_select=self._on_tree_select)
            .props("dense no-connectors selected-color=primary")
            .classes("w-full px-2 py-3 text-sm text-slate-600")
        )
        if expanded_nodes:
            tree.expand(list(dict.fromkeys(expanded_nodes)))
        if selected_node:
            tree.select(selected_node)

    def _filtered_operands(self) -> list[dict[str, Any]]:
        query = self.query.casefold()
        results: list[dict[str, Any]] = []
        for category in self.data.get("categories", []):
            group = self._group_for_category(category)
            category_name = str(category.get("name", ""))
            if not query and category_name != self.selected_category:
                continue
            for operand in category.get("operands", []):
                item = {
                    "group": group["label"],
                    "group_key": group["key"],
                    "category": category_name,
                    "category_title": self._category_title(category),
                    "category_url": category.get("url", ""),
                    **operand,
                }
                if query:
                    haystack = " ".join(
                        (
                            str(item.get("code", "")),
                            str(item.get("group", "")),
                            str(item.get("category", "")),
                            str(item.get("description", "")),
                            " ".join(item.get("parameters", [])),
                        )
                    ).casefold()
                    if query not in haystack:
                        continue
                results.append(item)
        return results

    @ui.refreshable
    def render_content(self) -> None:
        with ui.column().classes("w-full max-w-[980px] mx-auto px-5 md:px-10 py-6 md:py-8 gap-0"):
            if not self.data.get("categories"):
                self._render_empty_state()
            elif self.selected_operand is not None:
                self._render_operand_detail(self.selected_operand)
            elif self.query:
                self._render_search_results()
            elif self.selected_category:
                category = self._selected_category_data()
                if category:
                    self._render_category_page(category)
                else:
                    self._render_empty_state()
            elif self.selected_group:
                group = self._group_by_key(self.selected_group)
                self._render_group_page(group) if group else self._render_overview()
            else:
                self._render_overview()

    def _render_breadcrumb(
        self,
        group: dict[str, Any] | None = None,
        category: dict[str, Any] | None = None,
        suffix: str = "",
    ) -> None:
        with ui.row().classes("items-center gap-2 text-sm mb-6 flex-wrap"):
            with (
                ui.row()
                .classes("items-center gap-1 text-blue-600 cursor-pointer hover:text-blue-800")
                .on("click", self._go_to_overview)
            ):
                ui.icon("home", size="17px")
                ui.label("操作数手册")
            if group:
                ui.label("/").classes("text-slate-300")
                with (
                    ui.row()
                    .classes("items-center text-blue-600 cursor-pointer hover:text-blue-800")
                    .on("click", self._make_group_handler(str(group["key"])))
                ):
                    ui.label(str(group["label"]))
            if category:
                ui.label("/").classes("text-slate-300")
                with (
                    ui.row()
                    .classes("items-center text-blue-600 cursor-pointer hover:text-blue-800")
                    .on("click", self._make_category_handler(str(category.get("name", ""))))
                ):
                    ui.label(self._category_title(category))
            if suffix:
                ui.label("/").classes("text-slate-300")
                ui.label(suffix).classes("text-slate-500")

    def _render_overview(self) -> None:
        self._render_breadcrumb()
        with ui.row().classes("items-center gap-3"):
            ui.icon("menu_book", size="34px").classes("text-blue-600")
            ui.label("Zemax OpticStudio 操作数手册").classes("text-3xl md:text-4xl font-semibold text-slate-700")
        self._render_meta(int(self.data.get("operand_count", 0) or 0))
        ui.separator().classes("mb-5")
        with ui.column().classes("w-full gap-3"):
            for group in CATEGORY_GROUPS:
                categories = self._categories_for_group(group)
                operand_count = sum(len(category.get("operands", [])) for category in categories)
                with (
                    ui.row()
                    .classes(
                        "w-full px-4 py-3 items-center gap-3 border border-slate-200 rounded-lg cursor-pointer "
                        "hover:border-blue-300 hover:bg-blue-50/50 transition-colors"
                    )
                    .on("click", self._make_group_handler(str(group["key"])))
                ):
                    ui.icon(str(group["icon"]), size="24px").classes("text-blue-600")
                    with ui.column().classes("flex-1 gap-0"):
                        ui.label(str(group["label"])).classes("text-lg font-semibold text-slate-700")
                        ui.label(f"{len(categories)} 个分类 · {operand_count} 个操作数").classes(
                            "text-xs text-slate-400"
                        )
                    ui.icon("chevron_right", size="20px").classes("text-slate-300")

    def _render_group_page(self, group: dict[str, Any]) -> None:
        categories = self._categories_for_group(group)
        operand_count = sum(len(category.get("operands", [])) for category in categories)
        self._render_breadcrumb(group)
        with ui.row().classes("items-center gap-3"):
            ui.icon(str(group["icon"]), size="34px").classes("text-blue-600")
            ui.label(str(group["label"])).classes("text-3xl md:text-4xl font-semibold text-slate-700")
        self._render_meta(operand_count)
        ui.separator().classes("mb-3")
        with ui.column().classes("w-full gap-0"):
            for category in categories:
                with (
                    ui.row()
                    .classes(
                        "w-full min-h-14 py-3 items-center gap-3 border-b border-slate-100 cursor-pointer "
                        "hover:bg-blue-50/60 transition-colors"
                    )
                    .on("click", self._make_category_handler(str(category.get("name", ""))))
                ):
                    ui.icon("article", size="20px").classes("text-blue-500")
                    with ui.column().classes("flex-1 gap-0"):
                        ui.label(self._category_title(category)).classes("text-base font-medium text-slate-700")
                        ui.label(self._category_english(category)).classes("text-xs text-slate-400")
                    ui.badge(str(len(category.get("operands", [])))).props("outline color=primary")
                    ui.icon("chevron_right", size="18px").classes("text-slate-300")

    def _render_meta(self, count: int | None = None) -> None:
        updated_at = str(self.data.get("updated_at", "") or "")
        date_text = "未更新"
        if updated_at:
            try:
                date_text = datetime.fromisoformat(updated_at).strftime("%Y年%m月%d日")
            except ValueError:
                date_text = updated_at
        with ui.row().classes("items-center gap-x-4 gap-y-2 text-sm text-slate-400 flex-wrap mt-4 mb-5"):
            with ui.row().classes("items-center gap-1"):
                ui.icon("account_circle", size="18px")
                ui.label("Ansys 官方资料")
            with ui.row().classes("items-center gap-1"):
                ui.icon("calendar_month", size="18px")
                ui.label(date_text)
            if count is not None:
                ui.badge(f"{count} 个操作数").props("color=deep-orange-1 text-color=deep-orange-7")

    def _adjacent_categories(
        self, current_category: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        category_by_key = self._category_by_key()
        ordered_categories = [
            category_by_key[key] for group in CATEGORY_GROUPS for key in group["categories"] if key in category_by_key
        ]
        current_key = self._category_key(current_category)
        index = next(
            (i for i, category in enumerate(ordered_categories) if self._category_key(category) == current_key),
            -1,
        )
        if index < 0:
            return None, None
        previous_category = ordered_categories[index - 1] if index > 0 else None
        next_category = ordered_categories[index + 1] if index + 1 < len(ordered_categories) else None
        return previous_category, next_category

    def _render_category_page(self, category: dict[str, Any]) -> None:
        group = self._group_for_category(category)
        title = self._category_title(category)
        operands = self._filtered_operands()
        previous_category, next_category = self._adjacent_categories(category)
        self._render_breadcrumb(group, category)
        with ui.row().classes("items-center gap-3"):
            with ui.row().classes("items-center gap-0 shrink-0"):
                previous_button = ui.button(
                    icon="chevron_left",
                    on_click=self._make_category_handler(str(previous_category.get("name", "")))
                    if previous_category
                    else None,
                ).props("flat round dense color=primary")
                previous_button.set_enabled(previous_category is not None)
                previous_button.tooltip(
                    f"上一分类：{self._category_title(previous_category)}" if previous_category else "已经是第一个分类"
                )
                next_button = ui.button(
                    icon="chevron_right",
                    on_click=self._make_category_handler(str(next_category.get("name", ""))) if next_category else None,
                ).props("flat round dense color=primary")
                next_button.set_enabled(next_category is not None)
                next_button.tooltip(
                    f"下一分类：{self._category_title(next_category)}" if next_category else "已经是最后一个分类"
                )
            ui.label(title).classes("text-3xl md:text-4xl font-semibold text-slate-700")
        self._render_meta(len(operands))
        ui.separator().classes("mb-3")

        with ui.column().classes("w-full gap-0 mt-2"):
            for item in operands:
                self._render_operand_row(item, show_category=False)

    def _render_search_results(self) -> None:
        results = self._filtered_operands()
        self._render_breadcrumb(suffix="搜索")
        with ui.row().classes("items-center gap-3"):
            ui.icon("search", size="34px").classes("text-blue-600")
            ui.label("搜索结果").classes("text-3xl md:text-4xl font-semibold text-slate-700")
        self._render_meta(len(results))
        ui.label(f"关键词“{self.query}”共找到 {len(results)} 条说明").classes("text-sm text-slate-500 mb-3")
        ui.separator().classes("mb-2")
        if not results:
            with ui.column().classes("w-full items-center py-16 gap-2"):
                ui.icon("search_off", size="54px").classes("text-slate-300")
                ui.label("没有匹配的操作数").classes("text-lg font-bold text-slate-600")
                ui.label("可尝试 4 位代码、中文名称、英文术语或参数名。 ").classes("text-sm text-slate-500")
            return
        for item in results[:150]:
            self._render_operand_row(item, show_category=True)
        if len(results) > 150:
            ui.label("结果较多，仅显示前 150 条，请增加关键词缩小范围。 ").classes(
                "w-full text-center text-xs text-slate-400 py-4"
            )

    @staticmethod
    def _brief_description(description: str) -> str:
        brief = description.split("。", 1)[0].strip() or description.strip()
        return f"{brief[:72]}…" if len(brief) > 72 else brief

    def _render_operand_row(self, item: dict[str, Any], show_category: bool) -> None:
        with (
            ui.row()
            .classes(
                "w-full min-h-11 py-2 items-start gap-3 flex-nowrap cursor-pointer border-b border-slate-100 "
                "hover:bg-blue-50/60 transition-colors"
            )
            .on("click", self._make_operand_handler(item))
        ):
            ui.label(str(item.get("code", ""))).classes("w-16 shrink-0 text-lg leading-7 font-semibold text-blue-600")
            with ui.column().classes("flex-1 min-w-0 gap-0"):
                ui.label(self._brief_description(str(item.get("description", "")))).classes(
                    "text-[15px] leading-7 text-slate-600"
                )
                if show_category:
                    ui.label(f"{item.get('group')} / {item.get('category_title')}").classes("text-xs text-slate-400")
            ui.icon("chevron_right", size="18px").classes("text-slate-300 mt-1 shrink-0")

    def _render_operand_detail(self, item: dict[str, Any]) -> None:
        code = str(item.get("code", ""))
        category = next(
            (value for value in self.data.get("categories", []) if value.get("name") == item.get("category")),
            None,
        )
        group = self._group_by_key(str(item.get("group_key", "")))
        if group is None and category is not None:
            group = self._group_for_category(category)
        category_title = self._category_title(category) if category else str(item.get("category_title", ""))
        self._render_breadcrumb(group, category, code)
        with ui.row().classes("items-center gap-3"):
            ui.icon("label", size="34px").classes("text-blue-600")
            ui.label(code).classes("text-4xl font-semibold text-slate-700 tracking-wide")
        self._render_meta()
        ui.separator().classes("mb-3")
        with ui.element("div").classes("w-full border-l-4 border-slate-200 pl-5 py-2 my-2"):
            ui.label(f"分类：{category_title}").classes("text-lg text-slate-500")

        ui.label(str(item.get("description", ""))).classes(
            "w-full mt-5 text-base md:text-[17px] leading-8 text-slate-700 whitespace-pre-line"
        )

        parameters = [str(value) for value in item.get("parameters", []) if value]
        if parameters:
            with ui.column().classes("w-full mt-7 gap-2"):
                ui.label("涉及参数").classes("text-sm font-bold text-slate-500")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    for parameter in parameters:
                        ui.chip(parameter).props("outline color=primary dense")

        with ui.row().classes("w-full items-center justify-between mt-10 pt-5 border-t border-slate-200"):
            back_text = "返回搜索" if self.query else "返回分类"
            ui.button(back_text, icon="arrow_back", on_click=self._back_to_category).props("flat color=primary no-caps")
            page_url = str(item.get("category_url", ""))
            if page_url:
                ui.link("查看 Ansys 官方原文", page_url, new_tab=True).classes("text-sm text-blue-600")

        previous_item, next_item = self._adjacent_operands(item)
        with ui.row().classes("w-full items-stretch justify-between gap-3 mt-5"):
            if previous_item:
                ui.button(
                    f"上一项  {previous_item['code']}",
                    icon="chevron_left",
                    on_click=self._make_operand_handler(previous_item),
                ).props("outline color=grey-7 no-caps")
            else:
                ui.space()
            if next_item:
                ui.button(
                    f"下一项  {next_item['code']}",
                    on_click=self._make_operand_handler(next_item),
                ).props("outline color=grey-7 no-caps icon-right=chevron_right")

        ui.label("资料来自 Ansys 官方帮助，版权归原权利人所有；本工具仅提供内部检索与原文定位。 ").classes(
            "w-full text-center text-xs text-slate-400 mt-10 pb-4"
        )

    def _back_to_category(self) -> None:
        self.selected_operand = None
        self.render_content.refresh()
        self._scroll_to_top()

    def _adjacent_operands(self, current_item: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        category_name = str(current_item.get("category", ""))
        category = next(
            (item for item in self.data.get("categories", []) if item.get("name") == category_name),
            None,
        )
        if not category:
            return None, None
        group = self._group_for_category(category)
        items = [
            {
                "group": group["label"],
                "group_key": group["key"],
                "category": category_name,
                "category_title": self._category_title(category),
                "category_url": category.get("url", ""),
                **operand,
            }
            for operand in category.get("operands", [])
        ]
        index = next((i for i, item in enumerate(items) if item.get("code") == current_item.get("code")), -1)
        if index < 0:
            return None, None
        previous_item = items[index - 1] if index > 0 else None
        next_item = items[index + 1] if index + 1 < len(items) else None
        return previous_item, next_item

    def _render_empty_state(self) -> None:
        with ui.column().classes("w-full items-center py-20 gap-3"):
            ui.icon("cloud_download", size="64px").classes("text-slate-300")
            ui.label("还没有本地操作数资料").classes("text-lg font-bold text-slate-600")
            ui.label("点击右上角“更新资料”，输入 Ansys 分类页网址开始同步。 ").classes("text-sm text-slate-500")
            ui.button("更新资料", icon="cloud_sync", on_click=self._open_update_dialog).props(
                "color=primary unelevated no-caps"
            )
