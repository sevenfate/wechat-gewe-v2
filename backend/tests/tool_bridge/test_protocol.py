from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from wechat_bot.db.tool_models import ToolCallStatus
from wechat_bot.maibot.runtime import MaiBotConnectionWorker
from wechat_bot.maibot.schemas import MaiBotActivationContext, MaiBotConnectorConfig
from wechat_bot.tool_bridge.protocol import (
    CUSTOM_TOOL_CALL_FRAME_TYPE,
    CUSTOM_TOOL_CATALOG_REQUEST_FRAME_TYPE,
    CUSTOM_TOOL_CATALOG_RESPONSE_FRAME_TYPE,
    CUSTOM_TOOL_RESULT_FRAME_TYPE,
    LEGACY_TOOL_CALL_FRAME_TYPE,
    MaiBotToolBridgeAdapter,
    ToolProtocolError,
    build_tool_catalog_envelope,
    build_tool_result_envelope,
    parse_tool_call_envelope,
    parse_tool_catalog_envelope,
)


def test_parse_tool_call_envelope_maps_versioned_payload() -> None:
    frame = _frame()
    request = parse_tool_call_envelope(frame)

    assert request.external_tool_call_id == "call-1"
    assert request.tool_name == "plugin.weather.query"
    assert request.arguments == {"city": "北京"}
    assert request.activation_epoch == 7


def test_parse_tool_call_rejects_unknown_fields_and_bad_deadline() -> None:
    frame = _frame()
    frame["payload"]["unexpected"] = True
    with pytest.raises(ToolProtocolError):
        parse_tool_call_envelope(frame)

    invalid = _frame()
    invalid["payload"]["deadline_at"] = "2026-08-31T00:00:00"
    with pytest.raises(ToolProtocolError):
        parse_tool_call_envelope(invalid)


def test_parse_tool_call_accepts_legacy_sys_alias_and_explicit_wrapper() -> None:
    frame = _frame()
    frame["type"] = LEGACY_TOOL_CALL_FRAME_TYPE
    payload = frame.pop("payload")
    frame["payload"] = {"data": payload, "bridge_version": "1.0"}

    request = parse_tool_call_envelope(frame)

    assert request.external_tool_call_id == "call-1"


def test_parse_catalog_request_and_use_custom_response_namespace() -> None:
    frame = {
        "ver": 1,
        "msg_id": "catalog-1",
        "type": CUSTOM_TOOL_CATALOG_REQUEST_FRAME_TYPE,
        "meta": {"platform": "gewe", "timestamp": datetime.now(UTC).timestamp()},
        "payload": {
            "bridge_version": "1.0",
            "connector_context_id": "opaque-context",
            "deployment_revision_id": str(uuid4()),
            "activation_epoch": 7,
        },
    }

    query = parse_tool_catalog_envelope(frame)

    assert query.activation_epoch == 7
    result = build_tool_catalog_envelope(
        transport_id="catalog-1",
        status=ToolCallStatus.SUCCEEDED,
        items=[],
    )
    assert result["type"] == CUSTOM_TOOL_CATALOG_RESPONSE_FRAME_TYPE


@pytest.mark.asyncio
async def test_worker_routes_custom_call_and_catalog_to_tool_adapter() -> None:
    adapter = RecordingToolAdapter()
    worker = MaiBotConnectionWorker(
        deployment_id=uuid4(),
        activation_epoch=7,
        config=MaiBotConnectorConfig(
            websocket_url="ws://maibot.test/ws",
            api_key="test-key",
            client_uuid="test-client",
        ),
        session_factory=DummySessionFactory(),  # type: ignore[arg-type]
        service=object(),  # type: ignore[arg-type]
        tool_adapter=adapter,  # type: ignore[arg-type]
    )
    socket = RecordingSocket()
    context = _context()

    await worker._receive_once(socket, context, json.dumps(_frame()))
    catalog = {
        "ver": 1,
        "msg_id": "catalog-1",
        "type": CUSTOM_TOOL_CATALOG_REQUEST_FRAME_TYPE,
        "meta": {"platform": "gewe", "timestamp": datetime.now(UTC).timestamp()},
        "payload": {
            "connector_context_id": "opaque-context",
            "deployment_revision_id": str(context.deployment_revision_id),
            "activation_epoch": context.activation_epoch,
        },
    }
    await worker._receive_once(socket, context, json.dumps(catalog))

    assert [item["type"] for item in map(json.loads, socket.sent)] == [
        CUSTOM_TOOL_RESULT_FRAME_TYPE,
        CUSTOM_TOOL_CATALOG_RESPONSE_FRAME_TYPE,
    ]
    assert len(adapter.calls) == 1
    assert len(adapter.catalog_calls) == 1


def test_result_envelope_contains_no_connector_secrets() -> None:
    result = build_tool_result_envelope(
        transport_id="transport-1",
        tool_call_id="call-1",
        status=ToolCallStatus.SUCCEEDED,
        result={"text": "晴"},
    )

    assert result["type"] == CUSTOM_TOOL_RESULT_FRAME_TYPE
    assert result["payload"] == {
        "tool_call_id": "call-1",
        "status": "SUCCEEDED",
        "replayed": False,
        "result": {"text": "晴"},
    }
    assert "api_key" not in str(result)
    assert "connector_context_id" not in str(result)


@pytest.mark.asyncio
async def test_adapter_binds_activation_and_commits_success() -> None:
    broker = FakeBroker()
    adapter = MaiBotToolBridgeAdapter(broker, DummySessionFactory())  # type: ignore[arg-type]
    context = _context()
    frame = _frame(
        deployment_revision_id=str(context.deployment_revision_id),
        activation_epoch=context.activation_epoch,
    )

    result = await adapter.handle(context, frame)

    assert result["payload"]["status"] == "SUCCEEDED"
    assert result["payload"]["result"] == {"ok": True}
    assert broker.request is not None
    assert broker.request.deployment_revision_id == context.deployment_revision_id


@pytest.mark.asyncio
async def test_adapter_rejects_stale_activation_without_calling_broker() -> None:
    broker = FakeBroker()
    adapter = MaiBotToolBridgeAdapter(broker, DummySessionFactory())  # type: ignore[arg-type]
    context = _context()
    frame = _frame(
        deployment_revision_id=str(context.deployment_revision_id),
        activation_epoch=context.activation_epoch + 1,
    )

    result = await adapter.handle(context, frame)

    assert result["payload"]["status"] == "CANCELLED"
    assert result["payload"]["error"] == {"code": "TOOL_STALE_ACTIVATION"}
    assert broker.request is None


class DummySession:
    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self) -> DummySession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class DummySessionFactory:
    def __init__(self) -> None:
        self.session = DummySession()

    def __call__(self) -> DummySession:
        return self.session


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)


class RecordingToolAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.catalog_calls: list[dict[str, Any]] = []

    async def handle(
        self,
        context: MaiBotActivationContext,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append({"context": context, "envelope": envelope})
        return build_tool_result_envelope(
            transport_id="transport-1",
            tool_call_id="call-1",
            status=ToolCallStatus.SUCCEEDED,
            result={"ok": True},
        )

    async def handle_catalog(
        self,
        context: MaiBotActivationContext,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        self.catalog_calls.append({"context": context, "envelope": envelope})
        return build_tool_catalog_envelope(
            transport_id="catalog-1",
            status=ToolCallStatus.SUCCEEDED,
            items=[],
        )


class FakeBroker:
    def __init__(self) -> None:
        self.request: Any = None

    async def invoke(self, session: object, *, request: Any) -> Any:
        del session
        self.request = request
        return SimpleNamespace(
            replayed=False,
            call=SimpleNamespace(status=ToolCallStatus.SUCCEEDED, result={"ok": True}),
        )


def _context() -> MaiBotActivationContext:
    return MaiBotActivationContext(
        deployment_id=uuid4(),
        deployment_revision_id=uuid4(),
        activation_id=uuid4(),
        activation_epoch=7,
        fencing_token="fence-token",
        workspace_id=uuid4(),
        plugin_id="builtin.maibot-connector",
        revision_grants=frozenset(),
        revision_scope={},
    )


def _frame(
    *,
    deployment_revision_id: str | None = None,
    activation_epoch: int = 7,
) -> dict[str, Any]:
    return {
        "ver": 1,
        "msg_id": "transport-1",
        "type": CUSTOM_TOOL_CALL_FRAME_TYPE,
        "meta": {
            "platform": "gewe",
            "timestamp": datetime.now(UTC).timestamp(),
        },
        "payload": {
            "tool_call_id": "call-1",
            "connector_context_id": "opaque-context",
            "deployment_revision_id": deployment_revision_id or str(uuid4()),
            "activation_epoch": activation_epoch,
            "tool_name": "plugin.weather.query",
            "tool_schema_version": "1.0",
            "arguments": {"city": "北京"},
            "invocation_mode": "USER_REQUESTED",
            "deadline_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        },
    }
