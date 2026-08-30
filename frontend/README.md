# 管理后台

Vue 3 + TypeScript + Vite 管理后台。所有业务请求使用 `/api/v1` 前缀；开发服务器默认代理到 `http://127.0.0.1:8000`。

```powershell
pnpm install
pnpm dev
pnpm build
```

如需修改开发代理目标，复制 `.env.example` 为 `.env.local` 后调整 `VITE_API_PROXY_TARGET`。
