# -*- encoding: utf-8 -*-
import logging
from datetime import datetime

from nicegui import app, events, ui

from .. import db_storage  # 导入我们创建的模块
from ..config import (
    BASE_DIR,
    IMG_DIR,
)
from ..overview_operation import get_latest_overview_reason
from ..project_test_summary_access import can_view_project_test_summary
from ..utils import (
    generate_watermark_css,
    setup_global_activity_tracking,
    sync_current_user_role,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


# --- 新增代码：独立的测试汇总报告页面 ---
@ui.page("/report/test_summary/{project_name}")
def test_summary_report(project_name: str):
    # 报表可通过地址直接打开，因此必须独立复核权限，不能只依赖上游按钮。
    stored_current_user = app.storage.user.get("current_user")
    if not isinstance(stored_current_user, str) or not stored_current_user:
        ui.navigate.to("/login")
        return
    if not can_view_project_test_summary(sync_current_user_role(), stored_current_user):
        ui.notify("当前账号没有查看生产测试项汇总表的权限", type="negative")
        ui.navigate.to("/main")
        return

    # --- 调用全局活跃跟踪组件 ---
    setup_global_activity_tracking()

    # 运行时初始化水印：透明度调至 0.2，印章缩小至 0.8倍，画布变小使得印章平铺更密集
    watermark_bg = generate_watermark_css(opacity=0.1, scale=1.2, spacing_width=500, spacing_height=300)
    # 独立注入动态样式块（双写 {{ }} 防止 f-string 报错）
    ui.add_head_html(f"""
        <style>
            .watermark-controlled::after {{
                background-image: {watermark_bg} !important;
            }}
        </style>
    """)
    # 2. 注入打印专用样式
    # 作用：打印时隐藏“打印按钮”，强制表格显示边框，优化A4纸显示
    ui.add_head_html("""
        <style>
            /* 1. 页面级设置：控制方向及隐藏浏览器默认文字 */
            @page {
                size: landscape;  /* 强制横向 */
                margin: 0mm;      /* 关键：设置为0去除浏览器默认边距，从而隐藏顶部的时间和标题 */
            }
                     
            /* 打印时的样式覆盖 */
            @media print {
                html, body {
                    width: 100%;
                    margin: 0 !important;
                    padding: 0 !important;
                }

                body {
                    /* 3. 模拟 50% 缩放 */
                    /* 注意：浏览器预览框的数字依然是100%，但内容已经被 CSS 缩小了，效果等同于手动设为50% */
                    zoom: 0.5; 
                    
                    /* 因为 @page 设为了0边距，我们需要给 body 加一点内边距，防止内容贴着纸张边缘 */
                    padding: 40px !important; 
                    
                    background-color: white;
                    font-family: sans-serif;
                }
                     
                /* 隐藏打印按钮 */
                .no-print { display: none !important; }
                
                /* 移除页面边距，交由打印机控制 */
                body { padding: 0; margin: 0; }
                
                /* 关键：合并边框，防止出现双线变粗 */
                .q-table { 
                    width: 100% !important;
                    border-collapse: collapse !important; 
                    font-size: 18px !important;
                }

                /* 单元格样式：使用 0.5pt 极细边框 */
                .q-table th, .q-table td {
                    border: 0.5pt solid #333 !important; /* 0.5pt 比 1px 更细，#333 比纯黑更柔和 */
                    color: #000 !important;
                    padding: 4px 8px !important; /* 稍微减小打印时的内边距，更紧凑 */
                }
                     
                /* 确保表头背景（如果浏览器开启背景打印） */
                thead tr th { background-color: #f0f0f0 !important; -webkit-print-color-adjust: exact; }
                
                /* 隐藏表格底部分页器 */
                .q-table__bottom { display: none !important; }
                
                /* 避免表格行在分页时被切断 */
                tr { page-break-inside: avoid; }
            }
            
            body { background-color: white; padding: 20px; font-family: sans-serif; }
                     
            /* --- 新增：强制所有表格的表头文字居中 --- */
            .q-table th {
                text-align: center !important;
                font-size: 17px;
                font-weight: bold;
            }
            .q-table td {
                font-size: 15px;
            }
                     
            /* 打印按钮样式：右上角悬浮 */
            .print-btn {
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 1000;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            }
            /* --- 受控水印的响应式样式 --- */
            .watermark-controlled {
                position: relative;
            }
            .watermark-controlled::after {
                content: "";
                position: absolute;
                top: 0; left: 0; right: 0; bottom: 0;
                pointer-events: none; 
                z-index: 10;
                
                /* 使用 Python 动态生成的背景 */
                background-repeat: repeat;
                background-position: center;
                
                -webkit-print-color-adjust: exact !important;
                print-color-adjust: exact !important;
            }
            
            /* 针对小屏幕(如移动端或窄窗口)的媒体查询适配，可通过 CSS 进一步缩放平铺密度 */
            @media (max-width: 768px) {
                .watermark-controlled::after {
                    background-size: 150px; /* 强制在窄屏幕上缩小画布背景的视觉尺寸 */
                }
            }
                     
        </style>
    """)

    # 3. 页面布局
    with ui.column().classes("w-full min-w-[1200px] mx-auto"):
        # --- 顶部标题区 ---
        with ui.row().classes("w-full items-center justify-center mt-6 mb-6 border-b-2 border-black pb-4 relative"):
            # --- 新增：公司 Logo ---
            # 2. 使用 absolute left-0 将其固定在左侧，不影响标题居中
            # 注意：请将 'logo.png' 替换为你实际的图片路径或 URL
            ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute left-0 top-0 bottom-2 m-auto w-30 object-contain")

            # ui.icon("fact_check", size="lg").classes("mr-2")
            ui.label(f"{project_name} 生产测试项汇总表").classes("text-2xl font-bold")

        # --- 右上角打印按钮 (no-print类用于打印时隐藏) ---
        ui.button("打印", icon="print", on_click=lambda: ui.run_javascript("window.print()")).props(
            "flat dense"
        ).classes("print-btn text-blue-7 no-print")

        # --- 数据处理 (复用之前的逻辑) ---
        ALL_OVER_DATA = db_storage.get_item(f"{project_name}_over_data", {})
        rows = []
        role_order = ["光学", "结构", "硬件", "软件", "UI", "工艺", "质量"]
        drive_voltage_list = []
        power_label_list = []

        # 辅助格式化函数
        def fmt_option(select_data, key_prefix):
            select_val = select_data.get(f"{key_prefix}_select")
            other_val = select_data.get(f"{key_prefix}_other_text")
            if select_val == "其它":
                return f"{other_val}" if other_val else "其它(未填)"
            if select_val:
                return select_val
            elif key_prefix == "test_nature":
                return "全检，作合格判定标准，写入报告"
            else:
                return "——"

        # 遍历提取数据
        for label, chips in ALL_OVER_DATA.items():
            label_title = label
            # 尝试获取中文标题
            if "over_config_data_flat" in app.storage.general:
                label_info = app.storage.general["over_config_data_flat"].get(label, {})
                label_title = label_info.get("title", label)
            if label == "drive_voltage":
                for chip_id, data in chips.items():
                    if data.get("content", "") and data.get("enabled"):
                        drive_voltage_list.append(data.get("content", ""))
            if label == "power_label":
                for chip_id, data in chips.items():
                    if data.get("content", "") and data.get("enabled"):
                        power_label_list.append(data.get("content", ""))
            for chip_id, data in chips.items():
                if data.get("type") == "test" and data.get("enabled"):
                    test_data = data.get("test_select_data", {})
                    rows.append(
                        {
                            "role": data.get("role", "未知"),
                            "category": label_title,
                            "content": data.get("content", ""),
                            "test_nature": fmt_option(test_data, "test_nature"),
                            "condition": fmt_option(test_data, "state"),
                            "node": fmt_option(test_data, "node"),
                            "instrument": fmt_option(test_data, "instrument"),
                            "notes": get_latest_overview_reason(data),
                        }
                    )

        # 排序
        rows.sort(key=lambda x: (role_order.index(x["role"]) if x["role"] in role_order else 99, x["category"]))

        # --- 表格显示 ---
        if not rows:
            ui.label("该项目暂无测试项数据").classes("text-xl text-gray-400 w-full text-center mt-10")
        else:
            project_state = app.storage.general.get("project_summary", {}).get(project_name, {}).get("state", "待定")
            if drive_voltage_list and power_label_list and len(drive_voltage_list) == 1 and len(power_label_list) == 1:
                ui.label(
                    f"输入电压：{drive_voltage_list[0].split('±')[0]}， 标签功率：{power_label_list[0]}， 产品状态：{project_state}"
                ).classes("text-base font-medium mb-0")
            else:
                ui.label("输入电压或标签功率数据异常（空白或内容不唯一），无法显示").classes(
                    "text-base font-medium mb-0 text-red-600"
                )
            with ui.column().classes("gap-1"):
                ui.label("注意：").classes("text-base font-medium mb-0")
                ui.label(
                    "1. 默认任意产品型号均需要测试并记录产品输入电压与输入电流，不写入报告，除非下表另行要求；"
                ).classes("text-sm font-medium mb-0")
                ui.label(
                    "2. 本测试项汇总表，试产前随时可能更新，只在产品进入试产或量产状态后，显示受控章进行受控；"
                ).classes("text-sm font-medium mb-0")
            columns = [
                {
                    "name": "role",
                    "label": "负责角色",
                    "field": "role",
                    "align": "center",
                    "style": "width: 50px;",
                },
                {"name": "category", "label": "分类", "field": "category", "align": "center", "style": "width: 80px;"},
                {
                    "name": "content",
                    "label": "测试内容 / 标准",
                    "field": "content",
                    "align": "left",
                    "style": "white-space: pre-wrap;",
                },
                {
                    "name": "test_nature",
                    "label": "测试性质",
                    "field": "test_nature",
                    "align": "center",
                    "style": "white-space: pre-wrap; width: 300px;",
                },
                {
                    "name": "condition",
                    "label": "条件",
                    "field": "condition",
                    "align": "center",
                    "style": "white-space: pre-wrap; width: 300px;",
                },
                {"name": "node", "label": "节点", "field": "node", "align": "center", "style": "width: 80px;"},
                {
                    "name": "instrument",
                    "label": "工具",
                    "field": "instrument",
                    "align": "center",
                    "style": "width: 150px;",
                },
                {
                    "name": "notes",
                    "label": "备注",
                    "field": "notes",
                    "align": "center",
                    "style": "white-space: pre-wrap;",
                },
            ]

            # 使用 dense 和 bordered 样式，使其更像传统的Excel打印单
            # --- 修改：提取实例到变量，以便动态注入状态类名 ---
            test_table = (
                ui.table(
                    columns=columns,
                    rows=rows,
                    row_key="content",
                    pagination={"rowsPerPage": 0},
                )
                .classes("w-full")
                .props('flat bordered dense hide-bottom separator="cell"')
            )

            # 根据项目状态追加水印
            if project_state in ["试产", "量产"]:
                test_table.classes("watermark-controlled")

        # --- 底部页脚 ---
        with ui.row().classes("w-full justify-between mt-8 pt-4 border-t border-gray-300 text-sm"):
            ui.label(f"导出人: {app.storage.user.get('current_user', 'System')}")
            ui.label("研发项目文件管理系统")
            ui.label(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
