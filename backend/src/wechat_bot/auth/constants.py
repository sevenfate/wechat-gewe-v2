from __future__ import annotations

from typing import Literal

OWNER_ROLE_CODE = "owner"
ADMIN_USER_MANAGE_PERMISSION = "admin.user.manage"
OUTBOX_READ_PERMISSION = "outbox.read"
TOOL_READ_PERMISSION = "tool.read"
OUTBOX_MANAGE_PERMISSION = "outbox.manage"
AGENT_READ_PERMISSION = "agent.read"
AGENT_WRITE_PERMISSION = "agent.write"
AGENT_RUN_PERMISSION = "agent.run"
AGENT_QUESTION_OVERRIDE_PERMISSION = "agent.question.override"
SESSION_COOKIE_NAME = "wechat_bot_session"
CSRF_COOKIE_NAME = "wechat_bot_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
BOOTSTRAP_HEADER_NAME = "X-Bootstrap-Token"
COOKIE_PATH = "/"
COOKIE_SAME_SITE: Literal["strict"] = "strict"
PRE_AUTH_CSRF_TTL_SECONDS = 600

SYSTEM_PERMISSION_CATALOG: dict[str, str] = {
    ADMIN_USER_MANAGE_PERMISSION: "Manage administrator accounts and RBAC",
    AGENT_READ_PERMISSION: "Read task-agent definitions, sessions, and execution state",
    AGENT_QUESTION_OVERRIDE_PERMISSION: "Override pending task-agent questions as an administrator",
    AGENT_RUN_PERMISSION: "Start and control task-agent sessions and runs",
    AGENT_WRITE_PERMISSION: "Create task-agent definitions and publish versions",
    "account.read": "Read WeChat bot accounts",
    "account.write": "Manage WeChat bot accounts",
    "connection.read": "Read GeWe connections",
    "connection.write": "Manage GeWe connections",
    "directory.read": "Read contacts, discovered groups, and members",
    "directory.sync": "Synchronize contacts, discovered groups, and members",
    "message.read": "Read normalized messages and raw provider callbacks",
    "audit.read": "Read policy decisions, audit events, and end-to-end traces",
    OUTBOX_MANAGE_PERMISSION: "Cancel and reconcile outgoing messages",
    OUTBOX_READ_PERMISSION: "Read outgoing message delivery state",
    TOOL_READ_PERMISSION: "Read the Tool Bridge catalog and call ledger",
    "plugin.deploy": "Install, upgrade, enable, disable, and remove plugins",
    "plugin.invoke": "Invoke plugin commands and tools",
    "plugin.read": "Read plugin catalog and runtime state",
    "policy.evaluate": "Explain effective runtime policy decisions",
    "policy.read": "Read runtime policies",
    "policy.write": "Manage runtime policies",
}
