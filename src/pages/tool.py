# -*- encoding: utf-8 -*-
import copy
import json
import logging
import os
from datetime import datetime

from nicegui import app, ui

from src.tools.etendue_calculator import EtendueCalculator
from src.tools.material_matcher import MaterialMatcherTool
from src.tools.microlens_calculator import MicrolensCalculator
from src.tools.mode_calculator import ModeCalculator
from src.tools.simple_coupling_calculator import SimpleCouplingCalculator
from src.tools.spherical_lens_calculator import SphericalLensCalculator

from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR, REQ_REMOVE_DIR
from ..utils import (
    get_cache_busted_path,
    logout,
    setup_global_activity_tracking,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


@ui.page("/tool")
def tool_page():
    # --- 全局注入 MathJax (为球面透镜工具服务) ---
    ui.add_head_html("""
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
    """)
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    # dialog = ui.dialog().props("persistent").classes("")

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    # 获取用户信息
    current_user = app.storage.user.get("current_user")
    current_role = app.storage.user.get("current_role")

    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

    #  定义工具元数据 (静态配置与运行时逻辑分离)
    #    key: 对应 JSON 文件中的键
    #    cls: 对应的 Python 类
    tool_definitions = [
        {
            "key": "etendue_calc",
            "title": "光学扩展量极限计算",
            "subtitle": "光源/光纤耦合效率估算",
            "icon": "flare",
            "color": "blue",
            "cls": EtendueCalculator,
        },
        {
            "key": "simple_coupling_calc",
            "title": "简单透镜组耦合效率",
            "subtitle": "单路透镜耦合效率仿真",
            "icon": "camera",
            "color": "indigo",
            "cls": SimpleCouplingCalculator,
        },
        {
            "key": "microlens_calc",
            "title": "复眼透镜耦合效率",
            "subtitle": "复眼透镜效率仿真",
            "icon": "hive",
            "color": "orange",
            "cls": MicrolensCalculator,
        },
        {
            "key": "mode_calc",
            "title": "激光横模分析",
            "subtitle": "LP/HG 模式场分布仿真",
            "icon": "lens_blur",
            "color": "purple",
            "cls": ModeCalculator,
        },
        {
            "key": "spherical_calc",
            "title": "球面透镜面型分析",
            "subtitle": "牛顿环与矢高偏差模拟",
            "icon": "trip_origin",
            "color": "teal",
            "cls": SphericalLensCalculator,
        },
        {
            "key": "material_matcher",
            "title": "智能物料请购核算",
            "subtitle": "多BOM聚合与ERP库存匹配",
            "icon": "inventory_2",
            "color": "green",
            "cls": MaterialMatcherTool,
        },
    ]

    # --- 新增：加载权限配置的函数 ---
    def load_tool_permissions():
        """
        加载工具权限配置文件
        建议将路径放在 config.py 中统一管理，这里为了演示直接写路径
        """
        # 假设文件名为 tools_permission.json，位于项目根目录或指定配置目录
        permission_path = os.path.join(BASE_DIR, "tools_permission.json")

        try:
            if not os.path.exists(permission_path):
                logger.warning(f"权限配置文件未找到: {permission_path}，默认所有工具可见")
                return None  # 返回 None 表示不限制，或者你可以返回空字典 {} 表示全都不显示

            with open(permission_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取权限配置文件失败: {e}")
            return {}

    # --- 辅助函数：创建一致的卡片 ---
    def create_tool_card(title, subtitle, icon, color, click_handler):
        with (
            ui.card()
            .classes(
                # 修改点1: 去掉 h-48，改为 min-h-[12rem] (即最小高度48)，这样如果文字很长换行了，卡片会自动长高
                # 修改点2: p-4 增加内边距
                f"w-full min-h-[12rem] p-4 hover:shadow-xl hover:border-{color}-500 transition-all cursor-pointer flex flex-col items-center justify-center gap-3 border-2 border-transparent group"
            )
            .on("click", click_handler)
        ):
            ui.icon(icon, size="56px").classes(f"text-{color}-500 group-hover:scale-110 transition-transform")
            # 修改点3: 添加 text-center (文字居中) 和 leading-tight (行距紧凑)
            # break-words 确保长单词也能换行（中文通常不需要，但保险起见）
            ui.label(title).classes("font-bold text-xl text-gray-700 text-center leading-tight break-words")

            ui.label(subtitle).classes("text-sm text-gray-400 text-center leading-tight")

    # --- 通用 Dialog 打开器 ---
    def open_tool(ToolClass):
        # 创建全屏 Dialog
        dialog = ui.dialog().props("maximized transition-show=slide-up transition-hide=slide-down")
        # 【关键修复】: 当弹窗关闭时，从 DOM 中彻底删除该组件，释放内存和 ID 资源
        dialog.on("close", lambda: dialog.delete())
        with dialog:
            # 实例化工具并显示
            # 使用 try-catch 确保即使工具初始化出错，弹窗也能正常处理或报错
            try:
                tool_instance = ToolClass()
                tool_instance.show(dialog)
            except Exception as e:
                ui.notify(f"工具加载失败: {str(e)}", type="negative")
                logger.error(f"工具加载失败: {e}", exc_info=True)
        dialog.open()

    # 加载权限配置
    permissions_config = load_tool_permissions()
    # 主界面
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("分析工具").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_display_path)
            with ui.menu().props("auto-close"):
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())

    # --- 2. 页面主体内容 ---
    # --- 核心修改：动态渲染网格 ---
    with ui.grid().classes("w-full gap-6 grid-cols-2 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-8"):
        visible_tools_count = 0

        for tool in tool_definitions:
            tool_key = tool["key"]

            # --- 权限判断逻辑 ---
            is_visible = False

            # 情况1: 如果配置文件不存在(permissions_config is None)，默认全部显示(或全部隐藏，看你策略)
            if permissions_config is None:
                is_visible = True
            else:
                # 情况2: 读取配置
                allowed_roles = permissions_config.get(tool_key, [])
                if current_role in allowed_roles:
                    is_visible = True

            # --- 渲染卡片 ---
            if is_visible:
                visible_tools_count += 1
                # 这里的 lambda 需要注意闭包捕获问题，使用 default argument (cls=tool["cls"]) 锁定变量
                create_tool_card(
                    tool["title"],
                    tool["subtitle"],
                    tool["icon"],
                    tool["color"],
                    lambda cls=tool["cls"]: open_tool(cls),
                )

        # 如果没有任何权限，显示友好提示
        if visible_tools_count == 0:
            with ui.column().classes("col-span-8 items-center justify-center mt-10"):
                ui.icon("lock", size="4em").classes("text-gray-300")
                ui.label("暂无可用工具的访问权限").classes("text-gray-400 text-lg")
