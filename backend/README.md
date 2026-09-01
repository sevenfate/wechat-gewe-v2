# 后端

基于 FastAPI 的 GeWe 微信机器人管理平台后端。当前包含认证与 RBAC、GeWe Connection/账号/Webhook、目录持久化、消息 Trace、可靠 Outbox、插件 Runner、群/成员运行 ACL、MaiBot Connector、受限 Tool Broker/Tool Call ledger，以及 Task Agent 持久状态与管理 API。

## Task Agent 管理 API

管理端路由前缀为 `/api/v1/task-agent`，所有接口要求已登录；写接口同时要求 Session 绑定的 CSRF Token。权限拆分如下：

| 权限 | 当前用途 |
| --- | --- |
| `agent.read` | 读取唯一 Workspace Context、Definition、Version、Session、Run 和最近状态历史 |
| `agent.write` | 创建 Definition、发布不可变 Version |
| `agent.run` | 创建 Session/Run、执行合法状态转换、创建待答问题 |
| `agent.question.override` | 由管理员代答待答问题；必须提交原因 |

安全边界：

- `GET /api/v1/task-agent/context` 返回本版本唯一 Workspace，Task Agent 页面不依赖 Connection 读取权限。
- Version 发布者和 Session 请求者由当前登录管理员映射为运行时 `ADMIN_USER Principal`；请求体不能指定或冒充 Publisher/Requester。
- 普通管理端 `/questions/{id}/answer` 不存在。管理员只能调用 `/questions/{id}/override-answer`，后端记录真实管理员、`ADMIN_OVERRIDE` 模式和必填原因，并把等待中的 Run 重新排入 `QUEUED`。
- 状态变更按 Workspace 限定并对相关记录加锁；终态 Run 不允许恢复，旧 ORM 状态不能覆盖数据库中的新状态。
- Session 状态接口的 `history_limit` 为 `1..200`，默认 100；Inbox、Event、Question 各自返回是否还有更早记录的 `*_has_more` 标志。
- 持久 JSON 最大 64 KiB，并拒绝 `analysis`、`thinking`、`reasoning*`、`chainOfThought` 等私有推理字段；读取旧数据时也会递归过滤这些字段。这是字段级安全边界，不替代 Secret/DLP 检查。
- Task Agent 管理写操作的成功及领域失败均进入审计日志。

当前尚无模型 Worker、Task Agent 调用 Tool Broker 的 Worker 入口、通用审批、预算/成本、检查点执行或微信/MaiBot 任务入口。因此 Task Agent API 仍只构成持久状态和人工控制面，不能宣称已完成 Agent 自动办事闭环；Tool Broker 本身已实现，但目前只由 MaiBot Connector 的受限适配器使用。

## Tool Bridge

Tool Broker 是 MaiBot 与未来 Task Agent Worker 共用的单一工具执行边界。应用启动时创建 `ToolBrokerService`，并将它注入 `MaiBotManagedRuntime`；每次调用都写入 `tool_call` ledger，结果和失败原因可审计、可查询。

当前 MaiBot 运行链路：

- Managed MaiBot WebSocket 接收 `custom_wechat_bot_tool_call` 和 `custom_wechat_bot_tool_catalog_request`（同时兼容旧的 `sys_tool_call` 输入）帧，并以对应的 `custom_wechat_bot_tool_result`、`custom_wechat_bot_tool_catalog_response` 回传。协议适配器先校验 frame 大小、字段和版本，再绑定当前 Deployment、Revision 和 activation epoch。
- Broker 只接受平台签发的加密 opaque conversation context，并从已发送的来源消息恢复账号、会话、真实成员、Trace 和原始事件；伪造、过期、跨群或旧 epoch 上下文会被拒绝。
- 执行前同时检查 Connector `tool_allowlist`、目标插件的 active Deployment/Revision、Manifest capability grant、群/成员 ACL、工具 `effect_class`、输入 Schema、截止时间和幂等键。MVP 只执行 `READ_ONLY`，`AUTONOMOUS` 调用直接拒绝。
- Runner 通过 `invoke_tool` 原语执行；输出必须符合声明的 JSON Schema 和大小限制。旧激活返回、运行时失败、可重试失败、最终失败、拒绝和取消都会写入 ledger 与审计，结果再封装回 MaiBot。

管理端只读接口（均要求登录、管理请求和 `tool.read` 权限）：

| 接口 | 用途 |
| --- | --- |
| `GET /api/v1/tool-bridge/catalog` | 查看当前 active 插件声明的 Tool；仅用于管理检查，不等于运行时授权 |
| `GET /api/v1/tool-bridge/calls` | 分页读取当前 Workspace 的 Tool Call ledger |
| `GET /api/v1/tool-bridge/calls/{id}` | 查看单次调用的状态、参数摘要、结果或错误 |

这里的 v1 是项目内部 MaiBot Connector 协议，不代表已支持通用 MCP、任意远程 Tool、宿主 Shell 或高风险写操作。当前 custom 帧命名遵循 `maim-message` 0.6.x/0.7.x 的 `custom_` 路由规则；真实 MaiBot 版本/协议和模型联调、Task Agent Worker 接入、审批、预算、成本与检查点仍未完成。

## 数据库边界

生产数据库要求 PostgreSQL 16+。SQLite 只用于本地单元测试和开发 Smoke，不能证明 PostgreSQL 行锁、多 Worker 并发和 fencing 语义。真实 GeWe、微信、MaiBot、模型 Provider 及生产 PostgreSQL 尚未完成联合验收。
