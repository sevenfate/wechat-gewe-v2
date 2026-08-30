# 后端

基于 FastAPI 的 GeWe 微信机器人管理平台后端。当前包含认证与 RBAC、GeWe Connection/账号/Webhook、目录持久化、消息 Trace、可靠 Outbox、插件 Runner、群/成员运行 ACL、MaiBot Connector，以及 Task Agent 持久状态与管理 API。

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

当前尚无模型 Worker、Tool Broker、通用审批、预算/成本、检查点执行或微信/MaiBot 任务入口，因此这些 API 只构成持久状态和人工控制面，不能宣称已完成 Agent 自动办事闭环。

## 数据库边界

生产数据库要求 PostgreSQL 16+。SQLite 只用于本地单元测试和开发 Smoke，不能证明 PostgreSQL 行锁、多 Worker 并发和 fencing 语义。真实 GeWe、微信、MaiBot、模型 Provider 及生产 PostgreSQL 尚未完成联合验收。
