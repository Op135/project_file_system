# src/tools/microlens_calculator.py
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Tuple, cast

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import PathPatch
from matplotlib.path import Path
from nicegui import ui

# --- 1. Matplotlib 全局配置 ---
plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei", "sans-serif"]
plt.rcParams["axes.unicode_minus"] = False


# --- 2. 核心计算逻辑 (完全保留原版) ---
@dataclass
class SourceConfig:
    shape: Literal["rect", "circle"]
    dims: Tuple[float, float]
    single_flux: float = 120.0


@dataclass
class ArrayConfig:
    type: Literal["rect_grid", "hex_grid"]
    pitch_x: float
    pitch_y: float
    grid_rows: int = 3
    grid_cols: int = 3
    hex_rings: int = 1
    lens_diameter: float = 5.0


@dataclass
class SystemConfig:
    lens_distance: float
    lens_count: int = 1
    coating_t0: float = 0.995


@dataclass
class ReceiverConfig:
    guide_diameter: float
    na: float


class OpticalCouplingCalculator:
    def __init__(self, source: SourceConfig, array: ArrayConfig, system: SystemConfig, receiver: ReceiverConfig):
        self.src = source
        self.arr = array
        self.sys = system
        self.rec = receiver

    def _get_array_stats(self) -> int:
        if self.arr.type == "rect_grid":
            return max(1, self.arr.grid_rows * self.arr.grid_cols)
        else:
            n = self.arr.hex_rings
            return 3 * n * (n + 1) + 1

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
            return np.array([[0, 0]]), 0
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
        num_units = self._get_array_stats()
        guide_area = np.pi * (self.rec.guide_diameter / 2) ** 2
        g_fiber_limit = np.pi * guide_area * (self.rec.na**2)

        spatial_points, dA = self._generate_spatial_grid(num_points=spatial_samples)
        total_rays_emitted = 0
        rays_hit_lens = 0
        unit_flux_collected_ratio = 0.0
        unit_etendue_collected = 0.0

        rng = np.random.default_rng(42)
        u = rng.random(angular_samples)
        v = rng.random(angular_samples)
        thetas = np.arcsin(np.sqrt(u))
        phis = 2 * np.pi * v
        tan_thetas = np.tan(thetas)

        z_dist = self.sys.lens_distance
        num_surfaces = self.sys.lens_count * 2
        t1 = self._coating_transmission(thetas, is_first_surface=True)
        t_rest = self._coating_transmission(thetas, is_first_surface=False) ** (num_surfaces - 1)
        transmission_profile = t1 * t_rest

        dx = z_dist * tan_thetas * np.cos(phis)
        dy = z_dist * tan_thetas * np.sin(phis)
        lens_r_sq = (self.arr.lens_diameter / 2.0) ** 2
        px, py = self.arr.pitch_x, self.arr.pitch_y

        for sp in spatial_points:
            sx, sy = sp
            lx = sx + dx
            ly = sy + dy

            if self.arr.type == "rect_grid":
                in_pitch = (np.abs(lx) <= px / 2) & (np.abs(ly) <= py / 2)
            else:
                ax, ay = np.abs(lx), np.abs(ly)
                in_pitch = (ay <= px / 2) & ((ay + np.sqrt(3) * ax) <= px)

            dist_sq = lx**2 + ly**2
            in_lens = dist_sq <= lens_r_sq
            hit_mask = in_pitch & in_lens
            hits = np.sum(hit_mask)
            rays_hit_lens += hits
            total_rays_emitted += angular_samples

            if hits > 0:
                unit_flux_collected_ratio += np.sum(transmission_profile[hit_mask])
                hit_ratio = hits / angular_samples
                unit_etendue_collected += dA * np.pi * hit_ratio

        geo_capture_rate = rays_hit_lens / total_rays_emitted if total_rays_emitted > 0 else 0
        raw_efficiency_unit = unit_flux_collected_ratio / total_rays_emitted if total_rays_emitted > 0 else 0
        avg_transmission = (raw_efficiency_unit / geo_capture_rate) if geo_capture_rate > 0 else 0

        total_system_etendue = unit_etendue_collected * num_units
        etendue_factor = 1.0
        if total_system_etendue > g_fiber_limit:
            etendue_factor = g_fiber_limit / total_system_etendue

        final_efficiency = raw_efficiency_unit * etendue_factor
        total_output_flux = self.src.single_flux * num_units * final_efficiency

        src_area = 0.0
        if self.src.shape == "rect":
            src_area = self.src.dims[0] * self.src.dims[1]
        elif self.src.shape == "circle":
            src_area = np.pi * (self.src.dims[0] / 2.0) ** 2

        l_src = 0.0
        if src_area > 1e-9:
            l_src = self.src.single_flux / (src_area * np.pi)

        l_out = 0.0
        if g_fiber_limit > 1e-9:
            l_out = total_output_flux / g_fiber_limit

        return {
            "num_units": num_units,
            "geo_rate": geo_capture_rate,
            "trans_rate": avg_transmission,
            "etendue_factor": etendue_factor,
            "final_eff": final_efficiency,
            "G_collected": total_system_etendue,
            "G_fiber": g_fiber_limit,
            "src_luminance": l_src,
            "out_luminance": l_out,
        }

    def get_sensitivity_data(self, param_name, start, end, steps=20):
        values = np.linspace(start, end, steps)
        results = []
        target_obj = None
        if hasattr(self.sys, param_name):
            target_obj = self.sys
        elif hasattr(self.rec, param_name):
            target_obj = self.rec
        elif hasattr(self.arr, param_name):
            target_obj = self.arr

        if not target_obj:
            return values, results
        original_val = getattr(target_obj, param_name)
        for v in values:
            setattr(target_obj, param_name, v)
            res = self.calculate_efficiency(spatial_samples=30, angular_samples=300)
            results.append(res["final_eff"] * 100)
        setattr(target_obj, param_name, original_val)
        return values, results


# --- 3. 状态管理 (放入类中) ---
class State:
    def __init__(self):
        self.src_w = 1.0
        self.src_h = 1.0
        self.src_type: str = "rect"
        self.src_flux_single: float = 100.0
        self.arr_type: str = "hex_grid"
        self.pitch_x = 5.0
        self.pitch_y = 5.0
        self.lens_d = 5.5
        self.grid_rows = 3
        self.grid_cols = 3
        self.hex_rings = 1
        self.lens_dist = 1.0
        self.lens_count = 1
        self.coating = 0.995
        self.fiber_d = 5.0
        self.fiber_na = 0.62
        self.last_result: Optional[Dict] = None


# --- 4. 主类封装 ---
class MicrolensCalculator:
    def __init__(self):
        self.state = State()

        # UI 组件引用
        self.preview_plot: Optional[ui.pyplot] = None
        self.result_plot: Optional[ui.pyplot] = None

        # UI Labels 引用
        self.ui_refs: Dict[str, Optional[ui.label]] = {
            "final_eff": None,
            "final_flux": None,
            "factor_geo": None,
            "factor_trans": None,
            "factor_etendue": None,
            "geo_detail": None,
            "etendue_val": None,
            "etendue_limit": None,
            "unit_count": None,
            "src_luminance": None,
            "out_luminance": None,
        }

    def set_text_safe(self, ref_key: str, text: str, color_class: Optional[str] = None):
        lbl = self.ui_refs.get(ref_key)
        if lbl is not None:
            lbl.set_text(text)
            if color_class:
                lbl.classes(
                    remove="text-red-600 text-orange-500 text-green-600 text-gray-400 text-slate-700", add=color_class
                )

    def draw_array_preview(self):
        if self.preview_plot is None:
            return

        # 这里使用 self.preview_plot 作为上下文，避免全局 plt 冲突
        with self.preview_plot:
            plt.clf()
            ax = plt.gca()
            ax.set_aspect("equal")

            centers = []
            if self.state.arr_type == "rect_grid":
                rows, cols = int(self.state.grid_rows), int(self.state.grid_cols)
                start_x = -(cols - 1) * self.state.pitch_x / 2
                start_y = -(rows - 1) * self.state.pitch_y / 2
                for r in range(rows):
                    for c in range(cols):
                        centers.append((start_x + c * self.state.pitch_x, start_y + r * self.state.pitch_y))
            else:
                centers = [(0, 0)]
                d = self.state.pitch_x
                dy = d * np.sqrt(3) / 2
                dx_offset = d / 2
                vectors = [(d, 0), (dx_offset, dy), (-dx_offset, dy), (-d, 0), (-dx_offset, -dy), (dx_offset, -dy)]
                seen = set([(0, 0)])
                queue = [(0, 0)]
                next_queue = []
                current_ring = 0
                rings = int(self.state.hex_rings)
                while current_ring < rings:
                    for cx, cy in queue:
                        for vx, vy in vectors:
                            nx, ny = round(cx + vx, 5), round(cy + vy, 5)
                            if (nx, ny) not in seen:
                                seen.add((nx, ny))
                                next_queue.append((nx, ny))
                                centers.append((nx, ny))
                    queue = next_queue
                    next_queue = []
                    current_ring += 1

            all_x = [c[0] for c in centers]
            all_y = [c[1] for c in centers]
            margin = self.state.pitch_x * 0.8
            ax.set_xlim(min(all_x) - margin, max(all_x) + margin)
            ax.set_ylim(min(all_y) - margin, max(all_y) + margin)

            led_w, led_h = self.state.src_w, (self.state.src_h if self.state.src_type == "rect" else self.state.src_w)
            lens_r = self.state.lens_d / 2.0

            for cx, cy in centers:
                verts = []
                if self.state.arr_type == "rect_grid":
                    verts = [
                        (cx - self.state.pitch_x / 2, cy - self.state.pitch_y / 2),
                        (cx + self.state.pitch_x / 2, cy - self.state.pitch_y / 2),
                        (cx + self.state.pitch_x / 2, cy + self.state.pitch_y / 2),
                        (cx - self.state.pitch_x / 2, cy + self.state.pitch_y / 2),
                        (cx - self.state.pitch_x / 2, cy - self.state.pitch_y / 2),
                    ]
                else:
                    r_hex = self.state.pitch_x / np.sqrt(3)
                    for i in range(6):
                        angle_deg = 30 + 60 * i
                        angle_rad = np.deg2rad(angle_deg)
                        vx = cx + r_hex * np.cos(angle_rad)
                        vy = cy + r_hex * np.sin(angle_rad)
                        verts.append((vx, vy))
                    verts.append(verts[0])

                    poly = patches.Polygon(
                        verts, closed=True, fill=False, edgecolor="#cbd5e1", linestyle="--", linewidth=0.5
                    )
                    ax.add_patch(poly)

                if self.state.arr_type == "rect_grid":
                    rect_poly = patches.Polygon(
                        verts, closed=True, fill=False, edgecolor="#cbd5e1", linestyle="--", linewidth=0.5
                    )
                    ax.add_patch(rect_poly)

                lens_circle = patches.Circle(
                    (cx, cy), radius=lens_r, linewidth=1, edgecolor="#ef4444", facecolor="#fee2e2", alpha=0.5
                )
                ax.add_patch(lens_circle)

                valid_area = patches.Circle((cx, cy), radius=lens_r, facecolor="#3b82f6", alpha=0.8, linewidth=0)
                pitch_path = Path(verts)
                valid_area.set_clip_path(PathPatch(pitch_path, transform=ax.transData))
                ax.add_patch(valid_area)

                if self.state.src_type == "rect":
                    led = patches.Rectangle(
                        (cx - led_w / 2, cy - led_h / 2), led_w, led_h, linewidth=0, facecolor="#f59e0b"
                    )
                else:
                    led = patches.Circle((cx, cy), radius=led_w / 2, linewidth=0, facecolor="#f59e0b")
                ax.add_patch(led)

            from matplotlib.lines import Line2D

            custom_lines = [
                Line2D([0], [0], color="#3b82f6", lw=4, label="有效通光区域"),
                Line2D([0], [0], color="#fee2e2", lw=4, label="截断/重叠区域"),
                Line2D([0], [0], marker="s", color="w", markerfacecolor="#f59e0b", label="LED光源"),
            ]
            ax.legend(
                handles=custom_lines, loc="upper center", bbox_to_anchor=(0.5, 0.0), fontsize=8, frameon=False, ncol=3
            )
            ax.axis("off")

        self.preview_plot.update()

    def update_result_ui(self, res):
        eff = res["final_eff"] * 100
        color_class = "text-red-600" if eff < 10 else ("text-orange-500" if eff < 50 else "text-green-600")

        self.set_text_safe("final_eff", f"{eff:.2f}%", color_class)

        total_flux = self.state.src_flux_single * res["num_units"] * res["final_eff"]
        self.set_text_safe("final_flux", f"{total_flux:.1f} lm")

        geo_pct = res["geo_rate"] * 100
        self.set_text_safe("factor_geo", f"{geo_pct:.1f}%")
        self.set_text_safe("geo_detail", "几何填充 & 拦截")

        trans_pct = res["trans_rate"] * 100
        self.set_text_safe("factor_trans", f"{trans_pct:.1f}%")

        etd_factor = res["etendue_factor"] * 100
        e_color = "text-green-600" if etd_factor > 99 else "text-red-600"
        self.set_text_safe("factor_etendue", f"{etd_factor:.1f}%", e_color)

        self.set_text_safe("etendue_val", f"{res['G_collected']:.2f} mm²sr")
        self.set_text_safe("etendue_limit", f"{res['G_fiber']:.2f} mm²sr")
        self.set_text_safe("unit_count", f"单元数 N = {res['num_units']}")

        self.set_text_safe("src_luminance", f"{res['src_luminance']:.2f} cd/mm²")
        self.set_text_safe("out_luminance", f"{res['out_luminance']:.2f} cd/mm²")

    def run_auto_process(self):
        dims = (self.state.src_w, self.state.src_h) if self.state.src_type == "rect" else (self.state.src_w, 0)
        src_cfg = SourceConfig(cast(Literal["rect", "circle"], self.state.src_type), dims, self.state.src_flux_single)
        arr_cfg = ArrayConfig(
            cast(Literal["rect_grid", "hex_grid"], self.state.arr_type),
            self.state.pitch_x,
            self.state.pitch_y,
            int(self.state.grid_rows),
            int(self.state.grid_cols),
            int(self.state.hex_rings),
            self.state.lens_d,
        )
        sys_cfg = SystemConfig(self.state.lens_dist, int(self.state.lens_count), self.state.coating)
        rec_cfg = ReceiverConfig(self.state.fiber_d, self.state.fiber_na)

        calc = OpticalCouplingCalculator(src_cfg, arr_cfg, sys_cfg, rec_cfg)
        ui.notify("正在计算中...", type="info", position="bottom-right")

        res = calc.calculate_efficiency(spatial_samples=150, angular_samples=2500)
        self.state.last_result = res
        self.update_result_ui(res)

        if self.result_plot:
            with self.result_plot:
                plt.clf()
                fig = plt.gcf()
                fig.patch.set_facecolor("white")

                ax1 = fig.add_subplot(1, 2, 1)
                d_curr = self.state.lens_d
                x1, y1 = calc.get_sensitivity_data("lens_diameter", d_curr * 0.5, d_curr * 1.5, steps=15)
                ax1.plot(x1, y1, "o-", color="#2563eb", markersize=4)
                ax1.set_title("敏感度：透镜口径", fontsize=10)
                ax1.set_ylabel("总效率 %", fontsize=9)
                ax1.grid(True, linestyle="--", alpha=0.3)
                ax1.axvline(d_curr, color="red", linestyle="--", alpha=0.5)

                ax2 = fig.add_subplot(1, 2, 2)
                z_curr = self.state.lens_dist
                x2, y2 = calc.get_sensitivity_data("lens_distance", z_curr - 1.5, z_curr + 1.5, steps=15)
                ax2.plot(x2, y2, "o-", color="#f97316", markersize=4)
                ax2.set_title("敏感度：光源距离", fontsize=10)
                ax2.grid(True, linestyle="--", alpha=0.3)
                ax2.axvline(z_curr, color="red", linestyle="--", alpha=0.5)
                plt.tight_layout()
            self.result_plot.update()
            ui.notify("计算完成", type="positive", position="bottom-right")

    # --- 5. UI 构建 (Show) ---
    def show(self, dialog: ui.dialog):
        THEME_BG = "bg-slate-50"
        SIDEBAR_BG = "bg-white"
        CARD_STYLE = "bg-white p-3 rounded-xl shadow-sm border border-gray-100"

        # 使用 w-full h-full 铺满 Dialog
        with ui.card().classes("w-full h-full p-0 gap-0 border-none"):
            # 顶部 Header (模拟原版 Header)
            with ui.row().classes("w-full bg-slate-800 h-14 items-center shadow-md flex-none px-4 justify-between"):
                with ui.row().classes("items-center"):
                    ui.icon("hub", size="md").classes("text-blue-400")
                    ui.label("微透镜阵列透镜组光耦合效率计算").classes(
                        "text-lg font-bold tracking-wide text-white ml-2"
                    )
                # 关闭按钮
                ui.button(icon="close", on_click=dialog.close).props("flat dense round color=white")

            # 主体内容
            with ui.row().classes(f"w-full flex-1 {THEME_BG} gap-0 flex-nowrap overflow-hidden"):
                # 左侧栏
                with ui.column().classes(
                    f"w-80 flex-none {SIDEBAR_BG} h-full p-4 gap-4 border-r border-gray-200 flex flex-col"
                ):
                    # 滚动区域 (Inputs)
                    with ui.column().classes("w-full flex-grow overflow-y-auto pr-2 gap-4"):
                        with ui.card().classes("w-full p-3 bg-blue-50 border border-blue-100"):
                            ui.label("1. 阵列参数设置 (Array)").classes("font-bold text-blue-900 text-sm")
                            ui.select({"rect_grid": "矩形阵列", "hex_grid": "蜂窝阵列"}, label="阵列类型").bind_value(
                                self.state, "arr_type"
                            ).on_value_change(self.draw_array_preview).classes("w-full")

                            with ui.column().classes("w-full pt-1 gap-1"):
                                with (
                                    ui.row()
                                    .classes("w-full")
                                    .bind_visibility_from(self.state, "arr_type", lambda x: x == "rect_grid")
                                ):
                                    ui.number("行数 (Rows)", value=3).bind_value(
                                        self.state, "grid_rows"
                                    ).on_value_change(self.draw_array_preview).classes("w-1/3")
                                    ui.number("列数 (Cols)", value=3).bind_value(
                                        self.state, "grid_cols"
                                    ).on_value_change(self.draw_array_preview).classes("w-1/3")
                                ui.number("层数 (Rings)", value=1).bind_value(self.state, "hex_rings").on_value_change(
                                    self.draw_array_preview
                                ).bind_visibility_from(self.state, "arr_type", lambda x: x == "hex_grid").classes(
                                    "w-full"
                                )

                                ui.separator().classes("bg-blue-200 my-1")
                                with (
                                    ui.row()
                                    .classes("w-full")
                                    .bind_visibility_from(self.state, "arr_type", lambda x: x == "rect_grid")
                                ):
                                    ui.number("间距 X (Px)", step=0.1).bind_value(
                                        self.state, "pitch_x"
                                    ).on_value_change(self.draw_array_preview).props("suffix=mm").classes("w-1/2 pr-1")
                                    ui.number("间距 Y (Py)", step=0.1).bind_value(
                                        self.state, "pitch_y"
                                    ).on_value_change(self.draw_array_preview).props("suffix=mm").classes("w-1/2 pl-1")

                                ui.number("单元间距 (Pitch)", step=0.1).bind_value(
                                    self.state, "pitch_x"
                                ).on_value_change(self.draw_array_preview).bind_visibility_from(
                                    self.state, "arr_type", lambda x: x == "hex_grid"
                                ).props("suffix=mm").classes("w-full")
                                ui.number("透镜口径 (Dia)", step=0.1).bind_value(self.state, "lens_d").on_value_change(
                                    self.draw_array_preview
                                ).props("suffix=mm").classes("w-full bg-yellow-50")

                        with ui.column().classes("w-full gap-2"):
                            ui.label("2. 光源与接收端配置").classes("font-bold text-slate-700 text-sm")
                            ui.number("单颗LED光通量", value=120.0).bind_value(self.state, "src_flux_single").props(
                                "suffix=lm dense outlined"
                            ).classes("w-full")
                            ui.select(
                                {"rect": "矩形芯片", "circle": "圆形芯片"}, value="rect", label="光源形状"
                            ).bind_value(self.state, "src_type").on_value_change(self.draw_array_preview).classes(
                                "w-full"
                            )
                            with ui.row().classes("w-full"):
                                ui.number("尺寸/宽 (W)", value=1.0).bind_value(self.state, "src_w").on_value_change(
                                    self.draw_array_preview
                                ).classes("w-1/2")
                                ui.number("尺寸/高 (H)", value=1.0).bind_value(
                                    self.state, "src_h"
                                ).bind_visibility_from(self.state, "src_type", lambda x: x == "rect").classes("w-1/3")

                            ui.separator()
                            ui.number("光源距离 (Z)", value=2.0).bind_value(self.state, "lens_dist").props(
                                "suffix=mm dense"
                            ).classes("w-full")

                            ui.label("接收端配置 (Receiver)").classes("text-xs text-gray-500 font-bold")
                            with ui.row().classes("w-full"):
                                ui.number("光纤直径 (Dia)", value=8.0).bind_value(self.state, "fiber_d").props(
                                    "suffix=mm dense"
                                ).classes("w-1/2")
                                ui.number("数值孔径 (NA)", value=0.6).bind_value(self.state, "fiber_na").props(
                                    "dense"
                                ).classes("w-1/3")

                        ui.button("开始计算", on_click=self.run_auto_process).props("icon=play_circle").classes(
                            "w-full flex-none h-12 bg-blue-600 text-white shadow-lg text-lg mb-2"
                        )

                # 右侧：结果展示区
                with ui.column().classes("flex-grow h-full overflow-y-auto p-4 gap-4"):
                    with ui.row().classes("w-full gap-4 items-stretch min-h-[350px]"):
                        with ui.card().classes(f"{CARD_STYLE} w-4/12 flex flex-col items-center justify-center"):
                            ui.label("阵列排布与有效孔径").classes("text-xs font-bold text-gray-400 mb-2")
                            self.preview_plot = ui.pyplot(figsize=(4, 4), close=False)
                            with self.preview_plot:
                                plt.axis("off")

                        with ui.column().classes("w-7/12 gap-3"):
                            with ui.row().classes("w-full gap-3"):
                                with ui.column().classes(
                                    f"{CARD_STYLE} flex-1 border-l-4 border-blue-500 bg-blue-50 justify-center"
                                ):
                                    ui.label("总耦合效率").classes("text-xs font-bold text-blue-400 uppercase")
                                    self.ui_refs["final_eff"] = ui.label("0.00%").classes(
                                        "text-4xl font-black text-slate-700"
                                    )

                                with ui.column().classes(
                                    f"{CARD_STYLE} flex-1 border-l-4 border-orange-400 bg-orange-50 justify-center"
                                ):
                                    ui.label("光纤总输入流明").classes("text-xs font-bold text-orange-400 uppercase")
                                    self.ui_refs["final_flux"] = ui.label("0.0 lm").classes(
                                        "text-3xl font-black text-slate-700"
                                    )
                                    self.ui_refs["unit_count"] = ui.label("单元数 N=0").classes(
                                        "text-xs text-orange-400 font-bold"
                                    )

                            ui.label("详细分项损耗分析").classes("text-lg font-bold text-gray-600 mt-2")

                            with ui.row().classes("w-full gap-2 mb-2"):
                                with ui.column().classes(
                                    f"{CARD_STYLE} w-full items-center bg-purple-50 border-purple-200"
                                ):
                                    ui.label("4. 亮度 (Luminance) 对比 [cd/mm²]").classes(
                                        "text-sm text-purple-800 font-bold"
                                    )
                                    with ui.row().classes("gap-8 items-center"):
                                        with ui.column().classes("items-center"):
                                            ui.label("光源亮度").classes("text-xs text-gray-400")
                                            self.ui_refs["src_luminance"] = ui.label("-").classes(
                                                "text-xl font-bold text-purple-600"
                                            )
                                        ui.icon("arrow_forward", size="sm").classes("text-gray-300")
                                        with ui.column().classes("items-center"):
                                            ui.label("光纤输入亮度 (平均)").classes("text-xs text-gray-400")
                                            self.ui_refs["out_luminance"] = ui.label("-").classes(
                                                "text-xl font-bold text-purple-600"
                                            )

                            with ui.row().classes("w-full gap-2"):
                                with ui.column().classes(f"{CARD_STYLE} flex-1 items-center"):
                                    ui.label("1. 几何拦截效率").classes("text-base text-gray-500 font-bold")
                                    self.ui_refs["factor_geo"] = ui.label("- %").classes(
                                        "text-3xl font-black text-slate-700"
                                    )
                                    self.ui_refs["geo_detail"] = ui.label("几何填充 & 拦截").classes(
                                        "text-xs text-gray-400 text-center"
                                    )

                                ui.label("×").classes("text-2xl text-gray-300 self-center")

                                with ui.column().classes(f"{CARD_STYLE} flex-1 items-center"):
                                    ui.label("2. 平均透过率").classes("text-base text-gray-500 font-bold")
                                    self.ui_refs["factor_trans"] = ui.label("- %").classes(
                                        "text-3xl font-black text-slate-700"
                                    )
                                    ui.label("镀膜+菲涅尔").classes("text-xs text-gray-400")

                                ui.label("×").classes("text-2xl text-gray-300 self-center")

                                with ui.column().classes(f"{CARD_STYLE} flex-1 items-center border border-gray-200"):
                                    ui.label("3. 光展量匹配系数").classes("text-base text-gray-500 font-bold")
                                    self.ui_refs["factor_etendue"] = ui.label("- %").classes("text-3xl font-black")

                                    with ui.row().classes("gap-2 text-xs text-gray-400"):
                                        with ui.column().classes("items-center"):
                                            ui.label("系统")
                                            self.ui_refs["etendue_val"] = ui.label("-").classes(
                                                "font-bold text-gray-600"
                                            )
                                        ui.label("/")
                                        with ui.column().classes("items-center"):
                                            ui.label("光纤")
                                            self.ui_refs["etendue_limit"] = ui.label("-").classes(
                                                "font-bold text-gray-600"
                                            )

                    with ui.card().classes(f"{CARD_STYLE} w-full min-h-[300px] flex flex-col"):
                        ui.label("参数敏感度趋势").classes("text-sm font-bold text-gray-400 mb-1")
                        self.result_plot = ui.pyplot(figsize=(10, 3), close=False)
                        with self.result_plot:
                            plt.text(0.5, 0.5, "等待计算中...", ha="center", color="#cbd5e1", fontsize=14)
                            plt.axis("off")

        # 首次绘制预览
        self.draw_array_preview()
