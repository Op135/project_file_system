# -*- encoding: utf-8 -*-
import ast
import copy
import hashlib
import io
import itertools
import json
import os
import re
from datetime import datetime
from itertools import islice
from pathlib import Path

from nicegui import app, events, ui
from nicegui.events import GenericEventArguments, KeyEventArguments, MouseEventArguments, UploadEventArguments

from .. import db_storage  # 导入我们创建的模块
from ..components import ButtonUploader, FileThumbnail, InteractiveButton
from ..config import IMG_DIR, PRESET_AVATARS, REQ_DIR, REQ_UPLOADS_FILE_TYPE, UPLOAD_URL_DIR, UPLOADS_DIR
from ..utils import (
    compare_configs_by_id,
    copy_overview_data,
    find_files_with_prefix_and_version,
    find_key_position,
    get_cache_busted_path,
    get_max_numeric_key,
    handle_key,
    logout,
    overview_role_update,
    validate_format_regex,
)


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
    current_user = app.storage.user.get("current_user")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)
    # 存储用户层级需求相关数据的变量初始化
    # 用于记录键盘按键状态
    app.storage.client.setdefault("key_state", {})
    # 需求配置数据字典初始化
    app.storage.client.setdefault("config_data", app.state.init_config_data)
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
        # project_name = app.storage.client["project_name"]
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
        elif target_project_name.split("-")[0] == "RFTS" and not validate_format_regex(
            target_project_name, r"^RFTS-\d{4}$"
        ):
            ui.notify(
                "不符合临时项目号命名规则：RFTS-4位数字！",
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
        app.storage.client["config_data"] = app.state.init_config_data
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
                file_url=v["file_url"],
                file_type=v["file_type"],
                file_name_suffix=v["file_name_suffix"],
                file_lab=v["file_lab"],
                parents_h=v["parents_h"],
                auto_create=False,
                delet_lab=True,
                on_add_ref_click=add_ref_button,
                on_question_display_click=question_display,
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
        # set_question_list(0)  # 初始化一次确认项列表
        # app.storage.client["buttons_dic"]["1"].props(remove="disabled")  # 启用按钮
        # question_display(None, "1")  # 触发点击事件
        # req_thumbnail_display()
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
            # 获取文件类型
            file_type = e.file.content_type  # 图片类返回image/xxx，文件类返回application/xxx，文本类型text/xxx

            # 如果是文件或文本类型，要检查后缀，图片类型不用检查
            if ("application" in file_type or "text" in file_type) and file_suffix not in REQ_UPLOADS_FILE_TYPE:
                ui.notify(
                    f'文件 "{file_name}" 不是规定的：{", ".join(REQ_UPLOADS_FILE_TYPE)} 文件类型，无法上传!',
                    type="warning",
                    position="center",
                    timeout=0,
                    progress=False,
                    close_button="✖",
                )
                return

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
                    file_thumbnail = FileThumbnail(
                        file_url=url_path,
                        file_type=e.file.content_type,
                        file_name_suffix=e.file.name,
                        file_lab=file_lab,
                        parents_h=parents_h,
                        on_add_ref_click=add_ref_button,
                        on_question_display_click=question_display,
                    )
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
                v["file_obj"].add_add_lab(ref_row, k, question_k, question)
                v["file_obj"].add_lab_bool = True

    # 缩略图加号删除函数
    def delete_activ_ref():
        for v in app.storage.client["file_thumbnail_dic"].values():
            # 防止重复添加加号激活按键
            if v["file_obj"].add_lab_bool:
                v["file_obj"].ref_lab.delete()
                v["file_obj"].add_lab_bool = False

    # 添加数字引用按钮函数
    def add_ref_button(thumbnail_obj, ref_row, question_k: str, question: str, add_bool: bool):
        """
        Args:
            thumbnail_obj：每个数字小按钮在被点击时相当于点击的缩略图图像。
            ref_row：在该行元素里添加数字小按钮。
            question_k：用于记录数字按钮添加在那个问题项里。
            question：用于记录数字按钮添加所在问题项的问题内容标题。
            add_bool：用于识别按照data数据生成已有数字按钮（False），还是新增添加数字按钮（True）
        Returns:
        """
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
            # print("单选", k, options_type, next)
            radio_bool = True

        # 多选，且用户做出勾选了其中某个选项
        elif options_type == "多选" and True in out_value:
            checkboxe_bool = True
            # print("多选", k, options_type, next)
        # 本次处理的是输入框
        elif (
            options_type
            in [
                "正整数",
                "单行文本",
                "多行文本",
            ]  # 不能省略，省略在上面条件不成立情况下，会导致直接执行v.strip()报某种类型无.strip方法的错误
            and all(v.strip() != "" for v in out_value)
            and all(w.strip() != "" for w in out_tolerance_value)
        ) and (
            (options_type == "正整数" and all(v.isdigit() for v in out_value) and all(int(v) != 0 for v in out_value))
            or (options_type in ["单行文本", "多行文本"])
        ):
            # print("输入", k, options_type, next)
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
                # 处理需要插入引用的确认项
                if ref_config_bool:
                    with ui.row().classes("gap-1 w-full justify-center"):
                        with ui.column().classes(
                            "w-1/4 h-fit -space-y-5 border-2 border-solid border-Gray-500 rounded-md"
                        ):
                            ui.label("引用：").classes("p-1 text-sm text-gray-500")
                            ref_row = ui.row().classes("space-x-0 p-2")

                            if app.storage.client["config_data"]["data"][k]["ref_out"]:
                                # 将需求data里的已有引用数字生成数字按钮
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
            data_json["current_user"] = current_user

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
            # if (
            #     # 当前版本的参照版本为0.0版（新填 或 1.0版且没改名，改名时会将参照版本更新为当前版本）
            #     original_version == "0.0"
            #     or version != "0.0"  # 当前版本不为0.0即高版本 且 没有改名
            #     and project_name == target_project_name
            # ):
            #     pass
            #  改了项目名
            if project_name != target_project_name:
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
                if project_name.split("-")[0] == "RFTS" and not validate_format_regex(project_name, r"^RFTS-\d{4}$"):
                    ui.notify(
                        "不符合临时项目号命名规则：RFTS-4位数字！",
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
                        "不符合临时项目号命名规则：RFTS-4位数字！",
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
                            old_data_json["current_user"] = current_user
                            old_data_json["original_project"] = project_name
                            old_data_json["version"] = "1.0"
                            old_data_json["original_version"] = version
                            old_data_json["req_timestamp"] = datetime.now().isoformat()
                            # 衍生复制过来的需求，默认通过审核
                            # old_data_json["review_state"] = True
                            # 将该需求版本标记到待审字典里
                            app.storage.general["wait_review"][target_project_name] = {
                                "1.0": {"state": "已审", "submitter": current_user}
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
                                await copy_overview_data(project_name, version, target_project_name)
                                # 更新目标项目概述角色统计结果，以便第一时间在项目总表能看到统计结果和状态
                                overview_role_update(target_project_name)
                                overview_role_update(target_project_name)
                                ui.notify(
                                    "复制衍生项目需求文件概述资料成功。",
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
                        "submitter": current_user,
                    }

                    # 将提交该需求的用户更新为该项目负责的销售员
                    app.storage.general["project_sale"][target_project_name] = current_user
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
            with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
                ui.image(current_display_path)
                with ui.menu().props("auto-close") as menu:
                    ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                    ui.separator().props("size=1px")
                    ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                    ui.menu_item("返回项目信息表", on_click=lambda: ui.navigate.to("/project_table"))
                    ui.separator().props("size=1px")
                    ui.menu_item(
                        "提交需求", on_click=lambda: output_config_data(app.storage.client["config_data"], "submit")
                    )
                    ui.menu_item(
                        "导出到本地", on_click=lambda: output_config_data(app.storage.client["config_data"], "export")
                    )
                    ui.menu_item("从本地导入", on_click=lambda: import_config_data(upload))
                    ui.separator().props("size=1px")
                    ui.menu_item("对比需求", on_click=show_comparison_dialog)
                    ui.menu_item("新建需求", on_click=lambda: get_project_dialog("new"))
                    ui.separator().props("size=1px")
                    ui.menu_item("注销登录", on_click=lambda: logout())
                    ui.menu_item("关闭菜单", menu.close)
            # 需求行
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
            # 缩略图行
            with ui.row().classes("fixed bottom-0 left-0 right-0 bg-sky-50 p-3 items-center shadow-inner"):
                # 创建一个按钮组件，组件里有一个空白行，待后续往里面放缩略图
                row_h = 9
                get_img_group("上传", "/*", row_h)
                with ui.row().classes(f"h-{str(row_h + 1)}").classes("p-0 overflow-y-auto") as img_row:
                    # 将新创建的 img_row 实例存入 user storage
                    app.storage.client["page_elements"]["img_row"] = img_row
                    # 检查缩略图对象存放字典，有对象则会创建缩略图
                    req_thumbnail_display()
            # 需求状态提醒
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
                    # 必须填写的输入内容为多行文本，则默认在最前面加上换行标签，且内部\n统一替换成换行标签
                    if answer_type == "多行文本":
                        user_out_str = f"<br>{str(user_out[k]).replace('\n', '<br>')}"
                    else:
                        user_out_str = str(user_out[k])
                    content_li.append(
                        content.replace("{K}", f'<b><span style="color: #603d30;">{k}</span></b>')
                        .replace(
                            "{V}",
                            f'<b><span style="color: #603d30;">{user_out_str}</span></b>',
                        )
                        .replace(
                            "{T}",
                            f'<b><span style="color: #603d30;">{str(tolerance_out[k]) if tolerance_out else "无"}</span></b>',
                        )
                    )
                result = f"{prefix}<br>{'<br>'.join(content_li)}<br>{suffix}"
            else:
                # 必须填写的输入内容为多行文本，则默认在最前面加上换行标签，且内部\n统一替换成换行标签
                if answer_type == "多行文本":
                    user_out_str = f"<br>{str(user_out[key_li[0]]).replace('\n', '<br>')}"
                else:
                    user_out_str = str(user_out[key_li[0]])
                result = (
                    show_template.replace("{K}", f'<b><span style="color: #603d30;">{key_li[0]}</span></b>')
                    .replace(
                        "{V}",
                        f'<b><span style="color: #603d30;">{user_out_str}</span></b>',
                    )
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

        # 判断服务器存存器概述数据字典里是否已经存在该项目键值对，没有则创建，用于后续储存该项目需求概述资料
        if not db_storage.get_item(f"{project_name}_over_data", {}):
            await db_storage.set_item(f"{project_name}_over_data", {})

        # 需求界面内容
        header.clear()
        with header:
            ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
            # )  # 左侧对齐
            ui.label("概述整理模块").classes(
                "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
            )  # 绝对定位居中

            with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
                ui.image(current_display_path)
                with ui.menu().props("auto-close") as menu:
                    ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                    ui.separator().props("size=1px")
                    ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                    ui.menu_item("返回项目信息表", on_click=lambda: ui.navigate.to("/project_table"))
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
                                                with ui.column().classes("items-start w-full gap-0") as old_column:
                                                    old_content = ui.markdown()
                                                    with ui.row().classes("items-start gap-0") as old_row:
                                                        ui.label("引用文件：")
                                                        old_ref_row = ui.row().classes("gap-0")
                                                    old_row.visible = False
                                                old_column.visible = False
                                                with ui.row().classes("items-start w-full gap-0"):
                                                    version_badge = ui.badge().classes("my-1 mr-1")
                                                    with ui.column().classes("items-start gap-0"):
                                                        content = ui.markdown()
                                                        with ui.row().classes("items-start gap-0") as new_row:
                                                            ui.label("引用文件：")
                                                            ref_row = ui.row().classes("gap-0")
                                                        new_row.visible = False
                                                    ui.space()
                                                    role_row = ui.row().classes("gap-0")
                                                # history_container = ui.column().classes("w-full pl-4 gap-0")

                                                # 存储UI元素引用
                                                ui_elements[node_id] = {
                                                    "container": container,
                                                    "group_card": ui_cards[group_id],
                                                    "old_column": old_column,
                                                    "old_content": old_content,
                                                    "old_ref_row": old_ref_row,
                                                    "new_row": new_row,
                                                    "old_row": old_row,
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
                                    user = version_data.get("current_user", "")
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
                                                    target["new_row"].visible = True
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
                                                    target["new_row"].visible = True
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
                                                        target["new_row"].visible = True
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
                                                    target["old_column"].visible = True
                                                    target["old_content"].set_content(old_text)
                                                    if item_data["old_data"]["ref_out"]:
                                                        # 在引用行里添加于缩略图编号一致的数字引用按钮
                                                        with target["old_ref_row"]:
                                                            for t_lab in item_data["old_data"]["ref_out"]:
                                                                thumbnail_obj = app.storage.client[
                                                                    "file_thumbnail_dic"
                                                                ][t_lab]["file_obj"]
                                                                add_overview_lab(thumbnail_obj)
                                                        target["old_row"].visible = True
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
                                                        target["new_row"].visible = True
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
                        overview_role_update(project_name)

                        # 显示概述模块内容
                        for role, over_data in app.state.over_config_data.items():
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
                # get_img_group("上传", "/*", row_h)
                with ui.row().classes(f"h-{str(row_h + 1)}").classes("p-0 overflow-y-auto") as img_row:
                    # 将新创建的 img_row 实例存入 user storage
                    app.storage.client["page_elements"]["img_row"] = img_row
                    # 检查缩略图对象存放字典，有对象则会创建缩略图
                    req_thumbnail_display()

    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
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
                file_url=v["file_url"],
                file_type=v["file_type"],
                file_name_suffix=v["file_name_suffix"],
                file_lab=v["file_lab"],
                parents_h=v["parents_h"],
                auto_create=False,
                delet_lab=False,
                # on_add_ref_click=add_ref_button,
            )
            app.storage.client["file_thumbnail_dic"][k] = {
                "file_obj": file_thumbnail,
                "file_information": v,
            }
        await overview_input_frame(json_data, temp_bool)
        # loads_overviews()
    else:
        new_requirement()
    # 添加全局键盘事件跟踪
    # ignore不设定默认导致键盘事件在'input', 'select', 'button', 'textarea'元素聚焦时被忽略
    ui.keyboard(on_key=handle_key)
