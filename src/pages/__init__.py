# -*- encoding: utf-8 -*-
# 依次导入所有页面模块
# 这将确保 @ui.page 装饰器被执行，从而注册路由
# 使用相对导入
from . import information, login, main_dashboard, manage, project_table, requirement, tool, user_profile
