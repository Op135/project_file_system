# ECN 模块职责

入口与 URL 保持 `src/pages/ecn_management.py`、`/ecn_management`。根目录业务 JSON、权限模块和通用审批接口不搬迁。

| 文件 | 职责 |
| --- | --- |
| `models.py` | 单据模板、初始信息、编号算法、审批日志辅助 |
| `repository.py` | 事务内创建/更新、唯一编号分配、保存结果、刷新版本戳 |
| `editing.py` | 字段三方合并、编辑冲突、轮询保留未提交内容、方案作者与确认校验 |
| `actions.py` | 草稿、提交、撤回、作废、评审、协作编辑的应用服务 |
| `approval_transaction.py` | 复用通用审批引擎，缓冲待办命令并在 ECN 事务连接中落盘 |
| `detail.py` | 详情弹窗、申请与影响表单、审批记录、页面同步和服务调用 |
| `scheme_dialogs.py` | 三类方案的新增/编辑窗口，保留编辑前快照 |
| `scheme_panel.py` | 方案列表、对照表、附件访问、驳回历史与参与者操作 |
| `execution_panel.py` | 执行阶段 UI、助理/物料责任项确认、概述执行调度 |
| `overview_execution.py` | 系统内资料逐项目新增、更换、失效及执行结果 |
| `list_view.py` | 主列表列定义、单据行和进度显示 |
| `notifications.py` | 与首页角标同口径的企业微信待办检查、调试转发、去重及重试 |

## 写入约定

`actions` 的服务返回 `ECNResult`。成功后使用返回的已提交单据更新界面；失败时显示消息并保留输入，不能把 `ATOMIC_NO_UPDATE` 当作业务保存成功。

新建窗口不预占编号，首次保存使用集合级事务完成“分配编号 + 插入”。并发用户可以同时创建，编号按提交时的日期和日序号生成。

影响字段使用 `current / baseline / submitted` 三方合并。不同字段的修改保留；相同字段的不同修改报冲突。扩展项目按实际新增/移除合并。轮询只能同步未编辑字段，并保留正在输入内容的原始基线。

方案更新/删除必须匹配打开窗口时的方案快照；确认时核对本人当前方案；发起评审在事务内检查最新参与者和方案覆盖率。每次提交有独立 `approval_round`，旧轮次页面不能继续审批。

`db_storage.atomic_deep_update_transaction` 的回调持有 SQLite 写事务，只允许使用提供的连接执行关联表写入。`ApprovalTransaction` 的同步方法只记录待办命令，`flush` 才使用该连接实际写入，避免同步/异步连接相互等待，并确保失败同时回滚单据与待办。

系统内资料跨项目执行仍采用逐项持久化与幂等重试，并非所有概述变更的单一事务。

## 验证

ECN微信使用文本卡片，标题标识模块与当前待办，灰色说明标识调试转发或经理抄送；摘要按UTF-8字节限制并转义用户内容，底部查看详情进入ECN列表。`public_base_url` 为空时回退文字提醒。卡片沿用原去重指纹与ECN业务重试，升级不强制重发旧通知。`tests/test_wecom_cards.py` 验证实际请求结构、失败返回以及原文本协议不变，全部模拟网络。

企业微信配置位于根目录 `ecn_management_config.json` 的 `wecom`：默认 `enabled=true`、`test_mode=true`，只发给通讯录职务为“研发经理”的 `test_notify_targets`，正文列出原应通知人员。正式上线时将 `test_mode` 改为 `false` 并重启服务，收件人由首页实际待办判断与企业微信绑定决定；不会更改首页角标或审批权限。未匹配调试人或正式人员未绑定微信时跳过，不回退给其它人员。

正式模式默认 `cc_manager_enabled=true`，同时抄送通讯录职务为“研发经理”的人员。同一ECN的正式通知对象及待办汇总成一条观察抄送；经理本人有待办时按微信账号合并。调试模式不额外抄送，抄送解析失败不影响正式提醒。观察结束改为 `false` 并重启即可停止抄送及其失败重试；开关变化不触发正式人员重发，重新开启仍遵守经理账号原有的重复间隔。

系统启动30秒后首次检查，随后每60秒检查；同一待办每24小时最多重复提醒一次，当前节点/人员变化产生新提醒。通知状态独立保存在 `ecn_wecom_notification_state`，不修改单据；数据库中的发送占用标记避免重复执行。失败默认300秒后重新检查当前待办再重试，避免公共重试队列发送已处理任务或绕过调试转发。链接打开 ECN 列表，正文提供单号便于查询。上述时间均可由 JSON 调整，重启生效。

- `tests/test_ecn_notifications.py`：调试隔离、正式绑定、未匹配跳过、关闭开关、去重和过期待办/失败重试；外部发送全部模拟。
- `tests/test_ecn_concurrency.py`：独立连接并发创建、字段合并/冲突、过期编辑、评审竞争、真实身份库会签和事务回滚。
- `tests/test_ecn_management_config.py`：覆盖率、配置、待办、追溯、驳回和执行清单。
- `tests/test_ecn_management_records.py`：审批日志与概述审计。
- 修改存储或审批事务时同时运行 `tests/test_db_storage_entities.py`、`tests/test_approval_workflow.py` 和 `tests/test_access_control.py`。
- 完成后运行全仓 Pyright；根目录 `AGENTS.md` 记录可空对象和分支变量的检查要求。
