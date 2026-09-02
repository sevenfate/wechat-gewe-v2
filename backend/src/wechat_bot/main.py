from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from wechat_bot.api.router import router
from wechat_bot.auth.service import ensure_system_permissions
from wechat_bot.connections.service import assert_single_workspace
from wechat_bot.core.config import Environment, Settings, get_settings
from wechat_bot.core.crypto import CredentialCipher
from wechat_bot.core.logging import configure_logging, get_logger
from wechat_bot.db.session import Database
from wechat_bot.events.dispatcher import EventDispatcher
from wechat_bot.events.outbox_sink import OutboxTextActionSink
from wechat_bot.events.worker import EventDispatcherWorker
from wechat_bot.outbox.sender import SenderOptions, SenderWorker
from wechat_bot.plugins.catalog import PluginCatalogService
from wechat_bot.plugins.supervisor import PluginSupervisor


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    logger = get_logger(component="application")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings)
        cipher = CredentialCipher.from_settings(resolved_settings)
        plugin_supervisor = PluginSupervisor()
        sender_worker = SenderWorker(
            session_factory=database.session_factory,
            cipher=cipher,
            options=SenderOptions.from_settings(resolved_settings),
        )
        event_dispatcher_worker = EventDispatcherWorker(
            database=database,
            dispatcher=EventDispatcher(
                invoker=plugin_supervisor,
                action_sink=OutboxTextActionSink(),
            ),
        )
        app.state.settings = resolved_settings
        app.state.database = database
        app.state.plugin_supervisor = plugin_supervisor
        app.state.sender_worker = sender_worker
        app.state.event_dispatcher_worker = event_dispatcher_worker
        workers_started = False
        if resolved_settings.environment is not Environment.TEST:
            async with database.session_factory() as session:
                await assert_single_workspace(session)
                await ensure_system_permissions(session)
                restore = await PluginCatalogService(cipher).restore_active_deployments(
                    session,
                    supervisor=plugin_supervisor,
                )
                try:
                    await session.commit()
                except BaseException:
                    for preparation in restore.runtime_activations:
                        await plugin_supervisor.abort_activation(preparation)
                    raise
            try:
                for preparation in restore.runtime_activations:
                    await plugin_supervisor.commit_activation(preparation)
            except BaseException:
                for preparation in restore.runtime_activations:
                    await plugin_supervisor.abort_activation(preparation)
                await plugin_supervisor.shutdown()
                await database.dispose()
                raise
            await sender_worker.start()
            await event_dispatcher_worker.start()
            workers_started = True
            logger.info(
                "runtime_workers_started",
                restored_plugins=len(restore.restored_deployment_ids),
                failed_plugins=len(restore.failed_deployment_ids),
            )
        logger.info("application_started", environment=resolved_settings.environment.value)
        try:
            yield
        finally:
            if workers_started:
                await event_dispatcher_worker.stop()
                await sender_worker.stop()
            await plugin_supervisor.shutdown()
            await database.dispose()
            logger.info("application_stopped")

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if resolved_settings.is_local else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if resolved_settings.is_local else None,
    )
    app.include_router(router)
    return app


app = create_app()
