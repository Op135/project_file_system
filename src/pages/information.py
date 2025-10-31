# -*- encoding: utf-8 -*-
import copy
import json
import os

from nicegui import app, ui

from .. import db_storage
from ..config import IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR
from ..utils import delete_file, get_overviow_page, logout


@ui.page("/information")
def information_page():
    # 检查用户是否已登录
    # {'current_user': '用户名', 'is_admin': False}
    if not app.storage.user.get("current_user"):
        ui.navigate.to("/login")  # 如果未登录，跳转到登录页
        return
    dialog = ui.dialog().props("persistent").classes("")
    # 获取用户信息
    current_user = app.storage.user.get("current_user")
    is_admin = app.storage.user.get("is_admin")
    current_role = app.storage.user.get("current_role")
    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个

    async def set_overview_active_state(project_name, ver):
        req_ver = int(float(ver))
        # req_path = os.path.join(REQ_DIR, f"{project_name}_需求配置_V{req_ver}.0.json")
        # req_json = {}
        # try:
        #     # 每次都以配置文件为准，不以服务器现有数据为准
        #     # 配置更新能直接呈现，但配置减项将导致原有数据不呈现
        #     with open(req_path, "r", encoding="utf-8") as f:
        #         # 使用 json.load() 读取文件内容并解析
        #         req_json = json.load(f)
        # except json.JSONDecodeError:
        #     print(f"错误：文件 '{req_path}' 不是有效的 JSON 格式。")
        # except Exception as e:
        #     print(f"读取文件时发生其他错误：{e}")

        # 按照需求概述资料里记录的需求最新版本，遍历处理服务器存储的该项目需求概述chip资料里的版本激活设置
        # 按照现有chip资料里的最高版本激活设置，生成更高版本设置
        # 如果服务器存储的概述资料里存在该项目对应数据
        overview_data = copy.deepcopy(db_storage.get_item(f"{project_name}_over_data", {}))
        # 遍历该项目概述内容，字典键为概述的各分类项，值为该项下chip字典
        for chip_dic in overview_data.values():
            # 遍历各个chip数据
            for chip_data in chip_dic.values():
                # 将chip数据里的选项激活设置字典的键，也就是版本整理成列表
                over_chip_ver_li = [int(float(k)) for k in chip_data.get("select_activ_dic", {}).keys()]
                # print(over_chip_ver_li)
                # 如果列表非空
                if over_chip_ver_li:
                    # 获取选项激活设置里最大的版本值
                    max_over_ver = max(over_chip_ver_li)

                    # 适用于正常项目迭代，无论是原项目升版本疑惑其它项目衍生过来升版本，
                    # 概述内容不会复制，需求版本值肯定大于激活设置的最大版本值
                    # 由1.0版本衍生到另外一个项目，需求版本2.0，概述复制了参照项目的1.0版本
                    if req_ver > max_over_ver:
                        # 获取激活设置最大版本值对应的布尔设置值
                        # activ_max_bool = chip_data["select_activ_dic"][f"{max_over_ver}.0"]
                        # 从现有激活设置最大版本值+1到当前需求版本值开始生成键值对
                        for key in range(max_over_ver + 1, req_ver + 1):
                            # 新版本值均设置为激活设置最大值一样的布尔值
                            # chip_data["select_activ_dic"][f"{key}.0"] = activ_max_bool
                            # 新版本值均设置为None，为第三状态值，待工程师处理
                            chip_data["select_activ_dic"][f"{key}.0"] = None
                    # 衍生项目且复制了2.0及以上版本的概述内容
                    # 最高版本的激活状态要改成None，让其黄色显示
                    else:
                        ui.notify(
                            f"出现需求版本{req_ver}不大于概述激活记录最高版本{max_over_ver}的情况。",
                            type="warning",
                            position="center",
                            timeout=0,
                            progress=False,
                            close_button="✖",
                        )
                        # 获取参考版本记录的激活状态（不一定是参考项目最高版本，因为可能用户选择了中间版本来参考衍生）
                        # reference_state = chip_data["select_activ_dic"][req_json["original_version"]]
                        # # 清空复位
                        # chip_data["select_activ_dic"] = {}
                        # # 1.0版本概述状态保留参考项目概述的参考版本记录
                        # chip_data["select_activ_dic"]["1.0"] = reference_state
                        # # 2.0版本固定位None
                        # chip_data["select_activ_dic"]["2.0"] = None

                    if chip_data["select_activ_dic"][f"{req_ver}.0"] is None:
                        # 将这个存在未手动选择激活状态的chip的相关状态配置成特殊显示
                        # 设置为None，这个chip的内容在项目总表展示时才会表明待选择处理
                        chip_data["enabled"] = None
                        chip_data["icon"] = "question_mark"
                        chip_data["bg_color"] = "bg-amber-5"
        await db_storage.set_item(f"{project_name}_over_data", overview_data)

    def set_review_revise(p_name, v):
        app.storage.general["wait_review"][p_name][v]["state"] = "待修改"

    async def set_review_pass(p_name, v):
        if app.storage.general["wait_review"][p_name][v]["state"] == "待审":
            app.storage.general["wait_review"][p_name][v]["state"] = "已审"
            # 需求评审通过了才更新概述chip激活状态
            await set_overview_active_state(p_name, v)
            delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")

    def del_requirement_file(button_group, p_name, v):
        delete_file(f"{REQ_DIR}/{p_name}_需求配置_V{v}.json")
        delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")
        app.storage.general["wait_review"][p_name].pop(v, None)
        button_group.delete()
        dialog.close()

    def del_requirement_dialog(button_group, p_name, v):
        dialog.clear()
        with dialog, ui.card():
            with ui.column():
                ui.label(f"确认删除{p_name}_需求配置_V{v}.json ？").classes("text-lg text-red-500")
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "确认",
                        color="red-5",
                        on_click=lambda bg=button_group, pro_name=p_name, ver=v: del_requirement_file(
                            bg, pro_name, ver
                        ),
                    )
                    ui.button("取消", on_click=lambda: dialog.close())
        dialog.open()

    def get_requirement_page(project_name, ver):
        file_path = os.path.join(REQ_DIR, f"{project_name}_需求配置_V{ver}.json")
        ui.navigate.to(f"/main/requirement?type=requirement&json_path={file_path}")

    def get_review_button(button_group, project_name, ver):
        # 1. 总是先从 storage 获取最新的状态
        review_str = app.storage.general["wait_review"][project_name][ver].get("state")
        submitter = app.storage.general["wait_review"][project_name][ver].get("submitter")
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
                        f"{submitter}提交：{project_name}_V{ver} 需求状态：「{review_str}」",
                        on_click=lambda p_name=project_name: get_overviow_page(p_name, True),
                    ).props("outline")
                    ui.button(
                        "审核通过",
                        color="green-8",
                        on_click=lambda p_name=project_name, v=ver: set_review_pass(p_name, v),
                    ).on("click", lambda bg=button_group, pn=project_name, v=ver: get_review_button(bg, pn, v)).props(
                        ""
                    )
                    ui.button(
                        "需修改",
                        color="amber-8",
                        on_click=lambda p_name=project_name, v=ver: set_review_revise(p_name, v),
                    ).on("click", lambda bg=button_group, pn=project_name, v=ver: get_review_button(bg, pn, v)).props(
                        ""
                    )
                    ui.button(
                        "删除",
                        color="red-8",
                        on_click=lambda bg=button_group, pn=project_name, v=ver: del_requirement_dialog(bg, pn, v),
                    ).props("")
                else:
                    ui.button(
                        f"{project_name}_V{ver} 需求状态：「{review_str}」",
                        on_click=lambda p_name=project_name, v=ver: get_requirement_page(p_name, v),
                    ).props("outline")
                    ui.button(
                        "修改",
                        color="amber-8",
                        on_click=lambda p_name=project_name, v=ver: set_review_revise(p_name, v),
                    ).on("click", lambda bg=button_group, pn=project_name, v=ver: get_review_button(bg, pn, v)).props(
                        ""
                    )

    # 主界面
    header = ui.header(elevated=True).classes("flex justify-between items-center bg-blue-500 h-12 px-4")
    with header:
        ui.image(f"{IMG_DIR}/Rayfine.png").classes("absolute w-20")
        ui.label("消息与统计").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(current_avatar_path)
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.menu_item("关闭菜单", menu.close)
    with ui.row():
        with ui.card().classes("gap-2 p-2"):
            ui.label("需求评审状态：").classes("text-base")
            # 如果用户是审核者，显示所有待审需求
            if current_role in ["研发经理"] and app.storage.general.get("wait_review", {}):
                show_bool = False
                for project_name, ver_dic in app.storage.general["wait_review"].items():
                    for ver, dic in ver_dic.items():
                        # 如果当前项目的当前版本未审
                        if dic.get("state") != "已审":
                            show_bool = True
                            button_group = ui.button_group().props("outline")
                            get_review_button(button_group, project_name, ver)
                if not show_bool:
                    ui.label("无待评审需求").classes("text-sm text-green-500")
            # 用户不是审核者，且存在待审数据
            elif app.storage.general.get("wait_review", {}):
                show_bool = False
                for project_name, ver_dic in app.storage.general["wait_review"].items():
                    for ver, dic in ver_dic.items():
                        if dic.get("state") != "已审" and dic.get("submitter") == current_user:
                            show_bool = True
                            button_group = ui.button_group().props("outline")
                            get_review_button(button_group, project_name, ver)
                if not show_bool:
                    ui.label("无待评审需求").classes("text-sm text-green-500")
