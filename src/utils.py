# -*- encoding: utf-8 -*-

import asyncio
import copy
import hashlib
import json
import mimetypes
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Final

from nicegui import app, ui
from nicegui.events import KeyEventArguments

from . import db_storage

# import config
from .config import AVATAR_DIR, AVATAR_URL_DIR, BASE_DIR, IMG_DIR, IMG_URL_DIR, OVER_DIR, REQ_DIR


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
        # print(f"✅ 通过扩展名推断的文件类型: {mime_type}")
        return mime_type, encoding
    else:
        # 尝试通过文件扩展名本身作为类型
        extension = p.suffix.lower().lstrip(".")
        if extension:
            # print(f"⚠️ 无法推断 MIME 类型，但文件扩展名为: {extension}")
            return f"extension/{extension}", None  # 格式化为非官方 MIME 类型
        else:
            # print("❌ 路径无扩展名，无法推断类型。")
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
    print(f"内层开始查找目标文件夹{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
    print(f"内层结束查找目标文件夹{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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

    except Exception as e:
        print(f"Error generating cache-busted path for {web_path}: {e}")
        return web_path  # 出错时返回原始路径


# 更新所有用户密码与角色数据
def update_users_data():
    try:
        app.state.users_data = app.state.user_service.load_users()
        ui.notify(
            "用户配置数据更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        ui.notify(
            f'用户配置数据更新出错： "{e}" ',
            type="negative",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )


# 全局键盘事件跟踪处理函数
def handle_key(e: KeyEventArguments):
    if e.modifiers.ctrl and e.action.keydown:
        app.storage.client["key_state"]["ctrl"] = 9
    else:
        app.storage.client["key_state"]["ctrl"] = 0

    if e.key.enter and e.action.keydown:
        app.storage.client["key_state"]["enter"] = 1
        # app.storage.client["key_state"]["enter"] = 0


# 更新需求配置文件，供后续管理员调用
def update_config_service():
    try:
        app.state.init_config_data = app.state.config_service.load_config()
        ui.notify(
            "需求配置文件更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        ui.notify(
            f'需求配置文件更新出错： "{e}" ',
            type="negative",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )


# 更新概述概述项配置设置
def updata_overview_config():
    try:
        # 每次都以配置文件为准，不以服务器现有数据为准
        # 配置更新能直接呈现，但配置减项将导致原有数据不呈现
        with open(f"{BASE_DIR}/overview_config.json", "r", encoding="utf-8") as f:
            # 使用 json.load() 读取文件内容并解析
            app.storage.general["over_config_data"] = json.load(f)
            ui.notify(
                "概述项配置更新成功!",
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
    except Exception as e:
        ui.notify(
            f'概述项配置文件更新出错： "{e}" ',
            type="negative",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )


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


def project_overview_config_update():
    try:
        # 解析JSON数据
        if os.path.exists(f"{BASE_DIR}/project_overview_config.json"):
            with open(f"{BASE_DIR}/project_overview_config.json", "r", encoding="utf-8") as f:
                app.storage.general["project_overview_config"] = json.load(f)
        ui.notify(
            "项目表滚动信息关联配置更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        ui.notify(
            f'项目表滚动信息关联配置更新出错： "{e}" ',
            type="negative",
            position="bottom",
            timeout=0,
            progress=False,
            close_button="✖",
        )


# 将项目摘要里手动控制的数据，以最高优先级添加/覆盖到服务器自动保存数据里
def project_summary_update():
    try:
        # 解析JSON数据
        if os.path.exists(f"{BASE_DIR}/project_summary.json"):
            project_data = {}
            with open(f"{BASE_DIR}/project_summary.json", "r", encoding="utf-8") as f:
                project_data = json.load(f)
                app.storage.general["project_summary"] = copy.deepcopy(project_data)
            for project_name, data in project_data.items():
                # app.storage.general["project_summary"].setdefault(project_name, {})
                # 设置所有项目手动设置在json配置文件里的展示内容
                # app.storage.general["project_summary"][project_name].update(data)
                # 设置所有项目均一致的展示内容
                app.storage.general["project_summary"][project_name].update(
                    {
                        "sub_project": project_name,
                        "project": project_name_process_string(project_name),
                        "requirement": "点击录入",
                        "overview": "查阅整理",
                    }
                )
        ui.notify(
            "项目列表更新成功!",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
    except Exception as e:
        ui.notify(
            f'项目列表更新出错： "{e}" ',
            type="negative",
            position="bottom",
            timeout=0,
            progress=False,
            close_button="✖",
        )


async def copy_overview_data(project_name, version, target_project_name):
    """
    用于将某个项目某个版本的概述内容复制衍生成一个 “新项目的初版” 概述

    Args:
        project_name：概述来源项目名
        version：概述来源版本
        target_project_name：复制到的目标项目

    """
    overview_data = copy.deepcopy(db_storage.get_item(f"{project_name}_over_data", {}))
    # 整理设置1.0版本概述激活状态，清空参照项目可能多出的版本激活记录，只复制参照版激活记录
    for chip_dic in overview_data.values():
        # 遍历各个chip数据
        for chip_data in chip_dic.values():
            # 获取参考版本记录的激活状态
            reference_state = chip_data["select_activ_dic"][version]
            # 清空激活状态字典
            chip_data["select_activ_dic"] = {}
            # 1.0版本概述状态保留参考项目概述的参考版本记录
            chip_data["select_activ_dic"]["1.0"] = reference_state
            # 获取参考版本激活修改记录最后一个记录
            last_timestamp = chip_data["timestamp"].popitem()
            # 将记录跟1.0版本记录对齐
            last_timestamp[1]["select_activ_dic"] = {"1.0": reference_state}
            # 清空激活状态修改记录
            chip_data["timestamp"] = {}
            chip_data["timestamp"][last_timestamp[0]] = last_timestamp[1]
            # 对齐设置chip状态参数
            if reference_state:
                chip_data["enabled"] = True
                if chip_data["type"] == "file":
                    chip_data["icon"] = "attachment"
                else:
                    chip_data["icon"] = None
                chip_data["bg_color"] = "bg-light-blue-1"
            elif reference_state is None:
                chip_data["enabled"] = None
                chip_data["icon"] = "question_mark"
                chip_data["bg_color"] = "bg-amber-5"
            else:
                chip_data["enabled"] = False
                chip_data["icon"] = "block"
                chip_data["bg_color"] = "bg-grey-5"

    await db_storage.set_item(f"{target_project_name}_over_data", overview_data)


# 更新概述工程角色统计结果
def overview_role_update(project_name):
    """
    app.storage.general["overview_role"][project_name]={"光学":{"most_user":"用户名","latest_user":"用户名"},...}
    """
    # 将服务器概述资料获取到
    OVERVIEW_DATA: Final[dict] = db_storage.get_item(f"{project_name}_over_data", {})
    # 设置时间对象识别格式
    format_string = "%Y-%m-%d %H:%M:%S"
    # 如果项目名存在服务器概述数据的键里
    if project_name not in app.storage.general["overview_role"]:
        temp_dic = {}
        for over_class in app.storage.general["over_config_data"].keys():
            temp_dic[over_class] = {"most_user": "", "latest_user": ""}
        app.storage.general["overview_role"][project_name] = temp_dic
    else:
        # 初始化概述角色字典
        over_role_dic = app.storage.general["overview_role"][project_name]
        # 遍历概述配置字典，主要用里面的角色分类，如光学、结构等等，和概述配置里的label
        for over_class, over_config_li in app.storage.general["over_config_data"].items():
            # 初始化临时保存概述里出现过的用户次数字典
            frequency_user_dic = {}
            # 初始化临时保存概述里出现过的用户最晚时间字典
            time_user_dic = {}
            # 遍历当前角色分类，如光学下，概述配置的各项
            for over_config in over_config_li:
                # 如果当前概述项的label存在服务器对应项目的概述数据字典键里
                if over_config["label"] in OVERVIEW_DATA and OVERVIEW_DATA[over_config["label"]] != {}:
                    # 遍历当前label下用户添加过的多个概述数据
                    for over_data in OVERVIEW_DATA[over_config["label"]].values():
                        # 如果数据的创建用户已经存在临时记录字典里
                        if over_data["creator"] in frequency_user_dic:
                            # 将该用户创建次数加1次
                            frequency_user_dic[over_data["creator"]] = frequency_user_dic[over_data["creator"]] + 1
                            # 生成用户本次概述创建的时间对象
                            time_obj_new = datetime.strptime(next(reversed(over_data["timestamp"])), format_string)
                            # 获取已保存的该用户概述最晚创建时间对象
                            time_obj_old = time_user_dic[over_data["creator"]]
                            # 两个时间对比，如果本次时间比已保存的时间更晚
                            if time_obj_new > time_obj_old:
                                # 将本次时间更新为该用户所有概述的最晚创建时间
                                time_user_dic[over_data["creator"]] = time_obj_new
                        # 如果数据的创建用户不存在临时记录字典里
                        else:
                            # 记该用户创建一次
                            frequency_user_dic[over_data["creator"]] = 1
                            # 记该用户首次创建时间
                            time_user_dic[over_data["creator"]] = datetime.strptime(
                                next(reversed(over_data["timestamp"])), format_string
                            )
            # 当前角色的所有概述存在创建记录
            if frequency_user_dic != {}:
                # 找到临时保存用户创建概述次数字典里，所有次数的最大值
                max_value = max(frequency_user_dic.values())
                # 找到跟最大次数相同的对应所有用户
                most_user_li = [key for key, value in frequency_user_dic.items() if value == max_value]
                # 如果有多个人都创建了最大次数
                if len(most_user_li) > 1:
                    # 找到这些人创建概述数据的最晚时间
                    lat_time = max([time_user_dic[user] for user in most_user_li])
                    # 找到这些人里哪个人是最晚创建概述的
                    for user in most_user_li:
                        if time_user_dic[user] == lat_time:
                            # 将找到的用户定义为概述创建最多次的人
                            over_role_dic[over_class]["most_user"] = f"最多：{user}"
                # 如果创建次数最多的情况只有一个人
                else:
                    # 将这个用户定义为概述创建最多次的人
                    over_role_dic[over_class]["most_user"] = f"最多：{most_user_li[0]}"

                # 找出临时保存用户最晚创建概述时间里最晚的时间点
                latest_time = max(list(time_user_dic.values()))
                # 找出最晚创建概述的用户
                for user in time_user_dic.keys():
                    if time_user_dic[user] == latest_time:
                        # 将这个用户定义为最晚创建概述的人
                        over_role_dic[over_class]["latest_user"] = f"最近：{user}"

        # 将最终各角色模块找到的最多与最晚创建者字典更新到对应项目键值对里
        # app.storage.general["overview_role"][project_name] = over_role_dic


# 在指定目录中查找包含特定前缀的文件名，并提取版本号
def find_files_with_prefix_and_version(directory, prefix):
    """
    在指定目录中查找包含特定前缀的文件名，并提取版本号

    Args:
        directory: 要搜索的目录路径
        prefix: 文件名中需要包含的前缀字符串（如"RFFM-1519-A"）

    Returns:
        字典以完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
    """
    result_dic = {}

    # 验证目录是否存在
    if not os.path.exists(directory):
        print(f"错误：目录 {directory} 不存在")
        return result_dic
    if not prefix:
        print(f"错误项目名： {prefix} ")
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
                if old_item.get(key) != new_item.get(key):
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
        # 第一步，准备旧版本数据
        # 获取更早版本文件数据，如果没有，将当做空数据来处理
        if version_a >= 1:
            while not os.path.exists(old_file_path) and version_a > 0:
                ui.notify(
                    f"上一个版本V{version_a}.0的需求配置文件可能丢失，将与更早版本做对比记录！",
                    type="negative",
                    position="bottom",
                    timeout=0,
                    progress=False,
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
                    type="negative",
                    position="bottom",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                await asyncio.sleep(2)
        # 当前版本的上一版为0.0，意味着当前版本为初版1.0
        else:
            ui.notify(
                "本次处理需求为初版，将做第一次记录！",
                type="info",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            await asyncio.sleep(2)

        # 第二部，准备新版本数据
        # 检查传入地址是否存在文件
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                new_data = json.load(f)
        else:
            ui.notify(
                "本次处理处理的需求配置文件未找到，无法处理！",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            await asyncio.sleep(2)
            return {}
    except Exception as e:
        ui.notify(f"读取或解析文件时出错: {e}", color="negative")
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
        print(f"警告：'{element}' 不存在于列表中。")
        return lst

    current_index = lst.index(element)

    # 如果元素已经是第一个，则不能再向前移动
    if step < 0 and current_index == 0:
        # print(f"'{element}' 已在最前面，无法再向前移动。")
        return lst
    elif step > 1 and current_index == len(lst) - 1:
        # print(f"'{element}' 已在最后面，无法再向后移动。")
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


def delete_file(file_path):
    try:
        # 2. 尝试删除文件
        os.remove(file_path)
        ui.notify(
            f"文件 '{file_path}' 已成功删除。",
            type="positive",
            position="bottom",
            timeout=2000,
            progress=True,
            close_button="✖",
        )
    except FileNotFoundError:
        # 3. 处理文件不存在的错误
        ui.notify(
            f"错误：文件 '{file_path}' 未找到。",
            type="warning",
            position="bottom",
            timeout=2000,
            progress=True,
            close_button="✖",
        )
    except PermissionError:
        # 4. 处理权限不足的错误
        ui.notify(
            f"错误：没有权限删除文件 '{file_path}'。",
            type="warning",
            position="bottom",
            timeout=2000,
            progress=True,
            close_button="✖",
        )
    except IsADirectoryError:
        # 5. 处理试图删除目录的错误
        ui.notify(
            f"错误：'{file_path}' 是一个目录，不能使用 os.remove() 删除。",
            type="warning",
            position="bottom",
            timeout=2000,
            progress=True,
            close_button="✖",
        )
        # (注意：删除空目录请使用 os.rmdir(), 删除非空目录请使用 shutil.rmtree())
    except Exception as e:
        # 6. 捕获其他可能的异常
        ui.notify(
            f"删除文件时发生未知错误: {e}",
            type="warning",
            position="bottom",
            timeout=2000,
            progress=True,
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
    if "ctrl" in app.storage.client["key_state"].keys() and app.storage.client["key_state"]["ctrl"] == 9:
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
    overviow_data = {}
    label_list = []
    try:
        with open(overview_file_path, "r", encoding="utf-8") as f:
            # 使用 json.load() 读取文件内容并解析
            overviow_data = json.load(f)
    except json.JSONDecodeError:
        print(f"错误：整理项目{project_name}的定制内容标签时，文件 '{overview_file_path}' 不是有效的 JSON 格式。")
        return
    except Exception as e:
        print(f"整理项目{project_name}的定制内容标签时，读取文件时发生其他错误：{e}")
        return
    # 获取最新版需求配置文件内容
    latest_data = overviow_data.get("0").get("added")
    if not latest_data:
        print(f"整理项目{project_name}的定制内容标签时，最新需求配置内容为空。")
        return
    for num, data in latest_data.items():
        answer_type = data["answer_type"]
        must_out_dic = data["user_must_out"]
        options_list = data["options"]
        # 该需求配置项存在输出标签配置
        if any([op_dic["option_label"] for op_dic in options_list]):
            for op_dic in options_list:
                # 当前需求项里的这个选填项存在输出标签
                if op_dic["option_label"]:
                    # 如果是单选，且 用户选择的输出值与选项输出配置值匹配
                    if (
                        "单选" in answer_type
                        and op_dic["option_out"] == must_out_dic.get("value")
                        and op_dic["option_label"] not in label_list
                    ):
                        label_list.append(op_dic["option_label"])
                    # 如果是多选，且 该选填项对应显示值在用户选择的输出字典里对应的布尔值是true
                    elif (
                        "多选" in answer_type
                        and must_out_dic.get(op_dic["option_content"])
                        and op_dic["option_label"] not in label_list
                    ):
                        label_list.append(op_dic["option_label"])
                    # 如果是文本类型
                    elif answer_type in ["正整数", "单行文本", "多行文本"]:
                        add_str = "，".join(must_out_dic.values())
                        if add_str:
                            label_str = op_dic["option_label"].replace("{V}", add_str)
                            if label_str not in label_list:
                                label_list.append(label_str)
    app.storage.general["custom_labels"][project_name] = label_list


# 根据传入的需求配置文件清单，核对检查是否有新需求配置未更新到概述文件里，并做相应整理，更新概述整理文件
async def requirement_version_tidy(project_name, review: bool) -> str:
    """
    project_name： 项目名。
    review：是否为了审核需求，True为了审核，False普通浏览概述
    """
    # 查找指定路径下，含有提供项目名的文件，得到一个字典，"完整版本" 为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
    project_exists_file = find_files_with_prefix_and_version(REQ_DIR, project_name)
    overview_file_path = os.path.join(OVER_DIR, f"{project_name}_概述整理.json")
    overview_file_path_temp = os.path.join(OVER_DIR, f"{project_name}_概述整理_temp.json")
    overviow_data = {}
    overviow_data["0"] = {"file_dic": {}}
    if project_exists_file:  # 完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
        project_version_li = [float(s) for s in project_exists_file.keys()]

        # 为了审核跳转的概述界面，不执行该块代码，直接呈现所有需求整理结果
        if not review:
            # 将版本列表按降序排列
            project_version_li.sort(reverse=True)
            # 从高版本需求遍历到低版本
            for v in project_version_li:
                project_name = project_exists_file[str(v)]["name"].split("_")[0]
                # old_data_path = os.path.join(REQ_DIR, project_exists_file[str(v)]["name"])
                # with open(old_data_path, "r", encoding="utf-8") as f:
                #     # 使用 json.load() 读取文件内容并解析
                #     old_data = json.load(f)
                # 遍历直至遇到已审状态的需求
                # if old_data.get("review_state", True):
                if app.storage.general["wait_review"].get(project_name, {}):
                    if (
                        app.storage.general["wait_review"][project_name].get(str(v), {"state": "已审"})["state"]
                        == "已审"
                    ):
                        # 当前版本需求已审核过了，可以开始处理继续处理概述
                        # 退出遍历处理
                        break
                    # 未审的需求，其版本号删掉，不参与后续需求概述整理
                    else:
                        project_version_li.remove(v)

        # 如果处理后的需求列表为空，即所有需求均未审
        if not project_version_li:
            ui.notify(
                "该项目不存在审核通过的需求，无法查阅！",
                type="info",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )
            return ""
        # 将版本列表按照升序排序
        project_version_li.sort()
        v_max = max(project_version_li)

        if os.path.exists(overview_file_path):
            try:
                with open(overview_file_path, "r", encoding="utf-8") as f:
                    # 使用 json.load() 读取文件内容并解析
                    overviow_data = json.load(f)
            except json.JSONDecodeError:
                print(f"错误：文件 '{overview_file_path}' 不是有效的 JSON 格式。")
                return ""
            except Exception as e:
                print(f"读取文件时发生其他错误：{e}")
                return ""
            overviow_version = float(overviow_data["version"])
            # 可追加情况
            if v_max > overviow_version:
                # 遍历需求配置文件版本号
                for pro_ver in project_version_li:
                    # 版本小于概述整理文件版本的跳过
                    if pro_ver <= overviow_version:
                        continue
                    # 以项目配置文件 版本 为键，该版本配置文件的 增删改内容及状态信息 为值，保存到概述字典里
                    temp_dict = await extract_requirement(
                        overviow_data["0"]["file_dic"],
                        os.path.join(REQ_DIR, project_exists_file[str(pro_ver)]["name"]),
                    )
                    if temp_dict:
                        overviow_data[str(pro_ver)] = temp_dict["contrast"]
                        overviow_data["0"] = temp_dict["latest"]
                        overviow_data["version"] = str(pro_ver)
                        overviow_data["first_create"] = False
                # 将字典转换为 JSON 字符串
                overviow_str = json.dumps(overviow_data, indent=4, ensure_ascii=False)
                # 写入文
                if review:
                    with open(overview_file_path_temp, "w", encoding="utf-8") as f:
                        f.write(overviow_str)
                    # print(f"临时概述文件新版内容写入成功：{overview_file_path_temp}")
                    return overview_file_path_temp
                else:
                    with open(overview_file_path, "w", encoding="utf-8") as f:
                        f.write(overviow_str)
                    # print(f"概述文件新版内容写入成功：{overview_file_path}")
                    return overview_file_path

            elif v_max == overviow_version:
                # 虽然需求没有新版本，但概述文件已经不是第一次创建
                # 也需将标记改为False，防止初版概述chip激活状态修改记录被抹除
                if overviow_data["first_create"]:
                    overviow_data["first_create"] = False
                    # 将字典转换为 JSON 字符串
                    overviow_str = json.dumps(overviow_data, indent=4, ensure_ascii=False)
                    # 写入文件
                    with open(overview_file_path, "w", encoding="utf-8") as f:
                        f.write(overviow_str)
                return overview_file_path
            else:
                ui.notify(
                    "出现需求配置丢失现象，请联系管理员处理，否则该项目资料将一直无法展示！",
                    type="warning",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return ""
        # 初次生成概述文件
        else:
            for pro_ver in project_version_li:
                # 以项目配置文件 版本 为键，该版本配置文件的 增删改内容及状态信息 为值，保存到概述字典里
                temp_dict = await extract_requirement(
                    overviow_data["0"]["file_dic"], os.path.join(REQ_DIR, project_exists_file[str(pro_ver)]["name"])
                )
                if temp_dict:
                    overviow_data[str(pro_ver)] = temp_dict["contrast"]
                    overviow_data["0"] = temp_dict["latest"]
                    overviow_data["version"] = str(pro_ver)
                    overviow_data["first_create"] = True
            # 将字典转换为 JSON 字符串
            overviow_str = json.dumps(overviow_data, indent=4, ensure_ascii=False)
            # print(f"准备写入的 data 数据: {data}")
            if review:
                with open(overview_file_path_temp, "w", encoding="utf-8") as f:
                    f.write(overviow_str)
                # print(f"临时概述文件新版内容写入成功：{overview_file_path_temp}")
                return overview_file_path_temp
            else:
                with open(overview_file_path, "w", encoding="utf-8") as f:
                    f.write(overviow_str)
                # print(f"概述文件新版内容写入成功：{overview_file_path}")
                return overview_file_path
    else:
        ui.notify(
            "无该项目需求配置文件，无法整理。",
            type="warning",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )
        await asyncio.sleep(2)
        return ""


async def get_overviow_page(project_name, review: bool):
    """
    project_name： 项目名。
    review：是否为了审核需求，True为了审核，False普通浏览概述
    """
    # 核对检查是否有新需求配置未更新到概述文件里，并做相应整理
    overview_file_path = await requirement_version_tidy(project_name, review)
    if overview_file_path:
        if review:
            ui.navigate.to(f"/main/requirement?type=temp_overview&json_path={overview_file_path}")
        else:
            ui.navigate.to(f"/main/requirement?type=overview&json_path={overview_file_path}")
