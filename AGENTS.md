# 开发约束

先阅读 `PROJECT_CONTEXT.md` 和 `PROJECT_STRUCTURE.md` 中与本次修改有关的内容。

## 类型检查与回归

- 保持 `src/`、`tests/` 的 Pyright/Pylance 零错误基线。完成修改后运行真实 Pyright CLI 与相关回归测试；不能只检查 Python 语法或 Ruff 后就宣称验证完成。
- 访问可能为 `None` 的对象属性或下标前，使用 `is not None`、`isinstance` 或提前返回显式收窄。`result.ok`、自定义布尔判定函数和 `unittest.assertTrue/assertIsNotNone` 不保证静态检查器能够收窄另一个字段。
- 对 `ECNResult`，成功分支同时确认 `result.record is not None`，再读取字段；测试中可用 `assert result.record is not None` 明确验证并收窄。
- 条件分支创建、后续分支使用的局部变量须先初始化，或重组为同一分支内完成使用，避免 `reportPossiblyUnboundVariable`。
- 不用 `type: ignore`、降低检查级别或删除断言来掩盖这些问题。
- 清理未使用导入时保留页面注册、插件初始化等副作用导入；可用显式同名导出表达用途，不能让自动修复删除路由注册入口。
- 如果检查工具报告标准库 `builtins` 等无法解析，先修复解释器/类型定义路径，再判断项目诊断；不能把环境异常产生的结果当作有效检查结论。

## ECN 协作与流程

- `/ecn_management` 的页面入口保留在 `src/pages/ecn_management.py`；功能拆分在 `src/modules/ecn/`，职责见该目录 README。
- ECN 未正式部署，不为本地测试记录新增历史字段迁移分支。
- 编号只能在首次保存的数据库事务中分配。更新不存在的记录必须拒绝，不得从旧页面恢复已删除单据。
- 多人编辑只合并实际变化的字段；同字段冲突明确拒绝。方案编辑使用打开编辑窗口时的原始快照，不能用保存时已被轮询刷新过的快照替代。
- 权限、当前阶段、审批轮次、参与人确认和覆盖率均在最新单据上校验。页面按钮的禁用状态不能代替后端校验。
- 审批单据和具体待办共用一个 SQLite 事务，失败必须一起回滚；不得在持有异步数据库写事务时调用另一个同步连接执行待办写操作。
- 并发测试使用临时数据库和独立连接，不操作实际运行数据库。
