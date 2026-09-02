# 后端

基于 FastAPI 的 GeWe 微信机器人管理平台后端。当前包含认证与 RBAC、Connection/账号/Webhook、通讯录持久化、消息 Trace、可靠 Outbox、独立插件 Runner、群/成员运行 ACL，以及 Task Agent 持久状态与管理 API。

## 运行链路

```text
GeWe Webhook
  -> 原始回调与标准消息持久化
  -> Event Worker
  -> ACL 与插件 Revision 校验
  -> 独立插件 Runner
  -> 受控 reply.text Action
  -> Outbox
  -> 发送前重新鉴权
  -> GeWe API
```

2026-09-01 已通过真实微信 `/echo` 命令验证上述文本主链路。其余真实场景和故障语义仍按验收清单推进。

## Task Agent 管理 API

管理端路由前缀为 `/api/v1/task-agent`。当前提供 Definition、Version、Session、Run、Inbox、Event 和 Pending Question 的持久控制面。

权限包括：

| 权限 | 用途 |
| --- | --- |
| `agent.read` | 读取 Definition、Version、Session、Run 和历史 |
| `agent.write` | 创建 Definition、发布不可变 Version |
| `agent.run` | 创建 Session/Run、执行合法状态转换、创建问题 |
| `agent.question.override` | 管理员代答待答问题，必须提交原因 |

当前没有模型 Worker、通用审批、预算/成本、检查点执行和微信任务入口，因此不能宣称已经形成自动办事闭环。

## 数据库边界

生产数据库要求 PostgreSQL 16+。SQLite 只用于本地单元测试和开发 Smoke，不能证明 PostgreSQL 行锁、多 Worker 并发和租约语义。

历史版本曾包含已经移除的外部聊天桥接数据。迁移 `c3a9f1e2d4b6` 会永久删除其专用状态和调用账本，且不可降级恢复；应用前应先完成数据库备份。

## 质量门禁

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov --cov-report=term-missing
```
