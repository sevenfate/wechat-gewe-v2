# 微信机器人管理与 Agent 平台：项目需求与总体设计说明书

| 文档项 | 内容 |
| --- | --- |
| 版本 | 2.0 |
| 日期 | 2026-09-02 |
| 架构方向 | Python 模块化核心、独立插件运行时、持久 Outbox、可选 Task Agent Runtime |
| 部署形态 | 单组织私有部署，MVP 单工作区 |

## 1. 目标与原则

平台通过 GeWe 管理微信账号和消息，在核心内部统一控制身份、权限、插件生命周期、可靠发送和审计。

设计原则：

1. **平台持有最终发送权**：插件只提交结构化 Action。
2. **先持久化再处理**：Webhook 快速落库，由 Worker 异步分发。
3. **发送前重新鉴权**：排队时允许不代表实际发送时仍允许。
4. **故障隔离**：普通插件运行在独立进程，核心不执行其业务代码。
5. **可追踪**：一条消息贯穿 Inbox、Event、ACL、插件、Outbox 和审计。
6. **严格说明证据**：代码完成、Mock 验证、真实联调和生产验收分别记录。

## 2. 系统范围

### 2.1 当前核心

- GeWe Connection 和加密凭据。
- 微信账号扫码、登录状态、在线检查和重连。
- 高熵 Webhook 路径、体积限制、原始报文与标准消息。
- 联系人、群和群成员持久化。
- 普通插件 Catalog、Deployment、Revision、Runner 和热切换。
- 账号、会话、群成员、插件、命令和 Tool ACL。
- 持久 Outbox、限速、租约、重试、未知状态和人工对账。
- 管理后台、RBAC、Trace 和审计。

### 2.2 后续能力

- Task Agent 模型 Worker、检查点、审批、预算和微信入口。
- 浏览器 E2E、告警、备份恢复和数据生命周期任务。
- 图片、语音、视频和文件等媒体消息。
- 插件签名、灰度、资源限制和网络白名单。

## 3. 总体架构

```text
                    +-----------------------+
微信 <-> GeWe       |  FastAPI Core         |
                    |                       |
Webhook ----------> |  Webhook Inbox        |
                    |       |               |
                    |  Normalized Event     |
                    |       |               |
                    |  Event Worker         |
                    |       |               |
                    |  ACL + Deployment     |
                    |       |               |
                    |  Plugin Supervisor ---+--> 独立 Plugin Runner
                    |       |               |
                    |  Structured Action    |
                    |       |               |
                    |  Persistent Outbox    |
                    |       |               |
                    |  Sender + Recheck ----+--> GeWe API -> 微信
                    +-----------------------+
                              |
                           PostgreSQL

Vue 管理后台 <-> 认证与 `/api/v1` 管理 API
Task Agent 控制面 -> 后续独立 Worker
```

## 4. 核心领域

### 4.1 Connection 与账号

`GeweConnection` 保存 API 地址、加密 Token、回调密钥、管理模式和健康状态。API 只返回 Token 指纹。

`BotAccount` 归属一个 Connection，以 `app_id` 唯一标识，保存扫码状态、微信身份、在线状态和最近错误。

回调默认采用手动管理：

1. 平台生成高熵回调地址。
2. 管理员复制到 GeWe 后台保存。
3. 收到业务回调后记录最近成功时间。
4. 只有切换到 `PLATFORM_MANAGED` 并显式执行时才调用 `setCallback`。

### 4.2 Webhook 与标准事件

Webhook 处理顺序：

1. 限制请求体大小并解析 JSON。
2. 通过回调 Secret 定位 Connection。
3. 归一化 v1/v2 字段。
4. 计算 payload SHA-256 和去重键。
5. 保存 `WebhookInbox` 与 `NormalizedEvent`。
6. 在 GeWe 三秒限制内返回空响应。

有 `newMsgId` 时去重键为 `appid + newMsgId`；否则使用报文哈希。相同键不同内容返回冲突，不静默覆盖。

### 4.3 通讯录

- `Contact`：账号联系人和必要展示信息。
- `Chatroom`：已发现群，支持收到首条群消息时创建占位记录。
- `ChatroomMembership`：群成员及 membership epoch。

成员离群后旧 epoch 不再拥有权限；重新入群会生成新 epoch，防止历史授权自动恢复。

### 4.4 插件系统

插件 Manifest 声明：

- 稳定插件 ID、版本和入口点。
- 订阅事件和命令。
- 可调用 Tool 及 JSON Schema。
- 所需 capability、超时和配置 Schema。

每次配置变更产生不可变 Revision。激活使用两阶段切换：先启动候选 Runner 并完成健康检查，再提交数据库和运行时状态。activation epoch 保证旧实例迟到结果失效。

当前只允许安装仓库内置 Echo 和天气插件。独立进程实现故障隔离，但不是恶意代码沙箱。

### 4.5 权限模型

运行时权限由以下维度共同决定：

```text
Workspace
  + Bot Account
  + Private Contact 或 Chatroom
  + Group Member Epoch（群聊时）
  + Plugin / Command / Tool / Capability
  + Deployment Scope 与 Revision Grant
```

规则效果为 `ALLOW`、`ASK`、`DENY`；拒绝优先，无匹配默认拒绝。每次决策记录命中规则和原因，前端可执行权限解释查询。

### 4.6 Outbox

插件回复先写入 `OutboxMessage`，使用幂等键避免重复 Action。Sender 按账号领取消息并持有账号锁：

- 同一账号严格串行。
- 不同账号可并发。
- 连接失败等确定可重试错误采用指数退避。
- 读取超时、协议异常等无法判断服务端结果的错误进入 `UNKNOWN`。
- `UNKNOWN` 不自动重发，必须人工对账。
- 发送前验证账号、Connection、目标、Revision grant 和最新 ACL。

### 4.7 Task Agent

现有数据模型：

- Definition 与不可变 Version。
- Session、Run 和单活动 Run 约束。
- Session Inbox、Event 和 Pending Question。
- 幂等创建、合法状态转换和管理员代答审计。

当前没有自动 Worker。后续 Worker 必须：

- 使用平台绑定的 Requester 身份。
- 将模型输出视为不可信输入。
- 只能调用注册且获权的结构化 Tool。
- 对高风险动作创建审批并在执行前再次鉴权。
- 执行预算、次数、时间和输出大小限制。
- 通过持久检查点实现重启后至多继续一次。

## 5. 安全设计

### 5.1 身份与后台权限

- 首位 Owner 通过一次性 Bootstrap Token 创建。
- 登录使用 HttpOnly Session Cookie 和独立 CSRF Token。
- 密码使用 Argon2 哈希。
- 后台 API 在服务端校验权限，前端隐藏菜单不是安全边界。
- Version 发布者、Session 请求者和管理员代答者均从登录会话映射，不能由请求体冒充。

### 5.2 Secret

- GeWe Token 和插件敏感配置使用 Fernet 加密。
- API、日志和审计不得输出明文 Secret。
- Staging/Production 必须显式提供主密钥。
- `.env`、数据库和密钥文件不进入 Git。

### 5.3 Webhook

- 使用高熵路径、TLS、请求体限制和反向代理限流。
- 未知 Secret 返回 404。
- 没有可靠上游签名时，不宣称具备密码学来源认证。

## 6. 数据与迁移

- MVP 只允许一个 Workspace，数据库唯一约束和启动检查同时执行。
- 生产使用 PostgreSQL 16+；SQLite 只用于开发和单元测试。
- 所有结构变更通过 Alembic。
- 包含数据删除的不可逆迁移必须在执行前备份并在发布说明中显式标注。
- 原始回调、消息、审计和任务数据的保留期由上线前的数据治理决策确定。

## 7. API 分组

| 路由 | 用途 |
| --- | --- |
| `/api/auth` | Bootstrap、CSRF、登录、当前用户和退出 |
| `/api/v1/admin` | 后台用户、角色和权限 |
| `/api/v1/connections` | GeWe Connection |
| `/api/v1/bot-accounts` | 微信账号和登录操作 |
| `/webhooks/gewe/{secret}` | GeWe 回调入口 |
| `/api/v1/directory` | 联系人、群和群成员 |
| `/api/v1/messages` | 标准消息、原始报文和 Trace |
| `/api/v1/outbox` | 发送状态、取消和人工对账 |
| `/api/v1/plugins` | 插件、Deployment、Revision 和运行状态 |
| `/api/v1/policy` | ACL、权限解释和规则管理 |
| `/api/v1/task-agent` | Task Agent 持久控制面 |

## 8. 测试策略

### 8.1 本地自动化

- Ruff、格式检查和 mypy strict。
- 服务层、API、状态机、权限和错误分类单元测试。
- SQLite 集成 Smoke。
- Vue 组件测试、TypeScript 检查和生产构建。

### 8.2 PostgreSQL

- 真实执行 Alembic upgrade 和 schema check。
- 并发 Webhook 去重。
- 多 Event Worker 使用 `SKIP LOCKED` 只领取一次。
- 多 Sender Worker 对同一账号严格串行且不重复发送。
- 租约过期恢复和权限 fencing。

### 8.3 真实微信

- 私聊和群聊文本、`@`、引用和多个账号路由。
- 重复回调、异常报文、掉线和重连。
- Echo、天气、默认拒绝、成员允许和撤权。
- 发送成功、确定失败、结果未知和人工对账。
- 重启后恢复且不重复执行。

## 9. 当前证据与发布门禁

2026-09-01 已完成真实 `/echo` 文本闭环，确认基础回调、分发、插件、Outbox 和回发可用。该结果是主链路里程碑，不替代完整 UAT。

发布到正式账号前必须满足：

1. PostgreSQL 多 Worker 测试通过。
2. 真实群聊、两个账号、权限与故障场景通过。
3. 备份恢复演练通过。
4. 公网 HTTPS、限流、健康监控和告警生效。
5. 数据保留期、运维责任和账号风险得到确认。
6. 全量自动化质量门禁通过且工作区干净。
