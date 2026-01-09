# src/tools/etendue_calculator.py
import inspect
import math
from typing import Optional

from nicegui import ui


class EtendueCalculator:
    def __init__(self):
        # 1. 状态变量移入类实例 (确保多用户互不干扰)
        self.state = {
            "shape": "circle",
            "flux": 1000.0,
            "src_d": 1.0,
            "src_w": 1.0,
            "src_h": 1.0,
            "guide_d": 5.0,
            "guide_na": 0.62,
        }

        # 2. UI 引用初始化
        self.res_lum: Optional[ui.label] = None
        self.res_int: Optional[ui.label] = None
        self.res_g_src: Optional[ui.label] = None
        self.res_g_guide: Optional[ui.label] = None
        self.res_phi: Optional[ui.label] = None
        self.res_eff: Optional[ui.label] = None
        self.limit_msg: Optional[ui.label] = None

    def calculate(self):
        """核心计算逻辑"""
        # 安全守卫
        # [修改点 1]：改用显式的 or 判断，Pylance 才能正确识别类型
        if (
            self.res_lum is None
            or self.res_int is None
            or self.res_g_src is None
            or self.res_g_guide is None
            or self.res_phi is None
            or self.res_eff is None
            or self.limit_msg is None
        ):
            return

        try:
            # A. 光源计算
            if self.state["shape"] == "circle":
                radius = self.state["src_d"] / 2.0
                area_src_mm2 = math.pi * (radius**2)
            else:
                area_src_mm2 = self.state["src_w"] * self.state["src_h"]

            area_src_m2 = area_src_mm2 * 1e-6

            # 亮度与强度
            if area_src_m2 > 0:
                luminance = self.state["flux"] / (math.pi * area_src_m2)
            else:
                luminance = 0.0

            intensity = self.state["flux"] / math.pi

            # 光源 Etendue
            g_source = math.pi * area_src_mm2

            # B. 导光束计算
            guide_r = self.state["guide_d"] / 2.0
            area_guide_mm2 = math.pi * (guide_r**2)
            g_guide = math.pi * area_guide_mm2 * (self.state["guide_na"] ** 2)

            # C. 耦合效率
            if g_source > 0:
                geo_eff = min(g_source, g_guide) / g_source
                phi_coupled = self.state["flux"] * geo_eff
            else:
                geo_eff = 0.0
                phi_coupled = 0.0

            # D. 更新 UI
            self.res_lum.text = f"{luminance / 1e6:.2f}"
            self.res_int.text = f"{intensity:.2f}"
            self.res_g_src.text = f"{g_source:.2f}"
            self.res_g_guide.text = f"{g_guide:.2f}"

            self.res_phi.text = f"{phi_coupled:.1f} lm"
            self.res_eff.text = f"{geo_eff * 100:.1f}%"

            # 动态样式
            if g_guide < g_source:
                self.res_g_guide.classes(add="text-red-600 font-bold", remove="text-green-600")
                self.limit_msg.text = "⚠️ 限制因素：导光束 Etendue 不足 (光束过大进不去)"
                self.limit_msg.classes(add="text-red-500", remove="text-green-600")
            else:
                self.res_g_guide.classes(add="text-green-600", remove="text-red-600 font-bold")
                self.limit_msg.text = "✅ 限制因素：光源总光通量 (光源较小，理论可全进)"
                self.limit_msg.classes(add="text-green-600", remove="text-red-500")

        except Exception as e:
            ui.notify(f"计算异常: {str(e)}", type="negative")

    def show(self, dialog: ui.dialog):
        """渲染 UI 到当前容器中"""
        # --- UI 布局构建 ---
        # 注意：这里去掉了外层的 ui.card().h-screen，改为适应父容器
        with ui.column().classes("w-full h-full p-0 gap-0 bg-white"):
            # 标题栏 (可选，如果 Dialog 外部有标题可省略)
            with ui.row().classes("w-full bg-slate-100 p-4 border-b items-center justify-between"):
                with ui.row().classes("items-center"):
                    ui.icon("lightbulb", size="32px").classes("text-blue-600")
                    ui.label("光学耦合极限计算器 (Etendue Calculator)").classes("text-xl font-bold text-slate-800")
                # [修改点 3]：直接调用传入的 dialog 对象的 close 方法
                ui.button(icon="close", on_click=dialog.close).props("flat dense round")

            # 主体布局 (利用 scroll-area 防止溢出)
            # [修改点 3]：将 h-[600px] 改为 flex-1，这样它会自动占满标题栏下方的所有剩余空间
            with ui.scroll_area().classes("w-full flex-1 p-6"):
                with ui.row().classes("w-full gap-8 items-start flex-nowrap"):
                    # --- 第一栏：光源参数 ---
                    with ui.column().classes("w-1/3 min-w-[250px] border-r pr-6"):
                        ui.label("1. 光源参数 (Source)").classes("text-lg font-bold text-blue-900 mb-2")

                        # 定义 refreshable 函数
                        @ui.refreshable
                        def render_shape_inputs():
                            if self.state["shape"] == "circle":
                                ui.number(
                                    "发光面直径 (mm)", value=10.0, format="%.2f", on_change=self.calculate
                                ).bind_value(self.state, "src_d").classes("w-full")
                            else:
                                with ui.row().classes("w-full"):
                                    ui.number(
                                        "宽度 (mm)", value=10.0, format="%.2f", on_change=self.calculate
                                    ).bind_value(self.state, "src_w").classes("w-1/2 pr-1")
                                    ui.number(
                                        "高度 (mm)", value=10.0, format="%.2f", on_change=self.calculate
                                    ).bind_value(self.state, "src_h").classes("w-1/2 pl-1")

                        ui.toggle(
                            ["circle", "rect"],
                            value="circle",
                            on_change=lambda e: (
                                self.state.update({"shape": e.value}),
                                render_shape_inputs.refresh(),
                                self.calculate(),
                            ),
                        ).bind_value(self.state, "shape").props('no-caps toggle-color="blue"').classes("w-full")

                        render_shape_inputs()

                        ui.number(
                            "总光通量 (lm)", value=1000.0, format="%.1f", step=50, on_change=self.calculate
                        ).bind_value(self.state, "flux").classes("w-full")

                        ui.separator().classes("my-4")

                        with ui.row().classes("w-full justify-between text-sm"):
                            ui.label("计算亮度:")
                            with ui.row().classes("items-baseline gap-1"):
                                self.res_lum = ui.label("-").classes("font-bold")
                                ui.label("Mnit")

                        with ui.row().classes("w-full justify-between text-sm"):
                            ui.label("轴向强度:")
                            with ui.row().classes("items-baseline gap-1"):
                                self.res_int = ui.label("-").classes("font-bold")
                                ui.label("cd")

                    # --- 第二栏：导光系统 ---
                    with ui.column().classes("w-1/3 min-w-[250px] border-r pr-6"):
                        ui.label("2. 导光束/光纤").classes("text-lg font-bold text-blue-900 mb-2")

                        ui.number("入口孔径 (mm)", value=3.0, format="%.2f", on_change=self.calculate).bind_value(
                            self.state, "guide_d"
                        ).classes("w-full")

                        ui.number(
                            "数值孔径 (NA)",
                            value=0.5,
                            min=0.01,
                            max=1.0,
                            format="%.2f",
                            step=0.05,
                            on_change=self.calculate,
                        ).bind_value(self.state, "guide_na").classes("w-full")

                        ui.markdown("**常见NA参考：**\n- 石英光纤: 0.22\n- 塑料光纤: 0.50\n- 液体光导: 0.59").classes(
                            "text-xs text-gray-400 mt-2"
                        )

                    # --- 第三栏：结果与分析 ---
                    with ui.column().classes("flex-1"):
                        ui.label("3. 耦合极限与分析").classes("text-lg font-bold text-blue-900 mb-2")

                        with ui.card().classes("w-full bg-slate-50 p-4 mb-4 border"):
                            ui.label("光学扩展量 (Etendue) 对比").classes("text-sm font-bold text-gray-600")

                            with ui.grid(columns=3).classes("w-full items-center gap-2 mt-2"):
                                ui.label("光源").classes("text-xs text-gray-500")
                                ui.label("vs").classes("text-xs text-center font-bold")
                                ui.label("导光束").classes("text-xs text-gray-500 text-right")

                                self.res_g_src = ui.label("-").classes("text-lg font-mono")
                                ui.icon("arrow_forward", size="xs").classes("justify-self-center opacity-50")
                                self.res_g_guide = ui.label("-").classes("text-lg font-mono text-right")

                            self.limit_msg = ui.label("").classes("text-xs mt-2 font-medium")

                        with ui.row().classes("w-full gap-4"):
                            with ui.card().classes("flex-1 bg-blue-600 p-3 items-center"):
                                ui.label("理想极限光通量").classes("text-white text-xs opacity-80")
                                self.res_phi = ui.label("-").classes("text-white text-2xl font-bold")

                            with ui.card().classes("flex-1 bg-blue-50 p-3 items-center border border-blue-200"):
                                ui.label("几何传输效率").classes("text-blue-800 text-xs")
                                self.res_eff = ui.label("-").classes("text-blue-900 text-2xl font-bold")

                        # --- 工程损耗注解 ---
                        ui.separator().classes("my-4")
                        with ui.expansion("⚠️ 实际工程需扣除的损耗", icon="info", value=True).classes(
                            "w-full bg-amber-50 rounded-lg text-sm"
                        ):
                            ui.markdown(
                                inspect.cleandoc("""
                                **计算结果仅为理想理论物理极限 (Etendue Limit)，实际工程必须扣除以下损耗：**
                                
                                1. **透镜组反射与吸收**: 透镜界面反射与材料吸收。
                                2. **透镜组孔径收集效率**: 并不是所有光源的能量都能通过透镜组输出。
                                3. **导光束菲涅尔反射**: 约 4% - 8% (无镀膜界面)。
                                4. **导光束填充因子**: 光纤束有效面积通常为 60% - 85%。
                                5. **对准误差**: X/Y 偏移或角度倾斜损耗。
                                """)
                            ).classes("text-gray-700 p-2 leading-relaxed")

        # 初始化第一次计算
        self.calculate()
