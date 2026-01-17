# src/tools/simple_coupling_calculator.py
from dataclasses import dataclass
from typing import Dict, Literal, Optional, Tuple, cast

import matplotlib.pyplot as plt
import numpy as np
from nicegui import ui

# --- 1. Matplotlib 全局配置 ---
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False


# --- 2. 核心计算逻辑 (完全保留) ---
@dataclass
class SourceConfig:
    shape: Literal["rect", "circle"]
    dims: Tuple[float, float]
    distribution: str = "lambertian"


@dataclass
class SystemConfig:
    lens_diameter: float
    lens_distance: float
    lens_count: int
    coating_t0: float = 0.995


@dataclass
class ReceiverConfig:
    guide_diameter: float
    na: float


class OpticalCouplingCalculator:
    def __init__(self, source: SourceConfig, system: SystemConfig, receiver: ReceiverConfig):
        self.src = source
        self.sys = system
        self.rec = receiver

    def _generate_spatial_grid(self, num_points=200):
        if self.src.shape == "rect":
            w, h = self.src.dims
            if h == 0 or w == 0:
                return np.array([[0, 0]]), 0
            nx = int(np.sqrt(num_points * w / h))
            ny = int(np.sqrt(num_points * h / w))
            nx, ny = max(1, nx), max(1, ny)
            x = np.linspace(-w / 2, w / 2, nx)
            y = np.linspace(-h / 2, h / 2, ny)
            xv, yv = np.meshgrid(x, y)
            points = np.column_stack([xv.ravel(), yv.ravel()])
            total_area = w * h
        elif self.src.shape == "circle":
            d = self.src.dims[0]
            r = d / 2.0
            n = num_points
            indices = np.arange(0, n, dtype=float) + 0.5
            r_pos = r * np.sqrt(indices / n)
            theta_pos = np.pi * (1 + 5**0.5) * indices
            x = r_pos * np.cos(theta_pos)
            y = r_pos * np.sin(theta_pos)
            points = np.column_stack([x, y])
            total_area = np.pi * r**2
        else:
            raise ValueError("Unsupported shape")
        if len(points) == 0:
            return np.array([[0, 0]]), 0
        return points, total_area / len(points)

    def _coating_transmission(self, theta_rad, is_first_surface=False):
        t0 = self.sys.coating_t0
        c_factor = 0.4
        eff_theta = theta_rad if is_first_surface else theta_rad * 0.5
        transmission = t0 * (1 - c_factor * (1 - np.cos(eff_theta)) ** 2)
        return np.clip(transmission, 0, 1.0)

    def calculate_efficiency(self, spatial_samples=200, angular_samples=2000):
        spatial_points, dA = self._generate_spatial_grid(num_points=spatial_samples)
        guide_area = np.pi * (self.rec.guide_diameter / 2) ** 2
        g_fiber_limit = np.pi * guide_area * (self.rec.na**2)

        total_rays_emitted = 0
        rays_hit_lens = 0
        total_flux_collected = 0.0
        total_etendue_collected = 0.0

        rng = np.random.default_rng(42)
        u = rng.random(angular_samples)
        v = rng.random(angular_samples)
        thetas = np.arcsin(np.sqrt(u))
        phis = 2 * np.pi * v
        tan_thetas = np.tan(thetas)

        lens_r_sq = (self.sys.lens_diameter / 2.0) ** 2
        z_dist = self.sys.lens_distance
        num_surfaces = self.sys.lens_count * 2

        t1 = self._coating_transmission(thetas, is_first_surface=True)
        t_rest = self._coating_transmission(thetas, is_first_surface=False) ** (num_surfaces - 1)
        transmission_profile = t1 * t_rest

        dx = z_dist * tan_thetas * np.cos(phis)
        dy = z_dist * tan_thetas * np.sin(phis)

        for sp in spatial_points:
            sx, sy = sp
            lx = sx + dx
            ly = sy + dy
            dist_sq = lx**2 + ly**2
            hit_mask = dist_sq <= lens_r_sq
            hits = np.sum(hit_mask)
            rays_hit_lens += hits
            total_rays_emitted += angular_samples

            if hits > 0:
                total_flux_collected += np.sum(transmission_profile[hit_mask])
                hit_ratio = hits / angular_samples
                total_etendue_collected += dA * np.pi * hit_ratio

        geo_capture_rate = rays_hit_lens / total_rays_emitted if total_rays_emitted > 0 else 0
        current_efficiency = total_flux_collected / total_rays_emitted if total_rays_emitted > 0 else 0
        avg_transmission = (current_efficiency / geo_capture_rate) if geo_capture_rate > 0 else 0

        etendue_transmission = 1.0
        if total_etendue_collected > g_fiber_limit:
            etendue_transmission = g_fiber_limit / total_etendue_collected

        final_efficiency = current_efficiency * etendue_transmission

        return {
            "geo_capture_rate": geo_capture_rate * 100,
            "avg_coating_trans": avg_transmission * 100,
            "geometric_efficiency": current_efficiency * 100,
            "etendue_limit_factor": etendue_transmission * 100,
            "final_efficiency": final_efficiency * 100,
            "G_collected": total_etendue_collected,
            "G_fiber": g_fiber_limit,
        }

    def get_sensitivity_data(self, param_name, start, end, steps=20):
        values = np.linspace(start, end, steps)
        results = []
        original_val = getattr(self.sys, param_name, None) or getattr(self.rec, param_name, None)
        for v in values:
            if hasattr(self.sys, param_name):
                setattr(self.sys, param_name, v)
            elif hasattr(self.rec, param_name):
                setattr(self.rec, param_name, v)
            res = self.calculate_efficiency(spatial_samples=50, angular_samples=500)
            results.append(res["final_efficiency"])

        if hasattr(self.sys, param_name):
            setattr(self.sys, param_name, original_val)
        elif hasattr(self.rec, param_name):
            setattr(self.rec, param_name, original_val)
        return values, results


# --- 3. 状态管理 ---
class State:
    def __init__(self):
        self.src_w = 1.0
        self.src_h = 1.0
        self.src_type: str = "rect"
        self.src_flux: float = 100.0
        self.lens_d = 15.0
        self.lens_dist = 3.0
        self.lens_count = 3
        self.coating = 0.995
        self.fiber_d = 5.0
        self.fiber_na = 0.62
        self.last_result: Optional[Dict[str, float]] = None


# --- 4. 主类封装 ---
class SimpleCouplingCalculator:
    def __init__(self):
        self.state = State()

        # UI 组件引用
        self.plot_view: Optional[ui.pyplot] = None
        self.ui_refs: Dict[str, Optional[ui.label]] = {
            "final_val": None,
            "flux_val": None,
            "geo_val": None,
            "trans_val": None,
            "etendue_status": None,
            "etendue_detail": None,
        }

    def update_result_ui(self, res):
        if self.ui_refs["final_val"] is None or self.ui_refs["flux_val"] is None:
            return

        eff = res["final_efficiency"]
        color_class = "text-red-600" if eff < 10 else ("text-orange-500" if eff < 50 else "text-green-600")

        final_lbl = cast(ui.label, self.ui_refs["final_val"])
        final_lbl.set_text(f"{eff:.2f}%")
        final_lbl.classes(remove="text-red-600 text-orange-500 text-green-600 text-gray-400", add=color_class)

        flux_in = self.state.src_flux
        flux_out = flux_in * (eff / 100.0)
        flux_lbl = cast(ui.label, self.ui_refs["flux_val"])
        flux_lbl.set_text(f"{flux_out:.2f} lm")
        flux_lbl.classes("font-black")

        cast(ui.label, self.ui_refs["geo_val"]).set_text(f"{res['geo_capture_rate']:.1f}%")
        cast(ui.label, self.ui_refs["trans_val"]).set_text(f"{res['avg_coating_trans']:.1f}%")

        factor = res["etendue_limit_factor"]
        status_lbl = cast(ui.label, self.ui_refs["etendue_status"])
        detail_lbl = cast(ui.label, self.ui_refs["etendue_detail"])

        if factor >= 99.9:
            status_lbl.set_text("✅ 完美匹配")
            status_lbl.classes(replace="text-green-600")
        else:
            status_lbl.set_text(f"❌ 瓶颈限制 ({factor:.1f}%)")
            status_lbl.classes(replace="text-red-600")
        detail_lbl.set_text(f"系统: {res['G_collected']:.3f} / 极限: {res['G_fiber']:.3f}")

    def run_auto_process(self):
        dims = (self.state.src_w, self.state.src_h) if self.state.src_type == "rect" else (self.state.src_w, 0)
        shape_literal = cast(Literal["rect", "circle"], self.state.src_type)
        src_cfg = SourceConfig(shape=shape_literal, dims=dims)
        sys_cfg = SystemConfig(self.state.lens_d, self.state.lens_dist, int(self.state.lens_count), self.state.coating)
        rec_cfg = ReceiverConfig(self.state.fiber_d, self.state.fiber_na)

        calc = OpticalCouplingCalculator(src_cfg, sys_cfg, rec_cfg)
        ui.notify("正在进行光学仿真计算...", type="info", position="bottom-right")
        res = calc.calculate_efficiency(spatial_samples=200, angular_samples=3000)
        self.state.last_result = res
        self.update_result_ui(res)

        if self.plot_view:
            with self.plot_view:
                plt.clf()
                fig = plt.gcf()
                fig.patch.set_facecolor("white")

                ax1 = fig.add_subplot(1, 2, 1)
                d_start = max(1.0, self.state.lens_d * 0.5)
                d_end = self.state.lens_d * 2.0
                x1, y1 = calc.get_sensitivity_data("lens_diameter", d_start, d_end, steps=20)
                ax1.plot(x1, y1, "o-", color="#2563eb", markersize=4, linewidth=1.5)
                ax1.set_title("敏感度分析：透镜口径", fontsize=11)
                ax1.set_xlabel("口径 Diameter (mm)", fontsize=9)
                ax1.set_ylabel("耦合效率 Efficiency (%)", fontsize=9)
                ax1.grid(True, linestyle="--", alpha=0.4)
                ax1.axvline(self.state.lens_d, color="#ef4444", linestyle="--", alpha=0.6)

                ax2 = fig.add_subplot(1, 2, 2)
                z_start = max(0.1, self.state.lens_dist - 2.0)
                z_end = self.state.lens_dist + 5.0
                x2, y2 = calc.get_sensitivity_data("lens_distance", z_start, z_end, steps=20)
                ax2.plot(x2, y2, "o-", color="#f97316", markersize=4, linewidth=1.5)
                ax2.set_title("敏感度分析：光源距离", fontsize=11)
                ax2.set_xlabel("距离 Distance (mm)", fontsize=9)
                ax2.grid(True, linestyle="--", alpha=0.4)
                ax2.axvline(self.state.lens_dist, color="#ef4444", linestyle="--", alpha=0.6)
                plt.tight_layout()
            self.plot_view.update()
            ui.notify("计算与绘图已完成", type="positive", position="bottom-right")

    # --- 5. UI 构建 (Show) ---
    def show(self, dialog: ui.dialog):
        THEME_BG = "bg-slate-50"
        SIDEBAR_BG = "bg-white"
        CARD_CLASS = "bg-white p-4 rounded-lg shadow-sm border border-gray-100 min-h-[140px]"

        with ui.card().classes("w-full h-full p-0 gap-0 border-none"):
            # 顶部 Header (模拟)
            with ui.row().classes("w-full bg-slate-800 h-14 items-center shadow-md flex-none px-4 justify-between"):
                with ui.row().classes("items-center"):
                    ui.icon("science", size="md").classes("text-blue-400")
                    ui.label("光耦合效率仿真平台").classes("text-lg font-bold tracking-wide text-white ml-2")
                    ui.label("V1.0版").classes("text-xs text-gray-400 bg-slate-900 px-2 py-0.5 rounded ml-2")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round color=white")

            # 主体
            with ui.row().classes(f"w-full flex-1 {THEME_BG} gap-0 flex-nowrap overflow-hidden"):
                # 左侧：参数控制
                with ui.column().classes(
                    f"w-80 flex-none {SIDEBAR_BG} h-full p-4 gap-4 border-r border-gray-200 overflow-y-auto"
                ):
                    ui.label("参数配置").classes("text-sm font-bold text-gray-500 uppercase tracking-wider mb-2")

                    with ui.column().classes("w-full gap-2"):
                        ui.label("1. 光源配置 (Source)").classes("font-bold text-slate-700")
                        ui.select(
                            {"rect": "矩形 (Rect)", "circle": "圆形 (Circle)"}, value="rect", label="形状"
                        ).bind_value(self.state, "src_type").classes("w-full bg-slate-50")
                        ui.number("光源总通量 (Flux)", value=100.0, step=10.0).bind_value(self.state, "src_flux").props(
                            "outlined dense suffix=lm"
                        ).classes("w-full").tooltip("请输入光源规格书中的 Total Luminous Flux (流明)")

                        with ui.row().classes("w-full"):
                            ui.number("宽度/直径", value=1.0, step=0.1).bind_value(self.state, "src_w").props(
                                "outlined dense suffix=mm"
                            ).classes("w-1/2")
                            ui.number("高度", value=1.0, step=0.1).bind_value(self.state, "src_h").bind_visibility_from(
                                self.state, "src_type", lambda x: x == "rect"
                            ).props("outlined dense suffix=mm").classes("w-1/3")
                    ui.separator()

                    with ui.column().classes("w-full gap-2"):
                        ui.label("2. 透镜系统 (System)").classes("font-bold text-slate-700")
                        ui.number("第一透镜口径", value=15.0, step=0.5).bind_value(self.state, "lens_d").props(
                            "outlined dense suffix=mm"
                        ).classes("w-full")
                        ui.number("光源到透镜距离", value=3.0, step=0.1).bind_value(self.state, "lens_dist").props(
                            "outlined dense suffix=mm"
                        ).classes("w-full")
                        with ui.row().classes("w-full"):
                            ui.number("透镜数量", value=3).bind_value(self.state, "lens_count").props(
                                "outlined dense"
                            ).classes("w-1/3")
                            ui.number("镀膜单面透过率", value=0.995, step=0.001, max=1.0).bind_value(
                                self.state, "coating"
                            ).props("outlined dense").classes("w-1/2")
                    ui.separator()

                    with ui.column().classes("w-full gap-2"):
                        ui.label("3. 接收端 (Receiver)").classes("font-bold text-slate-700")
                        with ui.row().classes("w-full"):
                            ui.number("光纤芯径", value=1.5, step=0.1).bind_value(self.state, "fiber_d").props(
                                "outlined dense suffix=mm"
                            ).classes("w-1/2")
                            ui.number("数值孔径 NA", value=0.5, step=0.05).bind_value(self.state, "fiber_na").props(
                                "outlined dense"
                            ).classes("w-1/3")

                    ui.button("开始仿真 (Run)", on_click=self.run_auto_process).props("icon=play_arrow").classes(
                        "w-full mt-4 h-12 text-lg font-bold shadow-md bg-blue-600 hover:bg-blue-700 text-white rounded-md"
                    )

                # 右侧：仪表盘
                with ui.column().classes("flex-grow p-6 gap-6 h-full overflow-y-auto"):
                    with ui.grid(columns=5).classes("w-full gap-4"):
                        with ui.column().classes(f"{CARD_CLASS} border-l-4 border-blue-500 justify-between"):
                            ui.label("最终耦合效率").classes("text-xs font-bold text-gray-400 uppercase")
                            with ui.row().classes("items-baseline"):
                                self.ui_refs["final_val"] = ui.label("- %").classes("text-4xl font-black text-gray-300")
                            ui.label("综合能量传输比").classes("text-xs text-gray-400")

                        with ui.column().classes(f"{CARD_CLASS} border-l-4 border-yellow-400 justify-between"):
                            ui.label("耦合入接收端光通量 (Flux)").classes("text-xs font-bold text-gray-400 uppercase")
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("lightbulb", color="orange").classes("text-2xl")
                                self.ui_refs["flux_val"] = ui.label("- lm").classes("text-3xl font-black text-gray-300")
                            ui.label("实际进入光纤的流明数").classes("text-xs text-gray-400")

                        with ui.column().classes(f"{CARD_CLASS} justify-between"):
                            ui.label("几何拦截率").classes("text-xs font-bold text-gray-400 uppercase")
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("filter_center_focus", color="gray").classes("opacity-50")
                                self.ui_refs["geo_val"] = ui.label("-").classes("text-2xl font-bold text-slate-700")
                            ui.label("击中透镜口径的比例").classes("text-xs text-gray-400")

                        with ui.column().classes(f"{CARD_CLASS} justify-between"):
                            ui.label("平均透过率").classes("text-xs font-bold text-gray-400 uppercase")
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("blur_on", color="gray").classes("opacity-50")
                                self.ui_refs["trans_val"] = ui.label("-").classes("text-2xl font-bold text-slate-700")
                            ui.label("透镜膜层损耗后").classes("text-xs text-gray-400")

                        with ui.column().classes(f"{CARD_CLASS} justify-between"):
                            ui.label("光展量匹配").classes("text-xs font-bold text-gray-400 uppercase")
                            self.ui_refs["etendue_status"] = ui.label("就绪").classes(
                                "text-lg font-bold text-slate-500"
                            )
                            self.ui_refs["etendue_detail"] = ui.label("System / Limit").classes("text-xs text-gray-400")
                            ui.label("红字代表光纤吃不下").classes("text-xs text-gray-400")

                    with ui.card().classes(
                        "w-full flex-grow min-h-[500px] p-1 shadow-sm rounded-lg border border-gray-100 flex flex-col"
                    ):
                        with ui.row().classes(
                            "w-full px-4 py-2 border-b border-gray-100 bg-white rounded-t-lg justify-between items-center"
                        ):
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("analytics", color="blue").classes("opacity-70")
                                ui.label("参数敏感度趋势图").classes("font-bold text-slate-700")
                            ui.label("自动生成").classes("text-xs text-gray-400 bg-gray-100 px-2 rounded-full")

                        with ui.column().classes("w-full flex-grow p-2 bg-white relative"):
                            self.plot_view = ui.pyplot(figsize=(10, 4), close=False).classes("")
                            with self.plot_view:
                                plt.text(
                                    0.5,
                                    0.5,
                                    "请点击左侧“开始仿真”生成图表",
                                    ha="center",
                                    va="center",
                                    color="#94a3b8",
                                    fontsize=14,
                                )
                                plt.axis("off")
