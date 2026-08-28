# -*- encoding: utf-8 -*-

import asyncio
import base64
import copy
import hashlib
import json
import logging
import mimetypes
import os
import re
import ssl
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from httpx import BasicAuth
from nicegui import app, ui
from nicegui.events import KeyEventArguments

from . import db_storage
from .permission_catalog import project_overview_permission_definitions

# import config
from .config import (
    AVATAR_DIR,
    AVATAR_URL_DIR,
    BASE_DIR,
    FILES_URL_DIR,
    IMG_DIR,
    IMG_URL_DIR,
    OVER_DIR,
    OVERVIEW_UI_RENDER_REGISTRY,
    REQ_DIR,
    SVN_PASSWORD,
    SVN_USERNAME,
)
from .requirement_overview_impact import (
    REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY,
    RequirementOverviewImpactConfigError,
    collect_requirement_change_node_ids,
    load_requirement_overview_impact_config,
    resolve_requirement_overview_impacts,
)
from .overview_operation import (
    append_overview_timestamp,
    get_automatic_overview_reason,
    get_latest_overview_operator,
    get_latest_overview_record,
)

# 获取一个以此模块命名的 logger
# 比如：如果你的文件是 src/components.py，这个 logger 的名字就会是 "src.components"
logger = logging.getLogger(__name__)


def get_overview_latest_responsible(project_name: str, label: str, chip_data: dict | None = None) -> str:
    """获取概述类别已有最近负责人，不把自动操作归给“系统”。"""
    role = app.storage.general.get("over_config_data_flat", {}).get(label, {}).get("role", "")
    latest_user_raw = (
        app.storage.general.get("overview_role", {}).get(project_name, {}).get(role, {}).get("latest_user", "")
    )
    latest_user = latest_user_raw.split("：", 1)[1] if "：" in latest_user_raw else latest_user_raw
    if latest_user and latest_user != "——":
        return latest_user
    return str((chip_data or {}).get("creator") or "待定负责人")

# 内存中的全局字典：{ client.id : { 'username': str, 'login_time': str, 'ip': str } }
online_users = {}

CHINESE_DATE_LOCALE = {
    "days": ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"],
    "daysShort": ["日", "一", "二", "三", "四", "五", "六"],
    "months": [
        "一月",
        "二月",
        "三月",
        "四月",
        "五月",
        "六月",
        "七月",
        "八月",
        "九月",
        "十月",
        "十一月",
        "十二月",
    ],
    "monthsShort": ["1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月"],
    "firstDayOfWeek": 1,
    "format24h": True,
    "pluralDay": "天",
}


def apply_chinese_date_locale(date_element):
    """给 Quasar QDate 设置中文月份和星期显示。"""
    date_element.props["locale"] = copy.deepcopy(CHINESE_DATE_LOCALE)
    return date_element


def setup_global_activity_tracking():
    """注入全局前端活跃监听与心跳上报"""
    client = None
    client_id = None
    username = "访客"
    try:
        client = ui.context.client
        client_id = client.id
        username = app.storage.user.get("current_user", "访客")
        existing_data = online_users.get(client.id, {})
        now = time.time()
        online_users[client.id] = {
            "username": username,
            "login_time": existing_data.get("login_time", datetime.now().strftime("%H:%M:%S")),
            "ip": client.ip or existing_data.get("ip", "Unknown"),
            "last_seen_ts": now,
            "last_activity_ts": now,
        }
    except Exception:
        pass

    # 注入前端监听脚本
    ui.add_head_html("""
        <script>
            window.lastActivityTime = Date.now();
            // 使用版本标志，允许后续扩展监听事件时自动升级绑定逻辑。
            if (window.activityTrackerVersion !== 2) {
                const updateActivity = () => { window.lastActivityTime = Date.now(); };
                
                ['mousedown', 'pointerdown', 'keydown', 'wheel', 'scroll', 'touchstart', 'input', 'change', 'focus'].forEach(evt =>
                    document.addEventListener(evt, updateActivity, {passive: true, capture: true})
                );
                window.activityTrackerInitialized = true;
                window.activityTrackerVersion = 2;
            }
        </script>
    """)

    # 注入心跳上报定时器
    async def report_heartbeat() -> None:
        if client is None or client_id is None:
            return
        try:
            last_activity_ms = await client.run_javascript("return window.lastActivityTime;", timeout=2.0)
            if last_activity_ms is not None:
                if client_id in online_users:
                    online_users[client_id]["username"] = username
                    online_users[client_id]["last_seen_ts"] = time.time()
                    online_users[client_id]["last_activity_ts"] = last_activity_ms / 1000.0
        except Exception as exc:
            # 用户正在切换页面或刷新时，执行JS会超时，直接忽略即可
            logger.debug("活跃心跳上报失败: %r", exc)

    # 每 10 秒向后端同步一次
    ui.timer(10.0, report_heartbeat)


async def async_path_exists(path_str: str) -> bool:
    """非阻塞的文件存在性检查"""
    if not path_str:
        return False
    # 将同步的 os.path.exists 放入线程池执行，防止阻塞 UI
    return await asyncio.to_thread(os.path.exists, path_str)


def generate_initial_ecn_data(applicant: str, target_projects: list) -> dict:
    """
    生成标准化的 ECN 初始数据模型，彻底避免运行时字段缺失。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ecn_id = f"ECN-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:4].upper()}"

    return {
        "ecn_id": ecn_id,
        "basic_info": {
            "title": "",
            "nature": "永久变更",  # 永久变更 / 临时变更
            "reason_type": "设计改善",  # 需求更改 / 设计改善 / 工艺调整 / 物料替换等
            "reason_desc": "",
            "applicant": applicant,
            "apply_date": now_str,
        },
        "target_projects": target_projects,  # 受影响的项目列表
        "change_items": [],  # 存放具体的变更项 (需求变更、概述变更、物料变更等)
        "workflow": {
            "current_state": "草稿",  # 对应 ECNState
            "current_phase": "",  # ECR_PHASE / ECN_SCHEME_PHASE / ECN_EXECUTION_PHASE
            "current_step_index": 0,  # 当前所处二维数组的外层索引
            "route_type": "",  # 记录走的是 SALES_INITIATED 还是 RD_INITIATED
            "pending_roles": [],  # 当前正在等待哪些角色审批
            "step_approvals": {},  # 记录当前步骤中各角色的同意状态 e.g. {"质量": True, "工程": False}
        },
        "approval_log": [],  # 扁平化审批日志，记录 [{"user": "x", "role": "y", "action": "同意", "note": "...", "time": "..."}]
        "timestamp": {now_str: f"由 {applicant} 创建草稿"},
    }


def trigger_global_sync(project_name: str):
    """触发全局同步标记，通知所有监听此项目的客户端"""
    # 用当前时间戳作为版本号
    app.storage.general["sync_versions"][project_name] = time.time()


def generate_watermark_css(
    opacity: float = 0.15, scale: float = 1.0, spacing_width: int = 400, spacing_height: int = 300
) -> str:
    """
    动态生成受控印章的 SVG Base64 背景字符串

    :param opacity: 透明度，0.0 到 1.0 之间。0.15为默认淡红。
    :param scale: 印章本身的缩放比例。1.0为标准大小。
    :param spacing_width: SVG 画布宽度（决定平铺时的水平间距）
    :param spacing_height: SVG 画布高度（决定平铺时的垂直间距）
    """
    # 计算画布中心点，以确保印章始终在平铺块的中心
    cx = spacing_width / 2
    cy = spacing_height / 2

    # 构造 SVG 字符串（UI 文字使用中文）
    svg_content = f"""
    <svg xmlns='http://www.w3.org/2000/svg' width='{spacing_width}' height='{spacing_height}'>
        <g transform='translate({cx}, {cy}) rotate(-30) scale({scale})'>
            <rect x='-100' y='-40' width='200' height='80' rx='10' ry='10' stroke='rgba(255,0,0,{opacity})' stroke-width='6' fill='none'/>
            <text x='0' y='15' font-size='45' fill='rgba(255,0,0,{opacity})' font-family='sans-serif' font-weight='bold' text-anchor='middle' letter-spacing='5'>受 控</text>
        </g>
    </svg>
    """

    # 转换为 Base64
    b64_encoded = base64.b64encode(svg_content.encode("utf-8")).decode("utf-8")
    return f"url('data:image/svg+xml;base64,{b64_encoded}')"


def handle_connect(client):
    """当用户建立连接时触发"""
    try:
        # 尝试获取用户名，如果没登录可能是 None 或 'Unknown'
        # 注意：app.storage.user 需要在上下文中使用，这里假设已能获取
        username = app.storage.user.get("current_user", "访客")

        # 记录用户信息
        now = time.time()
        online_users[client.id] = {
            "username": username,
            "login_time": datetime.now().strftime("%H:%M:%S"),
            "ip": client.ip or "Unknown",
            "last_seen_ts": now,
            "last_activity_ts": now,  # 一连上就给一个初始的静态绝对时间戳
        }
    except Exception as e:
        print(f"Connection track error: {e}")


def handle_disconnect(client):
    """当用户断开连接时触发"""
    if client.id in online_users:
        del online_users[client.id]


async def validate_search_path(content: str, config: dict, projects: list, pending_overrides: dict = {}) -> tuple:
    """
    校验 search 类型的路径引用是否合法 (已通过 asyncio.to_thread 彻底解决 UI 阻塞问题)
    返回: (is_valid: bool, url_path: str, file_type: str, local_filepath: str, message: str)
    """

    upload_path = config.get("upload_path", "")

    # 【改造点 1】：将原本同步的 Path.exists() 推入线程池执行
    upload_path_exists = await asyncio.to_thread(Path(upload_path).exists)
    if not upload_path_exists:
        return False, "", "", "", f"上传根目录不存在: {upload_path}"

    search_scope_regular = config.get("search_scope_regular", "")
    search_folder_according_li = config.get("search_folder_according", [])
    search_hierarchy = config.get("search_hierarchy", [])

    primary_project = projects[0] if projects else ""
    target_path_list = []
    according_folder_name_li = []

    if search_folder_according_li and primary_project:
        for according in search_folder_according_li:
            # 核心优化：优先从前端传入的本单暂存数据中获取新依赖项
            if according in pending_overrides:
                according_folder_name_li.append(pending_overrides[according])
            else:
                for DATA in db_storage.get_deep_item([f"{primary_project}_over_data", according], {}).values():
                    if DATA.get("enabled"):
                        according_folder_name_li.append(DATA["content"])

        if not according_folder_name_li:
            return False, "", "", "", "项目缺少依赖的目录项配置，无法构建路径"

        for folder_name in according_folder_name_li:
            if search_scope_regular:
                match = re.search(search_scope_regular, folder_name)
                if match:
                    # 假设这里原本已经是真正的异步，继续保持 await
                    target_path_list.extend(
                        await find_dirs_by_name_os_walk(f"{upload_path}\\{match.group(1)}", folder_name)
                    )
            else:
                target_path_list.extend(await find_dirs_by_name_os_walk(upload_path, folder_name))
    else:
        if search_scope_regular:
            match = re.search(search_scope_regular, content)
            if match:
                target_path_list = await find_dirs_by_name_os_walk(upload_path, match.group(1))
        else:
            target_path_list = [upload_path]

    if search_hierarchy:
        target_path_list = [f"{tp}\\{h}" for tp in target_path_list for h in search_hierarchy]

    files_li = []
    for target_path in target_path_list:
        if not target_path:
            continue

        # 【改造点 2】：将检查是否为目录的 I/O 操作推入线程池
        is_dir = await asyncio.to_thread(Path(target_path).is_dir)
        if is_dir:
            # 【改造点 3】：最关键的耗时操作！将同步的目录遍历搜索推入线程池
            found_files = await asyncio.to_thread(find_files_pathlib, str(target_path), content)
            files_li.extend(found_files)

    if not files_li:
        return False, "", "", "", f"未找到文件: {content}。请检查依赖配置或文件名规范。"
    elif len(files_li) > 1:
        return False, "", "", "", f"存在多个同名文件: {content}。请确保唯一性。"
    else:
        # 【改造点 4】：将具体文件的存在性检查推入线程池
        file_exists = await asyncio.to_thread(files_li[0].exists)
        if not file_exists:
            return False, "", "", "", f"文件路径不存在: {str(files_li[0])}。"

        # 所有耗时检查通过，执行常规赋值
        file_type = get_file_type_by_extension(str(files_li[0]))[0]
        url_path = f"{FILES_URL_DIR}/{content}"
        local_filepath = str(files_li[0])  # 提取本地绝对路径

        return True, url_path, file_type, local_filepath, "校验通过，文件存在！"


async def validate_svn_url(content: str, config: dict, projects: list, pending_overrides: dict = {}) -> tuple:
    """
    校验 svn 类型的网络引用是否合法
    返回: (is_valid: bool, url_path: str, file_type: str, message: str)
    """

    from nicegui import app

    primary_project = projects[0] if projects else ""
    project_state = app.storage.general.get("project_summary", {}).get(primary_project, {}).get("state", "")
    svn_main_folder = config.get("state_path", {}).get(project_state)

    if not svn_main_folder:
        return False, "", "", f"当前项目状态({project_state})下未配置SVN仓库路径"

    upload_path = config.get("upload_path", "")
    search_scope_regular = config.get("search_scope_regular", "")
    search_folder_according_li = config.get("search_folder_according", [])
    search_hierarchy = config.get("search_hierarchy", [])

    according_folder_name = []
    target_url_li = []

    if search_folder_according_li and primary_project:
        for according in search_folder_according_li:
            # 核心优化：优先从前端传入的本单暂存数据中获取新依赖项
            if according in pending_overrides:
                according_folder_name.append(pending_overrides[according])
            else:
                for DATA in db_storage.get_deep_item([f"{primary_project}_over_data", according], {}).values():
                    if DATA.get("enabled"):
                        according_folder_name.append(DATA["content"])

        if not according_folder_name:
            return False, "", "", "项目缺少依赖的目录项配置，无法构建SVN路径"

        if search_scope_regular:
            for folder_name in according_folder_name:
                match = re.search(search_scope_regular, folder_name)
                if match:
                    match_folder = f"{match.group(1)}-{match.group(2)}"
                    target_url_li.append(f"{upload_path}/{svn_main_folder}/{match_folder}/{folder_name}")
        else:
            for folder_name in according_folder_name:
                target_url_li.append(f"{upload_path}/{svn_main_folder}/{folder_name}")
    else:
        if search_scope_regular:
            match = re.search(search_scope_regular, content)
            if match:
                match_folder = f"{match.group(1)}-{match.group(2)}"
                target_url_li.append(f"{upload_path}/{svn_main_folder}/{match_folder}")
        else:
            target_url_li.append(f"{upload_path}/{svn_main_folder}")

    if not target_url_li:
        return False, "", "", "未能生成有效的 SVN 校验路径"

    return_url_li = []
    for target_url in target_url_li:
        if search_hierarchy:
            for h in search_hierarchy:
                target_url = f"{target_url}/{h}"
        return_url_li.append(f"{target_url}/{content}")

    if len(return_url_li) > 1:
        return False, "", "", "生成了多个 SVN 路径，存在歧义不合规"

    target_url = return_url_li[0]

    # HTTP 探测
    headers = {"User-Agent": "Mozilla/5.0"}

    # ssl.create_default_context(): Python 标准库 ssl 的函数，用于创建一个具有默认安全设置的 SSL 上下文对象。
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    # BasicAuth: httpx 库提供的类，用于构造 HTTP 基本认证 (Basic Authentication) 的凭证对象，底层会自动将账号密码进行 Base64 编码并注入 Headers。
    auth = BasicAuth(SVN_USERNAME, SVN_PASSWORD) if SVN_USERNAME and SVN_PASSWORD else None

    file_type = None
    is_valid = False

    try:
        # httpx.AsyncClient: httpx 库提供的核心类，用于创建一个异步的 HTTP 客户端实例，负责管理连接池、HTTP/2 支持及全局配置。
        # 【修复核心】trust_env=False: 强制客户端忽略操作系统的环境代理变量（如 HTTP_PROXY, HTTPS_PROXY），避免请求内网地址时被代理拦截。
        async with httpx.AsyncClient(follow_redirects=False, verify=ssl_context, auth=auth, trust_env=False) as client:
            # client.stream(): AsyncClient 实例的方法，用于发起异步的流式 HTTP 请求。它只读取响应头而不立即下载响应体，非常适合仅需探测文件类型或处理大文件的场景，可极大节省内存。
            async with client.stream("GET", target_url, timeout=15, headers=headers) as response:
                if response.status_code < 400:
                    ct = response.headers.get("Content-Type")
                    file_type = ct.split(";")[0].strip() if ct else None
                    if (file_type == "application/octet-stream" or file_type is None) and target_url.lower().endswith(
                        ".pdf"
                    ):
                        file_type = "application/pdf"
                    is_valid = True
    except Exception as e:
        # 建议将 str(e) 替换为 repr(e)，以保留更完整的异常类名，方便后续排查其他未知问题
        return False, "", "", f"SVN 连接异常: {repr(e)}"

    if is_valid:
        return True, target_url, file_type, "SVN 文件校验通过！"
    else:
        return False, "", "", f"SVN 文件探测失败: 状态异常或不存在于 {target_url}"


def update_overview_charge_pending_dic(scope, des_user="", project_name="", des_label=""):
    """
    scope传入all时，刷新所有项目概述负责人待定状态字典信息（采用全量重建策略，自动清理已失效的标题Key）；
    scope传入local时，只刷新指定负责人、指定项目、指定概述标签类的待定状态信息，极致响应速度；
    """

    if scope == "all":
        new_pending_storage = {}

        # 1. 静态配置预处理
        role_config_map = {}
        for role, group_dict in app.storage.general.get("over_config_data", {}).items():
            role_config_map[role] = []
            for group_li in group_dict.values():
                for ver_dic in group_li:
                    role_config_map[role].append(
                        {"nature": ver_dic.get("nature"), "title": ver_dic.get("title"), "label": ver_dic.get("label")}
                    )

        # 2. 遍历项目
        for project, project_dic in app.storage.general.get("overview_role", {}).items():
            # 项目工程师未指定的，不用仔细检查整个项目的概述填写情况
            # 展示关闭不检查，让整理过概述但是还没有项目工程师的项目，概述负责人也能在待办项看到条目
            # project_engineer = app.storage.general["project_engineer"].get(project, "未指定")
            # if project_engineer == "未指定":
            #     continue
            for role, charge_user_dic in project_dic.items():
                latest_user_raw = charge_user_dic.get("latest_user", "")
                latest_user = latest_user_raw.split("：")[1] if "：" in latest_user_raw else latest_user_raw

                if not latest_user:
                    latest_user = "待定负责人"
                elif latest_user == "——":
                    continue

                user_proj_dict = new_pending_storage.setdefault(latest_user, {}).setdefault(project, {})

                # 3. 遍历预处理好的配置项
                for config_item in role_config_map.get(role, []):
                    nature = config_item["nature"]
                    label = config_item["label"]
                    group_name = app.storage.general.get("over_config_data_flat", {}).get(label, {}).get("group_name")

                    # 💡 核心新增：判断当前组是否为表格模式
                    is_table_group = OVERVIEW_UI_RENDER_REGISTRY.get(group_name) == "OverviewTableGroup"

                    if nature == "必填":
                        user_proj_dict.setdefault(label, "缺必填")
                    elif nature == "需填":
                        user_proj_dict.setdefault(label, "缺需填")

                    if latest_user == "待定负责人":
                        continue

                    LABEL_CHIP_DIC = db_storage.get_deep_item([f"{project}_over_data", label], {}).values()

                    if not LABEL_CHIP_DIC:
                        if nature == "必填":
                            user_proj_dict[label] = "缺必填"
                        elif nature == "需填":
                            user_proj_dict[label] = "缺需填"
                        else:
                            user_proj_dict.pop(label, None)
                        continue

                    # 4. 状态判定分流
                    has_none = False
                    has_active = False

                    if is_table_group:
                        # ================= 表格模式逻辑（严格行匹配） =================
                        first_col_label = (
                            app.storage.general.get("over_config_data", {})
                            .get(role, {})
                            .get(group_name, [])[0]["label"]
                        )
                        FIRST_COL_LABEL_CHIP_DIC = db_storage.get_deep_item(
                            [f"{project}_over_data", first_col_label], {}
                        ).values()

                        chip_activ_li = []
                        first_col_chip_activ_li = []

                        for first_chip_info in FIRST_COL_LABEL_CHIP_DIC:
                            first_chip_state = first_chip_info.get("enabled")
                            first_chip_row_id = first_chip_info.get("row_id")
                            if (
                                first_chip_row_id
                                and first_chip_row_id not in first_col_chip_activ_li
                                and first_chip_state
                            ):
                                first_col_chip_activ_li.append(first_chip_row_id)

                        for chip_info in LABEL_CHIP_DIC:
                            state = chip_info.get("enabled")
                            chip_row_id = chip_info.get("row_id")
                            if state is None:
                                has_none = True
                                break
                            if chip_row_id:
                                if (
                                    state
                                    and chip_row_id not in chip_activ_li
                                    and chip_row_id in first_col_chip_activ_li
                                ):
                                    chip_activ_li.append(chip_row_id)
                            else:
                                if state is True:
                                    has_active = True

                        if chip_activ_li and len(chip_activ_li) == len(first_col_chip_activ_li):
                            has_active = True
                    else:
                        # ================= 独立按钮模式逻辑（散装独立匹配） =================
                        for chip_info in LABEL_CHIP_DIC:
                            state = chip_info.get("enabled")
                            if state is None:
                                has_none = True
                                break
                            elif state is True:
                                has_active = True
                                # 这里不 break 是为了继续寻找可能存在的 None（待定优先级最高）

                    # 统一收尾判定
                    if has_none:
                        user_proj_dict[label] = "有待定"
                    elif has_active:
                        user_proj_dict.pop(label, None)
                    else:
                        if nature == "必填":
                            user_proj_dict[label] = "缺必填"
                        elif nature == "需填":
                            user_proj_dict[label] = "缺需填"
                        else:
                            user_proj_dict.pop(label, None)

                # 5. 清理空节点
                if not user_proj_dict:
                    new_pending_storage[latest_user].pop(project, None)
                if latest_user in new_pending_storage and not new_pending_storage[latest_user]:
                    new_pending_storage.pop(latest_user, None)

        app.storage.general["overview_charge_pending"] = new_pending_storage

    elif scope == "local":
        pending_storage = app.storage.general.setdefault("overview_charge_pending", {})

        if not des_user or not project_name or not des_label or des_user == "——":
            return

        user_proj_dict = pending_storage.setdefault(des_user, {}).setdefault(project_name, {})

        ver_dic = app.storage.general.get("over_config_data_flat", {}).get(des_label, {})
        if ver_dic:
            nature = ver_dic.get("nature")
            role = ver_dic.get("role")
            group_name = ver_dic.get("group_name")

            # 💡 核心新增：判断当前组是否为表格模式
            is_table_group = OVERVIEW_UI_RENDER_REGISTRY.get(group_name) == "OverviewTableGroup"

            if nature == "必填":
                user_proj_dict.setdefault(des_label, "缺必填")
            elif nature == "需填":
                user_proj_dict.setdefault(des_label, "缺需填")

            LABEL_CHIP_DIC = db_storage.get_deep_item([f"{project_name}_over_data", des_label], {}).values()

            if not LABEL_CHIP_DIC:
                if nature == "必填":
                    user_proj_dict[des_label] = "缺必填"
                elif nature == "需填":
                    user_proj_dict[des_label] = "缺需填"
                else:
                    user_proj_dict.pop(des_label, None)
            else:
                has_none = False
                has_active = False

                if is_table_group:
                    # ================= 表格模式逻辑 =================
                    first_col_label = (
                        app.storage.general.get("over_config_data", {}).get(role, {}).get(group_name, [])[0]["label"]
                    )
                    FIRST_COL_LABEL_CHIP_DIC = db_storage.get_deep_item(
                        [f"{project_name}_over_data", first_col_label], {}
                    ).values()

                    chip_activ_li = []
                    first_col_chip_activ_li = []

                    for first_chip_info in FIRST_COL_LABEL_CHIP_DIC:
                        first_chip_state = first_chip_info.get("enabled")
                        first_chip_row_id = first_chip_info.get("row_id")
                        if first_chip_row_id and first_chip_row_id not in first_col_chip_activ_li and first_chip_state:
                            first_col_chip_activ_li.append(first_chip_row_id)

                    for chip_info in LABEL_CHIP_DIC:
                        state = chip_info.get("enabled")
                        chip_row_id = chip_info.get("row_id")
                        if state is None:
                            has_none = True
                            break
                        if chip_row_id:
                            if state and chip_row_id not in chip_activ_li and chip_row_id in first_col_chip_activ_li:
                                chip_activ_li.append(chip_row_id)
                        else:
                            if state is True:
                                has_active = True

                    if chip_activ_li and len(chip_activ_li) == len(first_col_chip_activ_li):
                        has_active = True
                else:
                    # ================= 独立按钮模式逻辑 =================
                    for chip_info in LABEL_CHIP_DIC:
                        state = chip_info.get("enabled")
                        if state is None:
                            has_none = True
                            break
                        elif state is True:
                            has_active = True

                if has_none:
                    user_proj_dict[des_label] = "有待定"
                elif has_active:
                    user_proj_dict.pop(des_label, None)
                else:
                    if nature == "必填":
                        user_proj_dict[des_label] = "缺必填"
                    elif nature == "需填":
                        user_proj_dict[des_label] = "缺需填"
                    else:
                        user_proj_dict.pop(des_label, None)

        if not user_proj_dict:
            pending_storage[des_user].pop(project_name, None)
        if not pending_storage.get(des_user):
            pending_storage.pop(des_user, None)

    app.storage.general.setdefault("overview_last_update", {})[project_name] = time.time()


# 判断传入的概述负责角色是否与当前登录的角色匹配
def overview_state_show_judge(charge_role) -> bool:
    # # 以下登录角色也要对所有概述状态进行了解
    if app.storage.user.get("current_role", "匿名用户") in ["研发经理", "研发助理"]:
        return True
    # UI负责部分相当于软件负责
    elif charge_role == "UI":
        # 以下登录角色也要对UI概述状态进行了解
        if app.storage.user.get("current_role", "匿名用户") in ["研发电子主管"]:
            return True
        else:
            return "软件" in app.storage.user.get("current_role", "匿名用户")
    else:
        # 以下登录角色也要对硬件、软件的概述状态进行了解
        if charge_role in ["硬件", "软件"] and app.storage.user.get("current_role", "匿名用户") in ["研发电子主管"]:
            return True
        else:
            return charge_role in app.storage.user.get("current_role", "匿名用户")


# 分析传入文件路径，文件的文件类型和编码方式
def get_file_type_by_extension(file_path):
    """
    通过文件扩展名获取 MIME 类型。
    """
    p = Path(file_path)

    # 确保路径存在
    if not p.exists():
        return f"文件不存在: {file_path}", None

    # guess_type 返回一个元组 (type, encoding)，如 ('image/jpeg', None)
    mime_type, encoding = mimetypes.guess_type(file_path, strict=True)

    if mime_type:
        return mime_type, encoding
    else:
        # 尝试通过文件扩展名本身作为类型
        extension = p.suffix.lower().lstrip(".")
        if extension:
            return f"extension/{extension}", None  # 格式化为非官方 MIME 类型
        else:
            return "unknown/unknown", None


# 在传入路径上查找指定文件，返回匹配的所有Path对象列表
def find_files_pathlib(start_dir: str, filename: str) -> list[Path]:
    """
    使用 pathlib.rglob 查找所有匹配的文件。
    返回一个 Path 对象的列表。
    """
    # 确保起始路径是一个 Path 对象
    start_path = Path(start_dir)

    # rglob 会递归地在 start_path 下查找所有匹配 filename 的项
    # 我们使用 list() 来立即获取所有结果
    return list(start_path.rglob(filename))


# 在传入路径上查找指定文件夹，返回匹配的所有Path对象列表
async def find_dirs_by_name_os_walk(start_dir: str, dir_name: str) -> list[Path]:
    """
    使用 os.walk 高效查找所有匹配名称的 *目录*。
    """
    found_dirs = []
    start_dir = str(start_dir)  # os.walk 倾向于使用字符串

    # os.walk 会生成 (当前路径, 目录名列表, 文件名列表)
    # dirpath: 当前正在遍历的目录的路径 (str)
    # dirnames: 在 dirpath 中找到的 *子目录* 名称列表 (list[str])
    # filenames: 在 dirpath 中找到的 *文件* 名称列表 (list[str])
    for dirpath, dirnames, filenames in os.walk(start_dir, topdown=True):
        # 核心优化：我们只检查 'dirnames' 列表。
        # 如果 'dir_name' 在这个列表中，我们100%确定它是一个目录。
        # 我们完全不需要调用 is_dir()，也不需要关心任何文件。
        if dir_name in dirnames:
            # 找到了，构建它的完整路径
            # 注意：os.walk 默认使用字符串，我们将其转换回 Path 对象
            found_dirs.append(Path(dirpath) / dir_name)
    return found_dirs


# 新增一个辅助函数，用于头像的“缓存清除”
def get_cache_busted_path(web_path: str) -> str:
    """
    根据文件的修改时间，为 web 路径添加 ?v=mtime 参数以清除缓存。
    可以智能处理“预设头像”(IMG_DIR) 和“自定义头像”(AVATAR_DIR)。
    """
    if not web_path:
        return web_path

    # 移除可能存在的旧查询参数
    clean_web_path = web_path.split("?")[0]

    filesystem_path = None

    try:
        # 1. 检查是否是“自定义头像”
        if clean_web_path.startswith(AVATAR_URL_DIR):
            relative_path = clean_web_path[len(AVATAR_URL_DIR) :].lstrip("/")
            filesystem_path = Path(AVATAR_DIR) / relative_path

        # 2. 检查是否是“预设头像” (或其他 IMG 目录下的图片)
        elif clean_web_path.startswith(IMG_URL_DIR):
            relative_path = clean_web_path[len(IMG_URL_DIR) :].lstrip("/")
            filesystem_path = Path(IMG_DIR) / relative_path

        # 3. 如果路径都匹配不上，或者文件不存在
        if filesystem_path is None or not filesystem_path.exists():
            return web_path  # 返回原始路径

        # 4. 获取文件修改时间并生成新 URL
        mtime = filesystem_path.stat().st_mtime
        return f"{clean_web_path}?v={mtime}"

    except Exception:
        logger.error(f"Error generating cache-busted path for {web_path}", exc_info=True)
        return web_path  # 出错时返回原始路径


# 更新所有用户密码与角色数据
def update_users_data():
    try:
        app.state.users_data = app.state.user_service.load_users()

        logger.info("成功更新用户配置数据。")
        ui.notify(
            "用户配置数据更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        logger.error(f"更新用户配置数据失败：{e}")
        ui.notify(
            f'用户配置数据更新出错： "{e}" ',
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


def sync_current_user_role(default: str = "未知角色") -> str:
    """用当前用户表中的角色刷新浏览器会话，并返回最新角色。

    ``app.storage.user`` 会跨服务重启保留，因此不能把登录时写入的
    ``current_role`` 长期当作权限事实来源。管理员修改用户角色后，页面入口调用本函数即可
    让现有浏览器会话在刷新时同步到最新角色。
    """
    current_user = str(app.storage.user.get("current_user", "")).strip()
    if not current_user:
        return default

    users_data = getattr(app.state, "users_data", {})
    user_info = users_data.get(current_user, {}) if isinstance(users_data, dict) else {}
    user_service = getattr(app.state, "user_service", None)
    if user_service is not None:
        try:
            # 直接读取用户表，兼容未来启用多进程后其它进程刚完成的角色修改。
            fresh_user_info = user_service.get_user(current_user)
            if isinstance(fresh_user_info, dict) and fresh_user_info:
                user_info = fresh_user_info
        except Exception:
            # 用户表短暂不可读时使用启动时/后台保存后刷新的内存数据，避免把正常会话踢下线。
            logger.warning("刷新当前用户角色失败，暂时使用内存用户数据：%s", current_user, exc_info=True)
    if not isinstance(user_info, dict):
        return default

    latest_role = str(user_info.get("role") or "").strip()
    if not latest_role:
        return default

    latest_user_id = user_info.get("user_id")
    if latest_user_id:
        # Existing browser sessions gain the stable identity after the server's
        # Excel-to-IAM migration without requiring a forced logout.
        app.storage.user["current_user_id"] = str(latest_user_id)

    app.storage.user["current_role"] = latest_role
    app.storage.user["is_admin"] = latest_role.lower() == "admin"
    return latest_role


# 全局键盘事件跟踪处理函数
def handle_key(e: KeyEventArguments):
    key_state = app.storage.client.setdefault("key_state", {})
    if e.modifiers.ctrl and e.action.keydown:
        key_state["ctrl"] = 9
    else:
        key_state["ctrl"] = 0

    if e.key.enter and e.action.keydown:
        key_state["enter"] = 1
        # app.storage.client["key_state"]["enter"] = 0


def validate_user_output(new_item_config, old_item_data):
    """
    智能迁移函数：
    利用旧配置中的 option_id 作为桥梁，将旧答案“翻译”为新模版对应的答案。
    """
    old_user_out = old_item_data.get("user_must_out", {})
    if not old_user_out:
        return {}

    answer_type = new_item_config.get("answer_type")

    # 获取旧配置里的选项列表（用于查 ID）
    # 注意：这里假设 old_item_data 是完整的旧节点数据，包含当时的 options
    old_options = old_item_data.get("options", [])
    # 获取新配置里的选项列表（用于查新值）
    new_options = new_item_config.get("options", [])

    # 建立新模版查找表：{option_id: new_option_obj}
    new_opt_map = {opt.get("option_id"): opt for opt in new_options if opt.get("option_id")}

    # 建立旧数据反查表：
    # 1. {option_out: option_id} (用于单选)
    old_val_to_id = {str(opt.get("option_out")): opt.get("option_id") for opt in old_options}
    # 2. {option_content: option_id} (用于多选)
    old_content_to_id = {str(opt.get("option_content")): opt.get("option_id") for opt in old_options}

    # --- 1. 单选/下拉单选逻辑 ---
    if answer_type in ["单选", "下拉单选"]:
        # 获取旧数据用户单选的选项输出值
        old_val = str(old_user_out.get("value"))

        # 步骤A: 尝试通过旧值找到 ID
        target_id = old_val_to_id.get(old_val)

        # 步骤B: 如果找到了 ID，且该 ID 在新模版里也存在
        if target_id and target_id in new_opt_map:
            # 【关键】：返回新模版里的 option_out，实现值自动升级
            return {"value": new_opt_map[target_id].get("option_out")}

        # 兜底：如果 ID 匹配失败（比如以前没 ID），尝试直接匹配值
        # 遍历新选项，看有没有值一样的
        for opt in new_options:
            if str(opt.get("option_out")) == old_val:
                return old_user_out  # 值没变，直接返回

        return {"value": None}  # 彻底匹配不上，置空

    # --- 2. 多选逻辑 ---
    elif answer_type == "多选":
        # 【关键修改】：初始化字典，先把新模版里所有的选项都填进去，默认值为 False
        # 这样就保证了 bind_value 时所有的 key 都在，不会报错
        cleaned = {opt.get("option_out"): False for opt in new_options}

        # 新模版里所有合法的 option_out 集合（用于兜底校验）
        new_valid_outs = set(opt.get("option_out") for opt in new_options)

        for old_k, old_v in old_user_out.items():
            if old_v:
                # 假设旧数据可能存的是 ID，也可能存的是 Content，甚至可能是 Out
                # 我们统一尝试转换

                # 路径 A: old_k 是 option_id? (未来扩展)
                if old_k in new_opt_map:
                    target_out = new_opt_map[old_k].get("option_out")
                    cleaned[target_out] = True
                    continue

                # 路径 B: old_k 是 Content? (旧有的文字数据) -> 转 ID -> 转 Out
                target_id = old_content_to_id.get(str(old_k))
                if target_id and target_id in new_opt_map:
                    target_out = new_opt_map[target_id].get("option_out")
                    cleaned[target_out] = True
                    continue

                # 路径 C: old_k 已经是 Out? (直接匹配)
                if old_k in new_valid_outs:
                    cleaned[old_k] = True

        return cleaned

    # --- 3. 输入类 ---
    # 输入类的 options 只是展示模版，不影响数据结构，直接保留旧数据
    # 输入类只有其数量、键名所依赖的类型为选项类，才有可能发生变化，
    # 这种情况直接交给前端显示时，如果发现存储里有一些键**“没被用到”**（即孤儿数据），就说明发生了键名不匹配
    # 我们直接把这些孤儿数据显示在黄色警告框里
    # 通常依赖的都同是输入类，这个时候用户不可能修改它们；因此输入类直接保持旧数据即可
    elif answer_type in ["正整数", "单行文本", "多行文本"]:
        return old_user_out

    return {}


def merge_data_with_template(user_data_full, template_data_full):
    """
    核心合并函数：
    1. 以新模版为基准。
    2. 检查结构性变更 (answer_type, accor, tolerance)。
    3. 若结构变更，废弃旧数据并存入 ref_old_data 快照。
    4. 若结构未变，执行 validate_user_output 清洗数据。
    """
    # 深拷贝新模版，确保逻辑和文字是最新的, 且不修改全局app.state.init_config_data字典
    merged_data = copy.deepcopy(template_data_full)

    # 建立旧数据索引 (node_id -> data)
    old_data_map = {}
    if user_data_full.get("data"):
        for k, v in user_data_full["data"].items():
            node_id = v.get("node_id")
            if node_id:
                old_data_map[str(node_id)] = v

    # 定义结构性字段，一旦这些变化，视为题目性质改变，必须重填
    structural_keys = ["answer_type", "input_num_accor", "input_name_accor", "input_tolerance"]

    # 遍历模板数据拷贝，将其处理成新需求数据
    for new_key, new_item in merged_data["data"].items():
        nid = str(new_item.get("node_id"))

        # 处理那些新需求模板里，选项ID也存在就配置文件的需求项
        # 新需求有单旧需求没有的，无需处理
        if nid in old_data_map:
            # 获取相同ID的旧需求数据
            old_item = old_data_map[nid]

            # 判断结构是否发生变化
            structure_changed = False
            # 遍历重点结构键
            for key in structural_keys:
                # 如果需求数据重点结构配置不一致
                if str(new_item.get(key)) != str(old_item.get(key)):
                    # 判定发生变化
                    structure_changed = True
                    break
            # 如果结构变了
            if structure_changed:
                # 强制重填，并创建快照
                new_item["user_must_out"] = {}
                new_item["option_tolerance_out"] = {}
                new_item["ref_out"] = []

                # 检查旧数据是否有实质内容，有则保存快照
                has_content = False
                if old_item.get("user_must_out"):
                    # 判断 value 是否非空，或 字典values是否包含True/非空字符
                    check_val = old_item["user_must_out"].get("value")
                    if (check_val is not None and str(check_val) != "") or any(old_item["user_must_out"].values()):
                        has_content = True
                # 存在非空有效旧数据
                if has_content:
                    new_item["ref_old_data"] = {
                        "main": old_item.get("user_must_out"),
                        "tolerance": old_item.get("option_tolerance_out"),
                        "ref": old_item.get("ref_out"),
                        "reason": "配置结构变更，请核对后重新录入",
                    }
            else:
                # 结构没变：清洗并保留数据
                new_item["user_must_out"] = validate_user_output(new_item, old_item)
                # 公差数据直接保留（因为前面已经校验过 input_tolerance 类型没变）
                new_item["option_tolerance_out"] = old_item.get("option_tolerance_out", {})
                # 引用文件直接保留
                new_item["ref_out"] = old_item.get("ref_out")

    # 迁移非结构性的项目元数据
    merged_data["file_dic"] = user_data_full.get("file_dic", {})
    merged_data["files"] = user_data_full.get("files", [])
    merged_data["deleted_files"] = user_data_full.get("deleted_files", [])
    merged_data["file_counter"] = user_data_full.get("file_counter", 0)
    merged_data["project_name"] = user_data_full.get("project_name", "")
    merged_data["version"] = user_data_full.get("version", "0.0")
    merged_data["original_project"] = user_data_full.get("original_project", "")
    merged_data["original_version"] = user_data_full.get("original_version", "0.0")
    merged_data["entry_status"] = user_data_full.get("entry_status", False)

    return merged_data


# 更新需求配置文件，供后续管理员调用
def update_config_service():
    try:
        app.state.init_config_data = app.state.config_service.load_config(force_reload=True)  # True表示强制重载需求文件

        logger.info("成功更新需求配置文件。")
        ui.notify(
            "需求配置文件更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        logger.error(f"更新需求配置文件失败：{e}")
        ui.notify(
            f'需求配置文件更新出错： "{e}" ',
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


# 更新需求配置文件，供后续管理员调用
def get_temp_config_service():
    try:
        app.state.config_service.get_temp_config()
        ui.notify(
            "临时需求配置文件生成校验完成!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        ui.notify(
            f'临时需求配置文件生成校验出错： "{e}" ',
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


# 更新需求节点与概述项的影响关系配置，并缓存到服务端通用内存。
def update_requirement_overview_impact_config() -> bool:
    valid_labels = set(app.storage.general.get("over_config_data_flat", {}))
    try:
        impact_config = load_requirement_overview_impact_config(valid_overview_labels=valid_labels)
    except RequirementOverviewImpactConfigError as exc:
        app.storage.general[REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY] = {
            "valid": False,
            "error": str(exc),
            "schema_version": None,
            "unmapped_policy": "block",
            "node_impacts": {},
        }
        logger.error("需求节点与概述影响配置加载失败：%s", exc)
        return False

    app.storage.general[REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY] = impact_config
    logger.info(
        "成功加载需求节点与概述影响配置：已配置 %s 个 node_id，未配置策略=%s。",
        len(impact_config["node_impacts"]),
        impact_config["unmapped_policy"],
    )
    return True


def get_requirement_overview_impacts(
    overview_data: dict,
    version: str,
    project_name: str = "",
) -> tuple[set[str], set[str], dict]:
    """使用内存配置解析指定需求版本影响的概述项。"""
    change_node_ids = collect_requirement_change_node_ids(overview_data, version)
    impact_config = app.storage.general.get(REQUIREMENT_OVERVIEW_IMPACT_STORAGE_KEY, {})
    all_overview_labels = set(app.storage.general.get("over_config_data_flat", {}))
    if project_name:
        # 兼容已从 overview_config 移除但数据库里仍存在的历史 label。
        all_overview_labels.update(db_storage.get_item(f"{project_name}_over_data", {}).keys())
    affected_labels, missing_node_ids = resolve_requirement_overview_impacts(
        change_node_ids,
        impact_config,
        all_overview_labels,
    )
    return affected_labels, missing_node_ids, change_node_ids


# 更新概述概述项配置设置
def updata_overview_config(*, show_notification: bool = True) -> bool:
    """同步概述配置和权限目录；后台调用时可关闭界面通知。"""
    try:
        # 每次都以配置文件为准，不以服务器现有数据为准
        # 配置更新能直接呈现，但配置减项将导致原有数据不呈现
        with open(f"{BASE_DIR}/overview_config.json", "r", encoding="utf-8") as f:
            # 使用 json.load() 读取文件内容并解析
            over_config_data = json.load(f)
            # 在覆盖内存配置前先校验全部 label，避免重复或非法标识污染运行数据和权限目录。
            project_overview_permission_definitions(over_config_data)
            user_service = getattr(app.state, "user_service", None)
            if user_service is not None and getattr(user_service, "storage_mode", "legacy_excel") == "database":
                # 先完成数据库目录同步；同步失败时不覆盖当前进程仍在使用的概述配置。
                user_service.sync_permission_catalog(
                    strict_overview=True,
                    overview_config=over_config_data,
                )
            # 为概述配置文件增加格式固定内容，在存放到app.storage.general
            for role, over_data_dic in over_config_data.items():
                for group_name, over_data_li in over_data_dic.items():
                    for over_data in over_data_li:
                        over_data["role"] = role
                        over_data["group_name"] = group_name
            app.storage.general["over_config_data"] = over_config_data
            # 扁平化概述项配置字典重新生成
            app.storage.general["over_config_data_flat"] = {}
            for role, role_dic in app.storage.general["over_config_data"].items():
                for group_name, group_li in role_dic.items():
                    for chip_dic in group_li:
                        app.storage.general["over_config_data_flat"][chip_dic.get("label")] = chip_dic
            update_requirement_overview_impact_config()
            logger.info("成功更新概述项配置。")
            # --- 新增：配置结构变更后，强制进行一次全局待定状态重构 ---
            update_overview_charge_pending_dic("all")
            logger.info("全局概述待定状态已基于最新配置文件重新构建。")
    except Exception as e:
        logger.error("更新概述项配置失败：%s", e, exc_info=True)
        if show_notification:
            try:
                ui.notify(f"概述配置更新失败：{e}", type="negative", multi_line=True)
            except RuntimeError:
                # 页面已关闭或后台任务没有 UI slot 时，日志已经保留真实错误，不再二次抛错。
                logger.warning("当前没有可用的 UI 上下文，已跳过概述配置失败通知。")
        return False

    if show_notification:
        try:
            ui.notify("概述配置与 label 级权限目录已同步。", type="positive")
        except RuntimeError:
            # 通知失败不能把已经成功完成的配置同步反向标记为业务失败。
            logger.warning("当前没有可用的 UI 上下文，已跳过概述配置成功通知。")
    return True


# 传入待判断字符串和正则表达式，输出判断结果
def validate_format_regex(s: str, ps: str) -> bool:
    """
    传入待判断字符串和正则表达式，输出判断结果

    Args:
        s: 待检查的字符串。
        ps: 传入正则表达式，必须用r"字符串"形式。

    Returns:
        如果字符串符合格式，则返回 True，否则返回 False。
    """
    # 编译正则表达式以提高性能（在多次调用时尤其有效）
    pattern = re.compile(f"{ps}")

    # re.fullmatch() 会尝试将整个字符串与模式进行匹配
    if pattern.fullmatch(s):
        return True
    else:
        return False


# 项目名切割处理函数
def project_name_process_string(s: str) -> str:
    """
    处理特定格式的字符串。
    如果字符串中包含至少两个 '-'，则移除第二个 '-' 及其后面的所有字符。
    否则，返回原始字符串。

    Args:
        s: 待处理的字符串。

    Returns:
        处理后的字符串。
    """
    # 使用 str.count() 方法判断 '-' 的出现次数，这是最直接可靠的方式。
    if s.count("-") >= 2:
        # 如果存在至少两个 '-'，我们找到第二个 '-' 的位置。
        # str.find() 只会找到第一个，因此我们使用 str.rfind() 从右边查找，
        # 或者使用 str.split() 更灵活地处理。

        # 拆分字符串，最多拆分两次
        parts = s.split("-", 2)

        # 将前两个部分重新组合，忽略第三个部分
        return f"{parts[0]}-{parts[1]}"
    else:
        # 如果 '-' 的数量少于两个，则原样返回
        return s


def project_table_update_config_update():
    try:
        # 解析JSON数据
        if os.path.exists(f"{BASE_DIR}/project_table_update_config.json"):
            with open(f"{BASE_DIR}/project_table_update_config.json", "r", encoding="utf-8") as f:
                app.storage.general["project_table_update_config"] = json.load(f)

        logger.info("成功更新项目表滚动信息关联配置。")
        ui.notify(
            "项目表滚动信息关联配置更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        logger.error(f"更新项目表滚动信息关联配置失败：{e}")
        ui.notify(
            f'项目表滚动信息关联配置更新出错： "{e}" ',
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


# 将项目摘要里手动控制的数据，以最高优先级添加/覆盖到服务器自动保存数据里
def project_summary_update():
    try:
        # 解析JSON数据
        if os.path.exists(f"{BASE_DIR}/data/project_summary.json"):
            project_data = {}
            with open(f"{BASE_DIR}/data/project_summary.json", "r", encoding="utf-8") as f:
                project_data = json.load(f)
                app.storage.general["project_summary"] = copy.deepcopy(project_data)
                app.storage.general["temp_project_name"] = []
            for project_name, data in project_data.items():
                # app.storage.general["project_summary"].setdefault(project_name, {})
                # 设置所有项目手动设置在json配置文件里的展示内容
                # app.storage.general["project_summary"][project_name].update(data)
                # 设置所有项目均一致的展示内容
                app.storage.general["project_summary"][project_name].update(
                    {
                        "sub_project": project_name,
                        "project": project_name_process_string(project_name),
                        # "requirement": "点击录入",
                        # "overview": "查阅整理",
                        "test_summary": "查阅打印",
                    }
                )
                # 将临时项目号增加到专门记录临时项目的服务器存储里
                if "RFTS" in project_name:
                    app.storage.general["temp_project_name"].append(project_name)

        logger.info("成功更新项目列表。")
        ui.notify(
            "项目列表更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        logger.info("更新项目列表失败。")
        ui.notify(
            f'项目列表更新出错： "{e}" ',
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


async def set_overview_active_state(
    project_name: str,
    ver: str,
    affected_labels: set[str] | None = None,
    *,
    rollback_context: dict | None = None,
) -> tuple[bool, set[str]]:
    """
    1. 适用于在项目概述内容复制了旧版本的记录后，统一处理新版本的激活状态记录。
    2. 所有概述项都会补齐到目标需求版本，避免精确按版本读取时把缺键误判成失活。
    3. 未受本次需求影响的概述继承上一版状态；受影响概述的 True/None 转为 None，False 保持 False。
    4. affected_labels=None 保留旧版兼容语义，即所有概述项均视为受影响。
    """
    req_ver = int(float(ver))
    req_ver_key = f"{req_ver}.0"
    changed_labels = set()
    affected_label_set = None if affected_labels is None else {str(label) for label in affected_labels}
    # 状态标记：用于记录是否需要触发前端 UI 通知
    ui_warning_needed = False
    warning_max_ver = 0
    version_conflict = False

    # 1. 定义数据更新的纯逻辑函数（将在锁的保护下执行）
    def process_active_state(overview_data):
        nonlocal ui_warning_needed, warning_max_ver, version_conflict  # 允许修改外部变量以回传状态

        if rollback_context is not None:
            rollback_context.clear()
            rollback_context["before"] = copy.deepcopy(overview_data or {})

        if not overview_data:
            if rollback_context is not None:
                rollback_context["after"] = copy.deepcopy(overview_data or {})
            return db_storage.ATOMIC_NO_UPDATE

        # 先完整预检，避免遍历到一半才发现数据库里已有更高版本而产生部分业务修改。
        for chip_dic in overview_data.values():
            for chip_data in chip_dic.values():
                numeric_versions = []
                for version_key in chip_data.get("select_activ_dic", {}):
                    try:
                        numeric_versions.append(int(float(version_key)))
                    except (TypeError, ValueError):
                        continue
                if numeric_versions and req_ver < max(numeric_versions):
                    version_conflict = True
                    ui_warning_needed = True
                    warning_max_ver = max(warning_max_ver, max(numeric_versions))

        if version_conflict:
            if rollback_context is not None:
                rollback_context["after"] = copy.deepcopy(overview_data)
            return db_storage.ATOMIC_NO_UPDATE

        # 遍历该项目概述内容，字典键为概述的各分类项，值为该项下chip字典
        for label, chip_dic in overview_data.items():
            label_changed = False
            label_is_affected = affected_label_set is None or label in affected_label_set
            # 遍历各个chip数据
            for chip_data in chip_dic.values():
                became_pending = False
                # 将chip数据里的选项激活设置字典的键，也就是版本整理成列表
                select_activ_dic = chip_data.get("select_activ_dic", {})
                over_chip_versions = []
                for version_key in select_activ_dic:
                    try:
                        over_chip_versions.append((int(float(version_key)), version_key))
                    except (TypeError, ValueError):
                        logger.warning(
                            "忽略无法解析的概述激活版本键: project=%s, label=%s, version=%r",
                            project_name,
                            label,
                            version_key,
                        )
                # 如果列表非空
                if over_chip_versions:
                    # 获取选项激活设置里最大的版本值
                    max_over_ver, max_over_ver_key = max(over_chip_versions, key=lambda item: item[0])

                    # 适用于正常项目迭代，无论是原项目升版本异或其它项目衍生过来升版本，
                    # 概述内容不会复制，需求版本值肯定大于激活设置的最大版本值
                    # 由指定版本衍生到另外一个新项目，需求版本2.0，概述复制了参照项目的指定版本激活设置，并先记录为目标项目1.0版本概述，需求版本值肯定大于激活设置的最大版本值
                    if req_ver > max_over_ver:
                        # 获取激活设置最大版本值对应的状态，并逐版本向前继承。
                        previous_state = select_activ_dic.get(max_over_ver_key)
                        # 从现有激活设置最大版本值+1到当前需求版本值开始生成键值对
                        for key in range(max_over_ver + 1, req_ver + 1):
                            new_state = previous_state
                            # 只有本次目标版本、且命中影响配置时，才把原激活状态降为待定。
                            if key == req_ver and label_is_affected and previous_state is not False:
                                new_state = None
                                became_pending = previous_state is True
                            select_activ_dic[f"{key}.0"] = new_state
                            previous_state = new_state
                            label_changed = True

                    if label_is_affected and select_activ_dic.get(req_ver_key) is None:
                        # 将这个存在未手动选择激活状态的chip的相关状态配置成特殊显示
                        # 设置为None，这个chip的内容在项目总表展示时才会表明待选择处理
                        if (
                            chip_data.get("enabled") is not None
                            or chip_data.get("icon") != "question_mark"
                            or chip_data.get("bg_color") != "bg-amber-5"
                        ):
                            chip_data["enabled"] = None
                            chip_data["icon"] = "question_mark"
                            chip_data["bg_color"] = "bg-amber-5"
                            label_changed = True
                    if became_pending:
                        append_overview_timestamp(
                            chip_data,
                            creator=get_overview_latest_responsible(project_name, label, chip_data),
                            reason=get_automatic_overview_reason("requirement_pending"),
                        )
            if label_changed:
                changed_labels.add(label)
        if not changed_labels:
            if rollback_context is not None:
                rollback_context["after"] = copy.deepcopy(overview_data)
            return db_storage.ATOMIC_NO_UPDATE
        if rollback_context is not None:
            rollback_context["after"] = copy.deepcopy(overview_data)
        return overview_data

    # 2. 执行原子更新
    success = await db_storage.atomic_deep_update([f"{project_name}_over_data"], process_active_state)

    # 3. 释放锁后，再根据记录的状态安全地触发前端 UI 通知
    if ui_warning_needed:
        from nicegui import ui  # 确保作用域内可用

        ui.notify(
            f"传入的需求版本{req_ver}小于{project_name}概述激活记录最高版本{warning_max_ver}，不做处理。",
            type="warning",
            position="bottom",
            timeout=3000,
            progress=True,
            close_button="✖",
        )

    return success and not version_conflict, changed_labels


async def restore_overview_active_state(
    project_name: str,
    before_data: dict,
    expected_current_data: dict,
) -> bool:
    """仅在概述仍等于本次审批结果时，补偿恢复审批前快照。"""
    rollback_conflict = False

    def restore_if_unchanged(current_data):
        nonlocal rollback_conflict
        current_normalized = current_data or {}
        if current_normalized != (expected_current_data or {}):
            rollback_conflict = True
            return db_storage.ATOMIC_NO_UPDATE
        return copy.deepcopy(before_data or {})

    success = await db_storage.atomic_deep_update(
        [f"{project_name}_over_data"],
        restore_if_unchanged,
    )
    if rollback_conflict:
        logger.critical("概述审批补偿回滚遇到并发修改，已拒绝覆盖: project=%s", project_name)
    return success and not rollback_conflict


def refresh_overview_pending_labels(project_name: str, labels: set[str]) -> None:
    """利用服务端内存中的概述配置和负责人信息，只刷新指定概述项的待办状态。"""
    over_config_flat = app.storage.general.get("over_config_data_flat", {})
    overview_role = app.storage.general.get("overview_role", {}).get(project_name, {})
    for label in labels:
        role = over_config_flat.get(label, {}).get("role")
        latest_user_raw = overview_role.get(role, {}).get("latest_user", "") if role else ""
        latest_user = latest_user_raw.split("：", 1)[1] if "：" in latest_user_raw else latest_user_raw
        if latest_user:
            update_overview_charge_pending_dic(
                scope="local",
                des_user=latest_user,
                project_name=project_name,
                des_label=label,
            )


async def copy_overview_data(project_name, version, target_project_name) -> None:
    """
    用于将某个项目某个版本的概述内容复制衍生成一个 “新项目的初版” 概述，原概述为激活或待定状态则统一处理为待定状态，原概述为禁用状态则统一处理为禁用状态。

    Args:
        project_name：概述来源项目名
        version：概述来源版本
        target_project_name：复制到的目标项目

    """
    # 1. 读取源数据（纯读操作，不加锁）
    SOURCE_OVERVIEW_DATA = db_storage.get_item(f"{project_name}_over_data", {})
    if not SOURCE_OVERVIEW_DATA:
        return

    # 2. 定义处理逻辑（将源数据转换为目标新数据）
    def init_target_data(current_target_data):
        # 如果目标项目已经有数据了，出于安全考虑，您可以选择直接返回现有数据(不覆盖)，或者进行合并
        # 这里假设您的业务逻辑是：只有当目标是空的时候才执行复制初始化
        if current_target_data:
            return current_target_data

        # 在这里直接修改源数据副本，安全且不污染原始缓存
        for chip_dic in SOURCE_OVERVIEW_DATA.values():
            for chip_data in chip_dic.values():
                reference_state = chip_data.get("select_activ_dic", {}).get(version)
                chip_data["select_activ_dic"] = {"1.0": reference_state}

                if chip_data.get("timestamp"):
                    last_timestamp = chip_data["timestamp"].popitem()
                    last_timestamp[1]["select_activ_dic"] = {"1.0": reference_state}
                    chip_data["timestamp"] = {last_timestamp[0]: last_timestamp[1]}

                if reference_state:
                    chip_data["enabled"] = True
                    chip_data["icon"] = (
                        "attachment"
                        if chip_data.get("type") == "file"
                        else ("image" if chip_data.get("type") == "image" else None)
                    )
                    chip_data["bg_color"] = "bg-light-blue-1"
                elif reference_state is None:
                    chip_data["enabled"] = None
                    chip_data["icon"] = "question_mark"
                    chip_data["bg_color"] = "bg-amber-5"
                else:
                    chip_data["enabled"] = False
                    chip_data["icon"] = "block"
                    chip_data["bg_color"] = "bg-grey-5"

        return SOURCE_OVERVIEW_DATA

    # 3. 原子化地更新目标项目
    await db_storage.atomic_deep_update([f"{target_project_name}_over_data"], init_target_data)


# 请确保头部已经引入了 update_overview_charge_pending_dic 函数


def parse_overview_timestamp(value: str) -> datetime:
    """兼容秒级和带微秒的历史概述时间。"""
    return datetime.fromisoformat(str(value))


def format_overview_timestamp(value: object) -> str:
    """把概述时间统一显示到秒，无法识别时保留原值。"""
    text = str(value or "")
    try:
        return parse_overview_timestamp(text).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return text


def overview_role_update(project_name, input_role="all_update"):
    """
    app.storage.general["overview_role"][project_name]={"光学":{"most_user":"用户名","latest_user":"用户名"},...}
    当input_role传入initialize时，只初始化准备好相应键值对，用于UI元素绑定；
    当input_role传入all_update时，更新整个项目的责任人信息；
    当input_role传入具体role时，更新项目指定角色的责任人信息；
    """
    # 将服务器概述资料获取到
    OVERVIEW_DATA = db_storage.get_item(f"{project_name}_over_data", {})
    # ---------------------------------------------------------
    # 核心新增：封装交接逻辑闭包函数，用于处理待办数据的抹除与继承
    # ---------------------------------------------------------
    def _execute_role_handover(role_name, new_user):
        over_role_dic = app.storage.general["overview_role"][project_name]
        old_user_raw = over_role_dic.get(role_name, {}).get("latest_user", "")
        old_user = old_user_raw.split("：")[1] if "：" in old_user_raw else old_user_raw

        # 只有负责人发生实质性变化时，才执行清洗和重建
        if old_user != new_user:
            pending_storage = app.storage.general.setdefault("overview_charge_pending", {})
            role_config = app.storage.general.get("over_config_data", {}).get(role_name, {})

            for group_li in role_config.values():
                for chip_dic in group_li:
                    label = chip_dic.get("label")
                    if not label:
                        continue

                    # 步骤 A: 抹除原负责人的待办数据（精准修剪）
                    if old_user and old_user in pending_storage:
                        if project_name in pending_storage[old_user]:
                            pending_storage[old_user][project_name].pop(label, None)
                            if not pending_storage[old_user][project_name]:
                                pending_storage[old_user].pop(project_name, None)

                    # 步骤 B: 为新负责人极速刷新待办状态（完美继承）
                    if new_user and new_user != "——":
                        update_overview_charge_pending_dic("local", new_user, project_name, label)

            # 步骤 C: 清理旧负责人的空节点，防止内存泄漏
            if old_user and old_user in pending_storage and not pending_storage[old_user]:
                pending_storage.pop(old_user, None)

    # ---------------------------------------------------------

    def _collect_role_statistics(over_data_dic: dict) -> tuple[dict, dict, str, datetime | None]:
        frequency_user_dic: dict[str, int] = {}
        original_user_time_dic: dict[str, datetime] = {}
        latest_operator = ""
        latest_operation_time: datetime | None = None
        for over_config_li in over_data_dic.values():
            for over_config in over_config_li:
                for over_data in OVERVIEW_DATA.get(over_config.get("label"), {}).values():
                    latest_time_text, latest_record = get_latest_overview_record(over_data)
                    if not latest_time_text:
                        continue
                    try:
                        operation_time = parse_overview_timestamp(latest_time_text)
                    except (TypeError, ValueError):
                        continue
                    original_creator = str(over_data.get("creator") or latest_record.get("creator") or "未知")
                    frequency_user_dic[original_creator] = frequency_user_dic.get(original_creator, 0) + 1
                    if operation_time > original_user_time_dic.get(original_creator, datetime.min):
                        original_user_time_dic[original_creator] = operation_time
                    operator = str(latest_record.get("creator") or original_creator)
                    if latest_operation_time is None or operation_time > latest_operation_time:
                        latest_operator = operator
                        latest_operation_time = operation_time
        return frequency_user_dic, original_user_time_dic, latest_operator, latest_operation_time

    def _apply_role_statistics(role_name: str, over_data_dic: dict) -> None:
        frequency_user_dic, original_time_dic, latest_user, latest_time = _collect_role_statistics(over_data_dic)
        if not frequency_user_dic:
            return
        over_role_dic = app.storage.general["overview_role"][project_name]
        max_value = max(frequency_user_dic.values())
        most_users = [user for user, count in frequency_user_dic.items() if count == max_value]
        most_user = max(most_users, key=lambda user: original_time_dic.get(user, datetime.min))
        over_role_dic[role_name]["most_user"] = f"最多：{most_user}"

        if not latest_user or latest_time is None:
            return
        latest_des_time = over_role_dic[role_name].get("latest_designation_time", "")
        if (
            "最近指定" in over_role_dic[role_name].get("latest_user", "")
            and latest_des_time
            and latest_time < parse_overview_timestamp(latest_des_time)
        ):
            return
        _execute_role_handover(role_name, latest_user)
        over_role_dic[role_name]["latest_user"] = f"最近：{latest_user}"

    # 如果项目名不存在服务器概述数据的键里
    if project_name not in app.storage.general["overview_role"]:
        temp_dic = {}
        for role in app.storage.general["over_config_data"].keys():
            temp_dic[role] = {"most_user": "", "latest_user": ""}
        app.storage.general["overview_role"][project_name] = temp_dic

    # 兼容 "all" 或 "all_update" 的传参
    elif input_role != "initialize" and input_role in ["all", "all_update"]:
        for role, over_data_dic in app.storage.general["over_config_data"].items():
            _apply_role_statistics(role, over_data_dic)

    elif input_role != "initialize" and input_role:
        _apply_role_statistics(input_role, app.storage.general["over_config_data"].get(input_role, {}))


# 在指定目录中查找包含特定前缀的文件名，并提取版本号
def find_files_with_prefix_and_version(directory, prefix):
    """
    description:
        在指定目录中查找包含特定前缀的文件名，并提取版本号

    Args:
        directory: 要搜索的目录路径
        prefix: 文件名中需要包含的前缀字符串（如"RFFM-1519-A"）

    Returns:
        字典: 以完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
    """
    result_dic = {}

    # 验证目录是否存在
    if not os.path.exists(directory):
        logger.info(f"错误：目录 {directory} 不存在")
        return result_dic
    if not prefix:
        logger.info(f"错误项目名： {prefix} ")
        return result_dic

    # 编译正则表达式：匹配前缀 + 提取版本号
    # 解释：前缀任意字符 + 下划线 + "V" + 1个或多个数字（捕获组） + 文件结束
    pattern = re.compile(rf".*{re.escape(prefix)}.*_V(\d+)\.(\d+).json")

    # 遍历目录中的每个文件
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)

        # 确保是文件而不是目录
        if os.path.isfile(file_path):
            # 尝试匹配正则表达式

            match = pattern.search(filename)
            if match:
                # 提取版本号并添加到结果
                version_a = match.group(1)
                version_b = match.group(2)
                result_dic[f"{version_a}.{version_b}"] = {
                    "name": filename,
                    "v_a": version_a,
                    "v_b": version_b,
                }

    return result_dic


# 提取需求节点真正参与版本差异判断的业务答案。
def get_effective_requirement_output(item: dict) -> object:
    """忽略选项型答案中仅用于 UI 绑定的未选值。

    多选在载入新模板时会为每个选项补 ``False``，这些键的增删不代表用户答案变化；
    单选则只关心当前选中值。其他题型保持原有完整字典比较口径。
    """
    user_output = item.get("user_must_out", {})
    if not isinstance(user_output, dict):
        return user_output

    answer_type = item.get("answer_type")
    if answer_type == "多选":
        return frozenset(str(key) for key, selected in user_output.items() if selected)

    if answer_type in {"单选", "下拉单选"}:
        selected_value = user_output.get("value")
        if selected_value is None or selected_value == "":
            return None
        return str(selected_value)

    return user_output


# 对比两个需求配置文件的需求确认项的差异
def compare_configs_by_id(old_data, new_data, add_options: list = []) -> dict:
    """
    通过唯一ID对比两个配置字典的变化。
    {
        "added": {"id":{字典内容},},
        "deleted": {"id":{字典内容},},
        "modified": {"id":{
                            "old_data": {字典内容},
                            "new_data": {字典内容},
                        },
                    }
    }
    """
    added_items = {}
    deleted_items = {}
    modified_items = {}
    if old_data:
        old_ids = {v["node_id"] for v in old_data.values()}
        new_ids = {v["node_id"] for v in new_data.values()}

        for id in new_ids - old_ids:
            for k, v in new_data.items():
                if id == v["node_id"]:
                    v["num"] = k
                    added_items[id] = v
        for id in old_ids - new_ids:
            for k, v in old_data.items():
                if id == v["node_id"]:
                    v["num"] = k
                    deleted_items[id] = v

        common_ids = old_ids & new_ids

        for id in common_ids:
            old_item = {}
            for k, v in old_data.items():
                if id == v["node_id"]:
                    v["num"] = k
                    old_item = v
                    break
            new_item = {}
            for k, v in new_data.items():
                if id == v["node_id"]:
                    v["num"] = k
                    new_item = v
                    break

            # 重点检查三个用户数据字段
            keys_to_check = ["user_must_out", "option_tolerance_out", "ref_out"]
            is_modified = False
            for key in keys_to_check:
                if key == "user_must_out":
                    old_value = get_effective_requirement_output(old_item)
                    new_value = get_effective_requirement_output(new_item)
                else:
                    old_value = old_item.get(key)
                    new_value = new_item.get(key)
                if old_value != new_value:
                    is_modified = True
                    break

            # 如果需要，也可以检查其他字段的变化，例如 guide_content
            if "guide_content" in add_options and old_item.get("guide_content") != new_item.get("guide_content"):
                is_modified = True

            if is_modified:
                modified_items[id] = {
                    "old_data": old_item,
                    "new_data": new_item,
                }
    else:
        new_ids = {v["node_id"] for v in new_data.values()}
        added_items = {}
        for id in new_ids:
            for k, v in new_data.items():
                if id == v["node_id"]:
                    v["num"] = k
                    added_items[id] = v
    added_items = dict(sorted(added_items.items(), key=lambda item: int(float(item[1]["num"]))))
    return {"added": added_items, "deleted": deleted_items, "modified": modified_items}


# 提取传入的需求配置文件里的待显示信息,即变动信息
async def extract_requirement(over_data_file_dic, file_path) -> dict:
    pattern = re.compile(r"(.*_V)(\d+)\.(\d+).json")
    match = pattern.search(file_path)
    version_a = 1
    old_file_path = ""
    file_path_a = ""
    if match:
        file_path_a = match.group(1)
        version_a = int(match.group(2)) - 1
        old_file_path = f"{file_path_a}{version_a}.0.json"
    # 读取和解析JSON文件
    old_data = {"data": {}}
    new_data = {}
    latest_data = {}
    try:
        ui.notify(
            "跳转中......",
            type="info",
            position="bottom",
            timeout=2000,
            progress=True,
            close_button="✖",
        )
        # 第一步，准备旧版本数据
        # 获取更早版本文件数据，如果没有，将当做空数据来处理
        if version_a >= 1:
            while not os.path.exists(old_file_path) and version_a > 0:
                ui.notify(
                    f"上一个版本V{version_a}.0的需求配置文件可能丢失，将与更早版本做对比记录！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                await asyncio.sleep(2)
                version_a -= 1
                old_file_path = f"{file_path_a}{version_a}.0.json"
            if os.path.exists(old_file_path):
                with open(old_file_path, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
            else:
                ui.notify(
                    "完全找不到任何低版本需求配置文件，只能将本次需求作为全新记录！",
                    type="warning",
                    position="bottom",
                    timeout=3000,
                    progress=True,
                    close_button="✖",
                )
                await asyncio.sleep(2)
        # 当前版本的上一版为0.0，意味着当前版本为初版1.0
        # else:
        #     ui.notify(
        #         "本次处理需求为初版，将做第一次记录！",
        #         type="info",
        #         position="bottom",
        #         timeout=2000,
        #         progress=True,
        #         close_button="✖",
        #     )
        #     await asyncio.sleep(2)

        # 第二部，准备新版本数据
        # 检查传入地址是否存在文件
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                new_data = json.load(f)
        else:
            ui.notify(
                "本次处理处理的需求配置文件未找到，无法处理！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            await asyncio.sleep(2)
            return {}
    except Exception as e:
        ui.notify(
            f"读取或解析文件时出错: {e}",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )
        await asyncio.sleep(2)
        return {}

    # 第三步，将新旧版本数据进行对比
    extract_data = compare_configs_by_id(old_data["data"], new_data["data"])
    extract_data["file_dic"] = over_data_file_dic | new_data.get("file_dic", {})
    extract_data["deleted_files"] = list(set(old_data.get("deleted_files", []) + new_data.get("deleted_files", [])))
    extract_data["file_counter"] = new_data["file_counter"]
    extract_data["project_name"] = new_data["project_name"]
    extract_data["current_user"] = new_data["current_user"]
    extract_data["version"] = new_data["version"]
    extract_data["original_version"] = new_data["original_version"]
    extract_data["original_project"] = new_data["original_project"]
    extract_data["req_timestamp"] = new_data["req_timestamp"]
    # 将最新数据提取出来
    latest_data["added"] = new_data["data"]
    latest_data["file_dic"] = over_data_file_dic | new_data.get("file_dic", {})
    latest_data["deleted_files"] = list(set(old_data.get("deleted_files", []) + new_data.get("deleted_files", [])))
    latest_data["file_counter"] = new_data["file_counter"]
    latest_data["project_name"] = new_data["project_name"]
    latest_data["current_user"] = new_data["current_user"]
    latest_data["version"] = new_data["version"]
    latest_data["original_version"] = new_data["original_version"]
    latest_data["original_project"] = new_data["original_project"]
    latest_data["req_timestamp"] = new_data["req_timestamp"]
    return {"contrast": extract_data, "latest": latest_data}


def move_element(lst, element, step: int):
    """
    将列表中的指定元素向前移动一步。

    Args:
        lst (list): 待操作的列表。
        element: 要移动的元素。
        step: 元素要移动的步距，负值向前，正值向后
    Returns:
        list: 移动后的新列表。如果元素不存在或已经在最前面，则返回原列表。
    """
    if element not in lst:
        logger.info(f"警告：'{element}' 不存在于列表中。")
        return lst

    current_index = lst.index(element)

    # 如果元素已经是第一个，则不能再向前移动
    if step < 0 and current_index == 0:
        return lst
    elif step > 1 and current_index == len(lst) - 1:
        return lst

    # 弹出元素
    value_to_move = lst.pop(current_index)

    # 插入到新位置
    lst.insert(current_index + step, value_to_move)

    return lst


# 获取传入字典的最大数字键，非数字键不计入
def get_max_numeric_key(d):
    numeric_keys = []
    for k in d.keys():
        try:
            numeric_keys.append((float(k), k))  # (数值, 原始键)
        except ValueError:
            pass  # 忽略无法转换成数字的键
    if not numeric_keys:
        return None  # 没有数字键时返回 None
    return max(numeric_keys, key=lambda x: x[0])[1]  # 返回原始键


# 获取当前系统时间并以指定时间格式返回
def get_time():
    # 获取当前的 datetime 对象
    now = datetime.now()
    # 使用 strftime 方法格式化时间
    # %Y: 四位数的年份 (例如 2023)
    # %m: 两位数的月份 (01-12)
    # %d: 两位数的日期 (01-31)
    # %H: 24 小时制的小时数 (00-23)
    # %M: 两位数的分钟数 (00-59)
    # %S: 两位数的秒数 (00-59)
    formatted_time = now.strftime("%Y年%m月%d日%H时%M分%S秒")
    return formatted_time


# 计算文件的哈希值
def get_file_hash(file_path, algorithm="md5"):
    """
    计算文件的哈希值
    :param file_path: 文件路径
    :param algorithm: 哈希算法，默认为 'md5'，可选 'sha1', 'sha256' 等
    :return: 哈希值的十六进制字符串
    """
    # 创建哈希对象
    if algorithm.lower() == "md5":
        hash_obj = hashlib.md5()
    elif algorithm.lower() == "sha1":
        hash_obj = hashlib.sha1()
    elif algorithm.lower() == "sha256":
        hash_obj = hashlib.sha256()
    else:
        raise ValueError("算法未经证实，请使用：'md5'，'sha1'，或'sha256'。")

    # 打开文件并分块读取
    hash_obj = hashlib.md5()
    try:
        with open(file_path, "rb") as file:
            while chunk := file.read(4096):  # 分块读取，每块 4096 字节
                hash_obj.update(chunk)
    except FileNotFoundError:
        return "文件未找到"
    except Exception as e:
        return f"文件读取报错: {e}"

    # 返回哈希值的十六进制字符串
    return hash_obj.hexdigest()


def move_file_with_timestamp_pathlib(source_file_path: str, destination_dir: str) -> str:
    """
    使用 pathlib 将文件移动到新目录，并在文件名（扩展名前）附加时间戳。
    """
    try:
        source_path = Path(source_file_path)
        dest_dir = Path(destination_dir)

        # 1. 检查源文件
        if not source_path.is_file():
            raise FileNotFoundError(f"错误：源文件未找到: {source_path}")

        # 2. 确保目标目录存在
        dest_dir.mkdir(parents=True, exist_ok=True)

        # 3. 生成时间戳
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # 4. 获取文件名部分
        base_name = source_path.stem  # "my_data_file"
        extension = source_path.suffix  # ".log"

        # 5. 创建新文件名
        new_filename = f"{base_name}_{timestamp}{extension}"

        # 6. 创建完整目标路径
        new_destination_path = dest_dir / new_filename

        # 7. 执行移动（重命名）
        # .replace() 在功能上等同于 os.rename() 或 shutil.move()
        source_path.replace(new_destination_path)

        ui.notify(
            f"文件成功移动到: {new_destination_path}",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
        return str(new_destination_path)  # 以字符串形式返回路径

    except FileNotFoundError as e:
        ui.notify(
            f"错误: {e}",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )
        raise
    except Exception as e:
        ui.notify(
            f"移动文件时发生未知错误: {e}",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )
        raise


# 删除指定路径的文件
def delete_file(file_path):
    try:
        # 2. 尝试删除文件
        os.remove(file_path)
        ui.notify(
            f"文件 '{file_path}' 已成功删除。",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except FileNotFoundError:
        pass
        # 3. 处理文件不存在的错误
        # ui.notify(
        #     f"错误：文件 '{file_path}' 未找到。",
        #     type="negative",
        #     position="center",
        #     timeout=0,
        #     progress=False,
        #     close_button="✖",
        # )
    except PermissionError:
        # 4. 处理权限不足的错误
        ui.notify(
            f"错误：没有权限删除文件 '{file_path}'。",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )
    except IsADirectoryError:
        # 5. 处理试图删除目录的错误
        ui.notify(
            f"错误：'{file_path}' 是一个目录，不能使用 os.remove() 删除。",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )
        # (注意：删除空目录请使用 os.rmdir(), 删除非空目录请使用 shutil.rmtree())
    except Exception as e:
        # 6. 捕获其他可能的异常
        ui.notify(
            f"删除文件时发生未知错误: {e}",
            type="negative",
            position="center",
            timeout=0,
            progress=False,
            close_button="✖",
        )


# 注销登录处理函数
def logout():
    del app.storage.user["current_user"]
    del app.storage.user["is_admin"]
    del app.storage.user["current_role"]
    ui.navigate.to("/login")


# 元素的显示函数
def ui_show(ui):
    key_state = app.storage.client.setdefault("key_state", {})
    if "ctrl" in key_state.keys() and key_state["ctrl"] == 9:
        ui.style("display: block;")


# 元素的隐藏函数
def ui_hide(ui):
    ui.style("display: none;")


# 查找字典指定健的索引
def find_key_position(dictionary, target_key):
    for index, key in enumerate(dictionary.keys()):
        if key == target_key:
            return index
    return -1


# 查阅指定项目概述文件里的最新需求内容，整理待显示的定制内容标签为列表（除重处理），保存到服务器层级储存里
def set_project_custom_labels(project_name):
    overview_file_path = os.path.join(OVER_DIR, f"{project_name}_概述整理.json")

    if not os.path.exists(overview_file_path):
        # logger.info(f"整理项目{project_name}的定制内容标签时，概述整理文件不存在。")
        return
    overviow_data = {}
    label_list = []
    try:
        with open(overview_file_path, "r", encoding="utf-8") as f:
            # 使用 json.load() 读取文件内容并解析
            overviow_data = json.load(f)
    except json.JSONDecodeError:
        logger.error(
            f"错误：整理项目{project_name}的定制内容标签时，文件 '{overview_file_path}' 不是有效的 JSON 格式。",
            exc_info=True,
        )
        return
    except Exception:
        logger.error(f"整理项目{project_name}的定制内容标签时，读取文件时发生其他错误", exc_info=True)
        return
    # 获取最新版需求配置文件内容
    latest_data = overviow_data.get("0").get("added")
    if not latest_data:
        logger.info(f"整理项目{project_name}的定制内容标签时，最新需求配置内容为空。")
        return
    for num, data in latest_data.items():
        answer_type = data["answer_type"]
        must_out_dic = data["user_must_out"]
        options_list = data["options"]
        # 该需求配置项存在输出标签配置
        if any([op_dic["option_label"] for op_dic in options_list]):
            for op_dic in options_list:
                op_dic_label = (
                    app.storage.general["config_service_custom_labels"]
                    .get(data["node_id"], {})
                    .get(op_dic["option_id"], "")
                )
                # 当前需求项里的这个选填项存在输出标签
                if op_dic["option_label"] and op_dic_label:
                    # 如果是单选，且 用户选择的输出值与选项输出配置值匹配
                    if (
                        "单选" in answer_type
                        and op_dic["option_out"] == must_out_dic.get("value")
                        and op_dic_label not in label_list
                    ):
                        label_list.append(op_dic_label)
                    # 如果是多选，且 该选填项对应显示值在用户选择的输出字典里对应的布尔值是true
                    elif (
                        "多选" in answer_type
                        and must_out_dic.get(op_dic["option_out"])
                        and op_dic_label not in label_list
                    ):
                        label_list.append(op_dic_label)
                    # 如果是文本类型
                    elif answer_type in ["正整数", "单行文本", "多行文本"]:
                        add_str = "，".join(must_out_dic.values())
                        if add_str:
                            label_str = op_dic_label.replace("{V}", add_str)
                            if label_str not in label_list:
                                label_list.append(label_str)
    app.storage.general["custom_labels"][project_name] = label_list


def _canonical_requirement_version(version) -> str:
    return f"{int(float(version))}.0"


def _write_json_atomic(file_path: str | Path, data: dict[str, object]) -> None:
    """在目标文件同目录完整写入后再原子替换，避免读到半截 JSON。"""
    destination = Path(file_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as file_obj:
            json.dump(data, file_obj, indent=4, ensure_ascii=False)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def snapshot_file_bytes(file_path: str | Path) -> tuple[bool, bytes]:
    path = Path(file_path)
    if not path.exists():
        return False, b""
    return True, path.read_bytes()


def restore_file_bytes(file_path: str | Path, existed: bool, content: bytes) -> None:
    """补偿恢复文件快照；原文件不存在时删除本次新生成的文件。"""
    destination = Path(file_path)
    if not existed:
        destination.unlink(missing_ok=True)
        return
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.rollback")
    try:
        with temp_path.open("wb") as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def create_requirement_overview_candidate_path(project_name: str, version: str) -> str:
    version_key = _canonical_requirement_version(version)
    return str(
        Path(OVER_DIR) / f".{project_name}_概述整理_V{version_key}_{uuid.uuid4().hex}.pending.json"
    )


# 根据传入的需求配置文件清单，核对检查是否有新需求配置未更新到概述文件里，并做相应整理，更新概述整理文件
async def requirement_version_tidy(
    project_name,
    review: bool,
    *,
    target_version: str | None = None,
    output_path: str | Path | None = None,
) -> str:
    """
    将需求配置文件与概述整理文件进行比对和更新。

    target_version 用于审批：目标版本即使仍为“待审”也会进入候选结果，但任何更高版本都不会混入。
    output_path 用于把结果写到不可见候选文件；未传时保持原有正式/审核预览路径行为。
    """
    project_exists_file_raw = find_files_with_prefix_and_version(REQ_DIR, project_name)
    overview_file_path = Path(OVER_DIR) / f"{project_name}_概述整理.json"
    overview_file_path_temp = Path(OVER_DIR) / f"{project_name}_概述整理_temp.json"
    if not project_exists_file_raw:
        ui.notify(
            "无该项目需求文件，暂时开放概述整理。",
            type="warning",
            position="bottom",
            timeout=3000,
            progress=True,
            close_button="✖",
        )
        return ""

    project_exists_file = {
        _canonical_requirement_version(version): file_info
        for version, file_info in project_exists_file_raw.items()
    }
    available_versions = sorted(int(float(version)) for version in project_exists_file)
    wait_review_data = app.storage.general.get("wait_review", {}).get(project_name, {})

    if target_version is not None:
        target_version_key = _canonical_requirement_version(target_version)
        target_version_number = int(float(target_version_key))
        if target_version_key not in project_exists_file:
            logger.error("审批目标需求文件不存在: project=%s, version=%s", project_name, target_version_key)
            return ""

        lower_unapproved = [
            _canonical_requirement_version(version)
            for version in available_versions
            if version < target_version_number
            and wait_review_data.get(_canonical_requirement_version(version), {"state": "已审"}).get("state")
            != "已审"
        ]
        if lower_unapproved:
            logger.error(
                "审批目标版本之前仍存在未通过版本: project=%s, target=%s, versions=%s",
                project_name,
                target_version_key,
                lower_unapproved,
            )
            ui.notify("目标版本之前仍有未通过评审的需求，已中止审批。", type="negative", position="center")
            return ""
        selected_versions = [version for version in available_versions if version <= target_version_number]
    elif review:
        # 审核预览维持原语义：展示当前已有的全部需求版本。
        selected_versions = available_versions
    elif wait_review_data:
        approved_versions = [
            version
            for version in available_versions
            if wait_review_data.get(_canonical_requirement_version(version), {"state": "已审"}).get("state") == "已审"
        ]
        if not approved_versions:
            ui.notify(
                "该项目不存在审核通过的需求，无法查阅！",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return ""
        max_approved_version = max(approved_versions)
        selected_versions = [version for version in available_versions if version <= max_approved_version]
    else:
        selected_versions = available_versions

    if not selected_versions:
        return ""

    # 根节点同时包含版本字典、字符串版本号和布尔标记，必须显式声明为异构 JSON 对象；
    # 否则 Pylance 会按首个字面量推断为 dict[str, dict]，并拒绝后续写入 str/bool。
    overview_data: dict[str, object] = {"0": {"file_dic": {}}}
    overview_version = None
    if overview_file_path.exists():
        try:
            with overview_file_path.open("r", encoding="utf-8") as file_obj:
                loaded_overview_data = json.load(file_obj)
            if not isinstance(loaded_overview_data, dict):
                raise TypeError("概述整理文件根节点必须是 JSON 对象")
            overview_data = loaded_overview_data
            raw_overview_version = overview_data.get("version")
            if not isinstance(raw_overview_version, (str, int, float)) or isinstance(raw_overview_version, bool):
                raise TypeError("概述整理文件 version 必须是数字或数字字符串")
            overview_version = int(float(raw_overview_version))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.error("读取正式概述整理文件失败: %s", overview_file_path, exc_info=True)
            return ""

        if max(selected_versions) < overview_version:
            ui.notify(
                "出现需求配置丢失现象，请联系管理员处理，否则该项目资料将一直无法展示！",
                type="warning",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
            return ""

    for version_number in selected_versions:
        if overview_version is not None and version_number < overview_version:
            continue
        version_key = _canonical_requirement_version(version_number)
        latest_section = overview_data.get("0")
        latest_file_dic = {}
        if isinstance(latest_section, dict):
            raw_file_dic = latest_section.get("file_dic")
            if isinstance(raw_file_dic, dict):
                latest_file_dic = raw_file_dic
        temp_dict = await extract_requirement(
            latest_file_dic,
            os.path.join(REQ_DIR, project_exists_file[version_key]["name"]),
        )
        if not temp_dict:
            logger.error("整理需求版本失败: project=%s, version=%s", project_name, version_key)
            return ""
        overview_data[version_key] = temp_dict["contrast"]
        overview_data["0"] = temp_dict["latest"]
        overview_data["version"] = version_key
        overview_data["first_create"] = overview_version is None

    destination = Path(output_path) if output_path is not None else (
        overview_file_path_temp if review else overview_file_path
    )
    try:
        _write_json_atomic(destination, overview_data)
        return str(destination)
    except (OSError, TypeError, ValueError):
        logger.error("原子写入概述整理文件失败: %s", destination, exc_info=True)
        return ""


async def prepare_requirement_version_tidy(project_name: str, target_version: str) -> tuple[str, dict]:
    """生成审批专用候选概述文件，并重新读取验证其内容。"""
    candidate_path = create_requirement_overview_candidate_path(project_name, target_version)
    written_path = await requirement_version_tidy(
        project_name,
        False,
        target_version=target_version,
        output_path=candidate_path,
    )
    if not written_path:
        return "", {}
    try:
        with open(written_path, "r", encoding="utf-8") as file_obj:
            candidate_data = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        logger.error("审批候选概述文件复读校验失败: %s", written_path, exc_info=True)
        Path(written_path).unlink(missing_ok=True)
        return "", {}
    return written_path, candidate_data


def build_overview_page_url(
    *,
    review: bool,
    overview_file_path: str = "",
    project_name: str = "",
    correction_label: str = "",
    correction_chip_id: str = "",
) -> str:
    """构造概述页地址，并可携带一次性的纠错弹窗定位参数。"""
    query = {
        "type": "temp_overview" if review else "overview",
    }
    if overview_file_path:
        query["json_path"] = overview_file_path
    elif project_name:
        query["project_name"] = project_name
    if correction_label and correction_chip_id:
        query["correction_label"] = correction_label
        query["correction_chip_id"] = correction_chip_id
    return f"/main/requirement?{urlencode(query)}"


async def get_overviow_page(
    project_name,
    review: bool,
    *,
    correction_label: str = "",
    correction_chip_id: str = "",
):
    """
    project_name： 项目名。
    review：是否为了审核需求，True为了审核，False普通浏览概述
    """
    # 核对检查是否有新需求配置未更新到概述文件里，并做相应整理
    overview_file_path = await requirement_version_tidy(project_name, review)
    if overview_file_path:
        ui.navigate.to(
            build_overview_page_url(
                review=review,
                overview_file_path=overview_file_path,
                correction_label=correction_label,
                correction_chip_id=correction_chip_id,
            )
        )
    else:
        ui.navigate.to(
            build_overview_page_url(
                review=False,
                project_name=project_name,
                correction_label=correction_label,
                correction_chip_id=correction_chip_id,
            )
        )


def get_project_engineer_project_list_dic():
    """
    获得所有扮演项目工程师的{项目工程师名:[负责项目,负责项目]}
    """
    project_engineer_dic = {}
    for pn, project_engineer in app.storage.general["project_engineer"].items():
        if project_engineer and project_engineer not in project_engineer_dic:
            project_engineer_dic.update({project_engineer: [pn]})
        elif project_engineer:
            project_engineer_dic[project_engineer].append(pn)
    return project_engineer_dic
