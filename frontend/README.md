# 管理后台

Vue 3 + TypeScript + Vite 管理后台。

当前页面包括：Owner Bootstrap、登录、总览、GeWe Connection、微信账号、联系人、已发现群与群成员、消息 Trace、Outbox、插件 Deployment/Revision、运行 ACL、后台用户与角色，以及 Task Agent 持久控制台。

开发运行：

```powershell
pnpm install --frozen-lockfile
pnpm dev
```

验证：

```powershell
pnpm test
pnpm build
```

Task Agent 页面目前只管理持久状态，不会触发真实模型执行。生产可用性仍需要补齐浏览器 E2E、错误诊断、告警和部署验证。
