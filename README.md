# GeWe 微信机器人管理与 Agent 平台

基于 GeWe 的单组织私有化微信机器人管理系统。GeWe 负责连接微信，本系统负责账号、Webhook、通讯录、消息持久化、可靠发送、普通插件、群/成员级权限和审计；Task Agent 作为后续阶段的多步骤任务执行能力，不参与普通插件命令链路。

## 当前状态

- M1 工程、认证、RBAC、CSRF、Secret 和数据库迁移底座已实现。
- M2 Connection、账号、Webhook、通讯录、消息 Trace 和可靠 Outbox 已实现。
- 2026-09-01 已通过真实微信完成 `/echo` 文本收发闭环，证明 GeWe 回调、插件分发、Outbox 和回发主路径可用。
- M3 独立插件 Runner、Deployment/Revision 热启停与回滚、群/成员 ACL、Echo 和天气插件已实现。
- Task Agent 已完成持久数据模型、Run 状态机、幂等、等待用户回答、管理 API 和后台工作台；模型 Worker、审批、预算及微信任务入口尚未实现。

真实 `/echo` 成功不等于全部生产验收完成。群聊、两个账号路由、重复回调、超时未知、服务重启、PostgreSQL 多 Worker、备份恢复和告警仍需逐项验证。完整进度见[《当前实现状态与验收边界》](docs/当前实现状态与验收边界.md)。

## 核心边界

- 默认由管理员在 GeWe 后台手动保存平台生成的回调地址。只有明确切换为平台代管并点击应用后，系统才调用 GeWe `setCallback`。
- 插件不持有 GeWe Token，只返回受控 Action；最终发送必须经过 ACL、Deployment grant、Outbox 和发送前重新鉴权。
- Webhook 先持久化并快速响应，再由后台 Worker 分发。
- Outbox 对同一微信账号严格串行发送；超时且结果未知时进入 `UNKNOWN`，不自动重复发送。
- MVP 在数据库、API 和启动检查三层强制单工作区；多工作区属于后续版本。
- Task Agent 是复杂任务执行器，不替代普通命令插件；在自动 Worker 完成前只视为持久控制面。

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

Staging/Production 强制使用 PostgreSQL、有效的 `WECHAT_BOT_MASTER_KEY` 和 HTTPS `WECHAT_BOT_PUBLIC_BASE_URL`。SQLite 只用于快速开发和单元测试。

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

- `.env`、GeWe/模型凭据、本地密钥、数据库文件和服务器登录说明不得进入 Git。
- GeWe Token 加密存储，管理 API 与日志不回显明文。
- 插件独立进程用于故障隔离，不表示可以安全运行任意恶意代码；当前只安装仓库内审核过的内置插件。
- Webhook 高熵路径不能替代上游签名；公网部署仍需要 TLS、反向代理限流和未知账号隔离。
- 真实文本基础闭环已经通过，但生产 PostgreSQL、公网故障演练、备份恢复和完整 UAT 尚未完成。
