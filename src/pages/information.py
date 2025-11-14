# -*- encoding: utf-8 -*-
import copy
import json
import os
from datetime import datetime

from nicegui import app, ui

from .. import db_storage
from ..config import BASE_DIR, IMG_DIR, OVER_DIR, PRESET_AVATARS, REQ_DIR
from ..utils import (
    delete_file,
    get_cache_busted_path,
    get_overviow_page,
    logout,
    project_summary_update,
    requirement_version_tidy,
    set_project_custom_labels,
)


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

    with open(f"{BASE_DIR}/information_module_show_role.json", "r", encoding="utf-8") as f:
        # 使用 json.load() 读取文件内容并解析
        module_show_data = json.load(f)

    # 从全局存储中获取用户当前的头像设置
    # (在 main.py 中定义 "user_preferences")
    user_prefs = app.storage.general.get("user_preferences", {}).get(current_user, {})
    current_avatar_path = user_prefs.get("avatar", PRESET_AVATARS[0])  # 默认为第一个
    # 在 *显示* 前，应用缓存清除
    current_display_path = get_cache_busted_path(current_avatar_path)

    async def set_overview_active_state(project_name, ver):
        req_ver = int(float(ver))

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
                    # 由指定版本衍生到另外一个新项目，需求版本2.0，概述复制了参照项目的指定版本激活设置，并先记录为目标项目1.0版本概述，需求版本值肯定大于激活设置的最大版本值
                    if req_ver > max_over_ver:
                        # 获取激活设置最大版本值对应的布尔设置值
                        activ_max_bool = chip_data["select_activ_dic"][f"{max_over_ver}.0"]
                        # 从现有激活设置最大版本值+1到当前需求版本值开始生成键值对
                        for key in range(max_over_ver + 1, req_ver + 1):
                            # 新版本值均设置为激活设置最大值一样的布尔值
                            # chip_data["select_activ_dic"][f"{key}.0"] = activ_max_bool

                            # 新版本值均设置为None，为第三状态值，待工程师处理
                            # chip_data["select_activ_dic"][f"{key}.0"] = None

                            # 如果最大版本值为True，则新版本都设置为None
                            if activ_max_bool:
                                chip_data["select_activ_dic"][f"{key}.0"] = None
                            # 如果最大版本值为False或者None，则新版本都设置为False
                            else:
                                chip_data["select_activ_dic"][f"{key}.0"] = False
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

                    if chip_data["select_activ_dic"][f"{req_ver}.0"] is None:
                        # 将这个存在未手动选择激活状态的chip的相关状态配置成特殊显示
                        # 设置为None，这个chip的内容在项目总表展示时才会表明待选择处理
                        chip_data["enabled"] = None
                        chip_data["icon"] = "question_mark"
                        chip_data["bg_color"] = "bg-amber-5"
        await db_storage.set_item(f"{project_name}_over_data", overview_data)

    def set_review_revise(p_name, v):
        app.storage.general["wait_review"][p_name][v]["state"] = "待修改"

    async def set_review_pass(button_group, p_name, v):
        app.storage.general["wait_review"][p_name][v]["state"] = "已审"
        # 将项目需求的最高版本号更新记录到服务器级储存里，供后续使用
        app.storage.general["project_req_max_ver"][p_name] = v
        # 需求评审前，如果是衍生为新项目，概述已经复制，但待到需求评审通过了，才更新概述chip激活状态
        await set_overview_active_state(p_name, v)
        # 删除评审时生成的临时概述整理文件
        delete_file(f"{OVER_DIR}/{p_name}_概述整理_temp.json")
        # 生成正式概述整理文件，里面整理出来的最新需求配置内容，给其它函数整理出该项目的定制内容标签，以供总表显示
        await requirement_version_tidy(p_name, False)
        # 整理该项目需求定制内容标签
        set_project_custom_labels(p_name)
        get_review_button(button_group, p_name, v)
        dialog.close()

    # 处理新建临时项目情况
    async def set_temporary_project_review_pass(button_group, p_name, v, data):
        if data.get("introduction").strip() and data.get("customer").strip():
            temp_data = {
                p_name: {
                    "model_notes": data.get("notes").strip(),
                    "creation_date": datetime.now().strftime("%Y-%m-%d"),
                    "introduction": data.get("introduction").strip(),
                    "customer": data.get("customer").strip(),
                }
            }
            project_data = {}
            with open(f"{BASE_DIR}/project_summary.json", "r", encoding="utf-8") as f:
                project_data = json.load(f)
            project_data.update(temp_data)
            json_str = json.dumps(project_data, indent=4, ensure_ascii=False)
            with open(f"{BASE_DIR}/project_summary.json", "w", encoding="utf-8") as f:
                f.write(json_str)
            # 将服务器json配置文件同步更新到服务器储存
            project_summary_update()
            await set_review_pass(button_group, p_name, v)
        else:
            ui.notify(
                "项目简介与客户简称必须填写!",
                type="warning",
                position="center",
                timeout=0,
                progress=False,
                close_button="✖",
            )

    # 判断是否为新建临时项目，分类处理
    async def set_temporary_project_dialog(button_group, p_name, v):
        # 临时项目且为新建项目
        if "RFTS" in p_name and p_name not in app.storage.general["project_summary"]:
            dialog.clear()
            with dialog, ui.card().classes("w-1/3"):
                pro_data = {"notes": "", "introduction": "", "customer": ""}
                ui.label("输入如下项目必要信息：")
                input_notes = (
                    ui.textarea(
                        label="项目备注",
                        placeholder="选填，填写特殊备注信息",
                    )
                    .props("outlined stack-label autogrow")
                    .classes("w-full text-[14px]/[16px] w-full")
                )
                input_notes.bind_value(pro_data, "notes")
                input_introduction = (
                    ui.textarea(
                        label="项目简介",
                        placeholder="必填，填写简介，比如功能需求，定制要点等信息",
                        validation={"不能空白": lambda value: value.strip() != ""},
                    )
                    .props("outlined stack-label autogrow")
                    .classes("w-full text-[14px]/[16px] w-full")
                )
                input_introduction.bind_value(pro_data, "introduction")
                input_customer = (
                    ui.textarea(
                        label="项目客户",
                        placeholder="必填，填写客户简称，注意查重",
                        validation={"不能空白": lambda value: value.strip() != ""},
                    )
                    .props("outlined stack-label autogrow")
                    .classes("w-full text-[14px]/[16px] w-full")
                )
                input_customer.bind_value(pro_data, "customer")
                with ui.row().classes("w-full justify-end"):
                    ui.button(
                        "确认",
                        color="red-5",
                        on_click=lambda bg=button_group,
                        pn=p_name,
                        ver=v,
                        data=pro_data: set_temporary_project_review_pass(bg, pn, ver, data),
                    )
                    ui.button("取消", on_click=lambda: dialog.close())
            dialog.open()
        # 正式项目 或 非新建临时项目，直接处理
        else:
            await set_review_pass(button_group, p_name, v)

    # 提醒需求提交人发生变化
    async def set_review_pass_dialog(button_group, p_name, v):
        if app.storage.general["wait_review"][p_name][v]["state"] == "待审":
            old_v = "1.0" if v == "1.0" else f"{int(float(v)) - 1}.0"
            new_submitter = app.storage.general["wait_review"][p_name].get(v, {}).get("submitter")
            old_submitter = app.storage.general["wait_review"][p_name].get(old_v, {}).get("submitter")
            # 需求提交人新版发生了变化，弹窗提醒
            if new_submitter != old_submitter:
                dialog.clear()
                with dialog, ui.card():
                    ui.label(f"需求提交人有变：{old_submitter} ——> {new_submitter}，是否继续通过审核？")
                    with ui.row().classes("w-full justify-end"):
                        ui.button(
                            "确认",
                            color="red-5",
                            on_click=lambda bg=button_group, pn=p_name, ver=v: set_temporary_project_dialog(
                                bg, pn, ver
                            ),
                        )
                        ui.button("取消", on_click=lambda: dialog.close())
                dialog.open()
            # 需求提交人没有变化，直接调用下一步处理函数
            else:
                await set_temporary_project_dialog(button_group, p_name, v)
        # 待修改等非待审状态则更新按钮组
        else:
            ui.notify(
                "需求非待审状态，不能通过审核，状态以刷新!",
                type="info",
                position="center",
                timeout=1000,
                progress=True,
                close_button="✖",
            )
            get_review_button(button_group, p_name, v)

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
                        on_click=lambda bg=button_group, pn=project_name, v=ver: set_review_pass_dialog(bg, pn, v),
                    ).props("")
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
            ui.image(current_display_path)
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.menu_item("关闭菜单", menu.close)
    with ui.row():
        if current_role in module_show_data["wait_review_module"]:
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
        if current_role in module_show_data["overview_charge_pending_module"]:
            with ui.card().classes("gap-2 p-2"):
                ui.label("待判断概述的项目：").classes("text-base")
                for user, project_list in app.storage.general["overview_charge_pending"].items():
                    if user == current_user:
                        for project_name in project_list:
                            ui.button(
                                f"点击更新{project_name}概述负责内容",
                                on_click=lambda pn=project_name: get_overviow_page(pn, False),
                            ).props("outline").on(
                                "click",
                                lambda pn=project_name, us=user: app.storage.general["overview_charge_pending"][
                                    us
                                ].remove(pn),
                            )
