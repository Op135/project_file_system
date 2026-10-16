# 本地调试身份与外部副作用控制

本模块只能由根目录 `run_local_debug.py` 注册。正式入口 `src/main.py` 不导入本模块，生产部署可直接排除整个目录和本地启动脚本。

## 启动

### VSCode F5（推荐）

将根目录 `.env.local.example` 复制为 `.env.local`，填写本机调试密码和允许接收测试消息的企业微信 userid。首次在“运行和调试”下拉框选择 `Python: 本地调试 NiceGUI（身份切换与消息拦截）`，以后直接按 F5 即可。

如需验证不带开发工具的正式入口，选择 `Python: 正式入口 NiceGUI（不加载本地调试模块）`。

### PowerShell

PowerShell 示例：

```powershell
$env:DEV_TOOLS_PASSWORD = "请设置本机专用密码"
$env:DEV_WECOM_RECIPIENT_ALLOWLIST = "developer_userid|manager_userid"
$env:DEV_CARD_BASE_URL = "http://127.0.0.1:8080"
& 'D:\miniforge3\envs\python_nicegui\python.exe' run_local_debug.py
```

服务固定监听 `127.0.0.1`。启动后访问：

```text
http://127.0.0.1:8080/dev/identity-switcher
```

本地调试策略安装成功后，系统首页右下角同时显示“调试控制中心”按钮；正式入口没有该按钮。

`DEV_WECOM_RECIPIENT_ALLOWLIST` 使用企业微信 userid，以 `|`、逗号或顿号分隔。测试转发只能选择该白名单中的成员。

## 安全规则

- 默认拦截全部企业微信文本和文本卡片，不发起网络请求，也不创建失败重试。
- 测试转发会把所有原收件人替换为选定白名单，并在正文按企业微信绑定标明原收件人的真实姓名；无法匹配绑定时保留原 userid。
- 测试转发卡片的链接域名会改写为 `DEV_CARD_BASE_URL`，默认为本机地址，防止误点生产系统链接。
- 真实发送必须输入确认短语，启用 15 分钟后自动恢复为强制关闭。
- 企业微信失败重试、通讯录定时刷新及各业务提醒默认不运行；控制页开关为进程级设置。
- 调试收件人选择只写入 `.local/dev_runtime_controls.json`，不修改业务 JSON、用户密码或复制来的生产数据库。每次进程启动都强制恢复“拦截发送 + 关闭后台通知任务”。
- 四份业务配置保持服务器值：ECN 旧测试模式和样品单旧经理转发均关闭，异常及样品问题后台提醒均开启；本地是否执行统一由本页的进程级策略覆盖。
- 模块安装时同时验证开发环境标记、显式开关、解锁密码和回环监听地址。

身份切换只更新当前浏览器的 NiceGUI 用户会话，目标用户的岗位、权限、待办和审批资格仍从复制的身份数据库实时读取。正常登录页面继续保留，用于单独验证密码和账号状态。
