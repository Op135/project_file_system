# -*- encoding: utf-8 -*-
import ast
import asyncio
import copy
import hashlib
import io
import itertools
import json
import math
import os
import re
import subprocess
import sys
import uuid
import warnings
from datetime import datetime
from itertools import islice
from pathlib import Path

import wcwidth
from html_sanitizer import Sanitizer
from nicegui import app, events, ui
from nicegui.events import GenericEventArguments, KeyEventArguments, MouseEventArguments, UploadEventArguments

import db_storage  # 导入我们创建的模块

# from nicegui_toolkit import inject_layout_tool
from config_service import ConfigService
from user_service import UserService

# 忽略所有来自 openpyxl 的 UserWarning
# 这样可以精确地屏蔽掉这个警告，而不影响其他库的警告
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# 从环境变量中读取密钥。如果找不到，则使用一个仅供本地开发的默认值。
# 在生产环境中，必须设置环境变量，否则会使用不安全的默认值。
ST = os.environ.get("STORAGE_SECRET", "this_is_not_a_secret_for_development_only")

# 初始化配置服务
user_service = UserService()
users_data = user_service.load_users()
# 获取最新配置文件，待用
config_service = ConfigService()
init_config_data = config_service.load_config()

# 文件夹路径设定
BASE_DIR = Path(__file__).parent.parent  # 项目根目录
IMG_DIR = f"{BASE_DIR}/img"
UPLOADS_DIR = f"{BASE_DIR}/uploads"
SUBMIT_FILES_DIR = Path(f"{BASE_DIR}/files")
REQ_DIR = f"{BASE_DIR}/req"
OVER_DIR = f"{BASE_DIR}/over"
os.makedirs(IMG_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(SUBMIT_FILES_DIR, exist_ok=True)
os.makedirs(REQ_DIR, exist_ok=True)
os.makedirs(OVER_DIR, exist_ok=True)
# URL路径设定
UPLOAD_URL_DIR = "/uploads"
FILES_URL_DIR = "/files"

# ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "pdf"}
# MAX_FILE_SIZE = 20 * 1024 * 1024

# 注册 NiceGUI 的生命周期事件
# 在服务器启动时调用 init_db
app.on_startup(db_storage.init_db)
# 在服务器关闭时调用 close_db
app.on_shutdown(db_storage.close_db)

# 存储服务器层级 概述数据 的变量初始化
# app.storage.general.setdefault("overview_data", {})
# 存储服务器层级 项目需求最高版本号 的变量初始化
app.storage.general.setdefault("project_req_max_ver", {})
# 存储服务器层级 项目简介 的变量初始化
app.storage.general.setdefault("project_summary", {})
# 存储服务器层级 项目简介与概述数据动态更新配置 的变量初始化
app.storage.general.setdefault("project_overview_config", {})
# 存储服务器层级 各项目各工程角色概述数据负责人 的变量初始化
# 这个变量用于保存overview_config.json配置文件数据，在概述界面得到赋值
over_config_data = {}
app.storage.general.setdefault("overview_role", {})
# 存储服务器层级 各项目负责销售 的变量初始化
app.storage.general.setdefault("project_sale", {})
# 储存服务器层级 等待审核的项目需求即待审版本
app.storage.general.setdefault("wait_review", {})


# 更新所有用户密码与角色数据
def update_users_data():
    global users_data
    try:
        users_data = user_service.load_users()
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
    global init_config_data
    try:
        init_config_data = config_service.load_config()
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


# 将项目摘要里手动控制的数据，以最高优先级添加/覆盖到服务器自动保存数据里
def project_summary_update():
    # 解析JSON数据
    if os.path.exists(f"{BASE_DIR}/project_summary.json"):
        project_data = {}
        with open(f"{BASE_DIR}/project_summary.json", "r", encoding="utf-8") as f:
            project_data = json.load(f)
        with open(f"{BASE_DIR}/project_overview_config.json", "r", encoding="utf-8") as f:
            app.storage.general["project_overview_config"] = json.load(f)
        for project_name, data in project_data.items():
            app.storage.general["project_summary"].setdefault(project_name, {})
            # 设置所有项目手动设置在json配置文件里的展示内容
            app.storage.general["project_summary"][project_name].update(data)
            # 设置所有项目均一致的展示内容
            app.storage.general["project_summary"][project_name].update(
                {
                    "sub_project": project_name,
                    "project": project_name_process_string(project_name),
                    "requirement": "点击录入",
                    "overview": "查阅整理",
                }
            )


# 更新概述工程角色统计结果
def overview_role_update(project_name):
    """
    app.storage.general["overview_role"][project_name]={"光学":{"most_user":"用户名","latest_user":"用户名"},...}
    """
    # 将服务器概述资料获取到
    overview_data = db_storage.get_item(f"{project_name}_over_data", {})
    # 设置时间对象识别格式
    format_string = "%Y-%m-%d %H:%M:%S"
    # 如果项目名存在服务器概述数据的键里
    if project_name not in app.storage.general["overview_role"]:
        temp_dic = {}
        for over_class in over_config_data.keys():
            temp_dic[over_class] = {}
        app.storage.general["overview_role"][project_name] = temp_dic
    else:
        # 初始化概述角色字典
        over_role_dic = app.storage.general["overview_role"][project_name]
        # 遍历概述配置字典，主要用里面的角色分类，如光学、结构等等，和概述配置里的label
        for over_class, over_config_li in over_config_data.items():
            # 初始化临时保存概述里出现过的用户次数字典
            frequency_user_dic = {}
            # 初始化临时保存概述里出现过的用户最晚时间字典
            time_user_dic = {}
            # 遍历当前角色分类，如光学下，概述配置的各项
            for over_config in over_config_li:
                # 如果当前概述项的label存在服务器对应项目的概述数据字典键里
                if over_config["label"] in overview_data and overview_data[over_config["label"]] != {}:
                    # 遍历当前label下用户添加过的多个概述数据
                    for over_data in overview_data[over_config["label"]].values():
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
        app.storage.general["overview_role"][project_name] = over_role_dic


# 在指定目录中查找包含特定前缀的文件名，并提取版本号
def find_files_with_prefix_and_version(directory, prefix):
    """
    在指定目录中查找包含特定前缀的文件名，并提取版本号

    参数:
    directory: 要搜索的目录路径
    prefix: 文件名中需要包含的前缀字符串（如"RFFM-1519-A"）

    返回:
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
                    print(f"临时概述文件新版内容写入成功：{overview_file_path_temp}")
                    return overview_file_path_temp
                else:
                    with open(overview_file_path, "w", encoding="utf-8") as f:
                        f.write(overviow_str)
                    print(f"概述文件新版内容写入成功：{overview_file_path}")
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
                print(f"临时概述文件新版内容写入成功：{overview_file_path_temp}")
                return overview_file_path_temp
            else:
                with open(overview_file_path, "w", encoding="utf-8") as f:
                    f.write(overviow_str)
                print(f"概述文件新版内容写入成功：{overview_file_path}")
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


# ======================
# 登录界面
# ======================
# 设置根路径重定向
@ui.page("/")
def root():
    ui.navigate.to("/login")  # 自动跳转至登录页


@ui.page("/login")
def login_page():
    # 用于记录键盘按键状态
    app.storage.client.setdefault("key_state", {})

    # 登录处理函数
    def try_login():
        # 处理非空密码情况
        input_username = str(username_input.value).strip()
        input_password = str(password_input.value).strip()

        try:
            # 获取对应用户的密码与角色组成的字典{'password': 'xxx', 'role': 'user'}
            user_info = user_service.get_user(input_username)
            if not user_info:
                ui.notify(
                    "用户不存在", type="warning", position="bottom", timeout=1000, progress=True, close_button="✖"
                )
                return

            # 正常密码验证流程
            if str(user_info.get("password", "")) == input_password:
                app.storage.user.update(
                    {
                        "current_user": input_username,
                        "is_admin": check_admin_role(input_username),
                        "current_role": user_info.get("role", "user"),
                    }
                )
                # 跳转到主界面
                ui.navigate.to("/main")
            else:
                ui.notify("密码错误", type="negative", position="bottom", timeout=1000, progress=True, close_button="✖")
        except Exception as e:
            ui.notify(
                f"登录失败: {str(e)}", type="negative", position="bottom", timeout=1000, progress=True, close_button="✖"
            )

    # 修改密码处理函数
    def change_password():
        # 处理非空密码情况
        input_username = str(username_input.value).strip()
        input_password = str(password_input.value).strip()

        try:
            # 获取对应用户的密码与角色组成的字典{'password': 'xxx', 'role': 'user'}
            user_info = user_service.get_user(input_username)
            if not user_info:
                ui.notify(
                    "用户不存在", type="warning", position="bottom", timeout=1000, progress=True, close_button="✖"
                )
                return

            # 正常密码验证流程
            if str(user_info.get("password", "")) == input_password:
                set_password(input_username)
            else:
                ui.notify("密码错误", type="negative", position="bottom", timeout=1000, progress=True, close_button="✖")
        except Exception as e:
            ui.notify(
                f"密码修改触发失败: {str(e)}",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # 密码比对函数
    def submit(new_pwd, confirm_pwd, target_username):
        if new_pwd.value != confirm_pwd.value:
            ui.notify(
                "两次输入密码不一致", type="warning", position="bottom", timeout=1000, progress=True, close_button="✖"
            )
            return
        try:
            success = user_service.update_password(target_username, new_pwd.value)
            if success:
                ui.notify(
                    "密码设置成功，正在跳转...",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
                # 跳转到登录页面
                ui.navigate.to("/login")
        except Exception as e:
            ui.notify(
                f"密码设置失败: {str(e)}",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # 密码设置函数
    def set_password(target_username: str):
        with (
            ui.dialog().props("persistent w-full") as dialog_set_password,
            ui.card().classes("w-1/4 p-4 bg-white shadow-md"),
        ):
            with ui.column().classes("w-full p-4"):
                ui.label("请设置密码").classes("text-lg")
                new_pwd = ui.input("新密码", password=True).props("type=password autofocus")
                confirm_pwd = ui.input("确认密码", password=True).props("type=password")

            with ui.row().classes("w-full p-4 flex-nowrap"):
                ui.button("提交", on_click=lambda: submit(new_pwd, confirm_pwd, target_username)).classes("w-1/2")
                ui.button("取消", on_click=lambda: dialog_set_password.close()).classes("w-1/2")
        dialog_set_password.open()

    # 返回用户是否为管理员的布尔值
    def check_admin_role(username: str) -> bool:
        try:
            return users_data.get(username, {}).get("role") == "admin"
        except Exception as e:
            ui.notify(
                f"权限验证失败: {str(e)}",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            return False

    # 实时检测是否需要设置初始密码
    def check_initial_password():
        input_username = username_input.value.strip()
        if not input_username:  # or not enable_event:
            return
        try:
            # 获取对应用户的密码与角色组成的字典{'password': 'xxx', 'role': 'user'}
            user_info = user_service.get_user(input_username)
            # 条件1：用户存在且密码为空
            if user_info and user_info.get("password") is None:
                set_password(input_username)  # 直接弹出密码设置
        except Exception as e:
            ui.notify(
                f"用户查询失败: {str(e)}",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # 回车登录
    def enter_try_login():
        if app.storage.client["key_state"].get("enter", 0) == 1:
            app.storage.client["key_state"]["enter"] = 0
            try_login()

    # 登录页面
    with ui.dialog().props("persistent") as dialog_login, ui.card().classes("w-1/3 p-4 bg-white shadow-md -space-y-6"):
        # 创建卡片组件
        ui.label("用户登录").classes("text-lg p-4")  # 显示文本内容
        with ui.column().classes("w-full p-4 space-y-2"):
            # 创建UI元素的引用
            username_input = (
                ui.input(label="用户名").classes("w-full").props('autofocus outlined :dense="dense" color="amber-8"')
            )
            password_input = (
                ui.input(label="密码", password=True, password_toggle_button=True)
                .classes("w-full")
                .props('outlined :dense="dense" color="amber-8"')
            )
        with ui.row().classes("w-full p-4 flex-nowrap"):
            ui.button("登录", on_click=lambda: try_login()).classes("w-1/2").props('outline color="amber-8"')
            ui.button("修改密码", on_click=lambda: change_password()).classes("w-1/2").props('outline color="amber-8"')
            # ui.button("关闭", on_click=lambda: dialog_login.close()).classes("w-1/3").props('outline color="amber-8"')
    dialog_login.open()
    # 添加全局键盘事件跟踪
    # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
    ui.keyboard(on_key=handle_key, ignore=["select", "button", "textarea"])
    # 监控用户是否按下回车键
    ui.timer(0.5, lambda: enter_try_login())

    # 添加实时检测
    username_input.on("blur", check_initial_password)  # 失去焦点时检测


# ======================
# 主界面路由
# ======================
@ui.page("/main")
def main_page():
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return

    # 定义导航项目
    # 格式：(图标, 标题, 描述, 目标路径)
    menu_items = [
        ("assignment", "正式项目", "录入与查看正式项目信息", "/project_table"),
        ("history_edu", "临时项目", "录入与查看临时项目信息", "/main"),
        # ("manage_accounts", "XX", "XX", "/main"),
        ("insert_chart", "消息图表", "查阅项目相关消息与统计图表", "/information"),
    ]

    # 主界面
    with ui.header().classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("百炼光研发管理系统").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white"):  # 右侧对齐
            with ui.menu() as menu:
                ui.menu_item("注销登录", on_click=lambda: logout())
                if app.storage.user.get("current_user") == "admin":
                    ui.menu_item("系统管理", on_click=lambda: ui.navigate.to("/manage"))
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)

    # 使用 ui.grid 创建一个响应式的网格布局
    # a-classes: 应用于所有子元素的通用样式
    # b-classes: 应用于特定子元素的样式 (这里没用，但可以写 b-col-6 c-col-4 等)
    with ui.column().classes("w-full h-[calc(100vh-5rem)] items-center justify-center"):
        with ui.grid(columns=3).classes("w-[calc(70vw)] gap-4 h-[calc(30vh)]"):
            for icon, title, subtitle, target in menu_items:
                # 每个功能模块都用一个 ui.card 包裹
                with ui.card().classes(
                    "flex flex-col items-center justify-center cursor-pointer "
                    "hover:shadow-xl hover:-translate-y-1 transition-all duration-300 ease-in-out"
                ) as card:
                    # 设置点击事件，导航到指定页面
                    # 当点击发生时，GenericEventArguments 对象被赋值给 _ 因为我们不需要处理这个点击事件对象，所以不关心它
                    card.on("click", lambda _, t=target: ui.navigate.to(t))

                    # 大图标
                    ui.icon(icon).classes("text-5xl text-blue-500 mb-4")
                    # 模块标题
                    ui.label(title).classes("text-xl font-semibold")
                    # 模块描述
                    ui.label(subtitle).classes("text-center text-gray-500 text-sm mt-1")


@ui.page("/project_table")
def project_table_page():
    # 向页面的 <head> 部分添加自定义的 HTML 代码。这通常用于添加自定义的 CSS 样式、JavaScript 代码或元数据（如 <meta> 标签）
    ui.add_head_html("""
        <style>
            .ag-theme-alpine {
                --ag-font-family: 'Arial', sans-serif !important;
                --ag-foreground-color: #111 !important;       /* 单元格文本颜色 */
                --ag-header-foreground-color: #000 !important;       /*  表头文本颜色 */
                --ag-header-background-color: #f1f0ed !important; /* 表头背景色 */
                --ag-odd-row-background-color: #f6dead33 !important; /* 奇数行背景色 */
                --ag-background-color: #93d5dc33 !important; /* 背景色 */
                --ag-row-hover-color: #41b34933 !important;     /* 行悬停颜色 */
                --ag-border-color: #ddd !important;           /* 边框颜色 */
                --ag-cell-horizontal-border: solid 1px var(--ag-border-color) !important; /* 单元格右侧边框 */
                --ag-row-border: solid 1px var(--ag-border-color) !important; /* 单元格底部边框 */
                --ag-header-column-resize-handle-display: none !important; /*隐藏表头单元格间多出来的竖线*/
                --ag-font-size: 12px !important;

            }
            .q-field--auto-height .q-field__control, .q-field--auto-height .q-field__native{
                min-height: 30px !important;
            }
            .q-field__marginal {
                height: 30px !important;
                color: rgba(0, 0, 0, .54);
                font-size: 24px;
            }
            /*控制表格筛选下拉菜单背景颜色*/
            .ag-menu {
                background-color: white;
            }
            .ag-picker-field-wrapper {
                background-color: #e9f7f8;
            }
            .ag-select-list{
                background-color: white;
            }
            /*控制展开式按钮样式*/
            .q-btn--fab {
                padding-left: 8px;
                padding-right: 8px;
                padding-bottom: 4px;
                padding-top: 4px;
                min-height: 30px;
                min-width: 30px;
            }
            .q-btn--fab-mini {
                padding: 6px;
                min-height: 30px;
                min-width: 30px;
            }
            .q-fab__actions .q-btn {
                margin: 3px;
            }
            /*控制选项框内选项样式*/
            .q-item {
                min-height: 30px;
                padding: 8px 16px;
                color: inherit;
                transition: color 0.3s,background-color 0.3s
            }
            .ag-header-group-cell-label, .ag-header-cell-label {
                display: flex;
                flex: 1 1 auto;
                align-self: center;
                align-items: center;
                justify-content: center;
            }
            .ag-cell {
                display: flex !important;
                position: absolute !important;
                white-space: nowrap !important;
                height: 100% !important;
                align-items: center;
                justify-content: center;
                line-height: 25px;
            }

            /* 基础单元格样式 */
            /* 文字自动换行样式 */
            .left-auto-break {
                white-space: pre-wrap !important;
                word-wrap: break-all;
                overflow: hidden;
                text-align: left;
                justify-content: left !important;
            }
            /* 文字居中自动换行样式 */
            .center-auto-break {
                white-space: pre-wrap !important;
                word-wrap: break-all;
                overflow: hidden;
                text-align: center;
            }
        
            /* 字体加粗样式 */
            .bold-text {
                font-weight: 700 !important;
            }
            .red-text {
                color: red !important;
            }
            .amber-text {
                color: #df8b00 !important;
            }
        </style>
    """)
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return

    # 按照项目名里“-”符号切分为大类和小类，并输出二层结构的类别字典
    def get_select_dic(select_li):
        select_li.sort()
        select_dic = {}
        # 单独加入一个所有大类，以供显示所有型号
        select_dic["所有"] = ["所有"]
        for s in select_li:
            # 判断指定字符出现次数
            if s.count("-") >= 1:
                parts = s.split("-")
                if parts[0] not in select_dic.keys():
                    # 每个小类都先给个 所有 的选项
                    select_dic[parts[0]] = ["所有"]
                if parts[1][:2] not in select_dic[parts[0]]:
                    # 将代表小类的 两位数字 加入到该大类的小类选项里
                    select_dic[parts[0]].append(parts[1][:2])
                    # 排序
                    select_dic[parts[0]].sort(reverse=True)
            # 对于没有-字符的类别做特殊处理
            else:
                # 单独设置一个其它的大类，且该大类下的小类选项里先加入 所有
                if "其它" not in select_dic.keys():
                    select_dic["其它"] = ["所有"]
                # 将完整项目号直接加入小类选项里
                if s not in select_dic["其它"]:
                    select_dic["其它"].append(s)
                    # 排序
                    select_dic["其它"].sort(reverse=True)
        return select_dic

    # 按照第一选项的值，生成更新第二选框的选项列表
    def update_sub_select(select_sub):
        select_sub.set_options(
            select_dic[select_major_value["value"]], value=select_dic[select_major_value["value"]][0]
        )

    # 按照两个选项的值，更新表格行数据，将概述填写内容同步到简介表，刷新表格显示
    def update_aggrid(aggrid):
        nonlocal rows_select
        # 清空
        rows_select = []
        s = ""
        # 如果第一选矿选择的是“所有”
        if select_major_value["value"] == "所有":
            rows_select = rows
        else:
            # 设置筛选字符串
            # 如果第二选项选的是“所有”
            if select_sub_value["value"] == "所有":
                # 且第一选项选的不是“其它”，择拿正常项目名前面的字符串来匹配RFFM
                if select_major_value["value"] != "其它":
                    s = select_major_value["value"]
                # 且第一选项选的是“其它”，则拿“-”字符来排除
                else:
                    s = "-"
            # 如果第一选项选的不是“其它”，且不是“所有”
            elif select_major_value["value"] != "其它":
                # 则拿正常项目-字符前后较完整字符串来匹配，如RFFM-17
                s = f"{select_major_value['value']}-{select_sub_value['value']}"
            # 第一选项选的是“其它”且第二选项不是“所有”，拿具体特殊项目名来匹配，如RM3000
            else:
                s = select_sub_value["value"]

            # 遍历无分类行数据列表，将符合筛选条件的行数据找出来
            for r in rows:
                # 如果匹配字符不为“-”且匹配字符串在项目名里（正常项目） 或 匹配字符为“-”且匹配字符不在项目名里（特殊项目）
                if s != "-" and s in r["project"] or s == "-" and s not in r["project"]:
                    # 获取当前行数据所属项目名
                    project_name = r["sub_project"]
                    overview_data = db_storage.get_item(f"{project_name}_over_data", {})
                    # 如果服务器储存的概述数据里存在该当前项目对应概述资料
                    if overview_data:
                        # 遍历服务器 项目简介与概述数据对照字典
                        for pro_key, over_key_li in app.storage.general["project_overview_config"].items():
                            # 如果当前处理的不是负责人配置，且项目简介对照配置非空
                            if "charge" not in pro_key and over_key_li != []:
                                show_str = ""
                                # 遍历对照配置列表（可能一个项目简介配置了多个对应的概述数据项）
                                for over_key in over_key_li:
                                    # 当前概述数据项label存在服务器概述数据对应项目里，说明可能存在概述内容
                                    if over_key in overview_data:
                                        chip_data_li = overview_data.get(over_key, {}).values()
                                        # 遍历概述内容每个chip数据
                                        for chip_data in chip_data_li:
                                            # 该chip内容是激活 或者 待定状态 才显示
                                            if chip_data["enabled"] or chip_data["enabled"] is None:
                                                text = ""
                                                # 如果有content键，则应该是文字型chip
                                                if "content" in chip_data:
                                                    text = chip_data["content"]
                                                # 如果有filename键，则应该是文件或图片型chip
                                                elif "filename" in chip_data:
                                                    text = ".".join(chip_data["filename"].split(".")[:-1])

                                                # 待定状态的概述内容串 加上特殊标记符号
                                                if chip_data["enabled"] is None:
                                                    text = f"「{text}」?"
                                                # 将文本拼接到待显示字符串上
                                                # 这几类换行拼接
                                                if pro_key in [
                                                    "light_source",
                                                    "target_distance",
                                                    "drive_pcb",
                                                    "electronic_bom",
                                                    "software_research",
                                                    "software_mass",
                                                ]:
                                                    show_str = f"{show_str}\n{text}"
                                                else:
                                                    show_str = f"{show_str}，{text}"
                                # 将处理完成的字符串作为该行数据对应项目简介项的显示内容
                                r[pro_key] = show_str.strip("，").removeprefix("\n")  # removeprefix移除字符串前缀
                            # 处理负责人配置部分显示内容
                            elif (
                                "charge" in pro_key
                                and over_key_li != ""
                                and project_name in app.storage.general["overview_role"]
                                and over_key_li in app.storage.general["overview_role"][project_name]
                            ):
                                show_str = app.storage.general["overview_role"][project_name][over_key_li][
                                    "latest_user"
                                ]
                                show_str = show_str.split("：")[1] if show_str else ""
                                if show_str:
                                    selected_bool = False
                                    for class_dic in overview_data.values():
                                        for ver_dic in class_dic.values():
                                            select_activ_dic = ver_dic.get("select_activ_dic", {})
                                            if select_activ_dic:
                                                max_ver = max([int(float(ver)) for ver in select_activ_dic.keys()])
                                                if select_activ_dic[f"{max_ver}.0"] is None:
                                                    if over_key_li == ver_dic.get("role", ""):
                                                        selected_bool = True
                                    if selected_bool:
                                        show_str = f"待{show_str}\n选概述"

                                r[pro_key] = show_str

                    # 单独处理项目简介表里每行 负责销售 单元格的显示
                    r["sale_charge"] = app.storage.general["project_sale"].get(project_name, "")
                    # 将行数据加入待显示的符合选框的数据列表里
                    rows_select.append(r)

        # aggrid.run_grid_method("setRowData", rows_select)
        aggrid.options["rowData"] = rows_select
        aggrid.update()

    # 设定aggrid元素某列的可见性为传入的visible，如果这个参数不传，则是切换可见性
    async def toggle_visibility(grid, field_li: list, visible=None):
        """
        设定 aggrid 元素指定列的可见性。

        :param grid: AgGrid 组件实例。
        :param field_li: 需要操作的列ID列表 (e.g., ['state', 'creation_date'])。
        :param visible: 如果提供布尔值，则直接设定可见性 (True=可见, False=隐藏)。
                        如果为 None，则切换这些列的当前可见性。
        """
        # 如果明确指定了 visible 状态，直接调用 API 并返回
        if visible is not None:
            grid.run_grid_method("setColumnsVisible", field_li, bool(visible))
            return

        # --- 切换可见性的逻辑 ---

        # 1. 一次性获取所有列的状态
        try:
            all_columns_state = await grid.run_grid_method("getColumnState")
        except Exception as e:
            ui.notify(f"获取状态失败: {e}", type="negative")
            return

        # 2. 在内存中计算哪些列需要显示，哪些需要隐藏
        cols_to_show = []
        cols_to_hide = []

        # 创建一个从 colId 到 hide 状态的映射，方便快速查找
        state_map = {col["colId"]: col.get("hide", False) for col in all_columns_state}

        for field in field_li:
            # 如果列当前是隐藏的 (hide=True)，那么我们就要显示它
            if state_map.get(field):  # .get(field) 默认为 None (False)，如果 hide=True 则为 True
                cols_to_show.append(field)
            # 如果列当前是可见的 (hide=False)，那么我们就要隐藏它
            else:
                cols_to_hide.append(field)

        # 3. 分批次更新，减少与前端的通信次数
        if cols_to_show:
            grid.run_grid_method("setColumnsVisible", cols_to_show, True)
            # ui.notify(f"已显示列: {', '.join(cols_to_show)}")

        if cols_to_hide:
            grid.run_grid_method("setColumnsVisible", cols_to_hide, False)
            # ui.notify(f"已隐藏列: {', '.join(cols_to_hide)}")
        # 刷新 Ag-Grid
        # grid.update()

    # 创建一个按钮，其点击事件调用 run_grid_method

    # 调用 AG Grid API 清除所有筛选模型
    def clear_all_filters(grid):
        grid.run_grid_method("setFilterModel", None)

    # 按钮点击事件（操作存储/行数据）
    async def handle_cell_click(event, aggrid):
        row_data = event.args["data"]  # 整行的数据
        col_id = event.args["colId"]  # 点击列的字段名
        # row_index = event.args["rowIndex"]  # 行索引
        # row_id = event.args["rowId"]  # 点击行的ID
        project_name = row_data["sub_project"]
        if col_id == "requirement":
            # 查找指定路径下，含有提供项目名的文件，得到一个字典，完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
            project_exists_file = find_files_with_prefix_and_version(REQ_DIR, project_name)
            if project_exists_file:
                v_max = max([float(s) for s in project_exists_file.keys()])
                # 定义文件路径
                file_path = os.path.join(REQ_DIR, project_exists_file[str(v_max)]["name"])
                ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")
            else:
                ui.navigate.to(f"/main/requirement?type=requirement&project_name={row_data['sub_project']}")
        elif col_id == "overview":
            await get_overviow_page(project_name, False)

    async def switch_toggle_vis(visible=None):
        # 切换传入列的可见性
        await toggle_visibility(
            aggrid,
            [
                "state",  # 状态列
                "introduction",  # 简介列
                "model_notes",  # 型号备注列
                "creation_date",  # 创建日期列
                "light_source",  # 光源选型列
                "project",  # 对外项目号列
                "target_distance",  # 目标距离列
                "output_mode",  # 输出型号模式列
                "guide_beam",  # 导光束要求列
                "adapter_options",  # 转接座选型列
                "customer",  # 客户简称列
                "drive_pcb",  # PCB规格
                "electronic_bom",  # 电子BOM
                "software_research",  # 研发版软件
                "software_mass",  # 量产版软件
            ],
            visible,
        )

    # 定义项目主界面列配置
    # 文本筛选器 ("filter": "agTextColumnFilter"): 用于文本列，支持包含、开始于、结束于等多种筛选模式。
    # 数值筛选器 ("filter": "agNumberColumnFilter"): 用于数值列，支持等于、大于、小于、范围等。
    # 集合筛选器 ("filter":  "agDateColumnFilter"): 用于日期列，支持等于、不等于、之前、之后、之间、空白、非空等。
    project_summary_columns = [
        {
            "field": "sub_project",
            "headerName": "内部产品型号",
            "width": 140,
            "pinned": "left",  # 固定到左侧
        },
        {"field": "project", "headerName": "对外产品型号", "width": 120},
        {"field": "model_notes", "headerName": "型号备注", "width": 150, "autoHeight": True},
        {"field": "state", "headerName": "产品状态", "width": 80, "filter": "agTextColumnFilter"},
        {"field": "creation_date", "headerName": "立项日期", "width": 100, "filter": "agDateColumnFilter"},
        {"field": "introduction", "headerName": "产品简介", "width": 300, "autoHeight": True},
        {"field": "custom_labels", "headerName": "定制要点", "width": 400, "autoHeight": True},
        {
            "field": "light_source",
            "headerName": "光源选型",
            "width": 400,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
            # "cellStyle": {"white-space": "pre-line"},
        },
        {"field": "photometric", "headerName": "光度学要求", "width": 120, "autoHeight": True},
        {"field": "target_distance", "headerName": "目标面距离", "width": 100, "autoHeight": True},
        {
            "field": "adapter_options",
            "headerName": "转接座可选类别",
            "width": 140,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "color", "headerName": "外观颜色", "width": 80, "filter": "agTextColumnFilter"},
        {"field": "input_voltage", "headerName": "产品输入电压", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "input_mode", "headerName": "输入控制模式", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "output_mode", "headerName": "输出模式", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "guide_beam", "headerName": "导光束要求", "width": 100},
        {
            "field": "drive_pcb",
            "headerName": "PCB规格",
            "width": 180,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "electronic_bom",
            "headerName": "电子BOM",
            "width": 180,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "software_research",
            "headerName": "研发版软件",
            "width": 200,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {
            "field": "software_mass",
            "headerName": "量产版软件",
            "width": 200,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "requirement", "headerName": "需求录入", "width": 80},
        {"field": "overview", "headerName": "概述整理", "width": 80},
        {"field": "customer", "headerName": "客户缩写", "width": 100, "filter": "agTextColumnFilter"},
        {"field": "sale_charge", "headerName": "销售", "width": 80, "filter": "agTextColumnFilter"},
        {
            "field": "project_charge",
            "headerName": "项目",
            "width": 80,
            "autoHeight": True,
            "filter": "agTextColumnFilter",
        },
        {"field": "optics_charge", "headerName": "光学", "width": 80, "autoHeight": True},
        {"field": "structure_charge", "headerName": "结构", "width": 80, "autoHeight": True},
        {"field": "hardware_charge", "headerName": "硬件", "width": 80, "autoHeight": True},
        {"field": "software_charge", "headerName": "软件", "width": 80, "autoHeight": True},
        {"field": "ui_charge", "headerName": "UI", "width": 80, "autoHeight": True},
        {"field": "craft_charge", "headerName": "工艺", "width": 80, "autoHeight": True},
    ]
    for col in project_summary_columns:
        if "width" in col:
            col["minWidth"] = col["width"]
        if "autoHeight" in col:
            # 该类使得\n符号会起作用，达到手动换行作用
            col["cellClass"] = "left-auto-break"
        col["headerClass"] = "center-auto-break"
        # 设置单元格样式规则
        col["cellClassRules"] = {
            # 当表达式为 true 时，应用 'red-text' 类
            # typeof x === 'string': 这是一个安全检查，确保我们只在字符串类型的数据上执行后续操作
            # x.includes('?') 这是 JavaScript 的内建函数，如果字符串 x 中包含子字符串 '?'，它就返回 true
            "amber-text": "typeof x === 'string' && (x.includes('?') || x.includes('概述'))",
        }
        # col["cellClass"] = "ag-cell"

    # 将手动数据添加覆盖到服务器保存数据里
    project_summary_update()
    # 从服务器获取完整项目摘要
    project_dic = app.storage.general["project_summary"]
    # 抽取出无分类项目摘要列表
    rows = list(project_dic.values())
    # 初始化表格行数据选项列表
    rows_select = []
    # 单独抽取出所有项目名，除重后生成列表
    select_li = list(set([pro_sum["project"] for pro_sum in rows]))
    # 用于同步两个选项框的选项值
    select_major_value = {"value": "RFFM"}
    select_sub_value = {"value": "10"}
    # 获取按照大类和小类整理后的项目类别字典，用于选项框的选项动态生成
    select_dic = get_select_dic(select_li)
    select_major_li = list(select_dic.keys())

    # 项目信息表
    with ui.header().classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("项目信息表").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white"):  # 右侧对齐
            with ui.menu() as menu:
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)
    with ui.column().classes("w-full h-[88vh] -space-y-2"):
        with ui.row().classes("items-center -space-x-2") as tool_row:
            ui.label("项目筛选：").classes("text-[16px]/[28px]")
            select_major = (
                ui.select(select_major_li).bind_value(select_major_value, "value").props("outlined").classes("")
            )
            select_sub = (
                ui.select(select_dic["RFFM"]).bind_value(select_sub_value, "value").props("outlined").classes("")
            )

        # 初始化 AG-Grid
        aggrid = ui.aggrid(
            {
                "columnDefs": project_summary_columns,
                "rowData": rows_select,
                "headerHeight": 50,
                # 强制渲染所有行，禁用虚拟滚动
                "suppressRowVirtualisation": True,
                # 允许单元格文本选择
                "enableCellTextSelection": True,
            }
        ).classes("ag-theme-alpine ag-header-cell-resize::after h-full")
        # min-width: 1000px;       /* 防止宽度过小 */
        # overflow-x: auto;        /* 启用水平滚动 */
        # aggrid.run_grid_method("domLayout", "print")
        # aggrid.style("text-align:center;width: 150%;")

        # 按照两个选项的值，更新表格行数据，将概述填写内容同步到简介表，刷新表格显示
        select_major.on_value_change(lambda select_sub=select_sub: update_sub_select(select_sub))
        select_major.on_value_change(lambda aggrid=aggrid: update_aggrid(aggrid))
        select_sub.on_value_change(lambda aggrid=aggrid: update_aggrid(aggrid))
        aggrid.on("cellClicked", lambda e, aggrid=aggrid: handle_cell_click(e, aggrid))
        update_aggrid(aggrid)

        with tool_row:
            with ui.row().classes("items-center -space-x-4"):
                ui.label("功能按键：").classes("text-[16px]/[28px]")
                # with ui.fab("construction", label="", color="blue", direction="right"):
                ui.button(icon="zoom_in_map", color="amber-9", on_click=lambda: switch_toggle_vis(False)).props(
                    "flat round"
                )
                ui.button(icon="zoom_out_map", color="green-9", on_click=lambda: switch_toggle_vis(True)).props(
                    "flat round"
                )
                ui.button(icon="filter_alt_off", color="purple-9", on_click=lambda: clear_all_filters(aggrid)).props(
                    "flat round"
                )


@ui.page("/manage")
def manage_page():
    # 管理员管理界面
    if app.storage.user.get("current_user") != "admin":
        ui.navigate.to("/main")  # 如果不是管理员，跳转到主界面
        return
    with ui.header().classes("flex justify-between items-center bg-blue-500 h-12 px-4"):
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("系统管理员界面").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")
        with ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white"):  # 右侧对齐
            with ui.menu() as menu:
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)
    with ui.column().classes("w-full h-[90vh] -space-y-2"):
        ui.button("更新需求配置文件", on_click=lambda: update_config_service()).props("").classes("")
        ui.button("更新用户配置数据", on_click=lambda: update_users_data()).props("").classes("")


# ======================
# 需求界面路由
# ======================
@ui.page("/main/requirement")
async def requirement_page(type="", json_path="", project_name=""):
    ui.add_head_html("""
        <style>
            .q-btn{
                /*min-height: 2.1em;*/   
            }
            .nicegui-editor .q-editor__content p, .nicegui-markdown p {
                margin: 0.2rem 0;
            }
            /*控制选项框内选项样式*/
            .q-item {
                min-height: 30px;
                padding: 10px 16px;
                color: inherit;
                transition: color 0.3s,background-color 0.3s
            }
            /*.q-menu {
                background-color:#efffff;
            }*/
            
            .q-dialog__inner--minimized {
                padding: 12px;
            }
            .q-textarea textarea {
                /* 1. 设置一个最小高度，而不是固定高度 */
                height: 50px;
                min-height: 50px;

                /* 2. 明确允许用户垂直方向拖动调整大小 (也可设为 "both") */
                resize: vertical;

                /* 3. 当内容超出当前高度时，自动显示垂直滚动条 */
                overflow-y: auto !important; /* Quasar 有时会设置 overflow:hidden, !important 确保覆盖 */
            }
        </style>
    """)

    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return

    # 存储用户层级需求相关数据的变量初始化
    # 用于记录键盘按键状态
    app.storage.client.setdefault("key_state", {})
    # 需求配置数据字典初始化
    app.storage.client.setdefault("config_data", init_config_data)
    # 一个空列表，用于存储当前管理的文件列表。可以在这个列表中添加文件路径、文件名或其他文件相关信息
    app.storage.client.setdefault("files", [])
    # 一个空集合（set），用于存储已经被删除的文件的标识符（例如文件名或路径）
    app.storage.client.setdefault("deleted_files", [])
    # 一个整数，初始值为 0，用于记录文件的总数或其他与文件计数相关的逻辑
    app.storage.client.setdefault("file_counter", 0)
    # 一个文件缩略图实例化对象字典
    app.storage.client.setdefault("file_thumbnail_dic", {})
    # 保存添加过某个数字引用的各个确认项构成的字典，{数字引用:[(确认项序号,确认项内容),......]}
    app.storage.client.setdefault("ref_question_dic", {})
    # 记录项目名称的变量
    app.storage.client.setdefault("project_name", "")
    # 记录项目改名的目标名称的变量
    app.storage.client.setdefault("target_project_name", "")
    # 初始化需求版本
    app.storage.client.setdefault("version", "0.0")
    # 初始化需求版本
    # app.storage.client.setdefault("target_version", "")
    # 需求确认项按钮字典
    app.storage.client.setdefault("buttons_dic", {})
    # 新增一个地方来存放当前页面的关键UI元素
    app.storage.client.setdefault("page_elements", {})
    # 用于后续保存需求问题项，已选填数目
    app.storage.client.setdefault("req_activ_num", 0)
    # 用于后续保存需求问题项，未选填数目
    app.storage.client.setdefault("req_not_activ_num", 0)
    # 用于后续保存需求问题项，总数目
    app.storage.client.setdefault("req_com_num", 0)
    # 用于保存当前需求问题序号
    app.storage.client.setdefault("current_question_num", 0)

    # 在全局作用域创建对话框（确保在菜单系统之外）
    # 创建项目名修改对话框
    with ui.dialog().classes("") as project_dialog:
        project_card = ui.card().classes("w-1/4")
    # 创建并显示对比对话框
    with ui.dialog() as contrast_dialog:
        contrast_card = (
            ui.card().classes("gap-2").style("min-width: 800px; max-width: 90vw; min-hight: 800px; max-hight: 90vw;")
        )
    # 创建用于选择需求版本的对话框
    with ui.dialog().classes("") as req_version_dialog:
        version_card = ui.card().classes("w-1/4")
    # 存储对话框引用
    app.storage.client["page_elements"]["project_card"] = project_card
    app.storage.client["page_elements"]["project_dialog"] = project_dialog
    app.storage.client["page_elements"]["contrast_card"] = contrast_card
    app.storage.client["page_elements"]["contrast_dialog"] = contrast_dialog
    app.storage.client["page_elements"]["version_card"] = version_card
    app.storage.client["page_elements"]["req_version_dialog"] = req_version_dialog

    # 获取所有JSON配置文件的文件名
    try:
        config_files = [f.name for f in Path(REQ_DIR).glob("*.json") if f.is_file()]
        if not config_files:
            ui.notify("系统初始化，目录下未找到任何JSON配置文件。", color="info")
            config_files = []
    except Exception as e:
        ui.notify(f"读取配置文件目录时出错: {e}", color="negative")
        config_files = []

    # 键盘事件跟踪处理函数
    def requirement_handle_key(e: KeyEventArguments):
        k = app.storage.client["current_question_num"]
        options_type = app.storage.client["config_data"]["data"][k]["answer_type"]

        if e.key.name == "ArrowLeft" and e.action.keydown:
            app.storage.client["key_state"]["arrowleft"] = 1
            get_option(k, options_type, -1)
        elif e.key.name == "ArrowLeft" and e.action.keyup:
            app.storage.client["key_state"]["arrowleft"] = 0
        if e.key.name == "ArrowRight" and e.action.keydown:
            app.storage.client["key_state"]["arrowright"] = 1
            get_option(k, options_type, 1)
        elif e.key.name == "ArrowRight" and e.action.keyup:
            app.storage.client["key_state"]["arrowright"] = 0

            # app.storage.client["key_state"]["enter"] = 0

    # 显示传入数据的用户填写内容
    def show_user_output(data):
        ui.label(f"确认项: {data['guide_content']}")
        if "单选" in data["answer_type"]:
            if not data["user_must_out"]:
                ui.label("（无此项配置）").classes("text-light-blue-9")
                return

            value = list(data["user_must_out"].values())[0]
            if value == "True":
                ui.label("（是）").classes("text-light-blue-9")
            elif value == "False":
                ui.label("（否）").classes("text-light-blue-9")
            else:
                ui.label(f"（{value}）").classes("text-light-blue-9")

            if data["ref_out"]:
                ui.label(f"（引用文件：{'，'.join(data['ref_out'])}）").classes("text-amber-9")

        elif "多选" in data["answer_type"]:
            if not data["user_must_out"]:
                ui.label("（无此项配置）").classes("text-light-blue-9")
                return

            for k, v in data["user_must_out"].items():
                if v:
                    ui.label(f"（{k}）").classes("text-light-blue-9")

            if data["ref_out"]:
                ui.label(f"（引用文件：{'，'.join(data['ref_out'])}）").classes("text-amber-9")

        elif data["answer_type"] in ["正整数", "单行文本", "多行文本"]:
            if not data["user_must_out"]:
                ui.label("（无此项配置）").classes("text-light-blue-9")
                return

            if data["input_tolerance"] == "正负":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）典型值（{v}），公差（{data['option_tolerance_out'][k]}）").classes(
                        "text-light-blue-9"
                    )
            elif data["input_tolerance"] == "范围":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）下限（{v}），上限（{data['option_tolerance_out'][k]}）").classes(
                        "text-light-blue-9"
                    )
            elif data["input_tolerance"] == "上限":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）上限（{v}）").classes("text-light-blue-9")
            elif data["input_tolerance"] == "下限":
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）下限（{v}）").classes("text-light-blue-9")
            else:
                for k, v in data["user_must_out"].items():
                    ui.label(f"（{k}）填写（{v}）").classes("text-light-blue-9")

            if data["ref_out"]:
                ui.label(f"（引用文件：{'，'.join(data['ref_out'])}）").classes("text-amber-9")

    def show_comparison_dialog():
        contrast_card = app.storage.client["page_elements"].get("contrast_card")
        contrast_card.clear()
        app.storage.client["page_elements"].get("contrast_dialog").props("persistent")
        app.storage.client["page_elements"].get("contrast_dialog").open()

        with contrast_card:
            with ui.row().classes("w-full justify-between"):
                ui.label("产品配置对比工具").classes("text-h6")

                ui.button(
                    "",
                    icon="close",
                    on_click=lambda: app.storage.client["page_elements"].get("contrast_dialog").close(),
                ).props("flat round").classes("text-black bg-transparent")
            with ui.row().classes("w-full items-center justify-between"):
                # 下拉选择框
                select1 = ui.select(config_files, label="选择旧版本配置 (产品A)").props("outlined").classes("w-2/5")
                # 对比按钮
                ui.button("开始对比", on_click=lambda: perform_comparison()).classes("bg-amber-8")
                select2 = ui.select(config_files, label="选择新版本配置 (产品B)").props("outlined").classes("w-2/5")

            ui.separator().props("size=1px")
            # 结果展示区域
            results_area = ui.scroll_area().classes("gap-2 w-full h-96 p-2 bg-grey-2 rounded-lg")
            ui.separator().props("size=1px")

        async def perform_comparison():
            """执行对比并更新UI"""
            old_file = select1.value
            new_file = select2.value

            if not old_file or not new_file:
                ui.notify("请选择两个需要对比的配置文件。", color="warning")
                return

            if old_file == new_file:
                ui.notify("请选择两个不同的配置文件进行对比。", color="warning")
                return

            # 读取和解析JSON文件
            try:
                old_data = {}
                new_data = {}
                with open(f"{REQ_DIR}/{old_file}", "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                with open(f"{REQ_DIR}/{new_file}", "r", encoding="utf-8") as f:
                    new_data = json.load(f)

            except Exception as e:
                ui.notify(f"读取或解析文件时出错: {e}", color="negative")
                return

            # 调用对比函数
            diff = compare_configs_by_id(old_data["data"], new_data["data"], ["guide_content"])

            # 清空并填充结果区域
            results_area.clear()
            with results_area:
                if not any(diff.values()):
                    ui.label("两个配置完全相同，没有差异。").classes("text-lg text-green-8")
                    return

                # 1. 展示新增项
                if diff["added"]:
                    with ui.expansion("新增项", icon="add_circle", value=True).classes(
                        "gap-2 w-full bg-green-100 rounded"
                    ):
                        for item_id, item_data in diff["added"].items():
                            with ui.card().classes("gap-1 w-full my-2"):
                                ui.label(f"ID: {item_id}").classes("text-bold")
                                ui.label(f"确认项内容: {item_data.get('guide_content', 'N/A')}")

                # 2. 展示删除项
                if diff["deleted"]:
                    with ui.expansion("删除项", icon="remove_circle", value=True).classes(
                        "gap-2 w-full bg-red-100 rounded"
                    ):
                        for item_id, item_data in diff["deleted"].items():
                            with ui.card().classes("gap-1 w-full my-2"):
                                ui.label(f"ID: {item_id}").classes("text-bold")
                                ui.label(f"确认项内容: {item_data.get('guide_content', 'N/A')}")

                # 3. 展示修改项
                if diff["modified"]:
                    with ui.expansion("修改项", icon="sync_alt", value=True).classes(
                        "gap-2 w-full bg-orange-100 rounded"
                    ):
                        for item_id, changes in diff["modified"].items():
                            with ui.card().classes("gap-1 w-full my-2"):
                                ui.label(f"ID: {item_id}").classes("text-bold mb-2")
                                ui.separator().props("size=1px")
                                with ui.grid(columns=2).classes("w-full mt-2"):
                                    # 旧值
                                    with ui.card_section():
                                        ui.label("旧版本").classes("text-grey-7")
                                        show_user_output(changes["old_data"])

                                    # 新值
                                    with ui.card_section():
                                        ui.label("新版本").classes("text-bold")
                                        show_user_output(changes["new_data"])

    # 弹出项目名设置弹窗
    def get_project_dialog(key_str="revise"):
        project_card = app.storage.client["page_elements"].get("project_card")
        project_old_name = app.storage.client["project_name"]
        project_card.clear()
        app.storage.client["page_elements"].get("project_dialog").props("persistent")
        app.storage.client["page_elements"].get("project_dialog").open()
        with project_card:
            ui.label("请输入项目号：").classes("text-xl font-bold")
            ui.label("提交/导出需求或选择查阅版本时该设置才生效").classes("text-base text-red")
            input_field = ui.input().classes("text-[20px]/[22px] w-full")
            # 写入的值绑定到目标项目名变量
            input_field.bind_value(app.storage.client, "target_project_name")
            with ui.row().classes("flex-nowrap w-full"):
                ui.button("确认", icon="check", on_click=lambda: confirm_peoject_name(key_str)).classes("w-full")
                ui.button("取消", icon="cancel", on_click=lambda: cancel_peoject_name(project_old_name)).classes(
                    "w-full"
                )

    # 确认项目命名处理函数
    def confirm_peoject_name(key_str):
        target_project_name = app.storage.client["target_project_name"]
        project_name = app.storage.client["project_name"]
        if target_project_name == "":
            ui.notify(
                "请输入非空名称！",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            app.storage.client["target_project_name"] = app.storage.client["project_name"]
        elif (
            target_project_name.split("-")[0] != "RFTS"
            and target_project_name not in app.storage.general["project_summary"]
        ):
            ui.notify(
                "非临时项目，又未正式立项，命名不可用，请重新命名！",
                type="negative",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )
            app.storage.client["target_project_name"] = app.storage.client["project_name"]
        else:
            app.storage.client["page_elements"].get("target_project_button").props(remove="icon")
            # 为了新建项目需求而弹窗，则调用新需求处理函数
            if key_str == "new":
                ui.navigate.to(f"/main/requirement?type=requirement&project_name={target_project_name}")

        project_dialog.close()

    # 取消项目命名处理函数
    def cancel_peoject_name(project_old_name):
        app.storage.client["project_name"] = project_old_name
        project_dialog.close()

    # 新建需求初始化所有配置
    def new_requirement():
        app.storage.client["config_data"] = init_config_data
        app.storage.client["files"] = []
        app.storage.client["deleted_files"] = []
        app.storage.client["file_counter"] = 0
        app.storage.client["file_thumbnail_dic"] = {}
        app.storage.client["ref_question_dic"] = {}
        app.storage.client["buttons_dic"] = {}
        app.storage.client["version"] = "0.0"
        # app.storage.client["target_version"] = ""
        app.storage.client["original_version"] = "0.0"
        app.storage.client["original_project"] = ""

        requirement_input_frame()
        # 刷新界面
        set_question_list(0)  # 初始化一次确认项列表
        app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
        question_display(None, "1")  # 触发点击事件
        app.storage.client["page_elements"].get("target_project_button").props(remove="icon")
        req_thumbnail_display()
        # 显示成功通知
        ui.notify(
            "成功创建新需求",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )

    # 解析json配置文件，并生成需求界面
    def loads_requirements(json_data):
        # 获取文件缩略图字典内容，直接覆盖现有内容
        file_information = json_data["file_dic"]
        app.storage.client["file_thumbnail_dic"] = {}
        for k, v in file_information.items():
            app.add_static_file(local_file=f"{UPLOADS_DIR}/{v['file_name_hash']}", url_path=v["file_url"])
            file_thumbnail = FileThumbnail(
                v["file_url"], v["file_type"], v["file_name_suffix"], v["file_lab"], v["parents_h"], False, True
            )
            app.storage.client["file_thumbnail_dic"][k] = {
                "file_obj": file_thumbnail,
                "file_information": v,
            }
        # 恢复文件状态记录
        app.storage.client["files"] = json_data["files"]
        app.storage.client["deleted_files"] = json_data["deleted_files"]
        app.storage.client["file_counter"] = json_data["file_counter"]
        # 恢复项目名称与版本
        app.storage.client["project_name"] = json_data["project_name"]
        app.storage.client["version"] = json_data["version"]
        # 设置提交目标名称与版本
        app.storage.client["target_project_name"] = json_data["project_name"]
        # app.storage.client["target_version"] = json_data["version"]
        # app.storage.client["target_version"] = ""
        # 将衍生自哪个项目的信息获取过来
        app.storage.client["original_project"] = json_data["original_project"]
        app.storage.client["original_version"] = json_data["original_version"]
        # print(app.storage.client["original_version"])
        # 将剩余配置与用户填写记录信息覆盖现有配置
        app.storage.client["config_data"] = json_data
        # 遍历配置信息，抽取引用信息，重新恢复引用_确认项记录
        app.storage.client["ref_question_dic"] = {}  # 先清空
        for k, v in json_data["data"].items():
            question_k = k
            question = v["guide_content"]
            if v["ref_out"]:
                for ref in v["ref_out"]:
                    if ref in app.storage.client["ref_question_dic"].keys():
                        app.storage.client["ref_question_dic"][ref].append([question_k, question])
                    else:
                        app.storage.client["ref_question_dic"][ref] = [
                            [question_k, question],
                        ]
        requirement_input_frame()
        set_question_list(0)  # 初始化一次确认项列表
        app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
        question_display(None, "1")  # 触发点击事件
        req_thumbnail_display()
        # 显示成功通知
        ui.notify(
            "成功导入项目数据",
            type="positive",
            position="bottom",
            timeout=1000,
            progress=True,
            close_button="✖",
        )

    # json数据导入处理函数——触发上传窗口
    def import_config_data(upload):
        # 在上传新文件前，先清空upload列表，否则后续删除文件后，不能在重新插入
        upload.reset()
        # 触发隐藏的上传组件
        upload.run_method("pickFiles")  # 触发浏览器的文件选择对话框

    # json数据导入处理函数——处理数据
    async def json_handle_upload(e: events.UploadEventArguments):
        """处理上传的JSON文件"""
        # 获取上传的文件内容
        content_obj = await e.file.read()
        content = content_obj.decode("utf-8")
        try:
            # 解析JSON数据
            json_data = json.loads(content)
            loads_requirements(json_data)

        except json.JSONDecodeError:
            ui.notify(
                "文件上传失败",
                type="negative",
                position="bottom",
                timeout=2000,
                progress=True,
                close_button="✖",
            )

    # 自定义按钮上传文件元素，隐藏nicegui默认的ui.upload元素
    class ButtonUploader(ui.element):
        def __init__(
            self, on_upload=None, label="上传", input_any_suffix=None, classes_str="", props_str="", parents_h=9
        ):
            super().__init__()
            self.on_upload = on_upload
            self.label = label
            self.input_any_suffix = input_any_suffix
            self.classes_str = classes_str
            self.props_str = props_str
            self.parents_h = parents_h

            # 创建隐藏的上传组件
            self.upload = ui.upload(on_upload=self.handle_upload, auto_upload=True, label=self.label).props(
                f"accept={self.input_any_suffix} "
            )
            # 隐藏upload元素
            self.upload.set_visibility(False)

            # 创建一个按钮用于触发上传
            self.upload_button = (
                ui.button(label, icon="upload", on_click=self.pick_file).classes(self.classes_str).props(self.props_str)
            )

        def pick_file(self):
            # 在上传新文件前，先清空upload列表，否则后续删除文件后，不能在重新插入
            self.upload.reset()
            # 触发隐藏的上传组件
            self.upload.run_method("pickFiles")  # 触发浏览器的文件选择对话框

        async def handle_upload(self, e: events.UploadEventArguments):
            # 处理上传事件
            if self.on_upload:
                await self.on_upload(e, self.parents_h)
            else:
                print("上传文件无绑定回调函数")

    # 文件缩略图对象，点击可以展示大图，并可进行拖动和缩放
    class FileThumbnail:
        def __init__(
            self,
            file_url,
            file_type,
            file_name_suffix,
            file_lab,
            parents_h,
            auto_create: bool = True,
            delet_lab: bool = True,
        ):
            self.file_url = file_url
            self.local_file_path = f"{UPLOADS_DIR}/{self.file_url.split('/')[-1]}"
            self.file_type = file_type
            self.file_neme_suffix = file_name_suffix
            self.file_neme_hash = self.file_url.split("/")[-1]
            self.file_neme = ""
            self.file_suffix = ""
            self.parents_h = parents_h
            self.zoom_level = 1.0
            self.offset = (0, 0)
            self.is_dragging = False
            self.last_pos = (0, 0)
            self.image_x = 0.0
            self.image_y = 0.0
            self.file_up_time = get_time()
            self.add_lab_bool = False
            self.delet_lab = delet_lab
            self.dialog = ui.dialog().props("maximized").classes("p-0")
            if self.file_type.startswith("image/"):
                with self.dialog:
                    with (
                        ui.card()
                        .classes("relative h-full w-full overflow-hidden items-center justify-center")
                        .style("background-color: rgba(0,0,0,0);")
                    ):
                        ui.label("按ESC键退出图片查看界面").classes(
                            "absolute top-15 right-10 text-xl text-amber-9 z-999"
                        )
                        self.image_big = ui.interactive_image(
                            self.file_url,
                        ).classes("cursor-grab")
                    # self.image_big.props("fit=contain")
                    # 绑定事件
                    self.image_big.on("mousedown", self.start_drag)
                    self.image_big.on_mouse(self.get_img_xy)
                    self.image_big.on("mousemove", self.handle_drag)
                    self.image_big.on("mouseup", self.end_drag)
                    self.image_big.on("mouseleave", self.end_drag)
                    self.image_big.on("wheel", self.handle_zoom)
            # 存取文件计数值，也就是文件数字标记
            self.file_index = file_lab
            if auto_create:
                # 初始化并显示缩略图
                self.get_thumbnail()

        # 缩略图显示函数
        def get_thumbnail(self):
            file_name_list = self.file_neme_suffix.split(".")
            for i in range(0, len(file_name_list) - 1):
                if not self.file_neme:
                    self.file_neme = self.file_neme + file_name_list[i]
            self.file_suffix = file_name_list[-1]
            str_len = wcwidth.wcswidth(self.file_neme)
            str_num = len(self.file_neme)
            font_px = math.floor(self.parents_h * 4 / 3)
            # 计算文件名标题元素的设置宽度
            label_w = math.ceil(((str_len - str_num) + (2 * str_num - str_len) * 0.7) / 3) * font_px

            # 根据文件类型创建缩略图
            if self.file_type.startswith("image/"):
                self.thumbnail = ui.interactive_image(self.file_url).classes(f"h-{str(self.parents_h)} cursor-pointer")
                self.thumbnail.on("click", self.show_fullscreen)

            elif self.file_type == "application/pdf":
                with ui.row().classes(f"h-{str(self.parents_h)} flex-nowrap gap-1") as self.pdf_row:
                    # 使用 PDF 图标作为 PDF 文件的缩略图
                    # with ui.link(text="NiceGUI on GitHub", target=f"{self.file_url}", new_tab=False) as self.thumbnail:
                    #     ui.image("/uploads/1.jpg").classes("h-full cursor-pointer")
                    self.thumbnail = (
                        ui.interactive_image(f"{IMG_DIR}/file_type_pdf.png", content="")
                        .classes("h-full aspect-[1/1] cursor-pointer")
                        .on("click", self.open_pdf_in_browser)  # 使用浏览器打开则用.open_pdf_in_browser
                    )
                    ui.label(self.file_neme).classes(
                        f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-words text-black p-0 m-0 bg-white-500"
                    )
            else:
                with ui.row().classes(f"h-{str(self.parents_h)} flex-nowrap gap-1") as self.other_row:
                    # 使用 其它文件 图标作为 其它 文件的缩略图
                    self.thumbnail = (
                        ui.interactive_image(f"{IMG_DIR}/file_type_other.png", content="")
                        .classes("h-full aspect-[1/1] cursor-pointer")
                        .on("click", self.check_and_download)
                    )
                    ui.label(self.file_neme).classes(
                        f"h-full w-[{str(label_w)}px] text-[{str(font_px)}px]/[{str(font_px)}px] break-words text-black p-0 m-0 bg-white-500"
                    )
                    with self.thumbnail:
                        bg_color = "amber-600"
                        if "xls" in self.file_suffix:
                            bg_color = "green-700"
                        elif "ppt" in self.file_suffix:
                            bg_color = "red-600"
                        elif "doc" in self.file_suffix:
                            bg_color = "blue-400"
                        ui.label(self.file_suffix).classes(
                            f"border-2 border-white m-0 p-[2px] bg-{bg_color} text-white text-[10px]/[10px]"
                        ).style("position: absolute; top: 65%; left: 20%; transform: translate(-50%, -50%);")

            with self.thumbnail:
                if self.delet_lab:
                    # 缩略图删除按钮
                    b = (
                        ui.button(on_click=lambda: self.clear_thumbnail(self.file_neme_hash, self.file_index))
                        .classes("absolute -top-0 -right-0 m-0 p-0 q-py-1 bg-red text-white ")
                        .props('round padding="0px 0px" icon="close"')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )
                    self.thumbnail.on("mouseover", lambda b=b: ui_show(b)).on("mouseout", lambda: ui_hide(b))
                # 缩略图创建日期提示
                ui.tooltip(self.file_up_time).classes("text-[10px]/[10px] text-white p-1 m-0 bg-light-blue-6").props(
                    'transition-show="fade" transition-hide="fade" max-height="18px"'
                )
                # 缩略图数字标签
                ui.label(str(self.file_index)).classes(
                    "absolute top-0 left-0 m-0 p-[2px] bg-black text-white text-[10px]/[10px]"
                ).style("z-index: 1000;")  # 添加数字标记

        # 为缩略图添加“+”号引用按钮
        def add_ref_lab(self, ref_row, k, question_k, question):
            with self.thumbnail:
                self.ref_lab = (
                    ui.button()
                    .classes("absolute -bottom-0 -right-0 m-0 p-0 q-py-1 bg-amber-8 text-white ")
                    .props('round padding="0px 0px" icon="add"')
                    .style("font-size: 8px;")
                )
                self.ref_lab.on("click", lambda: add_ref_button(self, ref_row, question_k, question, True))
                self.ref_lab.on("click", js_handler="(e) => {e.stopPropagation()}")

        # 删除文件缩略图
        def clear_thumbnail(self, file_neme_suffix, file_index):
            if (
                self.file_index in app.storage.client["ref_question_dic"].keys()
                and app.storage.client["ref_question_dic"][self.file_index]
            ):
                # 创建对话框
                with ui.dialog() as dialog, ui.card().classes("w-full max-w-md"):
                    # 对话框标题
                    ui.label("文件引用提示").classes("text-h6 font-bold")

                    # 内容区域
                    with ui.column().classes("max-h-64 w-full"):
                        ui.label("需将如下确认项里，对该文件的引用解除掉方可删除：").classes("text-subtitle2")

                        # 使用纯文本区域显示问题
                        for q in app.storage.client["ref_question_dic"][self.file_index]:
                            b = (
                                ui.button(q[1], on_click=lambda e, k=q[0]: question_display(e, k))
                                .props("flat")
                                .classes("w-full")
                            )
                            b.on("click", dialog.close)

                    # 关闭按钮
                    ui.button("确定", on_click=dialog.close).classes("self-center")

                # 打开对话框
                dialog.open()
            else:
                if hasattr(self, "pdf_row"):
                    self.pdf_row.delete()
                elif hasattr(self, "other_row"):
                    self.other_row.delete()
                elif hasattr(self, "thumbnail"):
                    self.thumbnail.delete()
                app.storage.client["deleted_files"].append(file_neme_suffix)
                app.storage.client["file_thumbnail_dic"][self.file_index]["file_information"]["file_del_bool"] = True
            # app.storage.client["file_counter"] -= 1 注释掉使得文件标签数字唯一

        # pdf文件打开函数
        def open_pdf_in_browser(self):
            # 在浏览器中打开 PDF 文件
            async def get_base_url():
                # 通过 JavaScript 获取当前页面的协议、域名和路径
                result = await ui.run_javascript("window.location.origin;")
                return result

            # 2. 异步执行并拼接完整 URL
            async def open_pdf():
                base_url = await get_base_url()
                full_url = f"{base_url}{self.file_url}"
                # 处理空格等特殊字符
                encoded_url = full_url.replace(" ", "%20")
                # 3. 打开新窗口
                ui.run_javascript(f'window.open("{encoded_url}", "_blank");')
                # print(f"尝试打开PDF：{encoded_url}")

            # 启动异步任务
            ui.timer(0.1, lambda: open_pdf(), once=True)

        def trigger_download(self, on_complete=None):
            """专门负责触发下载的辅助函数"""
            ui.notify(
                f"开始下载文件: {self.file_neme_suffix}",
                type="info",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            ui.download(self.local_file_path)
            if on_complete:
                on_complete()

        # 我们创建一个新的、更智能的下载处理函数
        async def check_and_download(self):
            """
            检查文件是否已在当前会话下载过。
            如果是，则弹出一个带引导信息的对话框；如果否，则开始下载并标记。
            """
            storage_key = f"downloaded_{self.file_neme_hash}"
            has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

            if has_downloaded:
                # 【修改点】对这里的对话框进行全面升级
                with ui.dialog() as dialog, ui.card().classes("min-w-[400px]"):
                    with ui.card_section():
                        ui.label(f'文件 "{self.file_neme_suffix}" 已在本次会话中下载。').classes("text-lg font-medium")
                        ui.separator().props("size=1px").classes("my-3")
                        ui.label("您可以：")
                        # 使用 HTML 来创建更丰富的文本格式
                        ui.html(
                            """
                            <ul class="q-pl-lg">
                                <li>在浏览器的<b>下载栏</b>中直接找到它。</li>
                                <li>按键盘快捷键 <kbd>Ctrl</kbd> + <kbd>J</kbd> (Windows/Linux) 或 <kbd>⌘</kbd> + <kbd>Shift</kbd> + <kbd>J</kbd> (Mac) 打开<b>下载内容页面</b>。</li>
                            </ul>
                        """,
                            sanitize=False,
                        ).classes("text-base")  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                    with ui.card_actions().props("align=right"):
                        # “重新下载”按钮，保持原样
                        ui.button("仍要重新下载", on_click=lambda: self.trigger_download(dialog.close), color="primary")
                        # 将“取消”按钮改为更中性的“关闭”
                        ui.button("关闭", on_click=dialog.close, color="grey")

                dialog.open()

            # 3. 如果标记不存在 (首次点击)
            else:
                # a. 立即触发下载
                self.trigger_download()
                # b. 通过JavaScript在客户端设置标记
                await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

        # 打开其它文件，废弃，服务器不能命令本地电脑直接打开文件
        # def open_other_file(self):
        #     # 获取操作系统类型
        #     os_type = sys.platform
        #     if os_type == "win32":
        #         # os.startfile(f"{UPLOADS_DIR}/{self.file_neme_suffix}")
        #         os.startfile(self.local_file_path)
        #     elif os_type == "darwin":
        #         # subprocess.run(["open", f"{UPLOADS_DIR}/{self.file_neme_suffix}"])
        #         subprocess.run(["open", self.local_file_path])
        #     else:
        #         ui.notify(
        #             "未适配当前操作系统，不能直接打开。",
        #             type="info",
        #             position="bottom",
        #             timeout=1000,
        #             progress=True,
        #             close_button="✖",
        #         )

        # 处理数字链接的点击事件
        async def handle_index_click(self):
            # print(self.file_neme_hash, app.storage.client["deleted_files"])
            # if self.file_neme_hash in app.storage.client["deleted_files"]:
            if app.storage.client["file_thumbnail_dic"][self.file_index]["file_information"]["file_del_bool"]:
                ui.notify(
                    "该文件已被销售删除，虽可查看，但谨慎参考！",
                    type="warning",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                await asyncio.sleep(3)
            if self.file_type.startswith("image/"):
                self.show_fullscreen()
            elif self.file_type == "application/pdf":
                self.open_pdf_in_browser()  # 使用浏览器打开则用open_pdf_in_browser()
            else:
                await self.check_and_download()

        # 图片开始拖拽
        def start_drag(self, e: GenericEventArguments):
            if e.args.get("button") == 0:
                self.is_dragging = True
                self.last_pos = (e.args["clientX"], e.args["clientY"])
                self.image_big.classes(replace="cursor-grabbing")
            elif e.args.get("button") == 1:
                self.reset_transform()

        # 图片移动
        def handle_drag(self, e: GenericEventArguments):
            if self.is_dragging:
                dx = e.args["clientX"] - self.last_pos[0]
                dy = e.args["clientY"] - self.last_pos[1]
                self.offset = (self.offset[0] + dx, self.offset[1] + dy)
                self.last_pos = (e.args["clientX"], e.args["clientY"])
                self.update_transform()

        # 图片结束拖拽
        def end_drag(self, e: GenericEventArguments):
            self.is_dragging = False
            self.image_big.classes(replace="cursor-grab")

        # 获取鼠标相对图片左上角的坐标值
        def get_img_xy(self, e: MouseEventArguments):
            self.image_x = e.image_x
            self.image_y = e.image_y

        # 处理图片缩放
        def handle_zoom(self, e: GenericEventArguments):
            # 更新缩放级别（限制在0.1x到5x之间）
            new_zoom = self.zoom_level * (1.1 if e.args["deltaY"] < 0 else 0.9)
            self.zoom_level = max(0.01, min(10, new_zoom))
            # 更新图片
            self.update_transform()

        # 显示大图
        def show_fullscreen(self):
            # 打开弹窗
            self.dialog.open()
            # 复位图片
            self.reset_transform()

        # 更新图片变换函数
        def update_transform(self):
            self.image_big.style(
                f"transform: translate({self.offset[0]}px, {self.offset[1]}px) scale({self.zoom_level})"
            )

        # 重置变换状态
        def reset_transform(self):
            self.zoom_level = 1.0
            self.offset = (0, 0)
            self.update_transform()

    class InteractiveButton:
        """
        一个自定义的 NiceGUI 组件，它创建一个按钮用于添加文本或文件 chip。
        所有 chip 的状态都通过 app.storage.general 在所有客户端之间实时同步。
        """

        def __init__(
            self,
            project: str,
            role: str,
            title: str,
            label: str,
            processing_type: str,
            permission: dict,
            upload_path: Path = SUBMIT_FILES_DIR,
            dialog_label: str = "按规定格式输入",
            dialog_placeholder: str = "",
            state_options: list = [],
            node_options: list = [],
            instrument_options: list = [],
            temp_bool: bool = False,
            # delete_bool: bool = True,
        ):
            if processing_type not in ["text", "file", "image", "test"]:
                raise ValueError("processing_type 必须是 'text','file','image','test'")

            self.role = role
            self.title = title
            self.label = label
            self.project = project
            self.processing_type = processing_type
            self.upload_path = upload_path
            self.dialog_placeholder = dialog_placeholder
            self.dialog_label = dialog_label
            self.permission = permission
            self.state_options = state_options
            self.node_options = node_options
            self.instrument_options = instrument_options
            self.temp_bool = temp_bool
            # self.delete_bool = delete_bool
            self.offset = (0, 0)
            self.is_dragging = False
            self.last_pos = (0, 0)
            self.image_x = 0.0
            self.image_y = 0.0
            # self.select_ver = {"value": None}
            self.chip_dialog = ui.dialog().classes("")
            self.img_dialog = ui.dialog().props("maximized").classes("p-0")
            self.check_down_dialog = ui.dialog().classes("")
            self.activ_dialog = ui.dialog().props("persistent").classes("")
            # self.image_show = {"image_show": True}
            # self.chip_dialog.bind_value_to(self.image_show, "image_show")

            # 为每个按钮实例在 app.storage.general 概述数据各项目字典里 以self.label作为键，后续保存用户输入
            # 初始化存储，如果 app.storage.general 中不存在对应的列表，则创建一个空列表
            # if self.label not in db_storage.get_item(f"{self.project}_over_data", {}):
            #     await db_storage.set_deep_item([f"{self.project}_over_data", self.label], {})

            # 创建主按钮，并绑定点击事件
            ui.button(f"{self.title}：", on_click=self._handle_main_button_click).props("flat").classes(
                "p-1 text-[14px]/[14px] mt-2 font-semibold"
            )

            # 创建一个行(row)容器，用于存放生成的所有 chip
            self.chip_container = ui.row().classes("w-full items-center gap-2 pl-8")

            # 根据处理类型，设置不同的交互逻辑
            if self.processing_type == "image":
                # 创建一个隐藏的 ui.upload 组件，我们将通过程序触发它
                self.uploader = ui.upload(
                    on_upload=self._handle_file_upload,
                    auto_upload=True,
                    max_files=1,
                ).props('accept="image/*"')
                # 隐藏upload元素
                self.uploader.set_visibility(False)
            else:
                # 创建一个隐藏的 ui.upload 组件，我们将通过程序触发它
                self.uploader = ui.upload(
                    on_upload=self._handle_file_upload,
                    auto_upload=True,
                    max_files=1,
                ).props('accept=".pdf, .xlsx, .docx, .pptx"')
                # 隐藏upload元素
                self.uploader.set_visibility(False)
            # 设置一个定时器，每隔0.5秒检查一次共享数据是否有变化，并更新UI
            # 这是实现多用户实时同步的关键
            ui.timer(0.5, self._update_chip_display)

        # 显示大图
        def show_fullscreen(self, url_path):
            self.img_dialog.clear()
            with self.img_dialog:
                with (
                    ui.card()
                    .classes("relative h-full w-full overflow-hidden items-center justify-center")
                    .style("background-color: rgba(0,0,0,0);")
                ):
                    ui.label("按ESC键退出图片查看界面").classes("absolute top-15 right-10 text-xl text-amber-9 z-999")
                    self.image_big = ui.interactive_image(
                        url_path,
                    ).classes("cursor-grab")
                # self.image_big.props("fit=contain")
                # 绑定事件
                self.image_big.on("mousedown", self.start_drag)
                self.image_big.on_mouse(self.get_img_xy)
                self.image_big.on("mousemove", self.handle_drag)
                self.image_big.on("mouseup", self.end_drag)
                self.image_big.on("mouseleave", self.end_drag)
                self.image_big.on("wheel", self.handle_zoom)
            # 打开弹窗
            self.img_dialog.open()
            # print(self.chip_dialog.value)
            # 复位图片
            self.reset_transform()

        # 图片开始拖拽
        def start_drag(self, e: GenericEventArguments):
            if e.args.get("button") == 0:
                self.is_dragging = True
                self.last_pos = (e.args["clientX"], e.args["clientY"])
                self.image_big.classes(replace="cursor-grabbing")
            elif e.args.get("button") == 1:
                self.reset_transform()

        # 图片移动
        def handle_drag(self, e: GenericEventArguments):
            if self.is_dragging:
                dx = e.args["clientX"] - self.last_pos[0]
                dy = e.args["clientY"] - self.last_pos[1]
                self.offset = (self.offset[0] + dx, self.offset[1] + dy)
                self.last_pos = (e.args["clientX"], e.args["clientY"])
                self.update_transform()

        # 图片结束拖拽
        def end_drag(self, e: GenericEventArguments):
            self.is_dragging = False
            self.image_big.classes(replace="cursor-grab")

        # 获取鼠标相对图片左上角的坐标值
        def get_img_xy(self, e: MouseEventArguments):
            self.image_x = e.image_x
            self.image_y = e.image_y

        # 处理图片缩放
        def handle_zoom(self, e: GenericEventArguments):
            # 更新缩放级别（限制在0.1x到5x之间）
            new_zoom = self.zoom_level * (1.1 if e.args["deltaY"] < 0 else 0.9)
            self.zoom_level = max(0.01, min(10, new_zoom))
            # 更新图片
            self.update_transform()

        # 更新图片变换函数
        def update_transform(self):
            self.image_big.style(
                f"transform: translate({self.offset[0]}px, {self.offset[1]}px) scale({self.zoom_level})"
            )

        # 重置变换状态
        def reset_transform(self):
            self.zoom_level = 1.0
            self.offset = (0, 0)
            self.update_transform()

        # <----------------------------------------------------------------
        # 辅助函数，利用传入的项目最大版本值，生成多选项字典用于存入chip数据里
        def _get_select_activ_dic(self, req_max_ver):
            select_dic = {}
            for select_label in [f"{i}.0" for i in range(1, int(float(req_max_ver)) + 1)]:
                # 新增加的chip，其之前的版本默认属于不激活，只有当前最新版本记录为激活
                if select_label == req_max_ver:
                    select_dic[select_label] = True
                else:
                    select_dic[select_label] = False
            return select_dic

        # 当用户点击“添加”按钮时，将文本数据添加到共享存储中
        async def _add_text_chip_data(self):
            text = self.chip_label.value
            notes = self.chip_notes.value
            if not text:
                ui.notify(
                    "概述内容不能为空!",
                    type="negative",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif not notes:
                ui.notify(
                    "注释不能为空!",
                    type="negative",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif text in [
                d["content"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
            ]:
                ui.notify(
                    "概述内容已存在。",
                    type="warning",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            else:
                # 准备要存储的 chip 数据
                chip_id = str(uuid.uuid4())
                req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")
                chip_data = {
                    "id": chip_id,  # 使用UUID确保每个chip都有一个唯一的ID
                    "role": self.role,
                    "icon": None,
                    "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                    # "removable": False,  # 控制元素是否有删除按钮
                    "bg_color": "bg-light-blue-1",
                    "type": "text",
                    "content": text,
                    "notes": notes,
                    "creator": creator,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                }

                # 将新数据追加到 app.storage.general 的列表中
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                # app.storage.general["overview_data"][self.project][self.label][chip_id] = chip_data
                # 清空文本框并关闭对话框
                self.chip_label.value = ""
                self.chip_notes.value = ""
                self.chip_dialog.close()
                ui.notify(
                    "内容已添加。",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )

        # 处理文件/图片上传事件
        async def _handle_file_upload(self, e):
            original_filename = e.file.name
            file_type = e.file.content_type
            # 生成一个唯一的内部文件名以避免覆盖，但保留原始文件名用于显示
            # unique_filename = f"{uuid.uuid4().hex}{Path(original_filename).suffix}"
            # filepath = self.upload_path / unique_filename
            filepath = self.upload_path / original_filename
            url_path = f"{FILES_URL_DIR}/{original_filename}"
            # 检查是否已存在该项里了
            if original_filename in [
                d["filename"] for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
            ]:
                ui.notify(
                    f'文件 "{original_filename}" 无需重复提交!',
                    type="warning",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            # 检查服务器是否存在同名文件
            elif os.path.exists(filepath):
                # app.add_static_file(local_file=filepath, url_path=url_path)
                self._select_file_show(original_filename, file_type, url_path)
            else:
                try:
                    # 1. 一次性将文件内容完整读入内存中的 bytes 对象
                    #    无论 e.file 是 SmallFileUpload 还是 FileUpload，.read() 都是支持的。
                    file_content = await e.file.read()
                    # 2. 使用 io.BytesIO 将内存中的 bytes 数据包装成一个标准的文件对象
                    #    这个 file_like_object 的行为与真实文件完全一致，始终支持 seek()。
                    file_content_object = io.BytesIO(file_content)

                    # e.file 是一个类文件对象，我们需要读取其内容并写入到本地文件
                    with open(filepath, "wb") as f:
                        f.write(file_content_object.read())
                    # app.add_static_file(local_file=filepath, url_path=url_path)
                except Exception as ex:
                    print(f"上传处理失败: {ex}")  # 在服务器端打印错误详情
                    ui.notify(
                        f"上传文件 '{original_filename}' 失败: {str(ex)}",
                        type="negative",
                        position="bottom",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                file_icon = ""
                # 文件类型的icon与图片的设置不一样
                if self.processing_type == "file":
                    # 文件类型才将icon设置为引用小图，图片类不设置
                    file_icon = "attach_file"
                chip_id = str(uuid.uuid4())
                req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")
                # 生成文件或图片的chip_data
                chip_data = {
                    "id": chip_id,
                    "role": self.role,
                    "icon": file_icon,
                    "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                    # "removable": False,  # 控制元素是否有删除按钮
                    "bg_color": "bg-light-blue-1",
                    "type": self.processing_type,
                    "file_type": file_type,
                    # "filepath": f"{filepath}", 路径不能记死
                    "filename": original_filename,
                    "url_path": url_path,
                    "notes": self.chip_notes.value,
                    "creator": creator,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                }
                self.chip_notes.value = ""
                self.chip_dialog.close()
                # 将新数据追加到共享列表中
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                ui.notify(
                    f'文件 "{original_filename}" 上传成功!',
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )

        # 显示服务器已有文件
        async def _show_have_file(self, original_filename, file_type, url_path):
            # 准备要存储的 chip 数据
            file_icon = ""
            if self.processing_type == "file":
                # 文件类型才将icon设置为引用小图，图片类不设置
                file_icon = "attach_file"
            chip_id = str(uuid.uuid4())
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            select_activ_dic = self._get_select_activ_dic(req_max_ver)
            creator = app.storage.user.get("current_user", "匿名用户")
            # 生成文件或图片的chip_data
            chip_data = {
                "id": chip_id,
                "role": self.role,
                "icon": file_icon,
                "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                # "removable": False,  # 控制元素是否有删除按钮
                "bg_color": "bg-light-blue-1",
                "type": self.processing_type,
                "file_type": file_type,
                # "filepath": f"{filepath}",
                "filename": original_filename,
                "url_path": url_path,
                "notes": self.chip_notes.value,
                "creator": creator,
                "timestamp": {
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    }
                },
                "req_ver": req_max_ver,
                "select_activ_dic": select_activ_dic,
            }
            self.chip_notes.value = ""
            self.chip_dialog.close()
            # 将新数据追加到共享列表中
            await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
            ui.notify(
                f'文件 "{original_filename}" 显示成功!',
                type="positive",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

        # 将测试项配置信息添加到共享储存中
        async def _add_test_chip_data(self, test_select_data):
            text = self.chip_label.value
            notes = self.chip_notes.value
            # 判断是否存在选择“其它”但不写明特殊要求的情况
            other_bool = False
            if test_select_data["state_select"] == "其它" and not test_select_data["state_other_text"]:
                other_bool = True
            if test_select_data["node_select"] == "其它" and not test_select_data["node_other_text"]:
                other_bool = True
            if test_select_data["instrument_select"] == "其它" and not test_select_data["instrument_other_text"]:
                other_bool = True

            # 测试项内容不能为空，选项一旦生成也不能不选（None）
            if (
                not text
                or test_select_data["state_select"] is None
                or test_select_data["node_select"] is None
                or test_select_data["instrument_select"] is None
            ):
                ui.notify(
                    "测试项内容及选项必须填写和选择!",
                    type="negative",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif not notes:
                ui.notify(
                    "注释不能为空!",
                    type="negative",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif other_bool:
                ui.notify(
                    "特殊要求不能为空!",
                    type="negative",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            elif (text, test_select_data) in [
                (d["content"], d["test_select_data"])
                for d in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values()
            ]:
                ui.notify(
                    "测试项内容标准已存在。",
                    type="warning",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            else:
                # 准备要存储的 chip 数据
                chip_id = str(uuid.uuid4())
                req_max_ver = app.storage.general["project_req_max_ver"][self.project]
                select_activ_dic = self._get_select_activ_dic(req_max_ver)
                creator = app.storage.user.get("current_user", "匿名用户")
                chip_data = {
                    "id": chip_id,  # 使用UUID确保每个chip都有一个唯一的ID
                    "role": self.role,
                    "icon": None,
                    "enabled": True,  # 控制元素是否可点击，接着用来控制是否在项目表上显示
                    # "removable": False,  # 控制元素是否有删除按钮
                    "bg_color": "bg-light-blue-1",
                    "type": "test",
                    "content": text,
                    "notes": notes,
                    "test_select_data": test_select_data,
                    "creator": creator,
                    "timestamp": {
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"): {
                            "creator": creator,
                            "select_activ_dic": select_activ_dic,
                        }
                    },
                    "req_ver": req_max_ver,
                    "select_activ_dic": select_activ_dic,
                }

                # 将新数据追加到 app.storage.general 的列表中
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id], chip_data)
                # 清空文本框并关闭对话框
                self.chip_notes.value = ""
                self.chip_dialog.close()
                ui.notify(
                    "内容已添加。",
                    type="positive",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )

        # ----------------------------------------------------------------->

        # 询问重复提交文件是否按服务器现有文件显示
        def _select_file_show(self, original_filename, file_type, url_path):
            self.chip_dialog.clear()
            self.chip_dialog.open()
            with self.chip_dialog, ui.card().classes("w-1/2 bg-orange-2"):
                ui.label("服务器已有同名文件，无法上传覆盖，是否使用服务器已有文件？").classes("text-lg")
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "是",
                        on_click=lambda: self._show_have_file(original_filename, file_type, url_path),
                        color="green-6",
                    )
                    ui.button("否", on_click=lambda: self.chip_dialog.close(), color="blue-grey-6")

        # 刷新chip容器
        def _refresh_chip_container(self):
            # 删除元素重新显示
            self.chip_container.clear()
            with self.chip_container:
                for chip_info in db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).values():
                    self._create_chip_from_data(chip_info)

        # 同步UI显示与共享存储中的数据
        def _update_chip_display(self):
            """
            同步UI显示与共享存储中的数据。
            这是由定时器调用的核心同步函数。
            """
            # 在用户打开了大图的情况下，不刷对应条目下的缩略图元素
            if not (self.chip_dialog.value or self.check_down_dialog.value or self.activ_dialog.value):
                # if self.processing_type == "image":
                #     print(self.chip_dialog.value, self.title)

                # 获取当前UI上所有 chip 的ID
                displayed_chip_ids = {child.props.get("data-chip-id") for child in self.chip_container}
                # 获取共享存储中所有 chip 的ID
                stored_chips_data = db_storage.get_deep_item([f"{self.project}_over_data", self.label], {})
                stored_chip_ids = set(stored_chips_data.keys())

                # 只有当UI和存储中的ID集合不一致时，才重新渲染，以提高效率
                if displayed_chip_ids != stored_chip_ids:
                    # 刷新chip容器内容
                    self._refresh_chip_container()
                    # 刷新角色负责用户数据
                    overview_role_update(self.project)

        # 打开文件
        # def open_file(self, filepath):
        #     # 获取操作系统类型
        #     os_type = sys.platform
        #     if os_type == "win32":
        #         # os.startfile(f"{UPLOADS_DIR}/{self.file_neme_suffix}")
        #         os.startfile(filepath)
        #     elif os_type == "darwin":
        #         # subprocess.run(["open", f"{UPLOADS_DIR}/{self.file_neme_suffix}"])
        #         subprocess.run(["open", filepath])
        #     else:
        #         ui.notify(
        #             "未适配当前操作系统，不能直接打开。",
        #             type="info",
        #             position="bottom",
        #             timeout=1000,
        #             progress=True,
        #             close_button="✖",
        #         )
        # pdf文件打开函数
        def open_pdf_in_browser(self, url_path):
            # 在浏览器中打开 PDF 文件
            async def get_base_url():
                # 通过 JavaScript 获取当前页面的协议、域名和路径
                result = await ui.run_javascript("window.location.origin;")
                return result

            # 2. 异步执行并拼接完整 URL
            async def open_pdf():
                base_url = await get_base_url()
                full_url = f"{base_url}{url_path}"
                # 处理空格等特殊字符
                encoded_url = full_url.replace(" ", "%20")
                # 3. 打开新窗口
                ui.run_javascript(f'window.open("{encoded_url}", "_blank");')
                # print(f"尝试打开PDF：{encoded_url}")

            # 启动异步任务
            ui.timer(0.1, lambda: open_pdf(), once=True)

        def trigger_download(self, filepath, file_name, on_complete=None):
            """专门负责触发下载的辅助函数"""
            ui.notify(
                f"开始下载文件: {file_name}",
                type="info",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            ui.download(filepath)
            if on_complete:
                on_complete()

        # 我们创建一个新的、更智能的下载处理函数
        async def check_and_download(self, filepath, file_name):
            """
            检查文件是否已在当前会话下载过。
            如果是，则弹出一个带引导信息的对话框；如果否，则开始下载并标记。
            """
            storage_key = f"downloaded_{file_name}"
            has_downloaded = await ui.run_javascript(f'sessionStorage.getItem("{storage_key}")')

            if has_downloaded:
                # 【修改点】对这里的对话框进行全面升级
                self.check_down_dialog.clear()
                with self.check_down_dialog, ui.card().classes("min-w-[400px]"):
                    with ui.card_section():
                        ui.label(f'文件 "{file_name}" 已在本次会话中下载。').classes("text-lg font-medium")
                        ui.separator().props("size=1px").classes("my-3")
                        ui.label("您可以：")
                        # 使用 HTML 来创建更丰富的文本格式
                        ui.html(
                            """
                            <ul class="q-pl-lg">
                                <li>在浏览器的<b>下载栏</b>中直接找到它。</li>
                                <li>按键盘快捷键 <kbd>Ctrl</kbd> + <kbd>J</kbd> (Windows/Linux) 或 <kbd>⌘</kbd> + <kbd>Shift</kbd> + <kbd>J</kbd> (Mac) 打开<b>下载内容页面</b>。</li>
                            </ul>
                        """,
                            sanitize=False,
                        ).classes("text-base")  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                    with ui.card_actions().props("align=right"):
                        # “重新下载”按钮，保持原样
                        ui.button(
                            "仍要重新下载",
                            on_click=lambda filepath=filepath, file_name=file_name: self.trigger_download(
                                filepath, file_name, self.check_down_dialog.close
                            ),
                            color="primary",
                        )
                        # 将“取消”按钮改为更中性的“关闭”
                        ui.button("关闭", on_click=self.check_down_dialog.close, color="grey")

                self.check_down_dialog.open()

            # 3. 如果标记不存在 (首次点击)
            else:
                # a. 立即触发下载
                self.trigger_download(filepath, file_name)
                # b. 通过JavaScript在客户端设置标记
                await ui.run_javascript(f'sessionStorage.setItem("{storage_key}", "true")')

        # 当元素被鼠标右键点击时触发的事件处理函数
        def on_right_click(self, chip_data):
            """
            当元素被鼠标右键点击时触发的事件处理函数。
            """
            # 将 Python 变量的内容传递给 JavaScript
            # navigator.clipboard.writeText(text) 是现代浏览器提供的剪贴板 API
            # 这里的 f-string 会将 Python 变量值安全地嵌入到 JS 代码中
            text = ""
            if "content" in chip_data.keys():
                text = chip_data.get("content")
            elif "filename" in chip_data.keys():
                text = chip_data.get("filename")
            js_code = f"navigator.clipboard.writeText('{text}');"
            ui.run_javascript(js_code)
            ui.notify("内容已复制到剪贴板！", type="positive", position="top")

        # <-----------------------------------------------------------------
        # 设置chip的激活状态
        async def _set_chip_activ(self, chip_id, old_chip_select_dic):
            # chip以当前最新版本的设置为当前显示状态
            req_max_ver = app.storage.general["project_req_max_ver"][self.project]
            if db_storage.get_deep_item(
                [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", req_max_ver]
            ):
                # 激活chip
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["enabled"] = True
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], True)
                if db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "type"]) == "file":
                    # app.storage.general["overview_data"][self.project][self.label][chip_id]["icon"] = "attach_file"
                    await db_storage.set_deep_item(
                        [f"{self.project}_over_data", self.label, chip_id, "icon"], "attach_file"
                    )
                else:
                    # app.storage.general["overview_data"][self.project][self.label][chip_id]["icon"] = None
                    await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], None)
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["bg_color"] = "bg-light-blue-1"
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-light-blue-1"
                )
            # 防止chip状态None（null）被当成False，当用户在弹窗选择激活状态时不做选择动作，保持原有null状态chip被处理成False显示效果
            elif (
                db_storage.get_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", req_max_ver]
                )
                is None
            ):
                # 该情况意味着用户没有修改当前chip最新版本的null状态，看了一下而已
                # 只要跳过这个情况不做任何修改即可；只单单修改当前一个chip状态，改需求所有类似状态的chip在概述框架渲染检测需求有没有新版本时统一设置了
                pass
                # 冗余设计，复用注意检查与整体刷新处设置是否一致
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["enabled"] = None
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["icon"] = "question_mark"
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["bg_color"] = "bg-amber-1"
            else:
                # 失活chip
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["enabled"] = False
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "enabled"], False)
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["icon"] = "block"
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "icon"], "block")
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["bg_color"] = "bg-grey-5"
                await db_storage.set_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "bg_color"], "bg-grey-5"
                )

            # 如果激活弹窗关闭时，检测到激活多选项发生了变化，则修改该chip的编辑人
            select_activ_dic = copy.deepcopy(
                db_storage.get_deep_item([f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {})
            )
            if old_chip_select_dic != select_activ_dic:
                creator = app.storage.user.get("current_user", "匿名用户")
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["creator"] = creator
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label, chip_id, "creator"], creator)
                # app.storage.general["overview_data"][self.project][self.label][chip_id]["timestamp"][
                #     datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # ] = {
                #     "creator": creator,
                #     "select_activ_dic": select_activ_dic,
                # }
                await db_storage.set_deep_item(
                    [
                        f"{self.project}_over_data",
                        self.label,
                        chip_id,
                        "timestamp",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ],
                    {
                        "creator": creator,
                        "select_activ_dic": select_activ_dic,
                    },
                )
            # 刷新chip容器内容
            self._refresh_chip_container()
            # 刷新概述负责人
            overview_role_update(self.project)

        # 创建用于让用户选择chip激活范围的弹窗
        def _select_activ_dialog(self, chip_id):
            self.activ_dialog.clear()
            with self.activ_dialog, ui.card().classes("w-1/2"):
                ui.label("选择概述生效的需求版本").classes("text-lg font-bold")
                chip_select_dic = db_storage.get_deep_item(
                    [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic"], {}
                )
                old_chip_select_dic = copy.deepcopy(chip_select_dic)
                with ui.grid(columns=6).classes("w-full gap-0"):
                    for select_label, val in chip_select_dic.items():
                        ui.checkbox(
                            text=select_label,
                            value=val,
                            on_change=lambda e: db_storage.set_deep_item(
                                [f"{self.project}_over_data", self.label, chip_id, "select_activ_dic", select_label],
                                e.value,
                            ),
                        )
                with ui.row().classes("w-full justify-end"):
                    ui.label("注意以上改动是即时生效的").classes("text-lg font-bold")
                    ui.button("关闭", on_click=lambda: self._set_chip_activ(chip_id, old_chip_select_dic)).on(
                        "click", lambda: self.activ_dialog.close()
                    )
            self.activ_dialog.open()

        # 删除或修改chip在app.storage.general对应的数据
        async def delete_chip_info(self, chip):
            # 如果用户具有编辑权限
            if self._edit_permission_judge():
                if app.storage.user["current_user"] == "admin":
                    # del app.storage.general["overview_data"][self.project][self.label][chip.props["data-chip-id"]]
                    await db_storage.del_deep_item(
                        [f"{self.project}_over_data", self.label, chip.props["data-chip-id"]]
                    )

                elif app.storage.user["current_user"] != "admin":
                    # app.storage.general["overview_data"][self.project][self.label][chip.props["data-chip-id"]]["removable"] = False
                    chip_id = chip.props["data-chip-id"]
                    self._select_activ_dialog(chip_id)

        # 删除或修改文件缩略图及其在app.storage.general的数据
        async def clear_thumbnail(self, thumbnail):
            # 如果用户具有编辑权限
            if self._edit_permission_judge():
                if app.storage.user["current_user"] == "admin":
                    thumbnail.delete()
                    # del app.storage.general["overview_data"][self.project][self.label][thumbnail.props["data-chip-id"]]
                    await db_storage.del_deep_item(
                        [f"{self.project}_over_data", self.label, thumbnail.props["data-chip-id"]]
                    )
                elif app.storage.user["current_user"] != "admin":
                    chip_id = thumbnail.props["data-chip-id"]
                    self._select_activ_dialog(chip_id)

        # 将该项插入的chip里指定chip上移一个位置
        async def move_up_data(self, chip_data):
            # 如果用户具有编辑权限
            if self._edit_permission_judge():
                temp_data = {}
                old_data_keys = list(db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).keys())
                new_data_keys = move_element(old_data_keys, chip_data["id"], -1)
                for k in new_data_keys:
                    # temp_data[k] = app.storage.general["overview_data"][self.project][self.label][k]
                    temp_data[k] = db_storage.get_deep_item([f"{self.project}_over_data", self.label, k], {})
                # app.storage.general["overview_data"][self.project][self.label] = temp_data
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label], temp_data)
                # 刷新chip容器内容
                self._refresh_chip_container()

        # 将该项插入的chip里指定chip上移一个位置
        async def move_down_data(self, chip_data):
            # 如果用户具有编辑权限
            if self._edit_permission_judge():
                temp_data = {}
                old_data_keys = list(db_storage.get_deep_item([f"{self.project}_over_data", self.label], {}).keys())
                new_data_keys = move_element(old_data_keys, chip_data["id"], 1)
                for k in new_data_keys:
                    # temp_data[k] = app.storage.general["overview_data"][self.project][self.label][k]
                    temp_data[k] = db_storage.get_deep_item([f"{self.project}_over_data", self.label, k], {})
                # app.storage.general["overview_data"][self.project][self.label] = temp_data
                await db_storage.set_deep_item([f"{self.project}_over_data", self.label], temp_data)
                # 刷新chip容器内容
                self._refresh_chip_container()

        # 根据字典数据创建一个具体的 ui.chip 组件。
        def _create_chip_from_data(self, chip_info: dict):
            chip_text = ""
            filepath = ""
            delete_icon = ""
            delete_bg = ""
            # 根据用户类型及删除按钮状态设置新的删除按钮类型
            if app.storage.user["current_user"] == "admin":
                delete_icon = "close"
                delete_bg = "bg-red text-white"
            else:
                if chip_info.get("icon") == "block":
                    delete_icon = "settings"  # 之前是check
                    delete_bg = "bg-white text-light-blue"
                else:
                    delete_icon = "settings"  # 之前是block
                    delete_bg = "bg-white text-light-blue"  # 之前是text-grey-10

            if chip_info.get("type") in ["text", "file", "test"]:
                # 根据chip类型配置文字标签内容
                if chip_info.get("type") in ["text", "test"]:
                    chip_text = chip_info.get("content", "")
                elif chip_info["type"] == "file":
                    chip_text = chip_info.get("filename", "")
                    # 每次生成都用更新配置的路径
                    filepath = f"{self.upload_path}/{chip_text}"
                # 创建 chip 并附加一个自定义属性 `data-chip-id` 用于后续的同步检查
                chip = (
                    ui.chip(text=chip_text, removable=False, icon=chip_info.get("icon"))
                    .props(f"data-chip-id={chip_info.get('id')} dense square")
                    .classes(f"m-0 {chip_info.get('bg_color')}")
                )
                # 创建chip元素的附属元素
                with chip:
                    # 为 chip 添加 tooltip
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>{chip_info.get('notes', '').replace('\n', '<br>')}"
                    with ui.tooltip():
                        ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                    # 注意：我们将on_click事件直接绑定在这里
                    delete_button = (
                        ui.button(on_click=lambda c=chip: self.delete_chip_info(c))
                        .classes(f"absolute -top-1 -right-1 m-0 p-0 q-py-0 {delete_bg}")
                        .props(f'round padding="0px 0px" icon={delete_icon}')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )
                    # chip上移按钮
                    move_up_button = (
                        ui.button(on_click=lambda chip_data=chip_info: self.move_up_data(chip_data))
                        .classes("absolute -top-1 right-7 m-0 p-0 q-py-0 bg-white text-light-blue")
                        .props('round padding="0px 0px" icon="arrow_drop_up"')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )
                    # chip下移按钮
                    move_down_button = (
                        ui.button(on_click=lambda chip_data=chip_info: self.move_down_data(chip_data))
                        .classes("absolute -top-1 right-3 m-0 p-0 q-py-0 bg-white text-light-blue")
                        .props('round padding="0px 0px" icon="arrow_drop_down"')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )
                # 设置chip元素是否显示
                # chip.set_value(chip_info["value"])
                # 设置chip元素是否可点击，会导致其上的好标签出不来
                # chip.set_enabled(chip_info["enabled"])

                # 为chip绑定各种事件
                chip.on("contextmenu", lambda chip_data=chip_info: self.on_right_click(chip_data))
                chip.on("mouseenter", lambda b=delete_button: ui_show(b)).on(
                    "mouseleave", lambda b=delete_button: ui_hide(b)
                )
                chip.on("mouseenter", lambda b=move_up_button: ui_show(b)).on(
                    "mouseleave", lambda b=move_up_button: ui_hide(b)
                )
                chip.on("mouseenter", lambda b=move_down_button: ui_show(b)).on(
                    "mouseleave", lambda b=move_down_button: ui_hide(b)
                )
                if chip_info.get("type") == "text":
                    pass
                    # chip.on_click(lambda: print(chip.value))
                    # chip.set_enabled(False)
                elif chip_info.get("type") == "file":
                    app.add_static_file(local_file=filepath, url_path=chip_info.get("url_path"))
                    if chip_info.get("file_type") == "application/pdf":
                        # 使用浏览器打开则用open_pdf_in_browser()
                        chip.on_click(lambda url_path=chip_info.get("url_path"): self.open_pdf_in_browser(url_path))
                    else:
                        chip.on_click(
                            lambda filepath=filepath, file_name=chip_text: self.check_and_download(filepath, file_name)
                        )

            # chip类型为缩略图
            elif chip_info.get("type") == "image":
                image_name = chip_info.get("filename")
                # 每次生成都用更新配置的路径
                image_path = f"{self.upload_path}/{image_name}"

                url_path = f"{FILES_URL_DIR}/{image_name}"
                # print(image_path, url_path)
                app.add_static_file(local_file=image_path, url_path=url_path)
                # 根据文件类型创建缩略图
                thumbnail = (
                    ui.interactive_image(url_path)
                    .props(f"data-chip-id={chip_info.get('id')}")
                    .classes("h-10 cursor-pointer")
                )
                thumbnail.on("click", lambda url_path=url_path: self.show_fullscreen(url_path))

                # 创建缩略图的附属元素
                with thumbnail:
                    if chip_info.get("icon"):
                        ui.icon(chip_info.get("icon", "")).props("flat fab color=red").classes(
                            "absolute top-0 left-0 text-xl"
                        )
                    # 缩略图创建日期提示
                    tooltip_text = f"创建节点: 需求V{chip_info.get('req_ver')}后<br>创建者: {chip_info.get('creator')}<br>时间: {next(reversed(chip_info.get('timestamp', {})))}<br>注释: <br>{chip_info.get('notes', '').replace('\n', '<br>')}"
                    with ui.tooltip():
                        ui.html(tooltip_text, sanitize=Sanitizer().sanitize)

                    # 缩略图删除按钮
                    delete_button = (
                        ui.button(on_click=lambda thumbnail=thumbnail: self.clear_thumbnail(thumbnail))
                        .classes(f"absolute -top-1 -right-1 m-0 p-0 q-py-1 {delete_bg}")
                        .props(f'round padding="0px 0px" icon={delete_icon}')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )
                    # 缩略图上移按钮
                    move_up_button = (
                        ui.button(on_click=lambda chip_data=chip_info: self.move_up_data(chip_data))
                        .classes("absolute bottom-3 -right-1 m-0 p-0 q-py-0 bg-white text-light-blue")
                        .props('round padding="0px 0px" icon="arrow_drop_up"')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )
                    # 缩略图下移按钮
                    move_down_button = (
                        ui.button(on_click=lambda chip_data=chip_info: self.move_down_data(chip_data))
                        .classes("absolute -bottom-1 -right-1 m-0 p-0 q-py-0 bg-white text-light-blue")
                        .props('round padding="0px 0px" icon="arrow_drop_down"')
                        .style("font-size: 8px; display: none;")
                        .on("click", js_handler="(e) => {e.stopPropagation()}")
                    )

                # 为缩略图绑定各种事件
                thumbnail.on("mouseover", lambda b=delete_button: ui_show(b)).on(
                    "mouseout", lambda b=delete_button: ui_hide(b)
                )
                thumbnail.on("mouseover", lambda b=move_up_button: ui_show(b)).on(
                    "mouseout", lambda b=move_up_button: ui_hide(b)
                )
                thumbnail.on("mouseover", lambda b=move_down_button: ui_show(b)).on(
                    "mouseout", lambda b=move_down_button: ui_hide(b)
                )

        # <-----------------------------------------------------------------
        # 创建用于输入文本chip的概述内容与注释的对话框
        def _setup_text_chip_dialog(self):
            self.chip_dialog.clear()
            with self.chip_dialog, ui.card().classes("w-1/2"):
                ui.label("添加新的概述内容").classes("text-lg font-bold")
                self.chip_label = (
                    ui.textarea(label=self.dialog_label, placeholder=self.dialog_placeholder)
                    .props("outlined")
                    .classes("w-full")
                )
                self.chip_notes = (
                    ui.textarea(
                        label="针对该技术概述的注释（必填）",
                        placeholder="首填/变更原因",
                        validation={"不能空白": lambda value: value.strip() != ""},
                    )
                    .props("outlined")
                    .classes("w-full")
                )
                with ui.row().classes("w-full justify-end"):
                    ui.button("添加", on_click=self._add_text_chip_data)
            self.chip_dialog.open()

        # 触发文件上传界面，用于给用户选择文件，然后自动触发文件处理函数
        def _get_file_upload(self):
            if not self.chip_notes.value:
                ui.notify(
                    "注释不能为空!",
                    type="negative",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            else:
                # 在上传新文件前，先清空uploader列表，否则后续删除文件后，不能在重新插入
                self.uploader.reset()
                # 调用JavaScript方法来触发隐藏的<input type="file">元素的点击事件
                self.uploader.run_method("pickFiles")

        # 创建用于输入文件注释的对话框
        def _setup_file_notes_dialog(self):
            self.chip_dialog.clear()
            with self.chip_dialog, ui.card().classes("w-1/2"):
                ui.label("添加上传文件的注释").classes("text-lg font-bold")
                self.chip_notes = (
                    ui.textarea(
                        label="针对该文件的注释（必填）",
                        placeholder="首次提交/变更原因",
                        validation={"不能空白": lambda value: value.strip() != ""},
                    )
                    .props("outlined")
                    .classes("w-full")
                )
                with ui.row().classes("w-full justify-end"):
                    ui.button("添加", on_click=self._get_file_upload)
            self.chip_dialog.open()

        def _set_other_ui(self, other_ui, select_value):
            if select_value == "其它":
                other_ui.set_visibility(True)
            elif other_ui:
                other_ui.set_visibility(False)
                other_ui.set_value("")

        # 创建用于配置测试项的对话框
        def _setup_test_chip_dialog(self):
            self.chip_dialog.clear()
            with self.chip_dialog, ui.card().classes("w-full"):
                ui.label(f"添加产品的{self.title}").classes("text-lg font-bold")
                placeholder = ""
                test_select_data = {
                    "state_select": "",
                    "state_other_text": "",
                    "node_select": "",
                    "node_other_text": "",
                    "instrument_select": "",
                    "instrument_other_text": "",
                }

                if self.label == "optical_testing":
                    placeholder = "色温：5500K±500K"
                elif self.label == "mechanical_testing":
                    placeholder = "测试项名称：测试条件、产品状态、操作步骤、合格标准等信息。"
                elif self.label == "electronic_testing":
                    placeholder = "电压：12V±3%"
                elif self.label == "software_testing":
                    placeholder = "测试项名称：操作步骤、合格标准等信息；或指明依据的文档。"
                elif self.label == "ui_testing":
                    placeholder = "写明UI检查内容与要求。"
                self.chip_label = (
                    ui.textarea(
                        label="检测内容与标准",
                        placeholder=placeholder,
                        validation={"不能空白": lambda value: value.strip() != ""},
                    )
                    .props("outlined")
                    .classes("w-full")
                )
                if self.state_options:
                    with ui.column().classes("w-full p-0 m-0"):
                        state_select = (
                            ui.select(
                                self.state_options,
                                multiple=False,
                                label="条件/状态",
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, "state_select")
                        )
                        state_other_ui = (
                            ui.textarea(
                                label="条件/状态特殊要求",
                                placeholder="写明特殊要求",
                                validation={"不能空白": lambda value: value.strip() != ""},
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, "state_other_text")
                        )
                        state_other_ui.set_visibility(False)
                        state_select.on_value_change(lambda: self._set_other_ui(state_other_ui, state_select.value))
                if self.node_options:
                    with ui.column().classes("w-full p-0 m-0"):
                        node_select = (
                            ui.select(
                                self.node_options,
                                multiple=False,
                                label="节点/位置",
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, "node_select")
                        )
                        node_other_ui = (
                            ui.textarea(
                                label="节点/位置特殊要求",
                                placeholder="写明特殊要求",
                                validation={"不能空白": lambda value: value.strip() != ""},
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, "node_other_text")
                        )
                        node_other_ui.set_visibility(False)
                        node_select.on_value_change(lambda: self._set_other_ui(node_other_ui, node_select.value))
                if self.instrument_options:
                    with ui.column().classes("w-full p-0 m-0"):
                        instrument_select = (
                            ui.select(
                                self.instrument_options,
                                multiple=False,
                                label="工具/仪器/治具",
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, "instrument_select")
                        )
                        instrument_other_ui = (
                            ui.textarea(
                                label="工具/仪器/治具特殊要求",
                                placeholder="写明特殊要求",
                                validation={"不能空白": lambda value: value.strip() != ""},
                            )
                            .props("outlined")
                            .classes("w-full")
                            .bind_value(test_select_data, "instrument_other_text")
                        )
                        instrument_other_ui.set_visibility(False)
                        instrument_select.on_value_change(
                            lambda: self._set_other_ui(instrument_other_ui, instrument_select.value)
                        )
                self.chip_notes = (
                    ui.textarea(
                        label="针对该检测内容与标准的注释（必填）",
                        placeholder="首填/变更原因",
                        validation={"不能空白": lambda value: value.strip() != ""},
                    )
                    .props("outlined")
                    .classes("w-full")
                )
                with ui.row().classes("w-full justify-end"):
                    ui.button("添加", on_click=lambda: self._add_test_chip_data(test_select_data))
            self.chip_dialog.open()

        # ----------------------------------------------------------------->

        # 判断当前用户是否具有编辑权限
        def _edit_permission_judge(self):
            # 判断用户是否具有编辑权限 且 不处于概述审核界面
            if app.storage.user["current_role"] in self.permission["edit_role"] and not self.temp_bool:
                return True
            elif self.temp_bool:
                ui.notify(
                    "当前处于需求审核界面，概述内容锁定不可编辑!",
                    type="info",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
                return False
            else:
                ui.notify(
                    "当前用户无该项编辑权限，请联系管理员申请!",
                    type="info",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
                return False

        # 处理主按钮的点击事件
        def _handle_main_button_click(self):
            # 如果用户具有编辑权限
            if self._edit_permission_judge():
                # 根据处理类型，设置不同的交互逻辑
                if self.processing_type == "text":
                    # 设置文本chip的弹窗格式
                    self._setup_text_chip_dialog()
                elif self.processing_type == "test":
                    # 设置文件类chip的弹窗格式
                    self._setup_test_chip_dialog()
                else:
                    # 设置文件类chip的弹窗格式
                    self._setup_file_notes_dialog()

    # 创建一个图片上传组件，包括一个上传按钮和上传好的图片缩略图
    def get_img_group(button_name="上传", input_any_suffix="/*", parents_h=9):
        with ui.row().classes(f"h-{str(parents_h)}").classes("p-0 -space-x-4"):
            ButtonUploader(
                on_upload=handle_upload,
                label=button_name,
                input_any_suffix=input_any_suffix,
                classes_str="h-full",
                parents_h=parents_h,
            )

    # 文件上传后的处理函数
    async def handle_upload(e: UploadEventArguments, parents_h):
        try:
            hash_obj = hashlib.md5()
            # new_file_hash = ""
            # 使用 os.path.splitext 来更稳健地分离文件名和后缀
            file_name, file_suffix = os.path.splitext(e.file.name)
            # 移除前导的点
            file_suffix = file_suffix.lstrip(".")

            # 1. 一次性将文件内容完整读入内存中的 bytes 对象
            #    无论 e.file 是 SmallFileUpload 还是 FileUpload，.read() 都是支持的。
            file_content = await e.file.read()
            # 2. 使用 io.BytesIO 将内存中的 bytes 数据包装成一个标准的文件对象
            #    这个 file_like_object 的行为与真实文件完全一致，始终支持 seek()。
            file_content_object = io.BytesIO(file_content)

            # 计算文件哈希值
            file_content_object.seek(0)  # <--- 重要：将文件指针重置到开头
            while chunk := file_content_object.read(4096):  # 分块读取，每块 4096 字节
                hash_obj.update(chunk)
            # 返回哈希值的十六进制字符串
            new_file_hash = hash_obj.hexdigest()
            # 拼接带哈希值的文件名
            file_name_hash = f"{file_name}.{new_file_hash}.{file_suffix}"
            # 拼接带哈希值文件名的文件服务器存放路径
            new_file_path = os.path.join(UPLOADS_DIR, file_name_hash)
            if not os.path.isfile(new_file_path):
                # 保存上传的文件
                file_content_object.seek(0)  # <--- 重要：再次将文件指针重置到开头以进行写入
                with open(new_file_path, "wb") as f:
                    while chunk := file_content_object.read(4096):  # <--- 重要：循环读取和写入
                        f.write(chunk)
                # ui.notify(f"文件 {e.file.name} 已上传并保存到 {file_path}")

            # 将文件路径映射为可访问的 URL
            url_path = f"{UPLOAD_URL_DIR}/{file_name_hash}"
            # print(new_file_path, url_path)
            app.add_static_file(local_file=new_file_path, url_path=url_path)
            if (
                file_name_hash in app.storage.client["files"]
                and file_name_hash not in app.storage.client["deleted_files"]
            ):
                print("文件已存在")
                ui.notify(
                    f"文件已存在: {str(e.file.name)}",
                    type="warning",
                    position="bottom",
                    timeout=1000,
                    progress=True,
                    close_button="✖",
                )
            else:
                app.storage.client["files"].append(file_name_hash)
                app.storage.client["file_counter"] += 1
                file_lab = str(app.storage.client["file_counter"])
                if file_name_hash in app.storage.client["deleted_files"]:
                    app.storage.client["deleted_files"].remove(file_name_hash)

                # 实例化缩略图对象
                # 从 user storage 中获取当前活跃的 question_column
                # 而不是使用闭包捕获的旧变量
                current_img_row = app.storage.client["page_elements"].get("img_row")
                with current_img_row:
                    file_thumbnail = FileThumbnail(url_path, e.file.content_type, e.file.name, file_lab, parents_h)
                    # 将文件缩略图实例存入字典
                    app.storage.client["file_thumbnail_dic"][file_thumbnail.file_index] = {
                        "file_obj": file_thumbnail,
                        "file_information": {
                            "file_del_bool": False,
                            "file_name": file_name,
                            "file_url": url_path,
                            "file_name_hash": file_name_hash,
                            "file_name_suffix": e.file.name,
                            "file_type": e.file.content_type,
                            "file_lab": file_lab,
                            "parents_h": parents_h,
                        },
                    }

                    # 显示缩略图
                    file_thumbnail.thumbnail
        except Exception as ex:
            print(f"上传处理失败: {ex}")  # 在服务器端打印错误详情
            ui.notify(
                f"上传文件 '{e.file.name}' 失败: {str(ex)}",
                type="negative",
                position="bottom",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 引用按钮上删除按钮点击响应函数
    def del_ref_button(ref, ref_row, question_k, question):
        # 删除数字引用按钮自己
        ref.delete()
        # 在数字引用于问题字典里，找到对应的引用数字键，删除一个里面记存的对应问题
        if ref.text in app.storage.client["ref_question_dic"].keys():
            app.storage.client["ref_question_dic"][ref.text].remove([question_k, question])
        # 在当前确认项引用行字典里，减掉一个对应的数字引用记录
        if app.storage.client["config_data"]["data"][question_k]["ref_out"]:
            app.storage.client["config_data"]["data"][question_k]["ref_out"].remove(ref.text)

        # 删除该数字按钮同级元素上面的“X”按钮
        for ref_lab in ref_row.default_slot.children:
            for lab in ref_lab.default_slot.children:
                lab.delete()

    # 为引用数字图标加“X”号删除按钮
    def add_del_lab(ref_row, question_k, question):
        for ref_button in ref_row.default_slot.children:
            with ref_button:
                (
                    ui.button(on_click=lambda e, ref=ref_button: del_ref_button(ref, ref_row, question_k, question))
                    .classes("absolute -bottom-2 -right-1 m-0 p-0 q-py-1 bg-red-8 text-white ")
                    .props('round padding="0px 0px" icon="close"')
                    .style("font-size: 8px;")
                    .on("click", js_handler="(e) => {e.stopPropagation()}")
                )

    # 缩略图加号激活添加函数
    def add_activ_ref(ref_row, question_k, question):
        for k, v in app.storage.client["file_thumbnail_dic"].items():
            # 防止重复添加加号激活按键
            if not v["file_obj"].add_lab_bool:
                v["file_obj"].add_ref_lab(ref_row, k, question_k, question)
                v["file_obj"].add_lab_bool = True

    # 缩略图加号删除函数
    def delete_activ_ref():
        for v in app.storage.client["file_thumbnail_dic"].values():
            # 防止重复添加加号激活按键
            if v["file_obj"].add_lab_bool:
                v["file_obj"].ref_lab.delete()
                v["file_obj"].add_lab_bool = False

    # 添加数字引用按钮函数
    def add_ref_button(thumbnail_obj, ref_row, question_k, question, add_bool):
        k = thumbnail_obj.file_index
        # 在引用行里添加于缩略图编号一致的数字引用按钮
        with ref_row:
            ui.button(k, on_click=lambda: thumbnail_obj.handle_index_click()).classes(
                "m-0 text-white bg-brown-6"
            ).props('round padding="0px 6px"').style("font-size: 11px;")
        if add_bool:
            # 如果该数字已经在数字引用于问题字典里存在
            if k in app.storage.client["ref_question_dic"].keys():
                # 在相应数字键的值列表里添加添加该数字引用的元素的问题内容
                app.storage.client["ref_question_dic"][k].append([question_k, question])
            else:
                # 在数字引用于问题字典里新建数字键并录入第一个引用该数字的问题内容
                app.storage.client["ref_question_dic"][k] = [
                    [question_k, question],
                ]
            # 在当前确认项引用行字典里，增加一个对应的数字引用记录
            if app.storage.client["config_data"]["data"][question_k]["ref_out"]:
                app.storage.client["config_data"]["data"][question_k]["ref_out"].append(k)
            else:
                app.storage.client["config_data"]["data"][question_k]["ref_out"] = [
                    k,
                ]
            # 删除同级元素的激活按钮
            delete_activ_ref()

    # 激活条件逻辑文本处理
    def logic_out(k, cond_lgoic_str):
        # 初始化节点激活判断，默认节点不激活
        logic_out_bool = False
        # 设定多条件逻辑分隔字符串列表，如："4any['硬件'] and 17==True"
        logic_delimiters = ["and", "or"]
        # 设定条件逻辑分隔字符串列表
        cond_delimiters = ["any", "all", "==", "!="]

        # 构造正则表达式，escape对字符串中的特殊字符进行转义成普通字符处理
        # 多条件逻辑分隔字符串正则表达式，分隔符有括号包裹起来
        logic_pattern = "|".join(f"({re.escape(delimiter)})" for delimiter in logic_delimiters)
        # 条件逻辑分隔字符串正则表达式
        cond_pattern = "|".join(map(re.escape, cond_delimiters))

        # 使用正则表达式分割字符串
        logic_result = re.split(logic_pattern, cond_lgoic_str)
        # 过滤掉空字符串
        logic_result = [s for s in logic_result if s]
        # 分离分割后的子字符串和分隔符
        # 分割开的各个条件，如：4any['硬件'] 和 17==True
        elements = [s for s in logic_result if s not in logic_delimiters]
        # 用于分隔的逻辑分割字符串，如：and
        separators = [s for s in logic_result if s in logic_delimiters]

        bool_list = []
        cond_id_list = []
        # 遍历分割出来的单个逻辑语句块，4any['硬件'] 和 17==True
        for p in elements:
            # 用条件分隔符分割条件逻辑字符串,如：4 和 ['硬件']
            cond_result = re.split(cond_pattern, p)
            # 将整条逻辑语句里的涉及的前置条件节点序号提取出来
            cond_id = cond_result[0].replace("not", "").strip()
            cond_id_list.append(cond_id)
        # 先排查用户是否存在未选择的节点，如有则不满足处理条件，退出
        # 遍历该节点条件里涉及的条件序号
        for c_id in cond_id_list:
            # print(f"处理节点序号{k}的逻辑")
            op_user_out = dict(app.storage.client["config_data"]["data"][c_id]["user_must_out"])
            # 如果依赖的节点还没有用户做选填操作
            if op_user_out == {}:
                # 先结束判断，返回该节点激活条件不够
                return logic_out_bool
        # 如果该节点的前提条件都有输出了，再详细判断
        # 复杂逻辑，处理本次条件节点序号用户输出在条件逻辑里出现的地方的运算情况
        # 遍历分割出来的单个逻辑语句块，4any['硬件'] 和 17==True
        for p in elements:
            # 将条件语句按照条件逻辑字符串进行切分
            # 4 和 ['硬件']
            cond_result = re.split(cond_pattern, p)
            # 遍历涉及的条件序号
            for c_id in cond_id_list:
                # 跳过条件序号与条件语句不匹配的
                if c_id != cond_result[0].strip():
                    continue
                # 如果条件序号与条件语句匹配
                # 获取条件节点的用户选填结果
                op_user_out = dict(app.storage.client["config_data"]["data"][c_id]["user_must_out"])
                op_user_out_list = []
                if len(op_user_out.keys()) > 1:
                    for op_key, op_value in op_user_out.items():
                        if op_value:
                            for op in app.storage.client["config_data"]["data"][c_id]["options"]:
                                if op["option_content"] == op_key:
                                    op_user_out_list.append(op["option_out"])
                else:
                    op_user_out_list = list(op_user_out.values())
                # 对比用户多选项列表与条件列表之间是否存在相同元素
                # isinstance判断变量是否为某个数据类型
                if "any" in p:  # and (isinstance(op_user_out, list) or op_user_out == [])
                    # ast.literal_eval 用于安全地解析和评估字符串中的字面量表达式
                    # ['硬件']
                    condition = ast.literal_eval(cond_result[1].strip())
                    # 判断用户选择项列表元素是否有任意一个在条件项列表里，并插入到判断结果列表里
                    if "not" in p:
                        # 看当前激活条件列表里，全部都跟条件节点用户输出匹配不上，返回false
                        bool_list.append(not any(item in condition for item in op_user_out_list))
                    else:
                        # 看当前激活条件列表里，只要有一个跟条件节点用户输出匹配上，返true
                        bool_list.append(any(item in condition for item in op_user_out_list))
                # 对比用户多选项列表是否是条件列表的子集
                elif "all" in p:  #  and (isinstance(op_user_out, list) or op_user_out == [])
                    # ['硬件']
                    condition = ast.literal_eval(cond_result[1].strip())
                    op_user_set = set(op_user_out_list)
                    cond_set = set(condition)
                    # 判断用户选择项集合是否为条件项集合的子集，并插入到判断结果列表里
                    if "not" in p:
                        bool_list.append(not op_user_set.issubset(cond_set))
                    else:
                        bool_list.append(op_user_set.issubset(cond_set))
                # 对比用户单选项是否与条件一致
                elif "==" in p:  #  and (isinstance(op_user_out, list) or op_user_out == [])
                    bool_list.append(op_user_out_list[0] == cond_result[1].strip() if op_user_out_list != [] else False)
                # 对比用户单选项是否与条件不一致
                elif "!=" in p:  #  and (isinstance(op_user_out, list) or op_user_out == [])
                    bool_list.append(op_user_out_list[0] != cond_result[1].strip() if op_user_out_list != [] else False)
                else:
                    print(f"节点{k}激活条件逻辑不符合语法")
                    continue

        result_str = "".join(f"{x} {y} " for x, y in itertools.zip_longest(bool_list, separators, fillvalue=""))
        logic_out_bool = eval(result_str)
        # print(f"节点{k}处理完毕，返回：{result_str}，判定为：{logic_out_bool}")
        return logic_out_bool

    # 问题列表展示函数
    def set_question_list(index):
        # 清空已填需求项数目记录
        app.storage.client["req_activ_num"] = 0
        # 清空未填需求项数目记录
        app.storage.client["req_not_activ_num"] = 0
        # 获取问题表元素
        current_question_table = app.storage.client["page_elements"].get("question_table")
        # 清空之前的 UI 元素
        current_question_table.clear()

        app.storage.client["buttons_dic"].clear()
        data = app.storage.client["config_data"]["data"]
        with current_question_table:
            button_num = 0
            for k, v in data.items():
                # 如果是 无条件 需要创立的就直接创建
                if v["condition"] == "无条件":
                    button_num += 1
                    with (
                        ui.button(
                            # 将按钮序号和问题内容作为按键文字显示
                            f"{button_num}. {v['guide_content']}",
                            on_click=lambda e, k=k: question_display(e, k),
                        )
                        .classes("text-sm w-full")
                        .props('align="left" disabled flat color="grey-10"') as button
                    ):
                        ui.badge(
                            f"{v['option_group_id']}组/ID{v['node_id']}", color="#22222222", text_color="white"
                        ).props("floating transparent").classes("my-1 mr-1 px-[2px] py-[1px] text-[10px]/[10px]")
                    # 如果该按钮对应的确认项有用户输出内容，则启用按钮
                    if v["user_must_out"]:
                        if "单选" in v["answer_type"] and v["user_must_out"]["value"]:
                            button.classes("bg-green-1").props(remove="disabled")
                        elif "多选" in v["answer_type"] and any(v["user_must_out"].values()):
                            button.classes("bg-green-1").props(remove="disabled")
                        elif v["answer_type"] in ["正整数", "单行文本", "多行文本"] and all(
                            v["user_must_out"].values()
                        ):
                            button.classes("bg-green-1").props(remove="disabled")
                    # 将新按钮加入到按钮字典里
                    app.storage.client["buttons_dic"][k] = button
                # 处理遇到节点序号条件为空的异常
                elif v["condition"] == "":
                    print(f"配置表节点序号为{k}的配置项激活条件为空，无法处理！")
                # 逻辑处理
                else:
                    # cond_id_list = v["condition_id"].split("&")
                    # 获取节点激活条件内容字符串
                    cond_lgoic_str = v["condition"].strip()
                    # 调用节点激活条件逻辑处理函数处理逻辑字符串，结果为真则按钮激活创建
                    if logic_out(k, cond_lgoic_str):
                        button_num += 1
                        with (
                            ui.button(
                                # 将按钮序号和问题内容作为按键文字显示
                                f"{button_num}. {v['guide_content']}",
                                on_click=lambda e, k=k: question_display(e, k),
                            )
                            .classes("text-sm w-full")
                            .props('align="left" disabled flat color="grey-10"') as button
                        ):
                            ui.badge(
                                f"{v['option_group_id']}组/ID{v['node_id']}", color="#22222222", text_color="white"
                            ).props("floating transparent").classes("my-1 mr-1  px-[2px] py-[1px] text-[10px]/[10px]")
                        # 如果该按钮对应的确认项有用户输出内容，则启用按钮
                        if v["user_must_out"]:
                            if "单选" in v["answer_type"] and v["user_must_out"]["value"]:
                                button.classes("bg-green-1").props(remove="disabled")
                            elif "多选" in v["answer_type"] and any(v["user_must_out"].values()):
                                button.classes("bg-green-1").props(remove="disabled")
                            elif v["answer_type"] in ["正整数", "单行文本", "多行文本"] and all(
                                v["user_must_out"].values()
                            ):
                                button.classes("bg-green-1").props(remove="disabled")

                        app.storage.client["buttons_dic"][k] = button
                    else:
                        # 不能激活的节点，即使前面曾经激活过并选填过内容，也要清理掉
                        app.storage.client["config_data"]["data"][k]["user_must_out"] = {}
                        app.storage.client["config_data"]["data"][k]["option_tolerance_out"] = {}
                        # 清空缩略图引用记录字典里，与当前失效问题有关的记录
                        if app.storage.client["config_data"]["data"][k]["ref_out"]:
                            for ref_num, que_li in app.storage.client["ref_question_dic"].items():
                                for q in que_li:
                                    if q[0] == k:
                                        app.storage.client["ref_question_dic"][ref_num].remove([q[0], q[1]])
                        app.storage.client["config_data"]["data"][k]["ref_out"] = []
                # 将当前按钮聚焦到视图中显示
                if len(app.storage.client["buttons_dic"].values()) > index:
                    ui.run_javascript(
                        f'document.getElementById("{list(app.storage.client["buttons_dic"].values())[index].html_id}").scrollIntoView({{ behavior: "smooth" }})'
                    )
        # 更新需求问题项总数目
        app.storage.client["req_com_num"] = len(app.storage.client["buttons_dic"])
        app.storage.client["page_elements"]["circular_activ"].props["max"] = app.storage.client["req_com_num"]
        app.storage.client["page_elements"]["circular_not_activ"].props["max"] = app.storage.client["req_com_num"]
        app.storage.client["page_elements"]["circular_activ"].update()
        app.storage.client["page_elements"]["circular_not_activ"].update()
        # 只有当所有激活确认项的必填项都非空，才意味着全部填完
        button_activ_li = []
        for b_k in app.storage.client["buttons_dic"].keys():
            # 必填项存在键值对
            if data[b_k]["user_must_out"]:
                # 单选类型，必填项的值为空，意味着该项确实有有效选填内容
                if "单选" in data[b_k]["answer_type"] and data[b_k]["user_must_out"]["value"]:
                    button_activ_li.append(True)
                    app.storage.client["req_activ_num"] += 1
                # 多选类型，存在至少一个True，意味着该项确实有有效选填内容
                elif "多选" in data[b_k]["answer_type"] and any(data[b_k]["user_must_out"].values()):
                    button_activ_li.append(True)
                    app.storage.client["req_activ_num"] += 1
                # 文本输入类型，所有必填输入框均非空，意味着该项确实有完整的有效内容
                elif data[b_k]["answer_type"] in ["正整数", "单行文本", "多行文本"] and all(
                    data[b_k]["user_must_out"].values()
                ):
                    button_activ_li.append(True)
                    app.storage.client["req_activ_num"] += 1
                # 其它情况判断该项没有完成选填
                else:
                    app.storage.client["req_not_activ_num"] += 1
                    button_activ_li.append(False)
            # 连键值对都没有，意味着该项都没有展示过，判定为没有完成选填
            else:
                app.storage.client["req_not_activ_num"] += 1
                button_activ_li.append(False)

        # 全部需求项均有有效选填
        if all(button_activ_li):
            # 更改录入状态
            app.storage.client["config_data"]["entry_status"] = True
        # 否则更新录入状态为False
        else:
            app.storage.client["config_data"]["entry_status"] = False

    # 问题展示页面按钮处理函数
    def get_option(k, options_type, next):
        # 单选，包括单选项与下拉单选
        radio_bool = False
        # 多选，包括多选项与下拉多选
        checkboxe_bool = False
        # dropdown_bool = False
        input_bool = False
        # 获取当前问题的配置表键
        index = find_key_position(app.storage.client["buttons_dic"], k)

        # 获取可能的输出值
        # out_keys = list(app.storage.client["config_data"]["data"][k]["user_must_out"].keys())
        out_value = list(app.storage.client["config_data"]["data"][k]["user_must_out"].values())
        # 输入框没有出来，则字典为{}，构成的列表则为[]
        out_tolerance_value = list(app.storage.client["config_data"]["data"][k]["option_tolerance_out"].values())

        # 单选或下拉单选，用户没选择键值对为："value": None; 用户选择了则为："value": "设定值"
        # 本次处理的是单选，且内容非空，说明是单选项且做了选择
        if (
            options_type in ["单选", "下拉单选"]
            and app.storage.client["config_data"]["data"][k]["user_must_out"]["value"] is not None
        ):
            radio_bool = True

        # 多选，且用户做出勾选了其中某个选项
        elif options_type == "多选" and True in out_value:
            checkboxe_bool = True

        # 本次处理的是输入框
        elif (
            options_type in ["正整数", "多行文本", "单行文本"]
            and all(v.strip() != "" for v in out_value)
            and all(w.strip() != "" for w in out_tolerance_value)
        ):
            if (
                options_type == "正整数" and all(v.isdigit() for v in out_value) and all(int(v) != 0 for v in out_value)
            ) or (options_type in ["单行文本", "多行文本"]):
                input_bool = True

        # 以上必填项没有任意一项有填写则弹出提醒，禁止进入下一道确认项，但允许返回
        if not (radio_bool or checkboxe_bool or input_bool) and next == 1:
            ui.notify(
                "请选填",
                type="warning",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            return
        # 禁止从第一道倒退回最后一道确认项
        if index == 0 and next == -1:
            ui.notify(
                "这已经是第一个问题了",
                type="warning",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            return

        index += next
        # 更新问题列表
        set_question_list(index)

        # 判断是否为最后一道确认项
        if index == len(app.storage.client["buttons_dic"].keys()):
            ui.notify(
                "这是最后一个问题，检查所有问题都选填后即可提交需求。",
                type="info",
                position="bottom",
                timeout=3000,
                progress=True,
                close_button="✖",
            )
        # 不是最后一道确认项
        else:
            new_k = list(app.storage.client["buttons_dic"].keys())[index]

            question_display(None, new_k)  # 触发点击事件

    # 问题内容展示函数
    def question_display(event, k):
        app.storage.client["current_question_num"] = k
        # 获取当前问题的配置表键
        index = find_key_position(app.storage.client["buttons_dic"], k)
        # 更新问题列表,重复更新是为了让所有按钮恢复应该的禁用状态
        set_question_list(index)
        # 目标确认项对应的列表按钮更新为可点击状态
        app.storage.client["buttons_dic"][k].classes("bg-amber-1").props(remove="disabled")  # 启用按钮
        # --- 修改开始 ---
        # 从 user storage 中获取当前活跃的 question_column
        # 而不是使用闭包捕获的旧变量
        current_question_column = app.storage.client["page_elements"].get("question_column")
        if not current_question_column:
            ui.notify("无法找到问题显示区域，请刷新页面重试。", type="negative")
            return
        # --- 修改结束 ---
        question = app.storage.client["config_data"]["data"][k]["guide_content"]
        option_hint = app.storage.client["config_data"]["data"][k]["option_hint"]
        options_type = app.storage.client["config_data"]["data"][k]["answer_type"]
        options_list = app.storage.client["config_data"]["data"][k]["options"]
        ref_config_bool = True if app.storage.client["config_data"]["data"][k]["ref_config"] == "True" else False
        # user_out_list = []
        # 清空元素的子元素
        current_question_column.clear()
        # print(f"处理节点序号{k}的显示:{app.storage.client['config_data']['data'][k]['user_must_out']}")
        with current_question_column:
            ui.label(question).classes("text-2xl text-black")
            ui.label(option_hint).classes("text-base text-grey-8 max-w-full")

            with ui.column().classes("m-0 gap-8 w-full items-center justify-start overflow-auto"):
                if options_type == "单选":
                    radio_dic = {}
                    for op_dic in options_list:
                        radio_dic[op_dic["option_out"]] = op_dic["option_content"]
                    # 创建单选按钮 options:	a list ['value1', ...] or dictionary {'value1':'label1', ...} specifying the options
                    radio = ui.radio(radio_dic).classes("").props("inline")
                    radio.bind_value(app.storage.client["config_data"]["data"][k]["user_must_out"], "value")

                elif options_type == "多选":
                    with ui.row().classes("items-start justify-center w-full"):
                        for op_dic in options_list:
                            # 创建复选框
                            checkbox = ui.checkbox(op_dic["option_content"]).classes("")
                            # 绑定复选框的值到列表
                            checkbox.bind_value(
                                app.storage.client["config_data"]["data"][k]["user_must_out"], op_dic["option_content"]
                            )

                elif options_type == "下拉单选":
                    dropdown_dic = {}
                    for op_dic in options_list:
                        dropdown_dic[op_dic["option_out"]] = op_dic["option_content"]
                    # 创建下拉选择框
                    dropdown = ui.select(dropdown_dic).classes("w-1/6 text-base")
                    # dropdown.bind_value(selected_dropdown_dic)
                    dropdown.bind_value(app.storage.client["config_data"]["data"][k]["user_must_out"], "value")

                elif options_type in ["正整数", "单行文本", "多行文本"]:
                    # 根据依据获取用户在输入框填入的数量，输入项有名称则名称为健，没有则用数字字符
                    input_num_accor = app.storage.client["config_data"]["data"][k]["input_num_accor"]
                    input_num = (
                        1
                        if input_num_accor == ""
                        else int(
                            float(app.storage.client["config_data"]["data"][input_num_accor]["user_must_out"]["1"])
                        )
                    )

                    # 根据依据获取用户在输入框填入的输入项名称
                    input_name_accor = app.storage.client["config_data"]["data"][k]["input_name_accor"]
                    if input_name_accor == "":
                        input_name_storage_dic = dict(app.storage.client["config_data"]["data"][k]["user_must_out"])
                    else:
                        input_name_storage_dic = dict(
                            app.storage.client["config_data"]["data"][input_name_accor]["user_must_out"]
                        )

                    # 如果用户修改输入项数量，且小于以前的，要清除掉以前多出来的已经生成过的多余键值对
                    if input_num < len(input_name_storage_dic.keys()):
                        app.storage.client["config_data"]["data"][k]["user_must_out"] = dict(
                            islice(input_name_storage_dic.items(), input_num)  # islice高效获取字典前N个键值对
                        )
                    input_name_dic = {} if input_name_accor == "" else input_name_storage_dic
                    # 获取公差要求
                    input_tolerance_bool = app.storage.client["config_data"]["data"][k]["input_tolerance"]
                    # 该项的项名称不需要依据，给项的健默认按照数字字符进行设置
                    if input_name_dic == {}:
                        for i in range(input_num):
                            input_name_dic[str(i + 1)] = str(i + 1)
                    # 获取可能的已有用户输入内容
                    with ui.column().classes("min-w-1/4 -space-y-2"):
                        for n in range(input_num):
                            with ui.row().classes("justify-center flex-nowrap items-stretch w-full"):
                                # 可能是数字123也可能是前置依赖的客户输出识别字符串
                                input_label_key = list(input_name_dic.values())[n]

                                label_1 = "值"
                                label_2 = ""
                                if input_tolerance_bool == "正负":
                                    label_1 = "典型值"
                                    label_2 = "正负公差范围"
                                elif input_tolerance_bool == "范围":
                                    label_1 = "下限值"
                                    label_2 = "上限值"
                                elif input_tolerance_bool == "下限":
                                    label_1 = "下限值"
                                elif input_tolerance_bool == "上限":
                                    label_1 = "上限值"

                                # 编辑配置好输入框标签内容
                                if input_label_key.isdigit():
                                    input_label = f"项{input_label_key}的{label_1}:"
                                    input_tolerance_label = f"项{input_label_key}的{label_2}:"
                                else:
                                    input_label = f"{input_label_key}的{label_1}:"
                                    input_tolerance_label = f"{input_label_key}的{label_2}:"

                                # 处理正整数输入框
                                if options_type == "正整数":
                                    input_field = (
                                        ui.input(
                                            label=input_label,
                                            placeholder="",
                                            validation={"必须是整数": lambda value: value.isdigit()},
                                        )
                                        .props("outlined stack-label")
                                        .classes("text-[14px]/[16px] w-full")
                                    )
                                    input_field.bind_value(
                                        app.storage.client["config_data"]["data"][k]["user_must_out"], input_label_key
                                    )
                                    if input_tolerance_bool in ["正负", "范围"]:
                                        input_tolerance = (
                                            ui.input(
                                                label=input_tolerance_label,
                                                placeholder="",
                                                validation={"不能空白": lambda value: value.strip() != ""},
                                            )
                                            .props("outlined stack-label")
                                            .classes("text-[14px]/[16px] w-full")
                                        )
                                        input_tolerance.bind_value(
                                            app.storage.client["config_data"]["data"][k]["option_tolerance_out"],
                                            input_label_key,
                                        )
                                # 处理单行文本输入框
                                elif options_type == "单行文本":
                                    input_field = (
                                        ui.input(
                                            label=input_label,
                                            placeholder="",
                                            validation={"不能空白": lambda value: value.strip() != ""},
                                        )
                                        .props("outlined stack-label")
                                        .classes("text-[14px]/[16px] w-full")
                                    )
                                    input_field.bind_value(
                                        app.storage.client["config_data"]["data"][k]["user_must_out"], input_label_key
                                    )
                                    if input_tolerance_bool in ["正负", "范围"]:
                                        input_tolerance = (
                                            ui.input(
                                                label=input_tolerance_label,
                                                placeholder="",
                                                validation={"不能空白": lambda value: value.strip() != ""},
                                            )
                                            .props("outlined stack-label")
                                            .classes("w-full text-[14px]/[16px] w-full")
                                        )
                                        input_tolerance.bind_value(
                                            app.storage.client["config_data"]["data"][k]["option_tolerance_out"],
                                            input_label_key,
                                        )
                                # 处理多行文本输入框，多行文本不处理公差范围.
                                elif options_type == "多行文本":
                                    input_field = (
                                        ui.textarea(
                                            label=input_label,
                                            placeholder="",
                                            validation={"不能空白": lambda value: value.strip() != ""},
                                        )
                                        .props("outlined stack-label autogrow")
                                        .classes("w-full text-[14px]/[16px] w-full")
                                    )
                                    input_field.bind_value(
                                        app.storage.client["config_data"]["data"][k]["user_must_out"], input_label_key
                                    )
                # 处理需要插入引用确认项
                if ref_config_bool:
                    with ui.row().classes("gap-1 w-full justify-center"):
                        with ui.column().classes(
                            "w-1/4 h-fit -space-y-5 border-2 border-solid border-Gray-500 rounded-md"
                        ):
                            ui.label("引用：").classes("p-1 text-sm text-gray-500")
                            ref_row = ui.row().classes("space-x-0 p-2")

                            if app.storage.client["config_data"]["data"][k]["ref_out"]:
                                for t_lab in app.storage.client["config_data"]["data"][k]["ref_out"]:
                                    add_ref_button(
                                        app.storage.client["file_thumbnail_dic"][t_lab]["file_obj"],
                                        ref_row,
                                        k,
                                        question,
                                        False,
                                    )
                        ui.button(
                            on_click=lambda ref_row=ref_row, question_k=k, question=question: add_activ_ref(
                                ref_row, question_k, question
                            )
                        ).props('icon-right="add_link"').classes("h-full p-2")
                        ui.button(
                            on_click=lambda ref_row=ref_row, question_k=k, question=question: add_del_lab(
                                ref_row, question_k, question
                            )
                        ).props('icon-right="link_off"').props().classes("h-full p-2 bg-blue-grey-8")

            # 确认项“确认”与“返回”按钮
            # with ui.button_group().props("push").classes("absolute bottom-0 right-2"):
            ui.button(
                # "上一个",
                icon="arrow_back_ios",
                color="amber-8",
                on_click=lambda kk=k: get_option(kk, options_type, -1),
            ).props("flat").classes("absolute top-1/2 left-2 -translate-y-1/2 h-1/2 text-xl")
            ui.button(
                # "下一个",
                icon="arrow_forward_ios",
                color="green-8",
                on_click=lambda kk=k: get_option(kk, options_type, 1),
            ).props("flat").classes("absolute top-1/2 right-2 -translate-y-1/2 h-1/2 text-xl")

    # 刷新需求录入界面文件缩略图显示区域函数
    def req_thumbnail_display():
        # 从 user storage 中获取当前活跃的 img_row
        # 而不是使用闭包捕获的旧变量
        current_img_row = app.storage.client["page_elements"].get("img_row")
        if not current_img_row:
            ui.notify("无法找到文件缩略图显示区域，请刷新页面重试。", type="negative")
            return
        current_img_row.clear()
        with current_img_row:
            if app.storage.client["file_thumbnail_dic"]:
                for file_data in app.storage.client["file_thumbnail_dic"].values():
                    if not file_data["file_information"]["file_del_bool"]:
                        file_data["file_obj"].get_thumbnail()

    def update_new_data_in_place(old_data: dict, new_data: dict) -> tuple:
        """
        根据 old_data 的内容，就地更新 new_data 字典。

        函数逻辑:
        1. 遍历 new_data['file_dic'] 中的每个文件条目。
        2. 如果文件在 old_data 中存在冲突（key 相同但内容不同），则为其分配一个新 ID。
        3. 新 ID 基于 old_data['file_counter'] 的递增值。
        4. **直接修改 new_data['file_dic']**：
        - 删除旧的数字键条目。
        - 以新 ID 为键，添加更新后的文件条目。
        - **更新文件条目字典内部的 'file_lab' 键值为新 ID**。
        5. 记录所有 ID 变更的映射关系 (old_key -> new_key)。
        6. **直接修改 new_data['data']**：使用映射关系更新 'ref_out' 列表中的文件引用。
        7. 函数返回修改后的 new_data 字典。

        Args:
            old_data (dict): 用于比对和提供文件计数器起点的旧字典。
            new_data (dict): 将被就地修改的新字典。

        Returns:
            tuple: 经过就地修改后的 new_data 字典和迭代后的file_counter。
        """
        # 从旧字典获取文件计数器起点和文件列表
        file_counter = old_data.get("file_counter", 0)
        old_file_dic = old_data.get("file_dic", {})

        # 直接操作新字典的文件列表
        new_file_dic = new_data.get("file_dic", {})
        if not new_file_dic:
            return (new_data, file_counter)  # 如果没有文件，则无需处理

        key_map = {}  # 用于存储旧键到新键的映射
        temp_file_dic = copy.deepcopy(new_file_dic)

        # 遍历键的列表副本，因为我们将在循环中修改字典本身
        for original_key in list(temp_file_dic.keys()):
            file_info = temp_file_dic[original_key]

            is_identical = False
            # 检查 key 是否存在于旧字典中且内容一致
            if original_key in old_file_dic:
                _temp_new = copy.deepcopy(file_info)
                del _temp_new["file_del_bool"]
                _temp_old = copy.deepcopy(old_file_dic[original_key])
                del _temp_old["file_del_bool"]
                if _temp_new == _temp_old:
                    is_identical = True

            if is_identical:
                # 内容一致，无需修改，键映射保持不变
                key_map[original_key] = original_key
                continue

            # 如果 key 在旧字典中不存在，或者存在但内容不一致（冲突），则分配新 key
            file_counter += 1
            new_key = str(file_counter)

            # 记录键的映射关系
            key_map[original_key] = new_key

            # 核心步骤：就地修改 new_data['file_dic']
            # 1. 首先更新文件条目字典内部的 "file_lab"
            file_info["file_lab"] = new_key

            # 2. 然后从字典中移除旧键的条目
            entry_to_move = temp_file_dic.pop(original_key)
            if int(original_key) <= old_data.get("file_counter", 0):
                del new_file_dic[original_key]
            # 3. 使用新键重新插入该条目
            new_file_dic[new_key] = entry_to_move

        # 就地更新 new_data['data'] 部分中的文件引用
        data_section = new_data.get("data", {})
        for node_content in data_section.values():
            if "ref_out" in node_content and isinstance(node_content.get("ref_out"), list):
                # 使用列表推导式和 key_map 来创建更新后的引用列表
                updated_ref_out = [key_map.get(ref, ref) for ref in node_content["ref_out"]]
                node_content["ref_out"] = updated_ref_out

        return (new_data, file_counter)

    # 需求数据输出处理函数
    async def output_config_data(data, type):
        change_name = False
        # 先复制整个数据
        data_json = data
        project_name = app.storage.client["project_name"].strip()
        version = app.storage.client["version"]
        target_project_name = app.storage.client["target_project_name"].strip()
        # target_version = app.storage.client["target_version"].strip()
        original_project = app.storage.client["original_project"]
        original_version = app.storage.client["original_version"]

        if target_project_name == "":
            ui.notify(
                "提交/导出必须给项目命名！",
                type="negative",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
        else:
            file_dic = {}
            for k, v in app.storage.client["file_thumbnail_dic"].items():
                file_dic[k] = v["file_information"]
            data_json["file_dic"] = file_dic
            data_json["file_counter"] = app.storage.client["file_counter"]
            data_json["files"] = app.storage.client["files"]
            data_json["deleted_files"] = app.storage.client["deleted_files"]
            data_json["current_user"] = app.storage.user["current_user"]

            # 在没改名情况下，这两个状态是同一个项目的，改名了就不是
            review_state = ""
            if app.storage.general["wait_review"].get(project_name, {}):
                review_state = app.storage.general["wait_review"][project_name].get(version, {"state": ""})["state"]

            # 当前项目已审情况可正常更新参照项目名
            # 其它状态，比如待修改、初次提交，不动作就保持了原有数据；待审后面拦截不能导出和提交
            if review_state in ["已审", ""]:
                # 记录项目名
                data_json["project_name"] = target_project_name
                # 记录参照当前版本
                data_json["original_version"] = version
                # 项目名相当于没变，接着记录
                data_json["original_project"] = project_name
            else:
                # 记录项目名
                data_json["project_name"] = project_name
                # 记录参照当前版本
                data_json["original_version"] = original_version
                # 项目名相当于没变，接着记录
                data_json["original_project"] = original_project

            # 处理项目名的衍生记录
            # 没改名情况
            if (
                # 当前版本的参照版本为0.0版（新填 或 1.0版且没改名，改名时会将参照版本更新为当前版本）
                original_version == "0.0"
                or version != "0.0"  # 当前版本不为0.0即高版本 且 没有改名
                and project_name == target_project_name
            ):
                pass
            #  改了项目名
            else:
                change_name = True

            version_str_li = version.split(".")
            # 输出类型为导出到本地
            if type == "export":
                # 禁止待审、待修改需求导出
                if review_state not in ["已审", ""]:
                    ui.notify(
                        "需求处于未审状态，不能导出到本地！",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    return
                # 记录衍生版本
                # 如果不等，则以为这改名时将当前版本改成改后名的最高版本了，这里参照版本得用真真参照的版本
                # if original_version != version:
                #     data_json["original_version"] = original_version
                # else:
                #     data_json["original_version"] = version
                # 将文件版本的小数点位加1
                version_a_str = version_str_li[0]
                # 注意出现3.11比3.2版本浮点数小，但是实际版本更高的影响
                version_b_str = str(int(version_str_li[1]) + 1)
                new_version = f"{version_a_str}.{version_b_str}"
                app.storage.client["version"] = new_version
                data_json["version"] = new_version
                # 导出时加入或更新时间戳
                data_json["req_timestamp"] = datetime.now().isoformat()
                # 1. 将字典转换为 JSON 字符串
                json_str = json.dumps(data_json, indent=4, ensure_ascii=False)
                # 2. 生成 JavaScript 下载代码
                js_code = f"""
                    const blob = new Blob([{json.dumps(json_str)}], {{ type: 'application/json' }});
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'data.json';  // 下载文件名
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                """
                # 3. 执行 JavaScript
                ui.run_javascript(js_code)

                ui.notify(
                    f"需求已导出，版本已迭代到: V{version}",
                    type="positive",
                    position="bottom",
                    timeout=2000,
                    progress=True,
                    close_button="✖",
                )
            # 输出类型为提交到服务器
            elif type == "submit":
                if app.storage.user.get("current_role") not in ["销售", "销售总监", "admin"]:
                    ui.notify(
                        "当前用户无权限提交需求，只能导出到本地！",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    return
                if project_name.split("-")[0] != "RFTS" and project_name not in app.storage.general["project_summary"]:
                    ui.notify(
                        "非临时项目，又未正式立项，不可提交服务器，只可导出到本地！",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    return
                if project_name.split("-")[0] == "RFTS" and not validate_format_regex(project_name, r"^RFTS-\d{4}$"):
                    ui.notify(
                        "不符合临时项目号命名规则：RFTS-4位数字，不可提交服务器，只可导出到本地！",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    return
                # 如果最近一次需求配置文件还处于未审状态，本次需求还不能提交
                if review_state == "待审":
                    ui.notify(
                        "需求仍处于待审状态，不能继续提交需求！",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )
                    return
                if data_json["entry_status"]:
                    new_version = version
                    # 迭代更新版本
                    version_a = int(version_str_li[0])

                    # 查找指定路径下，含有提供项目名的文件，得到一个字典，完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
                    project_exists_file = find_files_with_prefix_and_version(REQ_DIR, target_project_name)
                    # 服务器存在该项目配置，则需要升级版本
                    if project_exists_file:
                        v_max = max([float(s) for s in project_exists_file.keys()])
                        # 当前版本比服务器最高版本低 或 由其它项目衍生过来的，均按照本项目服务器最高版本来+1保存
                        # if float(version) < v_max or change_name:
                        version_a = int(project_exists_file[str(v_max)]["v_a"])
                        # 已审状态才能升级版本, 待修改不升级

                        if review_state in ["已审", ""]:
                            new_version = f"{version_a + 1}.0"

                        try:
                            # 获取旧版最高版需求文件数据
                            old_data_path = os.path.join(REQ_DIR, project_exists_file[str(v_max)]["name"])
                            with open(old_data_path, "r", encoding="utf-8") as f:
                                # 使用 json.load() 读取文件内容并解析
                                old_data = json.load(f)

                                # 处理新需求插入文件数字可能的与旧版本需求的冲突
                                return_tuple = update_new_data_in_place(old_data, data_json)
                                data_json = return_tuple[0]
                                data_json["file_counter"] = return_tuple[1]
                        except json.JSONDecodeError:
                            print(f"错误：文件 '{json_path}' 不是有效的 JSON 格式。")
                        except Exception as e:
                            print(f"读取文件时发生其他错误：{e}")

                    # 服务器不存在该项目配置文件
                    else:
                        # 刚刚改了项目名,临时项目与正式项目均先复制参考的项目需求
                        if change_name:
                            # 定义文件路径
                            old_file_path = os.path.join(
                                REQ_DIR, f"{project_name}_需求配置_V{version.split('.')[0]}.0.json"
                            )
                            old_data_json = {}
                            try:
                                # 每次都以配置文件为准，不以服务器现有数据为准
                                # 配置更新能直接呈现，但配置减项将导致原有数据不呈现
                                with open(old_file_path, "r", encoding="utf-8") as f:
                                    # 使用 json.load() 读取文件内容并解析
                                    old_data_json = json.load(f)
                            except json.JSONDecodeError:
                                print(f"错误：文件 '{old_file_path}' 不是有效的 JSON 格式。")
                            except Exception as e:
                                print(f"读取文件时发生其他错误：{e}")
                            old_data_json["project_name"] = target_project_name
                            old_data_json["current_user"] = app.storage.user["current_user"]
                            old_data_json["original_project"] = project_name
                            old_data_json["version"] = "1.0"
                            old_data_json["original_version"] = version
                            old_data_json["req_timestamp"] = datetime.now().isoformat()
                            # 衍生复制过来的需求，默认通过审核
                            # old_data_json["review_state"] = True
                            # 将该需求版本标记到待审字典里
                            app.storage.general["wait_review"][target_project_name] = {
                                "1.0": {"state": "已审", "submitter": app.storage.user["current_user"]}
                            }

                            # 将字典转换为 JSON 字符串
                            old_json_str = json.dumps(old_data_json, indent=4, ensure_ascii=False)
                            # print(f"准备写入的 data 数据: {data}")
                            # 写入文件
                            copy_file_path = os.path.join(REQ_DIR, f"{target_project_name}_需求配置_V1.0.json")
                            try:
                                with open(copy_file_path, "w", encoding="utf-8") as f:
                                    f.write(old_json_str)
                                # 成功复制参照项目需求文件后，马上复制该项目概述内容
                                overview_data = db_storage.get_item(f"{project_name}_over_data", {})
                                await db_storage.set_item(
                                    f"{target_project_name}_over_data", copy.deepcopy(overview_data)
                                )
                                ui.notify(
                                    "复制衍生临时项目需求文件成功。",
                                    type="positive",
                                    position="bottom",
                                    timeout=2000,
                                    progress=True,
                                    close_button="✖",
                                )
                            except Exception as e:
                                print(f"复制修改衍生临时项目需求文件时发生其他错误：{e}")
                            # 更新客户端数据
                            app.storage.client["version"] = "1.0"
                            app.storage.client["project_name"] = target_project_name
                            app.storage.client["target_project_name"] = target_project_name
                            # app.storage.client["target_version"] = ""
                            app.storage.client["original_project"] = target_project_name
                            app.storage.client["original_version"] = "1.0"
                            # 复制保存好旧版本临时需求配置文件后，接着处理一次
                            await output_config_data(data, type)
                            return
                        # 排除其它项目衍生过来的情况，那种情况保持衍生的记录版本
                        else:
                            original_version = "0.0"
                            new_version = "1.0"

                    # 不管服务器有没有该项目需求配置文件
                    # app.storage.client["version"] = new_version
                    # app.storage.client["original_version"] = version
                    data_json["version"] = new_version

                    # 导出时加入或更新时间戳
                    data_json["req_timestamp"] = datetime.now().isoformat()
                    # 定义文件路径
                    file_path = os.path.join(REQ_DIR, f"{target_project_name}_需求配置_V{new_version}.json")
                    # 将字典转换为 JSON 字符串
                    json_str = json.dumps(data_json, indent=4, ensure_ascii=False)

                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(json_str)
                    # 将该需求版本标记到待审字典里
                    if not app.storage.general["wait_review"].get(target_project_name, {}):
                        app.storage.general["wait_review"][target_project_name] = {}
                    app.storage.general["wait_review"][target_project_name][new_version] = {
                        "state": "待审",
                        "submitter": app.storage.user["current_user"],
                    }

                    # 将提交该需求的用户更新为该项目负责的销售员
                    app.storage.general["project_sale"][target_project_name] = app.storage.user.get("current_user")
                    ui.notify(
                        f"需求已提交，版本已迭代到: V{new_version}",
                        type="positive",
                        position="bottom",
                        timeout=2000,
                        progress=True,
                        close_button="✖",
                    )
                    ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")
                else:
                    ui.notify(
                        "需求确认项未全部选填完毕，不能提交！",
                        type="negative",
                        position="center",
                        timeout=0,
                        progress=False,
                        close_button="✖",
                    )

    def get_select_req(select_project_name):
        if select_project_name:
            # 定义文件路径
            file_path = os.path.join(REQ_DIR, select_project_name)
            ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    # 滚动获取特定版本需求配置文件，并重新跳转页面
    def select_project_req():
        select_value = {"value": ""}
        target_project_name = app.storage.client.get("target_project_name", "")
        if target_project_name == "":
            ui.notify(
                "项目名或需求版本获取失败，无法响应！",
                type="warning",
                position="center",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            return
        req_version_dialog = app.storage.client["page_elements"].get("req_version_dialog").props("persistent")
        version_card = app.storage.client["page_elements"].get("version_card")
        version_card.clear()
        # 查找指定路径下，含有提供项目名的文件，得到一个字典，完整版本为键，值为：{"name":文件名, "v_a":版本号整数部分, "v_b":版本号小数部分}
        project_exists_file = find_files_with_prefix_and_version(REQ_DIR, target_project_name)
        if project_exists_file:
            # current_version = float(current_version)
            version_li = list([float(s) for s in project_exists_file.keys()])
            version_li.sort()
            with version_card:
                with ui.column().classes("w-full"):
                    ui.label("选择切换的需求版本：").classes("text-xl font-bold")
                    ui.label("确定切换将覆盖当前编辑的需求内容").classes("text-base text-red")
                    ui.radio(version_li, value=version_li[-1]).props("inline").bind_value_to(select_value, "value")
                    with ui.row().classes("w-full justify-end"):
                        ui.button(
                            "确定",
                            on_click=lambda: get_select_req(project_exists_file[str(select_value["value"])]["name"]),
                        ).on("click", lambda: req_version_dialog.close())
                        ui.button("取消", on_click=lambda: req_version_dialog.close())
            req_version_dialog.open()
        else:
            ui.notify(
                "该项目当前没有其它需求配置！",
                type="info",
                position="bottom",
                timeout=1000,
                progress=True,
                close_button="✖",
            )

    # 需求显示界面框架构造函数
    def requirement_input_frame():
        # 需求界面内容
        header.clear()
        with header:
            ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
            ui.label("需求管理模块").classes(
                "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
            )  # 绝对定位居中
            # 创建文件上传组件
            upload = ui.upload(
                on_upload=json_handle_upload,  # 绑定上传处理函数
                auto_upload=True,
                label="选择JSON文件",
            ).props("accept=.json")
            upload.set_visibility(False)  # 隐藏上传组件
            with (
                ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white")
            ):  # 右侧对齐
                with ui.menu().props("auto-close") as menu:
                    ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                    ui.menu_item("返回正式项目信息表", on_click=lambda: ui.navigate.to("/project_table"))
                    ui.menu_item("注销登录", on_click=lambda: logout())
                    ui.separator().props("size=1px")
                    ui.menu_item("新建需求", on_click=lambda: get_project_dialog("new"))
                    ui.menu_item(
                        "提交需求", on_click=lambda: output_config_data(app.storage.client["config_data"], "submit")
                    )
                    ui.menu_item("对比需求", on_click=show_comparison_dialog)
                    ui.separator().props("size=1px")
                    # ui.menu_item("临时保存", on_click=lambda: save_config_data(app.storage.client["config_data"]))
                    ui.menu_item(
                        "导出到本地", on_click=lambda: output_config_data(app.storage.client["config_data"], "export")
                    )
                    ui.menu_item("从本地导入", on_click=lambda: import_config_data(upload))
                    ui.separator().props("size=1px")
                    ui.menu_item("关闭菜单", menu.close)
            with ui.row().classes("font-sans h-[calc(100vh-9rem)] items-stretch flex-nowrap w-full text-black"):
                with ui.column().classes("w-1/4 min-w-[400px] items-center justify-start overflow-y-auto"):
                    with ui.row().classes("-space-x-2 items-center justify-center w-full"):
                        ui.space()
                        ui.label("确认项清单").classes("text-xl")
                        ui.space()
                        # 统计圆环
                        circular_activ = (
                            ui.circular_progress(size="md", color="green")
                            .bind_value_from(app.storage.client, "req_activ_num")
                            .props("rounded")
                            .classes("")
                        )
                        with circular_activ:
                            ui.tooltip("已选填")
                        circular_not_activ = (
                            ui.circular_progress(size="md", color="orange")
                            .bind_value_from(app.storage.client, "req_not_activ_num")
                            .props("rounded")
                            .classes("")
                        )
                        with circular_not_activ:
                            ui.tooltip("未选填")
                        app.storage.client["page_elements"]["circular_activ"] = circular_activ
                        app.storage.client["page_elements"]["circular_not_activ"] = circular_not_activ

                    question_table = ui.column().classes("w-full items-center overflow-y-auto -space-y-3")
                    with question_table:
                        # 将新创建的 question_table 实例存入 user storage
                        app.storage.client["page_elements"]["question_table"] = question_table
                        # 初始化一次确认项列表
                        set_question_list(0)

                ui.separator().props("vertical size=1px")
                with ui.column().classes("relative w-3/4 min-w-[700px] items-center"):
                    with ui.row().classes("relative w-full items-center justify-center"):
                        with ui.column().classes("absolute left-0 -top-2 -space-y-5 items-center justify-left"):
                            with ui.row().classes("-space-x-3 items-center justify-left w-full"):
                                ui.label("型号设置").classes("text-base ")
                                target_project_button = (
                                    ui.button("", on_click=lambda: get_project_dialog())
                                    .props("flat")
                                    .classes("text-base text-amber-9 px-0")
                                    .bind_text(app.storage.client, "target_project_name")
                                )
                                if app.storage.client["target_project_name"].strip() == "":
                                    target_project_button.set_icon("quiz")
                                app.storage.client["page_elements"]["target_project_button"] = target_project_button
                            with ui.row().classes("-space-x-3 items-center justify-left w-full"):
                                ui.label("版本查阅").classes("text-base ")
                                ui.button(icon="list_alt", on_click=lambda: select_project_req()).props("flat").classes(
                                    "text-base text-amber-9 px-0"
                                )
                                # .bind_text(app.storage.client, "target_version")

                                # if app.storage.client["target_version"].strip() == "":
                                # target_version_button.set_icon("quiz")
                                # app.storage.client["page_elements"]["target_version_button"] = target_version_button
                        with ui.row().classes("-space-x-3 items-center justify-center w-full "):
                            ui.label("当前编辑需求：").classes("text-xl ")
                            ui.label().classes("text-xl text-amber-8").bind_text(app.storage.client, "project_name")
                            ui.label("_V").classes("text-xl text-amber-8")
                            ui.label().classes("text-xl text-amber-8").bind_text(app.storage.client, "version")

                    with ui.column().classes(
                        "mx-0 mt-10 px-22 gap-8 w-full items-center justify-start overflow-y-auto"
                    ) as question_column:
                        # --- 修改开始 ---
                        # 将新创建的 question_column 实例存入 user storage
                        app.storage.client["page_elements"]["question_column"] = question_column
                        # --- 修改结束 ---
                        app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
                        question_display(None, "1")  # 触发点击事件
            with ui.row().classes("fixed bottom-0 left-0 right-0 bg-sky-50 p-3 items-center shadow-inner"):
                # 创建一个按钮组件，组件里有一个空白行，待后续往里面放缩略图
                row_h = 9
                get_img_group("上传", '"image/*, .pdf, .xlsx, .docx, .pptx"', row_h)
                with ui.row().classes(f"h-{str(row_h + 1)}").classes("p-0 overflow-y-auto") as img_row:
                    # 将新创建的 img_row 实例存入 user storage
                    app.storage.client["page_elements"]["img_row"] = img_row
                    # 检查缩略图对象存放字典，有对象则会创建缩略图
                    req_thumbnail_display()
            if app.storage.general.get("wait_review", {}):
                if app.storage.general["wait_review"].get(app.storage.client["project_name"], {}):
                    if app.storage.general["wait_review"][app.storage.client["project_name"]].get(
                        app.storage.client["version"], {}
                    ):
                        if (
                            app.storage.general["wait_review"][app.storage.client["project_name"]][
                                app.storage.client["version"]
                            ]["state"]
                            == "待审"
                        ):
                            ui.notify(
                                "当前需求处于待审状态，禁止导出和提交！",
                                type="warning",
                                position="center",
                                timeout=0,
                                progress=False,
                                close_button="✖",
                            )
                        elif (
                            app.storage.general["wait_review"][app.storage.client["project_name"]][
                                app.storage.client["version"]
                            ]["state"]
                            == "待修改"
                        ):
                            ui.notify(
                                "当前需求处于待修改状态，修改后可提交，但禁止导出！",
                                type="warning",
                                position="center",
                                timeout=0,
                                progress=False,
                                close_button="✖",
                            )
        # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
        ui.keyboard(on_key=requirement_handle_key, ignore=[])

    # 根据需求条目数据，格式化最终显示的字符串
    def format_show_string(item: dict) -> str:
        if not item:
            return "无"

        # show_template = item.get("option_show", "{V}")
        user_out = item.get("user_must_out", {})
        answer_type = item.get("answer_type")

        if not user_out:
            # 如果没有用户输出
            return "无"

        # 1. 处理单选类型
        if answer_type == "单选" or answer_type == "下拉单选":
            val = user_out.get("value")
            # 遍历所有单选项配置
            for option in item.get("options", []):
                # 当选项输出值与用户选择的选项输出值匹配上
                if str(option.get("option_out")) == str(val):
                    # 优先使用选项中的option_bold，如果它不为空
                    if option.get("option_bold"):
                        return option.get("option_show").replace(
                            "{V}", f'<b><span style="color: #2376b7;">{option["option_bold"]}</span></b>'
                        )
            # 如果没找到匹配的option_bold，则直接返回值
            return "无"

        # 2. 处理多选类型
        elif answer_type == "多选":
            show_template = ""
            show_bool = False
            selected_options = [key for key, value in user_out.items() if value]
            selec_show = []
            # 遍历所有多选项配置
            for option in item.get("options", []):
                # 遍历用户选择的选项的展示内容构成的列表
                for selec_cont in selected_options:
                    # 如果当前选项展示内容与用户选择的选项展示内容匹配上
                    if selec_cont == option["option_content"]:
                        selec_show.append(option["option_bold"])
                # 只认改确认项选项配置里，靠最前的选型展示语句
                if option["option_show"] and not show_bool:
                    show_template = option["option_show"]
                    show_bool = True
            # 如果选项展示语句为空
            if show_template == "":
                show_template = "选项无展示配置"
            val_str = "、".join(selec_show) if selec_show else "无"
            vor_num = str(len(selec_show))
            return show_template.replace("{V}", f'<b><span style="color: #207f4c;">{val_str}</span></b>').replace(
                "{N}", f'<b><span style="color: #207f4c;">{vor_num}</span></b>'
            )

        # 3. 处理文本输入类型 (单行/多行)
        elif answer_type in ["单行文本", "多行文本", "正整数"]:
            # 替换 {V}, {K}, {T}
            content_li = []
            # 键为1/2/3或用户起的多个名字
            key_li = list(user_out.keys())
            tolerance_out = item.get("option_tolerance_out", {})

            option_li = item.get("options", [])
            if not option_li:
                return "选项无展示配置"
            show_template = option_li[0]["option_show"]
            pattern = r"(.*?)(?:\[(.*?)\])(.*)"
            match = re.search(pattern, show_template)
            if match:
                # 提取并打印所有捕获组的内容
                prefix = match.group(1)  # [ 之前的内容
                content = match.group(2)  # [ ] 之间的内容
                suffix = match.group(3)  # ] 之后的内容
                # 键为1/2/3或用户起的多个名字
                for k in key_li:
                    content_li.append(
                        content.replace("{K}", f'<b><span style="color: #603d30;">{k}</span></b>')
                        .replace("{V}", f'<b><span style="color: #603d30;">{str(user_out[k])}</span></b>')
                        .replace(
                            "{T}",
                            f'<b><span style="color: #603d30;">{str(tolerance_out[k]) if tolerance_out else "无"}</span></b>',
                        )
                    )
                result = f"{prefix}<br>{'<br>'.join(content_li)}<br>{suffix}"
            else:
                result = (
                    show_template.replace("{K}", f'<b><span style="color: #603d30;">{key_li[0]}</span></b>')
                    .replace("{V}", f'<b><span style="color: #603d30;">{str(user_out[key_li[0]])}</span></b>')
                    .replace(
                        "{T}",
                        f'<b><span style="color: #603d30;">{str(tolerance_out[key_li[0]]) if tolerance_out else "无"}</span></b>',
                    )
                )
            return result

        # 默认回退
        return "、".join(map(str, user_out.values()))

    # 概述界面，需求项后面数字引用按钮添加函数
    def add_overview_lab(thumbnail_obj):
        k = thumbnail_obj.file_index
        ui.button(k, on_click=lambda: thumbnail_obj.handle_index_click()).classes("ml-1 text-white bg-purple-5").props(
            'round padding="0px 5px"'
        ).style("font-size: 8px;")

    # 根据传入的字符串生成对应的小标签
    def add_role_badge(role_text: str):
        color_data = {
            "光学": ["光", "cyan-3"],
            "结构": ["机", "blue-3"],
            "硬件": ["硬", "green-3"],
            "软件": ["软", "purple-3"],
            "工艺": ["艺", "orange-3"],
            "质量": ["质", "red-3"],
            "全员": ["全", "brown-5"],
        }
        color_str = color_data[role_text][1] if color_data[role_text] else "blue-grey-6"
        text_str = color_data[role_text][0] if color_data[role_text] else role_text[0]
        ui.badge(text=text_str, color=color_str).props("rounded").classes("p-1 text-[8px]/[8px]")

    # 需求显示界面框架构造函数
    async def overview_input_frame(json_data, temp_bool):
        project_name = json_data["1.0"]["project_name"]
        # 获取当前需求最新版本值
        new_ver = int(float(json_data["version"]))
        # 判断服务器存存器概述数据字典里是否已经存在该项目键值对，没有则创建，用于后续储存该项目需求概述资料
        if not db_storage.get_item(f"{project_name}_over_data", {}):
            await db_storage.set_item(f"{project_name}_over_data", {})

        # 按照需求概述资料里记录的需求最新版本，遍历处理服务器存储的该项目需求概述chip资料里的版本激活设置
        # 按照现有chip资料里的最高版本激活设置，生成更高版本设置
        # 如果服务器存储的概述资料里存在该项目对应数据
        overview_data = copy.deepcopy(db_storage.get_item(f"{project_name}_over_data", {}))
        # 数据存在 且 不是查看临时概述（审核需求）
        if overview_data and not temp_bool:
            # 设置一个结束整个遍历的变量
            # break_bool = False
            # 遍历该项目概述内容，字典键为概述的各分类项，值为该项下chip字典
            for chip_dic in overview_data.values():
                # 该项目的版本激活设置里已经存在于当前概述版本相同的版本设置，意味着不需要更新补充
                # if break_bool:
                #     break
                # 如果chip字典非空
                if chip_dic:
                    # 遍历各个chip数据
                    for chip_data in chip_dic.values():
                        # 将chip数据里的选项激活设置字典的键，也就是版本整理成列表
                        activ_key_li = [int(float(k)) for k in chip_data.get("select_activ_dic", {}).keys()]
                        # print(activ_key_li)
                        # 如果列表非空
                        if activ_key_li:
                            # 获取选项激活设置里最大的版本值
                            old_max_ver = max(activ_key_li)

                            # 如果当前需求版本值大于激活设置的最大版本值
                            if new_ver > old_max_ver:
                                # 获取激活设置最大版本值对应的布尔设置值
                                # activ_max_bool = chip_data["select_activ_dic"][f"{old_max_ver}.0"]
                                # 从现有激活设置最大版本值+1到当前需求版本值开始生成键值对
                                for key in range(old_max_ver + 1, new_ver + 1):
                                    # 新版本值均设置为激活设置最大值一样的布尔值
                                    # chip_data["select_activ_dic"][f"{key}.0"] = activ_max_bool
                                    # 新版本值均设置为None，为第三状态值，待工程师处理
                                    chip_data["select_activ_dic"][f"{key}.0"] = None
                            # 如果是首次生成概述界面，且有服务器储存存在概述内容，则属于衍生项目且复制了概述内容
                            # 最高版本的激活状态要改成None，让其黄色显示
                            if json_data["first_create"]:
                                chip_data["select_activ_dic"][f"{old_max_ver}.0"] = None
                            # 如果当前需求版本值刚好等于激活设置的最大版本值，则提前终止整个遍历
                            # elif new_ver == old_max_ver:
                            #     break_bool = True
                            #     break
                            # 与上一elif判断语句块矛盾，不能同时存在才能发挥检查刷新所有chip待选择显示效果效果
                            if chip_data["select_activ_dic"][f"{new_ver}.0"] is None:
                                # 将这个存在未手动选择激活状态的chip的相关状态配置成特殊显示
                                # 设置为None，这个chip的内容在项目总表展示时才会表明待选择处理
                                chip_data["enabled"] = None
                                chip_data["icon"] = "question_mark"
                                chip_data["bg_color"] = "bg-amber-5"

        # 需求界面内容
        header.clear()
        with header:
            ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
            # )  # 左侧对齐
            ui.label("概述整理模块").classes(
                "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
            )  # 绝对定位居中

            with (
                ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white")
            ):  # 右侧对齐
                with ui.menu().props("auto-close") as menu:
                    ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                    ui.menu_item("返回正式项目信息表", on_click=lambda: ui.navigate.to("/project_table"))
                    ui.menu_item("注销登录", on_click=lambda: logout())
                    ui.separator().props("size=1px")

                    ui.menu_item("对比需求", on_click=show_comparison_dialog)
                    ui.separator().props("size=1px")

                    ui.menu_item("关闭菜单", menu.close)
            with ui.row().classes("font-sans h-[calc(100vh-9rem)] items-stretch flex-nowrap w-full text-black"):
                # 需求内容列
                with ui.column().classes("w-1/2 min-w-[400px]"):
                    ui.label(f"{project_name} 需求内容").classes("text-xl text-center w-full")
                    with ui.column().classes("w-full overflow-y-auto p-1 gap-4"):
                        # === 步骤 1: 预处理 - 收集所有条目并获取其排序/分组信息 ===
                        version_keys = sorted([k for k in json_data if k.replace(".", "", 1).isdigit()], key=float)
                        # 将项目需求的最高版本号更新记录到服务器级储存里，供后续使用
                        app.storage.general["project_req_max_ver"][project_name] = max(version_keys)
                        # 储存最新版元素
                        ui_expansion = {}
                        ui_elements_latest = {}
                        for version in version_keys:
                            all_items_info = {}
                            version_data = json_data[version]
                            # 从 added 和 deleted 和 modified.new_data 中收集
                            all_change_items = (
                                list(version_data.get("added", {}).values())
                                + list(version_data.get("deleted", {}).values())
                                + [v["new_data"] for v in version_data.get("modified", {}).values()]
                            )
                            for item_data in all_change_items:
                                node_id = item_data.get("node_id")
                                if node_id and node_id not in all_items_info:
                                    all_items_info[node_id] = {
                                        "node_id": node_id,
                                        "num": item_data.get("num", 999),  # 默认值，确保未提供序号的排在最后
                                        "option_group_id": item_data.get("option_group_id", 999),
                                    }

                            # === 步骤 2: 排序 - 根据分组ID和组内序号进行排序 ===
                            sorted_items = sorted(
                                all_items_info.values(),
                                key=lambda x: (int(float(x["option_group_id"])), int(float(x["num"]))),
                            )

                            # === 步骤 3: 搭建UI骨架 - 根据排序结果创建占位容器和分隔线 ===
                            ui_elements = {}
                            ui_cards = {}
                            group_id_li = []
                            original_str = ""
                            original_version = version_data.get("original_version", "0.0")
                            original_project = version_data.get("original_project", "")
                            # 非全新配置需求
                            if original_project != "":
                                if original_project == project_name:
                                    original_str = f"修改自：{original_project}"
                                elif version == "1.0":
                                    original_str = f"复制自：{original_project}"
                                else:
                                    original_str = f"衍生自：{original_project}"
                                # 不是汇总最新数据，且衍生自某个版本
                                if version != "0" and original_version != "0.0":
                                    original_str = f"{original_str}，V{original_version}"
                                # 特殊情况，全新输入再提交前改了名字，依旧判定为全新
                                elif version != "0" and original_version == "0.0":
                                    original_str = "全新配置需求"
                                # 术语汇总最新数据的
                                else:
                                    original_str = ""
                            # 全新配置需求
                            else:
                                if version != "0" and original_version == "0.0":
                                    original_str = "全新配置需求"

                            # 处理需求内容标题内容
                            version_label = f"需求版本V{version}增删改内容"
                            if version == "0":
                                version_label = f"最新版需求内容_V{version_data['version']}"
                            exp = ui.expansion(
                                version_label,
                                icon="storage",
                                value=False,
                                caption=f"{original_str}",
                                group="group",
                            ).classes("gap-1 w-full bg-gray-100/30 rounded")
                            # 将最新版扩展元素存放，以便后续持续刷新
                            if version == "0":
                                ui_expansion["latest"] = exp
                            with exp:
                                with ui.column().classes("w-full") as exp_content:
                                    for item_info in sorted_items:
                                        # 获取需求ID
                                        node_id = item_info["node_id"]
                                        # 获取分组ID
                                        group_id = item_info["option_group_id"]

                                        if group_id == "":
                                            continue
                                        # 如果是新的分组，则添加卡元素
                                        if group_id not in group_id_li:
                                            # ui.separator().props("size=1px").classes("my-2 bg-grey-1 h-0.3 rounded-sm shadow-1")
                                            with ui.card().classes(
                                                # f"bg-{'blue-50/50' if float(group_id) % 2 == 0 else 'amber-50/50'} rounded-md shadow-1 p-2 gap-2 w-full"
                                                "rounded-md shadow-1 px-2 pt-2 pb-0 gap-2 w-full"
                                            ) as ui_card:
                                                # ui.label(f"需求组编号：{int(float(group_id))}").classes(
                                                #     "text-gray-500 text-[10px]/[16px] font-medium"
                                                # )
                                                ui.badge(f"{int(float(group_id))}", color="bg-gray-500/10").classes(
                                                    "bg-gray-500/30 py-0 px-1 rounded-md text-[8px]/[12px]"
                                                ).style("position:absolute;top: -4px;left: -3px;")
                                            ui_cards[group_id] = ui_card
                                            group_id_li.append(group_id)
                                            # 将容器的可见性先设为False，有内容时再打开
                                            ui_card.visible = False

                                        # 创建UI容器和占位符
                                        with ui_cards[group_id]:
                                            with ui.column().classes(
                                                "w-full gap-2 mb-1 text-[14px]/[20px] text-gray-500 bg-gradient-to-b from-gray-50/10 to-gray-300/10 rounded-md"
                                            ) as container:
                                                # 将容器的可见性先设为False，有内容时再打开
                                                container.visible = False
                                                with ui.row().classes("items-center w-full gap-0") as old_row:
                                                    old_content = ui.markdown()
                                                    old_ref_row = ui.row().classes("gap-0")
                                                old_row.visible = False
                                                with ui.row().classes("items-center w-full gap-0"):
                                                    version_badge = ui.badge().classes("my-1 mr-1")
                                                    content = ui.markdown()
                                                    ref_row = ui.row().classes("gap-0")
                                                    ui.space()
                                                    role_row = ui.row().classes("gap-0")
                                                # history_container = ui.column().classes("w-full pl-4 gap-0")

                                                # 存储UI元素引用
                                                ui_elements[node_id] = {
                                                    "container": container,
                                                    "group_card": ui_cards[group_id],
                                                    "old_row": old_row,
                                                    "old_content": old_content,
                                                    "old_ref_row": old_ref_row,
                                                    "version_badge": version_badge,
                                                    "content": content,
                                                    "ref_row": ref_row,
                                                    "role_badge": role_row,
                                                    # "history_container": history_container,
                                                }
                                                # 单独创建最新版模块的元素字典
                                                # if version == "0":
                                                #     ui_elements_latest[node_id] = ui_elements[node_id]

                                    # === 步骤 4: 按时间顺序填充和更新UI ===
                                    # for version in version_keys:
                                    # version_data = json_data[version]
                                    # version_num = version_data.get("version", "N/A")
                                    user = version_data.get("current_user", "N/A")
                                    timestamp = version_data.get("req_timestamp", "N/A").replace("T", " ").split(".")[0]

                                    # 处理新增
                                    for node_id, item_data in version_data.get("added", {}).items():
                                        if node_id in ui_elements:
                                            target = ui_elements[node_id]
                                            show_str = format_show_string(item_data)
                                            if show_str != "无":
                                                target["container"].visible = True  # 填充内容，设为可见
                                                target["group_card"].visible = True  # 填充内容，设为可见
                                                status = "新增"
                                                if version == version_keys[1]:
                                                    status = "初版"
                                                # 如果是最新版模块，则显示版本标签
                                                elif version == "0":
                                                    ui_elements_latest[node_id] = "1.0"
                                                    target["version_badge"].bind_text_from(ui_elements_latest, node_id)
                                                    status = "1.0"
                                                else:
                                                    if node_id in ui_elements_latest.keys():
                                                        ui_elements_latest[node_id] = version
                                                target["version_badge"].set_text(f"{status}")
                                                color = "blue-grey-2" if status == "初版" else "green-7"
                                                # if node_id in ui_elements_latest.keys():
                                                #     ui_elements_latest[node_id]["version_badge"].set_text(f"{version}")

                                                # ui_expansion["latest"].update()
                                                target["version_badge"].props(f"color={color}")
                                                with target["version_badge"]:
                                                    # target["version_badge"].clear()
                                                    tooltip_text = (
                                                        f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                    )
                                                    with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                        ui.html(
                                                            tooltip_text, sanitize=False
                                                        )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize
                                                target["content"].set_content(show_str)
                                                if item_data["ref_out"]:
                                                    # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                    with target["ref_row"]:
                                                        for t_lab in item_data["ref_out"]:
                                                            thumbnail_obj = app.storage.client["file_thumbnail_dic"][
                                                                t_lab
                                                            ]["file_obj"]
                                                            add_overview_lab(thumbnail_obj)
                                                if item_data["option_view"]:
                                                    with target["role_badge"]:
                                                        for role in item_data["option_view"].split("+"):
                                                            add_role_badge(role)
                                    # 处理删除
                                    for node_id, item_data in version_data.get("deleted", {}).items():
                                        if node_id in ui_elements:
                                            target = ui_elements[node_id]
                                            show_str = format_show_string(item_data)
                                            if show_str != "无":
                                                target["container"].visible = True
                                                target["group_card"].visible = True  # 填充内容，设为可见
                                                target["version_badge"].set_text("删除")
                                                target["version_badge"].props("color=red-7")
                                                with target["version_badge"]:
                                                    # target["version_badge"].clear()
                                                    tooltip_text = (
                                                        f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                    )
                                                    with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                        ui.html(
                                                            tooltip_text, sanitize=False
                                                        )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                                                target["content"].set_content(f"<del>{show_str}</del>")
                                                target["content"].classes(add="text-gray-400")
                                                if item_data["ref_out"]:
                                                    # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                    with target["ref_row"]:
                                                        for t_lab in item_data["ref_out"]:
                                                            thumbnail_obj = app.storage.client["file_thumbnail_dic"][
                                                                t_lab
                                                            ]["file_obj"]
                                                            add_overview_lab(thumbnail_obj)
                                                if item_data["option_view"]:
                                                    with target["role_badge"]:
                                                        for role in item_data["option_view"].split("+"):
                                                            add_role_badge(role)
                                    # 处理修改
                                    for node_id, item_data in version_data.get("modified", {}).items():
                                        if node_id in ui_elements:
                                            target = ui_elements[node_id]
                                            new_text = format_show_string(item_data["new_data"])
                                            old_text = format_show_string(item_data["old_data"])
                                            # 判断是首次填充还是追加历史
                                            # 之前是空的，现在首次填充
                                            if old_text == "无":
                                                if new_text != "无":
                                                    target["container"].visible = True
                                                    target["group_card"].visible = True  # 填充内容，设为可见
                                                    target["version_badge"].set_text("新增")
                                                    # 更新最新版模块版本标签
                                                    if node_id in ui_elements_latest.keys():
                                                        ui_elements_latest[node_id] = version
                                                    target["version_badge"].props("color=green-7")
                                                    with target["version_badge"]:
                                                        # target["version_badge"].clear()
                                                        tooltip_text = (
                                                            f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                        )
                                                        with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                            ui.html(
                                                                tooltip_text, sanitize=False
                                                            )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize
                                                    target["content"].set_content(new_text)
                                                    if item_data["new_data"]["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["ref_row"]:
                                                            for t_lab in item_data["new_data"]["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                    if item_data["new_data"]["option_view"]:
                                                        with target["role_badge"]:
                                                            for role in item_data["new_data"]["option_view"].split("+"):
                                                                add_role_badge(role)
                                            else:  # 之前已有内容，追加更改
                                                if new_text == "无":
                                                    target["container"].visible = True
                                                    target["group_card"].visible = True  # 填充内容，设为可见
                                                    target["version_badge"].set_text("作废")
                                                    # 更新最新版模块版本标签，作废的一般进入不了这个条件判断，保险先放着
                                                    if node_id in ui_elements_latest.keys():
                                                        ui_elements_latest[node_id] = version
                                                    target["version_badge"].props("color=red-7")
                                                    with target["version_badge"]:
                                                        # target["version_badge"].clear()
                                                        tooltip_text = (
                                                            f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                        )
                                                        with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                            ui.html(
                                                                tooltip_text, sanitize=False
                                                            )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize

                                                    target["content"].set_content(f"<del>{old_text}</del>")
                                                    target["content"].classes(add="text-gray-400")
                                                    if item_data["old_data"]["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["ref_row"]:
                                                            for t_lab in item_data["old_data"]["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                    if item_data["old_data"]["option_view"]:
                                                        with target["role_badge"]:
                                                            for role in item_data["old_data"]["option_view"].split("+"):
                                                                add_role_badge(role)
                                                else:
                                                    target["container"].visible = True
                                                    target["group_card"].visible = True  # 填充内容，设为可见
                                                    target["old_row"].visible = True
                                                    target["old_content"].set_content(old_text)
                                                    if item_data["old_data"]["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["old_ref_row"]:
                                                            for t_lab in item_data["old_data"]["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                    target["version_badge"].set_text("更改为")
                                                    # 更新最新版模块版本标签
                                                    if node_id in ui_elements_latest.keys():
                                                        ui_elements_latest[node_id] = version
                                                    target["version_badge"].props("color=orange-7")
                                                    with target["version_badge"]:
                                                        tooltip_text = (
                                                            f"需求ID：{node_id}<br>提交人：{user}<br>时间：{timestamp}"
                                                        )
                                                        with ui.tooltip("").classes("bg-gray-700 text-white min-w-40"):
                                                            ui.html(
                                                                tooltip_text, sanitize=False
                                                            )  # 如果有用户输入内容，则建议改为sanitize=Sanitizer().sanitize
                                                    target["content"].set_content(new_text)
                                                    if item_data["new_data"]["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["ref_row"]:
                                                            for t_lab in item_data["new_data"]["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                    if item_data["new_data"]["option_view"]:
                                                        with target["role_badge"]:
                                                            for role in item_data["new_data"]["option_view"].split("+"):
                                                                add_role_badge(role)
                            # 只给显示出来的card进行间隔上色
                            n = 1
                            for child in exp_content.default_slot.children:
                                if not child.visible:
                                    continue
                                if n == 1:
                                    child.classes("bg-blue-100/40 shadow-xs shadow-blue-300/30")
                                    n = 0
                                else:
                                    child.classes("bg-amber-100/40 shadow-xs shadow-amber-300/30")
                                    n = 1
                ui.separator().props("vertical size=1px")
                # 概述内容列
                with ui.column().classes("w-1/2 min-w-[400px] items-center"):
                    ui.label(f"{project_name} 概述整理").classes("text-xl")
                    with ui.column().classes("w-full overflow-y-auto p-1 gap-2"):
                        try:
                            global over_config_data
                            # 每次都以配置文件为准，不以服务器现有数据为准
                            # 配置更新能直接呈现，但配置减项将导致原有数据不呈现
                            with open(f"{BASE_DIR}/overview_config.json", "r", encoding="utf-8") as f:
                                # 使用 json.load() 读取文件内容并解析
                                over_config_data = json.load(f)
                        except json.JSONDecodeError:
                            print(f"错误：文件 '{json_path}' 不是有效的 JSON 格式。")
                        except Exception as e:
                            print(f"2读取文件时发生其他错误：{e}")

                        overview_role_update(project_name)

                        # 显示概述模块内容
                        for role, over_data in over_config_data.items():
                            with ui.card().classes("w-full px-3 gap-0"):
                                with ui.row().classes("flex-nowrap -space-x-2 items-center"):
                                    ui.label(f"{role}概述：").classes("text-base text-left w-full px-1 font-bold")
                                    ui.chip(icon="history", color="brown-7").props("outline").classes(
                                        "text-xs"
                                    ).bind_text(app.storage.general["overview_role"][project_name][role], "most_user")
                                    ui.chip(icon="add_reaction", color="green-7").props("outline").classes(
                                        "text-xs"
                                    ).bind_text(app.storage.general["overview_role"][project_name][role], "latest_user")
                                for data in over_data:
                                    user_role = app.storage.user["current_role"]
                                    if (
                                        user_role in data["permission"]["read_role"]
                                        or user_role in data["permission"]["edit_role"]
                                    ):
                                        if data["processing_type"] == "text":
                                            InteractiveButton(
                                                project=project_name,
                                                role=role,
                                                title=data["title"],
                                                label=data["label"],
                                                processing_type=data["processing_type"],
                                                dialog_placeholder=data["dialog_placeholder"],
                                                permission=data["permission"],
                                                temp_bool=temp_bool,
                                                # delete_bool=False,
                                            )
                                        elif data["processing_type"] in ["file", "image"]:
                                            InteractiveButton(
                                                project=project_name,
                                                role=role,
                                                title=data["title"],
                                                label=data["label"],
                                                processing_type=data["processing_type"],
                                                permission=data["permission"],
                                                temp_bool=temp_bool,
                                                # upload_path=Path(""),
                                                # delete_bool=False,
                                            )
                                        elif data["processing_type"] in ["test"]:
                                            InteractiveButton(
                                                project=project_name,
                                                role=role,
                                                title=data["title"],
                                                label=data["label"],
                                                processing_type=data["processing_type"],
                                                permission=data["permission"],
                                                state_options=data["state_options"],
                                                node_options=data["node_options"],
                                                instrument_options=data["instrument_options"],
                                                temp_bool=temp_bool,
                                                # upload_path=Path(""),
                                                # delete_bool=False,
                                            )

            with ui.row().classes("fixed bottom-0 left-0 right-0 bg-sky-50 p-3 items-center shadow-inner"):
                ui.label(text="参考文件：").classes("text-lg text-black m-0")
                # 创建一个按钮组件，组件里有一个空白行，待后续往里面放缩略图
                row_h = 9
                # get_img_group("上传", '"image/*, .pdf, .xlsx, .docx, .pptx"', row_h)
                with ui.row().classes(f"h-{str(row_h + 1)}").classes("p-0 overflow-y-auto") as img_row:
                    # 将新创建的 img_row 实例存入 user storage
                    app.storage.client["page_elements"]["img_row"] = img_row
                    # 检查缩略图对象存放字典，有对象则会创建缩略图
                    req_thumbnail_display()

    header = ui.header().classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    # 如果跳转传入了json文件路径，则解析这个路径并借此生成界面
    if type == "requirement" and os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                # 使用 json.load() 读取文件内容并解析
                json_data = json.load(f)
                # 将json_data数据更新到客户端储存里，调用requirement_input_frame()显示需求确认项
                loads_requirements(json_data)
        except json.JSONDecodeError:
            print(f"错误：文件 '{json_path}' 不是有效的 JSON 格式。")
        except Exception as e:
            print(f"读取文件时发生其他错误：{e}")
    # 如果跳转传入的仅为项目名，则意味着服务器没有改项目配置文件，新建项目
    elif type == "requirement" and project_name:
        # 设置项目型号
        app.storage.client["project_name"] = project_name
        app.storage.client["target_project_name"] = project_name
        # 客户端储存里数据初始化，调用requirement_input_frame()显示需求确认项
        new_requirement()
    # 如果跳转传入了json文件路径，则解析这个路径并借此生成界面
    elif type in ["overview", "temp_overview"] and os.path.exists(json_path):
        temp_bool = False
        if type == "temp_overview":
            temp_bool = True
        json_data = {}
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                # 使用 json.load() 读取文件内容并解析
                json_data = json.load(f)
        except json.JSONDecodeError:
            print(f"错误：文件 '{json_path}' 不是有效的 JSON 格式。")
        except Exception as e:
            print(f"读取文件时发生其他错误：{e}")
        # 获取概述文件里，版本最高的文件缩略图字典内容，复现文件缩略图
        file_information = json_data[get_max_numeric_key(json_data)]["file_dic"]
        app.storage.client["deleted_files"] = json_data[get_max_numeric_key(json_data)]["deleted_files"]
        app.storage.client["file_thumbnail_dic"] = {}
        for k, v in file_information.items():
            app.add_static_file(local_file=f"{UPLOADS_DIR}/{v['file_name_hash']}", url_path=v["file_url"])
            file_thumbnail = FileThumbnail(
                v["file_url"], v["file_type"], v["file_name_suffix"], v["file_lab"], v["parents_h"], False, False
            )
            app.storage.client["file_thumbnail_dic"][k] = {
                "file_obj": file_thumbnail,
                "file_information": v,
            }
        await overview_input_frame(json_data, temp_bool)
        # loads_overviews()
    else:
        requirement_input_frame()
    # 添加全局键盘事件跟踪
    # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
    ui.keyboard(on_key=handle_key)


@ui.page("/information")
def information_page():
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    # 获取用户信息
    current_user = app.storage.user["current_user"]
    is_admin = app.storage.user["is_admin"]
    current_role = app.storage.user["current_role"]

    def set_review_revise(p_name, v):
        app.storage.general["wait_review"][p_name][v]["state"] = "待修改"

    def set_review_pass(p_name, v):
        if app.storage.general["wait_review"][p_name][v]["state"] == "待审":
            app.storage.general["wait_review"][p_name][v]["state"] = "已审"
            delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")

    def get_requirement_page(project_name, ver):
        file_path = os.path.join(REQ_DIR, f"{project_name}_需求配置_V{ver}.json")
        ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    def get_review_button(button_group, project_name, ver):
        # 1. 总是先从 storage 获取最新的状态
        review_str = app.storage.general["wait_review"][project_name][ver]["state"]
        # 2. 关键逻辑：检查新状态
        if review_str == "已审":
            # 3. 如果状态是 "已审"，删除这个按钮组
            button_group.delete()
        else:
            # 4. 否则 (例如 "待修改" 或 "待审")，才更新按钮组的内容
            button_group.clear()
            with button_group:
                if current_role in ["研发经理"]:
                    ui.button(
                        f"{project_name}_V{ver} 需求状态：「{review_str}」",
                        on_click=lambda p_name=project_name: get_overviow_page(p_name, True),
                    )
                    ui.button(
                        "审核通过",
                        color="green-8",
                        on_click=lambda p_name=project_name, v=ver: set_review_pass(p_name, v),
                    ).on("click", lambda bg=button_group, pn=project_name, v=ver: get_review_button(bg, pn, v)).props()
                    ui.button(
                        "需修改",
                        color="amber-8",
                        on_click=lambda p_name=project_name, v=ver: set_review_revise(p_name, v),
                    ).on("click", lambda bg=button_group, pn=project_name, v=ver: get_review_button(bg, pn, v)).props()
                else:
                    ui.button(
                        f"{project_name}_V{ver} 需求状态：「{review_str}」",
                        on_click=lambda p_name=project_name, v=ver: get_requirement_page(p_name, v),
                    )
                    ui.button(
                        "修改",
                        color="amber-8",
                        on_click=lambda p_name=project_name, v=ver: set_review_revise(p_name, v),
                    ).on("click", lambda bg=button_group, pn=project_name, v=ver: get_review_button(bg, pn, v)).props()

    # 主界面
    header = ui.header().classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("项目信息").classes("text-white text-lg absolute left-1/2 transform -translate-x-1/2")  # 绝对定位居中
        with ui.button(icon="menu").props("flat round").classes("ml-auto -mt-3.5 h-4 text-sm/4 text-white"):  # 右侧对齐
            with ui.menu() as menu:
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)
    review_column = ui.column()
    with review_column:
        # 如果用户是审核者，显示所有待审需求
        if current_role in ["研发经理"] and app.storage.general.get("wait_review", {}):
            for project_name, ver_dic in app.storage.general["wait_review"].items():
                for ver, dic in ver_dic.items():
                    # 如果当前项目的当前版本未审
                    if dic["state"] != "已审":
                        button_group = ui.button_group()
                        get_review_button(button_group, project_name, ver)
        # 用户不是审核者，且存在待审数据
        elif app.storage.general.get("wait_review", {}):
            for project_name, ver_dic in app.storage.general["wait_review"].items():
                for ver, dic in ver_dic.items():
                    if dic["state"] != "已审" and dic["submitter"] == current_user:
                        button_group = ui.button_group()
                        get_review_button(button_group, project_name, ver)


# ======================
# 运行程序
# ======================
if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="项目文件管理系统",
        favicon=f"{IMG_DIR}/RFRF.png",
        # host='0.0.0.0' 允许来自局域网的任何IP访问
        host="0.0.0.0",
        # port=8080 是您选择的端口，可以自定义
        port=8080,
        storage_secret=ST,  # 添加存储密钥
        dark=False,
        # 在生产环境中，必须禁用热重载功能，以获得更好的性能和稳定性
        # False 不自动重载，True自动重载
        reload=True,
    )
