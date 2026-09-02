from fastapi import APIRouter

from wechat_bot.api.accounts import router as accounts_router
from wechat_bot.api.admin_rbac import router as admin_rbac_router
from wechat_bot.api.auth import router as auth_router
from wechat_bot.api.connections import router as connections_router
from wechat_bot.api.directory import router as directory_router
from wechat_bot.api.health import router as health_router
from wechat_bot.api.messages import router as messages_router
from wechat_bot.api.outbox import router as outbox_router
from wechat_bot.api.plugins import router as plugins_router
from wechat_bot.api.policy import router as policy_router
from wechat_bot.api.task_agent import router as task_agent_router
from wechat_bot.api.webhooks import router as webhooks_router

router = APIRouter()
router.include_router(health_router)
router.include_router(webhooks_router)
router.include_router(auth_router)
router.include_router(admin_rbac_router)
router.include_router(accounts_router)
router.include_router(connections_router)
router.include_router(directory_router)
router.include_router(messages_router)
router.include_router(outbox_router)
router.include_router(policy_router)
router.include_router(plugins_router)
router.include_router(task_agent_router)
