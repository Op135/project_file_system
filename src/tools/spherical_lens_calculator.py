# src/tools/spherical_lens_calculator.py
import asyncio
import base64
import io
from typing import Any, Dict, Optional, Tuple

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.figure import Figure
from matplotlib.patches import Circle, FancyArrowPatch
from nicegui import run, ui  # 引入 run 用于异步执行

# ================= 1. 核心计算与绘图 (提取到类外，供 run.io_bound 调用) =================


def calculate_physics_static(params: Dict[str, float]):
    """纯数学计算逻辑 (无 UI 依赖)"""
    R = params["radius"]
    D = params["diameter"]
    if D >= 2 * abs(R):
        return None

    R_des = abs(R)
    half_D = D / 2.0
    nominal_sag = R_des - np.sqrt(R_des**2 - half_D**2)

    calc_wave = 546.1 if params["wavelength"] == 0 else params["wavelength"]
    sag_error_mm = params["N"] * 0.5 * (calc_wave * 1e-6)

    actual_sag = nominal_sag + np.sign(R) * sag_error_mm

    if actual_sag <= 1e-9:
        actual_R = np.inf
    else:
        actual_R_abs = (actual_sag**2 + half_D**2) / (2 * actual_sag)
        actual_R = actual_R_abs * np.sign(R)

    radius_error = actual_R - R

    idx = params["refractive_index"]
    if abs(idx - 1.0) < 1e-4:
        f_err = np.inf
    else:
        nominal_f = R / (idx - 1)
        if np.isinf(actual_R):
            f_err = np.inf
        else:
            actual_f = actual_R / (idx - 1)
            f_err = actual_f - nominal_f

    return {
        "sag_err_mm": sag_error_mm,
        "sag_err_um": sag_error_mm * 1000.0,
        "R_err": radius_error,
        "R_act": actual_R,
        "f_err": f_err,
    }


def generate_plots_static(params: Dict[str, float], phys_res: Dict[str, float]) -> Tuple[str, str]:
    """
    静态绘图函数，生成两张图片的 Base64 字符串
    这样可以在后台进程中运行，不阻塞 UI
    """
    # --- 绘图 1: 干涉图 ---
    res = 300
    x = np.linspace(-1.0, 1.0, res)
    X, Y = np.meshgrid(x, x)
    Rho = np.sqrt(X**2 + Y**2)
    Theta = np.arctan2(Y, X)
    alpha = np.radians(params["angle"])

    opd_norm = params["N"] * Rho**2 + params["dN"] * Rho**2 * np.cos(2 * (Theta - alpha))
    decay_factor = np.exp(-0.2 * np.abs(opd_norm))

    def compute_channel(wave_nm):
        current_wl = params["wavelength"]
        ref_wave = 546.1 if current_wl == 0 else current_wl
        phase_shift = np.pi
        k_delta = (opd_norm * (ref_wave / wave_nm)) * 2 * np.pi
        interference = 1 + decay_factor * np.cos(k_delta + phase_shift)
        return interference / 2.0

    fig1 = Figure(figsize=(5, 5), facecolor="black")
    FigureCanvasAgg(fig1)
    ax1 = fig1.add_axes((0.0, 0.0, 1.0, 1.0))
    ax1.axis("off")

    if params["wavelength"] == 0:
        I_r = compute_channel(635.0)
        I_g = compute_channel(530.0)
        I_b = compute_channel(460.0)
        img_data = np.dstack((I_r, I_g, I_b))
        im = ax1.imshow(img_data, extent=(-1.0, 1.0, -1.0, 1.0), origin="lower", interpolation="bicubic")
    else:
        I_mono = compute_channel(params["wavelength"])
        colors = ["black", "#00ff00"] if params["wavelength"] < 600 else ["black", "#ff0000"]
        laser_cmap = LinearSegmentedColormap.from_list("laser", colors, N=256)
        im = ax1.imshow(
            I_mono,
            extent=(-1.0, 1.0, -1.0, 1.0),
            cmap=laser_cmap,
            vmin=0,
            vmax=1,
            origin="lower",
            interpolation="bicubic",
        )

    patch = Circle((0, 0), radius=1.0, transform=ax1.transData)
    im.set_clip_path(patch)

    buf1 = io.BytesIO()
    fig1.savefig(buf1, format="png", facecolor="black")
    b64_interfero = f"data:image/png;base64,{base64.b64encode(buf1.getvalue()).decode('utf-8')}"

    # 清理内存
    fig1.clf()
    del fig1

    # --- 绘图 2: 剖面图 ---
    R_act = phys_res["R_act"]
    sag_err_mm = phys_res["sag_err_mm"]
    sag_err_um = sag_err_mm * 1000.0
    D = params["diameter"]
    R_des = params["radius"]

    x_prof = np.linspace(-D / 2.0 + 1e-6, D / 2.0 - 1e-6, 200)

    def get_sag_curve(r_val, x_vals):
        if np.isinf(r_val):
            return np.zeros_like(x_vals)
        r_abs = abs(r_val)
        sag = np.sqrt(r_abs**2 - x_vals**2)
        if r_val > 0:
            return sag - r_abs
        else:
            return r_abs - sag

    y_des = get_sag_curve(R_des, x_prof)
    y_act_raw = get_sag_curve(R_act, x_prof)

    edge_idx = 0
    edge_offset = y_act_raw[edge_idx] - y_des[edge_idx]
    y_act_aligned = y_act_raw - edge_offset

    deviation_phys = y_act_aligned - y_des
    max_dev_phys = np.max(np.abs(deviation_phys))

    sag_depth = abs(y_des.min() if R_des > 0 else y_des.max())
    visual_limit_height = max(sag_depth * 0.4, 0.2)
    BASE_SCALE = 3000.0
    linear_visual_height = max_dev_phys * BASE_SCALE

    soft_visual_height = (
        visual_limit_height * np.tanh(linear_visual_height / visual_limit_height) if visual_limit_height > 1e-6 else 0
    )
    final_scale = soft_visual_height / max_dev_phys if max_dev_phys > 1e-9 else BASE_SCALE

    y_act_plot = y_des + deviation_phys * final_scale

    fig2 = Figure(figsize=(6, 3), facecolor="#1f2937")
    FigureCanvasAgg(fig2)

    ax2 = fig2.add_subplot(111)
    ax2.set_facecolor("#111827")

    ax2.plot(x_prof, y_des, color="gray", linestyle="--", linewidth=1, label="设计基准", zorder=5)
    line_color = "#3b82f6" if sag_err_um > 0 else "#ef4444"
    ax2.plot(x_prof, y_act_plot, color=line_color, linewidth=2, label="实际面型", zorder=6)
    ax2.fill_between(x_prof, y_des, y_act_plot, color=line_color, alpha=0.3, zorder=4)

    if abs(sag_err_um) > 0.01:
        mid_idx = len(x_prof) // 2
        y_start = y_des[mid_idx]
        y_end = y_act_plot[mid_idx]
        arrow = FancyArrowPatch(
            (0, y_start), (0, y_end), arrowstyle="<|-|>", mutation_scale=15, color="white", linewidth=1.5, zorder=10
        )
        ax2.add_patch(arrow)

        ax2.text(
            D * 0.08,
            (y_start + y_end) / 2,
            f"{sag_err_um:+.2f} μm",
            color="white",
            fontsize=11,
            fontweight="bold",
            va="center",
            ha="left",
            zorder=10,
            bbox=dict(facecolor="#1f2937", edgecolor=line_color, boxstyle="round,pad=0.3", alpha=0.9),
        )

    ax2.text(
        0.02,
        0.05,
        f"* 视觉倍率: {int(final_scale)}x",
        transform=ax2.transAxes,
        color="gray",
        fontsize=8,
        fontstyle="italic",
    )
    ax2.axis("off")

    y_all = np.concatenate([y_des, y_act_plot])
    margin = (y_all.max() - y_all.min()) * 0.2 + 0.1
    ax2.set_ylim(y_all.min() - margin, y_all.max() + margin)

    fig2.tight_layout()
    buf2 = io.BytesIO()
    fig2.savefig(buf2, format="png", facecolor="#1f2937")
    b64_profile = f"data:image/png;base64,{base64.b64encode(buf2.getvalue()).decode('utf-8')}"

    fig2.clf()
    del fig2

    return b64_interfero, b64_profile


# ================= 2. 类封装 =================


class SphericalLensCalculator:
    def __init__(self):
        # 1. 核心参数状态
        self.params: Dict[str, float] = {
            "radius": 100.0,
            "diameter": 50.0,
            "wavelength": 632.8,
            "refractive_index": 1.5168,
            "N": 2.0,
            "dN": 0.2,
            "angle": 0.0,
        }
        self.calculating = False  # 防止重复提交

        # 2. UI 组件引用
        self.inp_r: Optional[ui.number] = None
        self.inp_d: Optional[ui.number] = None
        self.inp_n: Optional[ui.number] = None
        self.sel_w: Optional[ui.select] = None
        self.inp_N: Optional[ui.number] = None
        self.inp_dN: Optional[ui.number] = None
        self.inp_ang: Optional[ui.number] = None

        self.img_interfero: Optional[ui.image] = None
        self.img_profile: Optional[ui.image] = None
        self.status_label: Optional[ui.label] = None
        self.desc_label: Optional[ui.label] = None

        self.lbl_r_err: Optional[ui.label] = None
        self.lbl_r_act: Optional[ui.label] = None
        self.lbl_sag: Optional[ui.label] = None
        self.lbl_f_err: Optional[ui.label] = None

    # --- 异步 UI 更新逻辑 ---
    # 【关键修改】添加 e=None，以便能够作为事件回调（event callback）使用
    async def update_interface(self, e=None):
        # 如果正在计算中，可以跳过
        if self.calculating:
            return

        if not self.inp_r or not self.inp_d or not self.inp_n or not self.sel_w:
            return
        if not self.inp_N or not self.inp_dN or not self.inp_ang:
            return

        try:
            # 更新参数
            self.params["radius"] = float(self.inp_r.value or -100.0)
            self.params["diameter"] = float(self.inp_d.value or 50.0)
            self.params["refractive_index"] = float(self.inp_n.value or 1.5168)
            self.params["wavelength"] = float(self.sel_w.value if self.sel_w.value is not None else 632.8)
            self.params["N"] = float(self.inp_N.value or 0.0)
            self.params["dN"] = float(self.inp_dN.value or 0.0)
            self.params["angle"] = float(self.inp_ang.value or 0.0)

            self.calculating = True
            if self.status_label:
                self.status_label.set_text("计算中...")

            # 1. 异步计算物理数据
            res = await run.io_bound(calculate_physics_static, self.params.copy())

            if (
                not self.lbl_r_err
                or not self.lbl_r_act
                or not self.lbl_sag
                or not self.lbl_f_err
                or not self.desc_label
            ):
                self.calculating = False
                return

            if res is None:
                self.lbl_r_err.set_text("错误: 口径 > 2倍半径")
                if self.status_label:
                    self.status_label.set_text("参数错误")
                self.calculating = False
                return

            # 更新文字 UI
            sign_r = "+" if res["R_err"] > 0 else ""
            self.lbl_r_err.set_text(f"{sign_r}{res['R_err']:.4f} mm")
            self.lbl_r_act.set_text("平面" if np.isinf(res["R_act"]) else f"{res['R_act']:.4f} mm")
            sign_s = "+" if res["sag_err_um"] > 0 else ""
            self.lbl_sag.set_text(f"{sign_s}{res['sag_err_um']:.3f} μm")
            self.lbl_f_err.set_text("无穷" if np.isinf(res["f_err"]) else f"{res['f_err']:.3f} mm")

            is_convex = self.params["radius"] > 0
            if res["sag_err_um"] > 0:
                status = "中心偏高 (凸起)"
                color_cls = "text-blue-400"
            else:
                status = "中心偏低 (凹陷)"
                color_cls = "text-red-400"

            self.desc_label.set_text(f"{'凸透镜' if is_convex else '凹透镜'} | {status}")
            self.desc_label.classes(remove="text-gray-400 text-blue-400 text-red-400", add=color_cls)

            # 2. 异步生成图片 (最耗时的部分)
            # 使用 io_bound 防止阻塞主线程
            b64_interfero, b64_profile = await run.io_bound(generate_plots_static, self.params.copy(), res)

            if self.img_interfero:
                self.img_interfero.set_source(b64_interfero)
            if self.img_profile:
                self.img_profile.set_source(b64_profile)
            if self.status_label:
                self.status_label.set_text("图表已更新")

        except Exception as e:
            if self.status_label:
                self.status_label.set_text(f"Err: {e}")
        finally:
            self.calculating = False

    # --- 主显示入口 ---
    def show(self, dialog: ui.dialog):
        with ui.card().classes("w-full h-full p-0 gap-0 bg-gray-900 border-none"):
            with ui.row().classes("w-full bg-gray-800 p-4 border-b border-gray-700 items-center justify-between"):
                with ui.row().classes("items-center"):
                    ui.icon("lens", size="32px").classes("text-blue-400")
                    ui.label("球面透镜面型偏差与牛顿环模拟").classes("text-xl font-bold text-gray-100")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round color=white")

            with ui.row().classes("w-full flex-1 p-4 gap-4 no-wrap items-stretch overflow-hidden"):
                # --- [左] 参数 ---
                with ui.column().classes(
                    "flex-1 min-w-[300px] bg-gray-800 border border-gray-700 h-full overflow-y-auto p-4 rounded"
                ):
                    ui.markdown("### 🛠️ 参数设定").classes("text-blue-400 mt-0")

                    # 【修改】因为 update_interface 现在接受 e=None，所以可以直接绑定到 on_change
                    # NiceGUI 会自动处理参数传递和 async await

                    with ui.grid(columns=2).classes("w-full gap-3"):
                        self.inp_r = ui.number(
                            "半径 R (mm)", value=self.params["radius"], step=0.1, on_change=self.update_interface
                        ).props("dark outlined")
                        self.inp_d = ui.number(
                            "口径 D (mm)", value=self.params["diameter"], step=0.1, on_change=self.update_interface
                        ).props("dark outlined")
                        self.inp_n = ui.number(
                            "折射率 Nd",
                            value=self.params["refractive_index"],
                            step=0.0001,
                            on_change=self.update_interface,
                        ).props("dark outlined")
                        self.sel_w = ui.select(
                            {546.1: "546.1 nm (绿光)", 632.8: "632.8 nm (红光)", 0: "⚪ 白光"},
                            value=self.params["wavelength"],
                            label="检测波长",
                            on_change=self.update_interface,
                        ).props("dark outlined")

                    ui.separator().classes("my-4 bg-gray-600")
                    ui.markdown("### 📉 误差模拟").classes("text-blue-400")

                    self.inp_N = (
                        ui.number(
                            "光圈数 N (Power)", value=self.params["N"], step=0.05, on_change=self.update_interface
                        )
                        .props("dark outlined")
                        .classes("w-full")
                    )

                    self.inp_dN = (
                        ui.number(
                            "局部光圈 ΔN (Irreg)", value=self.params["dN"], step=0.05, on_change=self.update_interface
                        )
                        .props("dark outlined input-class=text-red-300 label-color=red-300")
                        .classes("w-full")
                    )

                    self.inp_ang = (
                        ui.number(
                            "轴向角度 (Angle)", value=self.params["angle"], step=1, on_change=self.update_interface
                        )
                        .props("dark outlined input-class=text-orange-300 label-color=orange-300")
                        .classes("w-full")
                    )

                # --- [中] 图像 ---
                with ui.column().classes("flex-1 min-w-[380px] items-center h-full gap-4"):
                    with ui.card().classes(
                        "w-full aspect-square bg-black p-1 border-2 border-gray-600 flex items-center justify-center shrink-0 relative"
                    ):
                        ui.label("样板干涉图").classes("absolute top-1 left-2 text-xs text-gray-500 z-10")
                        self.img_interfero = ui.image().classes("w-full h-full object-contain")

                    with ui.card().classes(
                        "w-full grow bg-gray-800 border border-gray-700 flex items-center justify-center p-0 overflow-hidden relative"
                    ):
                        ui.label("面型偏差剖面").classes("absolute top-1 left-2 text-xs text-blue-300 z-10 font-bold")
                        self.img_profile = ui.image().classes("w-full h-full object-contain")

                    self.status_label = ui.label("").classes("text-xs text-gray-500")

                # --- [右] 数据 ---
                with ui.column().classes("flex-1 min-w-[280px] h-full gap-4"):
                    with ui.card().classes("w-full -space-y-4 bg-gray-800 border border-gray-700 p-4 rounded"):
                        ui.markdown("### 📊 实际工程误差").classes("text-green-400 mt-0")
                        self.desc_label = ui.label("").classes("text-xs text-gray-400 mb-2 italic")

                        def info_row(label, color="text-white"):
                            with ui.row().classes("w-full justify-between py-2 border-b border-gray-700"):
                                ui.label(label).classes("text-gray-400")
                                val = ui.label("--").classes(f"font-mono font-bold text-lg {color}")
                            return val

                        self.lbl_r_err = info_row("半径偏差 (ΔR)", "text-blue-300")
                        self.lbl_r_act = info_row("实际半径 (R_act)", "text-white")
                        self.lbl_sag = info_row("中心矢高差 (Sag)", "text-yellow-400")
                        self.lbl_f_err = info_row("焦距漂移 (Δf)", "text-green-400")

                    with ui.card().classes(
                        "w-full grow bg-gray-800 border border-gray-700 overflow-y-auto p-4 rounded"
                    ):
                        ui.markdown(r"""
                        <h3 class="text-purple-400 text-lg font-bold mt-0 mb-2">📐 物理依据 (Physics)</h3>
                        
                        **1. 矢高公式 (Sag Equation):**
                        $$z(r) = R - \sqrt{R^2 - r^2}$$
                        
                        **2. 光圈与误差:**
                        $$\Delta z \approx N \cdot \frac{\lambda}{2}$$
                        """).classes("text-gray-300 text-sm space-y-2")

        # 触发初始计算
        asyncio.create_task(self.update_interface())
