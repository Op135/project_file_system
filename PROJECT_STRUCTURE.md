# 项目结构说明

本文档描述当前部署结构和主要目录边界。部署前只进行了不改变运行时导入路径的低风险整理；
核心 Python 模块及根目录业务 JSON 暂不移动。

## 根目录

| 路径 | 用途 | 部署说明 |
| --- | --- | --- |
| `src/` | 应用代码、公共服务、页面和分析工具 | 必须部署 |
| `tests/` | 自动化回归测试 | 建议随代码保留，部署包可按运维策略排除 |
| `scripts/` | 一次性迁移、修复和离线转换工具 | 建议部署，执行前阅读脚本说明 |
| `img/` | 页面图标、Logo 和预设头像 | 必须部署 |
| `data/` | 旧用户工作簿及少量静态数据 | 服务器 `users.xlsx` 是服务器自己的数据，不能被本地文件覆盖 |
| `db/` | SQLite 运行数据库 | 环境数据，不进入 Git，不可用本地数据库覆盖服务器数据库 |
| `backups/` | 自动备份、用户迁移备份和配置导入前备份 | 环境数据，不进入 Git |
| `files/`、`over/`、`req/`、`uploads/` | 业务附件和上传内容 | 环境数据，部署时保留服务器原目录 |
| `logs/` | 运行日志 | 环境数据，不进入 Git |
| `.nicegui/` | NiceGUI 本地存储文件 | 环境数据，部署时保留服务器原目录 |
| `.overview_*_staging/` | 概述批量操作临时目录 | 临时数据，不进入 Git |

根目录的 `*_config.json`、`overview_config.json`、`tools_permission.json` 等文件仍有运行时读取路径。
其中既包含有效业务参数，也包含旧 Excel 模式兼容配置，现阶段不能集中移动或删除。

## `src/` 主要边界

| 文件或目录 | 作用 |
| --- | --- |
| `main.py` | 应用启动、数据库初始化、后台任务和页面装配 |
| `db_storage.py` | 通用业务 JSON/实体的异步 SQLite 存储 |
| `identity_store.py` | 用户、组织、岗位、权限、流程和具体待办的同步 SQLite 数据层 |
| `user_service.py` | 旧 Excel 与身份数据库之间的统一服务门面 |
| `legacy_compatibility.py` | 旧身份、角色授权、通知和审批路由实际命中的统一限频日志 |
| `identity_config_transfer.py` | 跨环境配置包导出、预检、备份和事务导入 |
| `permission_catalog.py` | 全系统稳定权限编码目录 |
| `access_control.py` 及 `*_access.py` | 公共和各业务模块权限判断 |
| `approval_workflow.py` | 通用审批流程匹配、具体审批人解析和多节点推进 |
| `notification_recipients.py` | 固定通知权限到企业微信收件人的解析 |
| `*_config.py` | 对应根目录 JSON 的校验、默认值和业务配置读取 |
| `pages/` | NiceGUI 路由、页面组合和业务交互 |
| `tools/` | 独立分析工具的界面和计算实现 |

更详细的权限迁移状态、每个业务页面职责和历史兼容边界见 `PROJECT_CONTEXT.md`。

## `scripts/` 工具

| 文件 | 用途 |
| --- | --- |
| `migrate_users_to_iam.py` | 把当前机器的 `data/users.xlsx` 安全迁移到身份数据库，默认不覆盖已有密码 |
| `admin_fix_storage_project_states.ps1` | 管理员按脚本说明修复特定项目状态数据 |
| `convert_excel_to_json.py` | 通用 Excel 转 JSON 离线工具，不被应用运行时代码引用 |

## 后续结构重构建议

服务器稳定运行后，再分批把公共身份模块收拢到 `src/iam/`，把 ECN、样品订单、异常单等拆到
`src/modules/<module>/`。每次只迁移一个模块，并保留旧导入路径的兼容转发层；不要在一次发布中
同时移动大型页面、JSON 配置和数据库逻辑。
