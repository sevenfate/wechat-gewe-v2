"""Import every model module so SQLAlchemy and Alembic see complete metadata."""

from importlib import import_module

MODEL_MODULES = (
    "wechat_bot.db.models",
    "wechat_bot.db.auth_models",
    "wechat_bot.db.policy_models",
    "wechat_bot.db.plugin_models",
    "wechat_bot.db.maibot_models",
    "wechat_bot.db.agent_models",
    "wechat_bot.db.tool_models",
)


def load_all_models() -> None:
    for module_name in MODEL_MODULES:
        import_module(module_name)
