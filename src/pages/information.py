# -*- encoding: utf-8 -*-
import os

from nicegui import app, ui

from ..config import IMG_DIR, OVER_DIR, REQ_DIR
from ..utils import delete_file, get_overviow_page, logout


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
        ui.label("状态与图表").classes(
            "text-white text-lg absolute left-1/2 transform -translate-x-1/2"
        )  # 绝对定位居中
        with ui.avatar(size="lg").classes("cursor-pointer ml-auto -mt-3"):  # 右侧对齐
            ui.image(
                app.storage.general.get("user_preferences", {})
                .get(app.storage.user.get("current_user"), {})
                .get("avatar", f"{IMG_DIR}/avatars/avatar1.png")
            )
            with ui.menu().props("auto-close") as menu:
                ui.menu_item(f"你好, {app.storage.user.get('current_user', '匿名')}").style("white-space: nowrap;")
                ui.menu_item("注销登录", on_click=lambda: logout())
                ui.separator().props("size=1px")
                ui.menu_item("返回主界面", on_click=lambda: ui.navigate.to("/main"))
                ui.separator().props("size=1px")
                ui.menu_item("关闭菜单", menu.close)
    with ui.row():
        with ui.card().classes("gap-2 p-2"):
            ui.label("需求评审状态").classes("text-base")
            # 如果用户是审核者，显示所有待审需求
            if current_role in ["研发经理"] and app.storage.general.get("wait_review", {}):
                for project_name, ver_dic in app.storage.general["wait_review"].items():
                    for ver, dic in ver_dic.items():
                        # 如果当前项目的当前版本未审
                        if dic["state"] != "已审":
                            button_group = ui.button_group().props("outline")
                            get_review_button(button_group, project_name, ver)
            # 用户不是审核者，且存在待审数据
            elif app.storage.general.get("wait_review", {}):
                for project_name, ver_dic in app.storage.general["wait_review"].items():
                    for ver, dic in ver_dic.items():
                        if dic["state"] != "已审" and dic["submitter"] == current_user:
                            button_group = ui.button_group().props("outline")
                            get_review_button(button_group, project_name, ver)
