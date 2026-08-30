# GeWe 微信机器人管理与 Agent 平台

基于 GeWe 的单组织私有化微信机器人管理系统。GeWe 负责连接微信，本系统负责账号、Webhook、目录持久化、可靠发送、插件、群/成员级权限和审计；MaiBot 通过独立 Connector 接入，Task Agent 负责后续多步骤办事能力。

## 当前状态

项目已经获得本地开发、测试和构建授权，当前按实施基线继续推进。

- M1 工程、认证与 RBAC 底座已实现。
- M2 GeWe Connection、账号、Webhook、通讯录、消息 Trace 和可靠 Outbox 已实现本地契约链路，等待真实 GeWe 环境验收。
- M3 独立插件 Runner、Deployment/Revision 热启停与回滚、群/成员 ACL、Echo、天气和 MaiBot Connector 已实现。
- MaiBot Connector 已完成受管 WebSocket、ACK/重连/TTL/fencing、回复与主动消息权限链路；真实 MaiBot 联调和可信 Tool Bridge 尚未完成。
- Task Agent 已完成持久数据模型、Run 状态机、幂等、等待用户回答、受 RBAC 保护的管理 API 和后台工作台；模型 Worker、Tool Broker、通用审批、预算及微信/MaiBot 任务入口仍属后续阶段。

不要把“本地或 Mock 已验证”理解为“可以直接生产上线”。完整进度、验收清单和外部联调边界见[《当前实现状态与验收边界》](docs/当前实现状态与验收边界.md)。

项目基线：

- [甲方需求确认与开发基线](docs/甲方需求确认与开发基线.md)
- [项目需求与总体设计说明书](docs/微信机器人管理与Agent平台-项目需求与总体设计说明书.md)
- GeWe API 本地快照位于 `docs/gewe-api/`，仅作为甲方提供的本机参考资料，不进入 Git。

## 核心边界

- 默认由甲方在 GeWe 后台手动保存平台生成的回调地址。只有管理员明确切换为平台代管并点击应用后，系统才调用 GeWe `setCallback`；启动、扫码和重连不会自动覆盖回调。
- 插件和外部 Agent 不持有 GeWe Token，只能提出受控 Action；最终发送必须经过 ACL、Deployment grant、Outbox 和发送前重新鉴权。
- MaiBot 独立运行并负责拟人、记忆和群友画像。本系统只负责 Connector、数据转发权限、回复/主动消息权限和审计。
- Task Agent 是复杂任务执行器，不替代 MaiBot 参与普通群聊。
- Task Agent 管理写操作不能由请求体冒充发布者或任务发起人，后端会把当前登录管理员映射为运行时 `ADMIN_USER Principal`。管理员代答使用独立的 `agent.question.override` 权限，必须填写原因并保留审计；这不等同于普通用户本人回答。
- MVP 在数据库、API 和启动检查三层强制单工作区；后台 RBAC 在这个唯一工作区内全局生效。多工作区和资源级后台角色绑定属于后续独立版本，当前不宣称具备跨工作区隔离。

## 本地运行

要求 Python 3.12、`uv`、Node.js 和 `pnpm 11`。未创建根目录 `.env` 时，development 默认使用 SQLite，并自动在 `.data/master.key` 创建本地开发密钥。

后端：

```powershell
cd backend
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn wechat_bot.main:app --reload
```

前端另开终端：

```powershell
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

默认管理后台为 `http://127.0.0.1:5173`，本地 OpenAPI 为 `http://127.0.0.1:8000/api/docs`。

首次创建 Owner 前需临时设置不少于 32 字符的 `WECHAT_BOT_AUTH_BOOTSTRAP_TOKEN`，通过 `/bootstrap` 创建后立即移除并重启后端。

## PostgreSQL 运行

生产和并发语义验收使用 PostgreSQL 16+：

```powershell
Copy-Item .env.example .env
# 修改 .env 中的数据库密码、Fernet 主密钥、Bootstrap Token 和公网地址
docker compose up -d postgres
cd backend
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn wechat_bot.main:app
```

Staging/Production 强制使用 PostgreSQL、有效的 `WECHAT_BOT_MASTER_KEY` 和 HTTPS `WECHAT_BOT_PUBLIC_BASE_URL`。SQLite 仅用于快速开发和单元测试，不能证明 PostgreSQL 行锁和多 Worker 并发语义。

## 验证

```powershell
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov --cov-report=term-missing

cd ..\frontend
pnpm test
pnpm build
```

## 安全说明

- `.env`、GeWe/MaiBot/模型凭据、本地密钥、数据库文件和服务器登录说明不得进入 Git。
- GeWe Token 和 Connector API key 加密存储，管理 API 与日志不回显明文。
- 插件独立进程用于故障隔离，不表示可以安全运行任意恶意代码；当前只安装仓库内审核过的内置插件。
- 真实 GeWe、MaiBot、模型 Provider、公网回调和生产 PostgreSQL 尚未完成联合验收，对应能力只能标注“本地或 Mock 验证完成”。
