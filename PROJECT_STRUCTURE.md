# 项目结构说明

本文档描述当前部署结构和主要目录边界。部署前只进行了不改变运行时导入路径的低风险整理；
根目录业务 JSON 保持原路径；ECN 已在正式部署前完成模块拆分。

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
| `modules/ecn/` | ECN 数据模型、事务、协作编辑、流程服务与页面组件 |
| `tools/` | 独立分析工具的界面和计算实现 |

照度/辐照度转强度分布工具位于 `src/tools/intensity_distribution.py`，复用像素统计工具的文件读取、中心定位和 `N×N` 块平均合并；工具页与权限目录分别负责入口和 `tools.intensity_distribution.use` 授权。

更详细的权限迁移状态、每个业务页面职责和历史兼容边界见 `PROJECT_CONTEXT.md`。

## `scripts/` 工具

| 文件 | 用途 |
| --- | --- |
| `migrate_users_to_iam.py` | 把当前机器的 `data/users.xlsx` 安全迁移到身份数据库，默认不覆盖已有密码 |
| `admin_fix_storage_project_states.ps1` | 管理员按脚本说明修复特定项目状态数据 |
| `convert_excel_to_json.py` | 通用 Excel 转 JSON 离线工具，不被应用运行时代码引用 |

## 后续结构重构建议

ECN 已按用户授权在正式部署前拆分到 `src/modules/ecn/`，保留原页面路由与旧辅助函数导入。目录内 README 描述职责，根目录 AGENTS.md 记录并发与类型检查约束。

其它模块与公共身份代码仍按业务边界逐步整理；根目录业务 JSON 和数据库路径不随页面拆分移动。
