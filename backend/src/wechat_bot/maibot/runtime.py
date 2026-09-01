from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from uuid6 import uuid7

from wechat_bot.core.logging import get_logger
from wechat_bot.db.base import utc_now
from wechat_bot.db.maibot_models import MaiBotConnectionStatus
from wechat_bot.db.tool_models import ToolCallStatus
from wechat_bot.maibot.constants import MAIBOT_CONNECTOR_PLUGIN_ID
from wechat_bot.maibot.mapping import (
    MaiBotProtocolError,
    build_ack_envelope,
    materialize_api_key,
    parse_ack_envelope,
)
from wechat_bot.maibot.schemas import (
    MaiBotActivationContext,
    MaiBotConnectorConfig,
)
from wechat_bot.maibot.service import MaiBotBridgeService
from wechat_bot.maibot.transport import MaiBotWebSocket, open_maibot_socket
from wechat_bot.plugins.supervisor import PluginLaunchSpec, PluginRuntimeError
from wechat_bot.tool_bridge.protocol import (
    TOOL_CALL_FRAME_TYPES,
    TOOL_CATALOG_FRAME_TYPES,
    MaiBotToolBridgeAdapter,
    build_tool_catalog_envelope,
    build_tool_result_envelope,
)
from wechat_bot.tool_bridge.service import ToolBrokerService

SocketFactory = Callable[
    [MaiBotConnectorConfig],
    AbstractAsyncContextManager[MaiBotWebSocket],
]

ACTIVATION_VISIBILITY_TIMEOUT_SECONDS = 30.0
SOCKET_SEND_TIMEOUT_SECONDS = 10.0


class MaiBotConnectionWorker:
    def __init__(
        self,
        *,
        deployment_id: UUID,
        activation_epoch: int,
        config: MaiBotConnectorConfig,
        session_factory: async_sessionmaker[AsyncSession],
        service: MaiBotBridgeService,
        socket_factory: SocketFactory = open_maibot_socket,
        expected_activation_id: UUID | None = None,
        expected_fencing_token: str | None = None,
        tool_adapter: MaiBotToolBridgeAdapter | None = None,
        activation_visibility_timeout_seconds: float = (ACTIVATION_VISIBILITY_TIMEOUT_SECONDS),
    ) -> None:
        self.deployment_id = deployment_id
        self.activation_epoch = activation_epoch
        self.config = config
        self._session_factory = session_factory
        self._service = service
        self._socket_factory = socket_factory
        self._expected_activation_id = expected_activation_id
        self._expected_fencing_token = expected_fencing_token
        self._tool_adapter = tool_adapter
        self._activation_visibility_timeout_seconds = activation_visibility_timeout_seconds
        self._stop_requested = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._context: MaiBotActivationContext | None = None
        self._logger = get_logger(
            component="maibot_connector",
            deployment_id=str(deployment_id),
            activation_epoch=activation_epoch,
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            raise PluginRuntimeError("MaiBot connector is already running")
        self._stop_requested.clear()
        self._task = asyncio.create_task(
            self._run(),
            name=f"maibot-connector-{self.deployment_id}-{self.activation_epoch}",
        )

    async def stop(self) -> None:
        self._stop_requested.set()
        task = self._task
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._task = None
        if self._context is not None:
            await self._record_status(MaiBotConnectionStatus.STOPPED)

    async def _run(self) -> None:
        delay = self.config.reconnect_initial_seconds
        activation_deadline = (
            asyncio.get_running_loop().time() + self._activation_visibility_timeout_seconds
        )
        while not self._stop_requested.is_set():
            context = await self._load_activation_context()
            if context is None:
                if asyncio.get_running_loop().time() >= activation_deadline:
                    self._logger.warning("maibot_activation_never_became_visible")
                    return
                await self._wait(min(delay, 0.5))
                continue
            self._context = context
            await self._record_status(MaiBotConnectionStatus.CONNECTING)
            try:
                async with self._socket_factory(self.config) as socket:
                    await self._record_status(MaiBotConnectionStatus.CONNECTED)
                    delay = self.config.reconnect_initial_seconds
                    if await self._run_connected(socket, context):
                        return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_status(
                    MaiBotConnectionStatus.BACKOFF,
                    error_code=f"MAIBOT_WS_{type(exc).__name__.upper()}",
                )
                self._logger.warning(
                    "maibot_connection_lost",
                    error_type=type(exc).__name__,
                )
                await self._wait(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)

    async def _run_connected(
        self,
        socket: MaiBotWebSocket,
        context: MaiBotActivationContext,
    ) -> bool:
        while not self._stop_requested.is_set():
            if await self._load_activation_context() != context:
                return True
            await self._send_once(socket, context)
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=0.25)
            except TimeoutError:
                continue
            await self._receive_once(socket, context, raw)
        return False

    async def _send_once(
        self,
        socket: MaiBotWebSocket,
        context: MaiBotActivationContext,
    ) -> None:
        async with self._session_factory() as session:
            envelope = await self._service.claim_next(session, context=context)
            if envelope is None:
                await session.commit()
                return
            wire_envelope = materialize_api_key(
                envelope.envelope,
                self.config.api_key.get_secret_value(),
            )
            try:
                await asyncio.wait_for(
                    socket.send(
                        json.dumps(
                            wire_envelope,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            allow_nan=False,
                        )
                    ),
                    timeout=SOCKET_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                await self._service.mark_retryable(
                    session,
                    envelope.id,
                    error_code="MAIBOT_WS_SEND_FAILED",
                    retry_seconds=self.config.reconnect_initial_seconds,
                )
                await session.commit()
                raise
            await self._service.mark_sent(
                session,
                envelope.id,
                ack_retry_seconds=self.config.ack_retry_seconds,
            )
            await session.commit()

    async def _receive_once(
        self,
        socket: MaiBotWebSocket,
        context: MaiBotActivationContext,
        raw: str | bytes,
    ) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise MaiBotProtocolError("MaiBot WebSocket frame must contain a JSON object")
        if decoded.get("type") == "sys_ack":
            ack = parse_ack_envelope(decoded)
            async with self._session_factory() as session:
                await self._service.acknowledge(
                    session,
                    context=context,
                    transport_message_id=ack.acked_envelope_id,
                )
                await session.commit()
            return
        frame_type = decoded.get("type")
        if frame_type in TOOL_CALL_FRAME_TYPES:
            if self._tool_adapter is None:
                result_envelope = build_tool_result_envelope(
                    transport_id=_transport_id(decoded),
                    tool_call_id=_transport_id(decoded),
                    status=ToolCallStatus.DENIED,
                    error_code="TOOL_BRIDGE_UNAVAILABLE",
                )
            else:
                result_envelope = await self._tool_adapter.handle(context, decoded)
            await asyncio.wait_for(
                socket.send(
                    json.dumps(
                        result_envelope,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                ),
                timeout=SOCKET_SEND_TIMEOUT_SECONDS,
            )
            return
        if frame_type in TOOL_CATALOG_FRAME_TYPES:
            if self._tool_adapter is None:
                result_envelope = build_tool_catalog_envelope(
                    transport_id=_transport_id(decoded),
                    status=ToolCallStatus.DENIED,
                    error_code="TOOL_BRIDGE_UNAVAILABLE",
                )
            else:
                result_envelope = await self._tool_adapter.handle_catalog(context, decoded)
            await asyncio.wait_for(
                socket.send(
                    json.dumps(
                        result_envelope,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                ),
                timeout=SOCKET_SEND_TIMEOUT_SECONDS,
            )
            return
        if isinstance(frame_type, str) and frame_type.startswith("custom_"):
            # MaiBot can carry unrelated custom messages on the same channel.
            # They are not part of this connector contract and must not force a
            # reconnect loop or be interpreted as a Tool request.
            self._logger.debug("maibot_custom_frame_ignored", frame_type=frame_type)
            return
        if decoded.get("type") != "sys_std":
            raise MaiBotProtocolError("unsupported MaiBot envelope type")
        async with self._session_factory() as session:
            await self._service.receive_standard(
                session,
                context=context,
                config=self.config,
                envelope=decoded,
            )
            await session.commit()
        transport_id = decoded.get("msg_id")
        if not isinstance(transport_id, str) or not transport_id.strip():
            return
        now = utc_now().timestamp()
        ack_envelope = build_ack_envelope(
            envelope_id=f"ack:{uuid7()}",
            acked_envelope_id=transport_id,
            connection_uuid=self.config.client_uuid,
            timestamp=now,
        )
        await asyncio.wait_for(
            socket.send(
                json.dumps(
                    ack_envelope,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            ),
            timeout=SOCKET_SEND_TIMEOUT_SECONDS,
        )

    async def _load_activation_context(self) -> MaiBotActivationContext | None:
        async with self._session_factory() as session:
            context = await self._service.activation_context(
                session,
                deployment_id=self.deployment_id,
                activation_epoch=self.activation_epoch,
            )
        if context is None:
            return None
        if (
            self._expected_activation_id is not None
            and context.activation_id != self._expected_activation_id
        ):
            return None
        if (
            self._expected_fencing_token is not None
            and context.fencing_token != self._expected_fencing_token
        ):
            return None
        return context

    async def _record_status(
        self,
        status: MaiBotConnectionStatus,
        *,
        error_code: str | None = None,
    ) -> None:
        context = self._context
        if context is None:
            return
        async with self._session_factory() as session:
            await self._service.set_connection_status(
                session,
                context=context,
                status=status,
                error_code=error_code,
            )
            await session.commit()

    async def _wait(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_requested.wait(), timeout=delay)
        except TimeoutError:
            pass


class MaiBotManagedRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        service: MaiBotBridgeService,
        socket_factory: SocketFactory = open_maibot_socket,
        tool_broker: ToolBrokerService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._service = service
        self._socket_factory = socket_factory
        self._tool_adapter = (
            MaiBotToolBridgeAdapter(tool_broker, session_factory)
            if tool_broker is not None
            else None
        )
        self._workers: dict[str, dict[str, MaiBotConnectionWorker]] = {}
        self._last_epoch: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def set_tool_broker(self, broker: ToolBrokerService) -> None:
        """Attach the broker after PluginSupervisor construction resolves the cycle."""
        self._tool_adapter = MaiBotToolBridgeAdapter(broker, self._session_factory)

    @staticmethod
    def handles(spec: PluginLaunchSpec) -> bool:
        return spec.manifest.plugin_id == MAIBOT_CONNECTOR_PLUGIN_ID

    async def activate(
        self,
        deployment_id: str,
        spec: PluginLaunchSpec,
        *,
        requested_epoch: int | None = None,
        activation_id: str | None = None,
        fencing_token: str | None = None,
    ) -> int:
        if not self.handles(spec):
            raise PluginRuntimeError("managed runtime cannot handle this plugin")
        try:
            parsed_id = UUID(deployment_id)
            parsed_activation_id = UUID(activation_id) if activation_id is not None else None
            config = MaiBotConnectorConfig.model_validate(spec.config)
        except (ValueError, TypeError) as exc:
            raise PluginRuntimeError("MaiBot connector configuration is invalid") from exc
        async with self._lock:
            last_epoch = self._last_epoch.get(deployment_id, 0)
            epoch = requested_epoch if requested_epoch is not None else last_epoch + 1
            if activation_id is None and epoch <= last_epoch:
                raise PluginRuntimeError("activation epoch must increase monotonically")
            workers = self._workers.setdefault(deployment_id, {})
            for key, existing in tuple(workers.items()):
                if not existing.running:
                    workers.pop(key)
            worker_key = activation_id or f"epoch:{epoch}"
            if worker_key in workers:
                raise PluginRuntimeError("MaiBot activation is already registered")
            worker = MaiBotConnectionWorker(
                deployment_id=parsed_id,
                activation_epoch=epoch,
                config=config,
                session_factory=self._session_factory,
                service=self._service,
                socket_factory=self._socket_factory,
                expected_activation_id=parsed_activation_id,
                expected_fencing_token=fencing_token,
                tool_adapter=self._tool_adapter,
            )
            await worker.start()
            workers[worker_key] = worker
            self._last_epoch[deployment_id] = max(last_epoch, epoch)
        return epoch

    async def deactivate(self, deployment_id: str) -> None:
        async with self._lock:
            workers = tuple(self._workers.pop(deployment_id, {}).values())
        for worker in workers:
            await worker.stop()

    async def shutdown(self) -> None:
        async with self._lock:
            deployment_ids = tuple(self._workers)
        for deployment_id in deployment_ids:
            await self.deactivate(deployment_id)


def _transport_id(envelope: dict[str, object]) -> str:
    raw = envelope.get("msg_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "unknown"
