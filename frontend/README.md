# 管理后台

Vue 3 + TypeScript + Vite 管理后台。认证请求使用 `/api/auth`，管理请求使用 `/api/v1`；开发服务器默认将 `/api` 代理到 `http://127.0.0.1:8000`。

当前已提供：Owner Bootstrap 与登录、总览、GeWe Connection、微信账号、联系人、已发现群与群成员、消息 Trace、Outbox、插件 Deployment/Revision、运行 ACL、后台用户与角色，以及 Task Agent 工作台。MaiBot 专项诊断、模型执行、Tool 调用与通用审批页面尚未实现。

```powershell
pnpm install
pnpm dev
pnpm test
pnpm build
```

如需修改开发代理目标，复制 `.env.example` 为 `.env.local` 后调整 `VITE_API_PROXY_TARGET`。

后台使用 HttpOnly Session Cookie 和 Session 绑定的 CSRF Token，不会把会话、密码或 Bootstrap Token 写入浏览器本地存储。

插件页当前只允许安装仓库内的 `builtin.echo`、`builtin.weather` 和 `builtin.maibot-connector`，并支持创建 Deployment/Revision、激活、停用和回滚；任意插件包上传、卸载和公共插件市场不在当前实现范围。

Task Agent 工作台当前支持：

- 自动从 `/api/v1/task-agent/context` 读取唯一 Workspace，不允许在前端手工切换或冒充其他 Workspace。
- 按 `agent.read`、`agent.write`、`agent.run`、`agent.question.override` 分离查看、定义/版本发布、Session/Run 控制和管理员代答权限。
- 创建 Definition、发布不可变 Version、创建 Session/Run、查看状态与合法转换、创建待答问题。
- 展示最近的 Event、Inbox 和 Question；后端默认分别返回最近 100 条，发生截断时页面明确提示，不把“只显示最近记录”误写成完整历史。
- 管理员代答必须同时填写回答和原因。页面不提供普通用户 `/answer` 操作，代答者由当前登录账号映射并写入审计，不能通过表单伪造回答者。

该工作台目前是持久状态与人工控制界面，不会实际调用模型或插件 Tool，也不代表 Task Agent 多步骤执行闭环已经完成。

前端测试使用 Vitest；生产构建同时执行 TypeScript/Vue 类型检查：

```powershell
pnpm test
pnpm build
```
