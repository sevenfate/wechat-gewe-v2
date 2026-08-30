import { createApp } from "vue";

import { configureApiClient } from "./api/client";
import App from "./App.vue";
import { authSession } from "./auth/session";
import { router } from "./router";
import "./styles/base.css";
import "./styles/operations.css";

configureApiClient({
  getCsrfToken: authSession.getCsrfToken,
  onUnauthorized: () => {
    const current = router.currentRoute.value;
    authSession.invalidate();
    if (current.name === "login" || current.name === "bootstrap") return;
    void router.replace({ name: "login", query: { redirect: current.fullPath } });
  },
});

createApp(App).use(router).mount("#app");
