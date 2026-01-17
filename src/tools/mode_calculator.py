# src/tools/mode_calculator.py
import asyncio
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle, Rectangle
from mpl_toolkits.axes_grid1 import make_axes_locatable
from nicegui import run, ui
from scipy.optimize import brentq
from scipy.special import hermite, jn, jn_zeros, kve

# ================= 1. 数学核心 (保持在类外) =================


def characteristic_eq_scaled(U, V, l_idx):
    """圆形光纤特征方程"""
    if U >= V or U <= 0:
        return 0.0
    W = np.sqrt(V**2 - U**2)
    val = U * jn(l_idx + 1, U) * kve(l_idx, W) - W * kve(l_idx + 1, W) * jn(l_idx, U)
    return val


def find_modes_math(l_idx, V):
    """求解圆形光纤导模"""
    modes = []
    max_n = int(V / np.pi) + 10
    try:
        zeros = jn_zeros(l_idx, max_n)
        candidates = zeros[zeros < V]
        for guess in candidates:
            delta = 0.5
            search_min = max(1e-4, guess - delta)
            search_max = min(V - 1e-4, guess + delta)
            root = None
            try:
                val_min = characteristic_eq_scaled(search_min, V, l_idx)
                val_max = characteristic_eq_scaled(search_max, V, l_idx)
                if val_min * val_max < 0:
                    root = brentq(characteristic_eq_scaled, search_min, search_max, args=(V, l_idx))
            except Exception:
                pass
            if root is None:
                root = guess
            modes.append(root)
    except Exception:
        return []
    return sorted(modes)


def compute_lp_field(l_idx, U, V, core_dia, size):
    """计算圆形 LP 模式场分布"""
    r_max = core_dia * 0.8
    x = np.linspace(-r_max, r_max, size)
    X, Y = np.meshgrid(x, x)
    R = np.sqrt(X**2 + Y**2)
    Phi = np.arctan2(Y, X)

    a = core_dia / 2
    W = np.sqrt(V**2 - U**2)
    E = np.zeros_like(R)

    mask_core = R < a
    E[mask_core] = jn(l_idx, U * R[mask_core] / a) * np.cos(l_idx * Phi[mask_core])

    mask_clad = ~mask_core
    val_at_boundary = jn(l_idx, U)
    k_at_boundary = kve(l_idx, W)
    scaling = val_at_boundary / k_at_boundary if k_at_boundary != 0 else 0

    r_clad_norm = W * R[mask_clad] / a
    E[mask_clad] = scaling * kve(l_idx, r_clad_norm) * np.cos(l_idx * Phi[mask_clad])

    max_e = np.max(np.abs(E))
    if max_e > 0:
        E /= max_e

    return X, Y, E**2, x


def compute_hg_field(m, n, waist, size):
    """计算方形 Hermite-Gaussian (HG) 模式"""
    w0 = waist / 2
    limit = waist * 1.5
    x_vec = np.linspace(-limit, limit, size)
    X, Y = np.meshgrid(x_vec, x_vec)

    sqrt2_w = np.sqrt(2) / w0
    xi = sqrt2_w * X
    yi = sqrt2_w * Y

    Hm = hermite(m)(xi)
    Hn = hermite(n)(yi)
    gaussian = np.exp(-(X**2 + Y**2) / w0**2)

    E = Hm * Hn * gaussian
    Intensity = E**2
    max_val = np.max(Intensity)
    if max_val > 0:
        Intensity /= max_val

    return X, Y, Intensity, x_vec


# ================= 2. 类封装 =================


class ModeCalculator:
    def __init__(self):
        # 状态变量初始化
        self.state: Dict[str, Any] = {
            "roots": [],
            "V": 0.0,
            "cached_data": None,
            "last_calc_hash": None,
            "is_plotting": False,
            "meta": {},
            "mode_type": "LP",
        }

        self.params: Dict[str, Any] = {
            "wl": 0.785,
            "dia": 200.0,
            "na_fib": 0.22,
            "na_src": 0.20,
            "l_idx": 0,
            "m_lp": 1,
            "m_hg": 0,
            "n_hg": 0,
        }

        # UI 组件引用 (添加 Optional 类型标注以解决 Pylance 报错)
        self.plot_element: Optional[ui.matplotlib] = None
        self.input_x: Optional[ui.number] = None
        self.input_y: Optional[ui.number] = None
        self.lbl_v: Optional[ui.label] = None
        self.sel_m_lp: Optional[ui.select] = None
        self.inp_l: Optional[ui.number] = None
        self.slider_res: Optional[ui.slider] = None
        self.select_cmap: Optional[ui.select] = None
        self.btn_refresh_plot: Optional[ui.button] = None
        self.progress: Optional[ui.linear_progress] = None

        # 容器引用
        self.container_lp_params: Optional[ui.column] = None
        self.container_lp_ctrl: Optional[ui.column] = None
        self.container_hg_ctrl: Optional[ui.column] = None
        self.input_dia: Optional[ui.number] = None
        self.btn_calc_lp: Optional[ui.button] = None

    def update_visibility(self):
        """根据模式类型切换 UI 显示"""
        # 安全检查：如果 UI 还没创建好，直接返回
        if (
            not self.container_lp_params
            or not self.container_lp_ctrl
            or not self.container_hg_ctrl
            or not self.input_dia
        ):
            return

        is_lp = self.state["mode_type"] == "LP"
        self.container_lp_params.set_visibility(is_lp)
        self.container_lp_ctrl.set_visibility(is_lp)
        self.container_hg_ctrl.set_visibility(not is_lp)

        if is_lp:
            self.input_dia.props('label="芯径 (Core Dia)"')
        else:
            self.input_dia.props('label="束腰/宽度 (Width)"')

    async def run_calc_lp(self):
        """LP 模式计算逻辑"""
        if self.btn_calc_lp:
            self.btn_calc_lp.disable()
        if self.progress:
            self.progress.set_visibility(True)
        try:
            k0 = 2 * np.pi / self.params["wl"]
            a = self.params["dia"] / 2
            V = k0 * a * self.params["na_fib"]
            self.state["V"] = V
            if self.lbl_v:
                self.lbl_v.set_text(f"{V:.2f}")

            # 这里的 self.inp_l 肯定是存在的，但为了严谨加个默认值处理
            if self.inp_l and self.inp_l.value is not None:
                l_val = int(self.inp_l.value)
            else:
                l_val = 0

            # 限制剖面坐标范围
            limit = self.params["dia"] * 0.8
            if self.input_x:
                self.input_x.min, self.input_x.max = -limit, limit
                self.input_x.update()
            if self.input_y:
                self.input_y.min, self.input_y.max = -limit, limit
                self.input_y.update()

            roots = await run.io_bound(find_modes_math, l_val, V)
            self.state["roots"] = roots

            if self.sel_m_lp:
                if not roots:
                    self.sel_m_lp.options = {}
                    self.sel_m_lp.value = None
                    ui.notify("无导模 (Cutoff)", type="warning")
                else:
                    count = len(roots)
                    self.sel_m_lp.options = {i + 1: f"m={i + 1}" for i in range(count)}
                    self.sel_m_lp.update()
                    self.sel_m_lp.value = 1
                    ui.notify(f"LP模式计算完成: 找到 {count} 个", type="positive")
                    await self.update_plot()
        except Exception as e:
            ui.notify(f"Error: {e}", type="negative")
        finally:
            if self.progress:
                self.progress.set_visibility(False)
            if self.btn_calc_lp:
                self.btn_calc_lp.enable()

    async def run_calc_hg(self):
        """HG 模式触发逻辑"""
        limit = self.params["dia"] * 1.5
        if self.input_x:
            self.input_x.min, self.input_x.max = -limit, limit
            self.input_x.update()
        if self.input_y:
            self.input_y.min, self.input_y.max = -limit, limit
            self.input_y.update()

        ui.notify(
            f"HG({self.params['m_hg']}, {self.params['n_hg']}) 模式已生成",
            type="positive",
            color="purple",
        )
        await self.update_plot()

    async def prepare_data_cache(self):
        dia = self.params["dia"]
        # 安全获取 slider 值
        res = self.slider_res.value if self.slider_res else 300
        m_type = self.state["mode_type"]

        if m_type == "LP":
            if not self.sel_m_lp or not self.sel_m_lp.value or not self.state["roots"]:
                return None
            try:
                m_idx = int(self.sel_m_lp.value) - 1
                l_val = int(self.inp_l.value) if (self.inp_l and self.inp_l.value is not None) else 0
            except Exception:
                return None

            if m_idx >= len(self.state["roots"]):
                m_idx = 0
            U = self.state["roots"][m_idx]
            V = self.state["V"]

            current_hash = ("LP", l_val, U, V, dia, res)
            if self.state["cached_data"] is None or self.state["last_calc_hash"] != current_hash:
                X, Y, II, axis = await run.io_bound(compute_lp_field, l_val, U, V, dia, res)
                self.state["cached_data"] = (X, Y, II, axis)
                self.state["last_calc_hash"] = current_hash
                req_na = self.params["na_fib"] * (U / V) if V != 0 else 0
                self.state["meta"] = {
                    "title": f"LP({l_val},{m_idx + 1})",
                    "color": "#22c55e" if req_na < self.params["na_src"] else "#ef4444",
                }

        elif m_type == "HG":
            m = int(self.params["m_hg"])
            n = int(self.params["n_hg"])
            current_hash = ("HG", m, n, dia, res)

            if self.state["cached_data"] is None or self.state["last_calc_hash"] != current_hash:
                X, Y, II, axis = await run.io_bound(compute_hg_field, m, n, dia, res)
                self.state["cached_data"] = (X, Y, II, axis)
                self.state["last_calc_hash"] = current_hash
                self.state["meta"] = {"title": f"HG({m},{n})", "color": "#9333ea"}

        return self.state["cached_data"], self.state["meta"]

    async def update_plot(self):
        if self.state["is_plotting"]:
            return
        self.state["is_plotting"] = True
        if self.btn_refresh_plot:
            self.btn_refresh_plot.props("loading")

        try:
            # 确保 UI 元素存在
            if not self.input_x or not self.input_y or not self.select_cmap or not self.plot_element:
                return

            res_data = await self.prepare_data_cache()
            if not res_data:
                return
            (X, Y, II, axis), meta = res_data

            cx = self.input_x.value if self.input_x.value is not None else 0.0
            cy = self.input_y.value if self.input_y.value is not None else 0.0
            cmap = self.select_cmap.value

            idx_x = (np.abs(axis - cx)).argmin()
            idx_y = (np.abs(axis - cy)).argmin()
            prof_x = II[idx_y, :]
            prof_y = II[:, idx_x]
            limit = float(axis[-1])

            with self.plot_element:
                self.plot_element.figure.clear()
                ax_main = self.plot_element.figure.add_subplot(111)
                divider = make_axes_locatable(ax_main)
                ax_bot = divider.append_axes("bottom", size="20%", pad=0.2, sharex=ax_main)
                ax_right = divider.append_axes("right", size="20%", pad=0.5, sharey=ax_main)

                extent_tuple = (-limit, limit, -limit, limit)
                ax_main.imshow(
                    II,
                    extent=extent_tuple,
                    origin="lower",
                    cmap=cmap,
                    interpolation="bicubic",
                )

                ax_main.axvline(cx, color="#fb8b05", linestyle="--", alpha=0.3, lw=0.5)
                ax_main.axhline(cy, color="#22c55e", linestyle="--", alpha=0.3, lw=0.5)

                if self.state["mode_type"] == "LP":
                    circ = Circle(
                        (0, 0),
                        self.params["dia"] / 2,
                        color="cyan",
                        linestyle=":",
                        fill=False,
                        lw=1,
                    )
                    ax_main.add_artist(circ)
                else:
                    w = self.params["dia"]
                    rect = Rectangle(
                        (-w / 2, -w / 2),
                        w,
                        w,
                        color="cyan",
                        linestyle=":",
                        fill=False,
                        lw=1,
                    )
                    ax_main.add_artist(rect)

                plt.setp(ax_main.get_xticklabels(), visible=False)
                plt.setp(ax_main.get_yticklabels(), visible=False)
                ax_main.set_title(meta["title"], color=meta["color"], fontweight="bold")

                ax_bot.plot(axis, prof_x, color="#22c55e", lw=1)
                ax_bot.fill_between(axis, prof_x, color="#22c55e", alpha=0.1)
                ax_bot.set_xlim(-limit, limit)
                ax_bot.grid(True, linestyle=":", alpha=0.5)
                ax_bot.spines["top"].set_visible(False)

                ax_right.plot(prof_y, axis, color="orange", lw=1)
                ax_right.fill_betweenx(axis, prof_y, color="orange", alpha=0.1)
                ax_right.set_ylim(-limit, limit)
                ax_right.grid(True, linestyle=":", alpha=0.5)
                ax_right.spines["left"].set_visible(False)
                plt.setp(ax_right.get_yticklabels(), visible=False)

                self.plot_element.update()

        except Exception as e:
            print(f"Plot Error: {e}")
            ui.notify(f"Plot Error: {e}")
        finally:
            self.state["is_plotting"] = False
            if self.btn_refresh_plot:
                self.btn_refresh_plot.props(remove="loading")

    def show(self, dialog: ui.dialog):
        """渲染 UI 到当前 Dialog"""
        with ui.column().classes("w-full h-full p-0 gap-0 bg-white"):
            # 顶部标题栏
            with ui.row().classes("w-full bg-slate-100 p-4 border-b items-center justify-between"):
                with ui.row().classes("items-center"):
                    ui.icon("lens_blur", size="32px").classes("text-blue-600")
                    ui.label("光纤/波束横模计算 (Fiber Modes)").classes("text-xl font-bold text-slate-800")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round")

            # 进度条
            self.progress = (
                ui.linear_progress().props("indeterminate").classes("absolute top-[64px] left-0 right-0 z-50")
            )
            self.progress.set_visibility(False)

            # 主内容区
            with ui.row().classes("w-full flex-1 p-4 gap-4 items-start no-wrap overflow-hidden"):
                # === 左侧控制面板 ===
                with ui.card().classes("w-80 flex-shrink-0 p-0 gap-0 shadow-lg h-full overflow-y-auto"):
                    with ui.tabs().classes("w-full text-gray-600 bg-white sticky top-0 z-10") as tabs:
                        tab_calc = ui.tab("参数计算", icon="settings")
                        tab_view = ui.tab("剖面分析", icon="analytics")

                    with ui.tab_panels(tabs, value=tab_calc).classes("w-full p-4 bg-white"):
                        # --- Panel 1: 计算设置 ---
                        with ui.tab_panel(tab_calc).classes("p-0 flex flex-col gap-2"):
                            ui.label("几何结构 / 模式类型").classes("text-xs font-bold text-gray-400")

                            mode_toggle = (
                                ui.toggle({"LP": "圆形光纤 (LP)", "HG": "方形波束 (HG)"}, value="LP")
                                .props("no-caps spread color=blue-7")
                                .classes("w-full border rounded")
                            )
                            mode_toggle.bind_value(self.state, "mode_type")
                            mode_toggle.on_value_change(self.update_visibility)

                            ui.separator()

                            ui.number("波长 (um)", value=0.785, step=0.01).bind_value(self.params, "wl").props(
                                "dense outlined"
                            )

                            self.input_dia = (
                                ui.number("芯径 / 宽度 (um)", value=200.0, step=1.0)
                                .bind_value(self.params, "dia")
                                .props("dense outlined")
                            )

                            # LP 模式独有
                            self.container_lp_params = ui.column().classes("w-full gap-2")
                            with self.container_lp_params:
                                ui.number("光纤 NA", value=0.22, step=0.01).bind_value(self.params, "na_fib").props(
                                    "dense outlined"
                                )
                                ui.number("光源 NA", value=0.20, step=0.01).bind_value(self.params, "na_src").props(
                                    "dense outlined"
                                )

                                with ui.row().classes("items-center justify-between bg-blue-50 p-2 rounded"):
                                    ui.label("V-Number:").classes("text-sm text-gray-600")
                                    self.lbl_v = ui.label("--").classes("font-mono font-bold text-blue-600")

                            ui.separator()

                            # LP 控制
                            self.container_lp_ctrl = ui.column().classes("w-full gap-2")
                            with self.container_lp_ctrl:
                                ui.label("圆形 LP(l, m) 指数").classes("text-xs font-bold text-gray-400")
                                self.inp_l = (
                                    ui.number("角向指数 (l)", value=0, min=0, step=1)
                                    .bind_value(self.params, "l_idx")
                                    .props("outlined dense")
                                )
                                self.sel_m_lp = (
                                    ui.select(options=[], label="径向指数 (m)")
                                    .props("outlined dense")
                                    .classes("w-full")
                                )
                                self.btn_calc_lp = ui.button(
                                    "计算 LP 模式", icon="memory", on_click=self.run_calc_lp
                                ).classes("w-full bg-blue-600")

                            # HG 控制
                            self.container_hg_ctrl = ui.column().classes("w-full gap-2")
                            with self.container_hg_ctrl:
                                ui.label("方形 HG(m, n) 指数").classes("text-xs font-bold text-gray-400")
                                with ui.row().classes("w-full"):
                                    ui.number("水平指数 (m)", value=0, min=0, step=1).bind_value(
                                        self.params, "m_hg"
                                    ).props("outlined dense").classes("w-1/2")
                                    ui.number("垂直指数 (n)", value=0, min=0, step=1).bind_value(
                                        self.params, "n_hg"
                                    ).props("outlined dense").classes("w-1/2")
                                btn_calc_hg = ui.button(
                                    "生成 HG 模式",
                                    icon="grid_view",
                                    on_click=self.run_calc_hg,
                                ).classes("w-full bg-purple-600")

                        # --- Panel 2: 视图 ---
                        with ui.tab_panel(tab_view).classes("p-0 flex flex-col gap-2"):
                            self.slider_res = ui.slider(min=100, max=600, step=50, value=300).props('label="分辨率"')
                            self.select_cmap = (
                                ui.select(
                                    ["inferno", "hot", "jet", "viridis", "gray"],
                                    value="inferno",
                                    label="配色",
                                )
                                .props("dense outlined")
                                .classes("w-full")
                            )

                            ui.separator()
                            ui.label("交互剖面 (坐标)").classes("text-xs font-bold text-gray-400")
                            with ui.row().classes("w-full gap-2"):
                                self.input_x = (
                                    ui.number(label="X (um)", value=0.0, step=1.0)
                                    .props("dense outlined input-class=text-orange-600")
                                    .classes("w-1/2")
                                )
                                self.input_y = (
                                    ui.number(label="Y (um)", value=0.0, step=1.0)
                                    .props("dense outlined input-class=text-green-600")
                                    .classes("w-1/2")
                                )

                            self.btn_refresh_plot = ui.button(
                                "更新视图",
                                icon="refresh",
                                on_click=lambda: asyncio.create_task(self.update_plot()),
                            ).classes("w-full bg-gray-800 text-white")

                            def reset_coords():
                                if self.input_x and self.input_y:
                                    self.input_x.set_value(0)
                                    self.input_y.set_value(0)
                                    asyncio.create_task(self.update_plot())

                            ui.button("归零坐标", icon="center_focus_strong", on_click=reset_coords).props(
                                "flat dense color=grey"
                            ).classes("w-full")

                # === 右侧绘图区 ===
                with ui.column().classes(
                    "flex-grow h-full bg-gray-50 rounded-xl shadow border border-gray-200 relative overflow-hidden"
                ):
                    self.plot_element = ui.matplotlib(figsize=(10, 8)).classes("w-full h-full")
                    with self.plot_element:
                        plt.text(
                            0.5,
                            0.5,
                            "请配置参数并点击计算",
                            ha="center",
                            va="center",
                            color="gray",
                        )
                        plt.axis("off")

        # --- 事件绑定与初始化 ---
        if self.sel_m_lp:
            self.sel_m_lp.on_value_change(lambda: asyncio.create_task(self.update_plot()))

        def refresh_view():
            if (self.state["mode_type"] == "LP" and self.sel_m_lp and self.sel_m_lp.value) or self.state[
                "mode_type"
            ] == "HG":
                asyncio.create_task(self.update_plot())

        if self.select_cmap:
            self.select_cmap.on_value_change(refresh_view)
        if self.slider_res:
            self.slider_res.on_value_change(refresh_view)

        # 初始化UI显示状态
        self.update_visibility()
