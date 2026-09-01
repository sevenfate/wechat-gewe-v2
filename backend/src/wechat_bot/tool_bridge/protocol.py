from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wechat_bot.db.tool_models import ToolCallStatus
from wechat_bot.maibot.schemas import MaiBotActivationContext
from wechat_bot.tool_bridge.schemas import ToolCallRequest, ToolCatalogItem, ToolCatalogQuery
from wechat_bot.tool_bridge.service import (
    ToolBrokerError,
    ToolBrokerService,
    ToolCallIdempotencyConflictError,
    ToolExecutionDeniedError,
    ToolInputValidationError,
    ToolStaleActivationError,
)

MAX_TOOL_FRAME_BYTES = 256 * 1024
# maim-message 0.7.x routes custom frames only when ``type`` starts with
# ``custom_``.  Keep the old sys names as input aliases for a rolling upgrade,
# but emit the official custom names for new traffic.
CUSTOM_TOOL_CALL_FRAME_TYPE = "custom_wechat_bot_tool_call"
CUSTOM_TOOL_RESULT_FRAME_TYPE = "custom_wechat_bot_tool_result"
CUSTOM_TOOL_CATALOG_REQUEST_FRAME_TYPE = "custom_wechat_bot_tool_catalog_request"
CUSTOM_TOOL_CATALOG_RESPONSE_FRAME_TYPE = "custom_wechat_bot_tool_catalog_response"
# Kept as an input alias for the first local prototype of the catalog frame.
LEGACY_CUSTOM_TOOL_CATALOG_FRAME_TYPE = "custom_wechat_bot_tool_catalog"
LEGACY_TOOL_CALL_FRAME_TYPE = "sys_tool_call"
LEGACY_TOOL_RESULT_FRAME_TYPE = "sys_tool_result"
LEGACY_TOOL_CATALOG_REQUEST_FRAME_TYPE = "sys_tool_catalog_request"

TOOL_CALL_FRAME_TYPE = CUSTOM_TOOL_CALL_FRAME_TYPE
TOOL_RESULT_FRAME_TYPE = CUSTOM_TOOL_RESULT_FRAME_TYPE
TOOL_CATALOG_FRAME_TYPE = CUSTOM_TOOL_CATALOG_REQUEST_FRAME_TYPE
TOOL_CALL_FRAME_TYPES = frozenset({CUSTOM_TOOL_CALL_FRAME_TYPE, LEGACY_TOOL_CALL_FRAME_TYPE})
TOOL_CATALOG_FRAME_TYPES = frozenset(
    {
        CUSTOM_TOOL_CATALOG_REQUEST_FRAME_TYPE,
        LEGACY_CUSTOM_TOOL_CATALOG_FRAME_TYPE,
        LEGACY_TOOL_CATALOG_REQUEST_FRAME_TYPE,
    }
)


class ToolProtocolError(ValueError):
    """A malformed Tool frame that is safe to expose as a stable error code."""


class MaiBotToolBridgeAdapter:
    """Map MaiBot Tool frames to the policy-enforcing broker.

    The adapter owns no authorization state.  It only verifies the wire shape
    and binds the request to the activation currently owned by the WebSocket
    worker before delegating to :class:`ToolBrokerService`.
    """

    def __init__(
        self,
        broker: ToolBrokerService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory

    async def handle(
        self,
        context: MaiBotActivationContext,
        envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        transport_id = _transport_id(envelope)
        try:
            encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            encoded = ""
        if len(encoded.encode("utf-8")) > MAX_TOOL_FRAME_BYTES:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=transport_id,
                status=ToolCallStatus.DENIED,
                error_code="TOOL_PROTOCOL_INVALID",
            )
        try:
            parsed = parse_tool_call_envelope(envelope)
        except ToolProtocolError:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=transport_id,
                status=ToolCallStatus.DENIED,
                error_code="TOOL_PROTOCOL_INVALID",
                frame_type=_result_frame_type(envelope),
            )

        request = parsed
        if (
            request.deployment_revision_id != context.deployment_revision_id
            or request.activation_epoch != context.activation_epoch
        ):
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.CANCELLED,
                error_code="TOOL_STALE_ACTIVATION",
                frame_type=_result_frame_type(envelope),
            )

        try:
            async with self._session_factory() as session:
                result = await self._broker.invoke(session, request=request)
                await session.commit()
        except ToolCallIdempotencyConflictError:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.DENIED,
                error_code="TOOL_IDEMPOTENCY_CONFLICT",
                frame_type=_result_frame_type(envelope),
            )
        except ToolExecutionDeniedError as exc:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.DENIED,
                error_code=exc.code,
                frame_type=_result_frame_type(envelope),
            )
        except ToolStaleActivationError:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.CANCELLED,
                error_code="TOOL_STALE_ACTIVATION",
                frame_type=_result_frame_type(envelope),
            )
        except ToolInputValidationError:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.FAILED_FINAL,
                error_code="TOOL_INVALID_ARGUMENTS",
                frame_type=_result_frame_type(envelope),
            )
        except ToolBrokerError:
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.FAILED_FINAL,
                error_code="TOOL_BRIDGE_FAILED",
                frame_type=_result_frame_type(envelope),
            )
        except Exception:
            # Never put provider exceptions, paths, credentials, or tracebacks
            # on the MaiBot wire.
            return build_tool_result_envelope(
                transport_id=transport_id,
                tool_call_id=request.external_tool_call_id,
                status=ToolCallStatus.FAILED_FINAL,
                error_code="TOOL_BRIDGE_FAILED",
                frame_type=_result_frame_type(envelope),
            )

        return build_tool_result_envelope(
            transport_id=transport_id,
            tool_call_id=request.external_tool_call_id,
            status=result.call.status,
            result=result.call.result,
            replayed=result.replayed,
            frame_type=_result_frame_type(envelope),
        )

    async def handle_catalog(
        self,
        context: MaiBotActivationContext,
        envelope: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Return the caller-scoped Tool catalog over a custom frame.

        Catalog visibility is an input reduction only; the broker repeats all
        authorization checks when a subsequent call is executed.
        """

        transport_id = _transport_id(envelope)
        if not _frame_within_limit(envelope):
            return build_tool_catalog_envelope(
                transport_id=transport_id,
                status=ToolCallStatus.DENIED,
                error_code="TOOL_PROTOCOL_INVALID",
            )
        try:
            query = parse_tool_catalog_envelope(envelope)
        except ToolProtocolError:
            return build_tool_catalog_envelope(
                transport_id=transport_id,
                status=ToolCallStatus.DENIED,
                error_code="TOOL_PROTOCOL_INVALID",
            )
        if (
            query.deployment_revision_id != context.deployment_revision_id
            or query.activation_epoch != context.activation_epoch
        ):
            return build_tool_catalog_envelope(
                transport_id=transport_id,
                status=ToolCallStatus.CANCELLED,
                error_code="TOOL_STALE_ACTIVATION",
            )
        try:
            async with self._session_factory() as session:
                items = await self._broker.list_visible_tools(
                    session,
                    deployment_revision_id=query.deployment_revision_id,
                    activation_epoch=query.activation_epoch,
                    connector_context_id=query.connector_context_id,
                )
                await session.commit()
        except ToolExecutionDeniedError as exc:
            return build_tool_catalog_envelope(
                transport_id=transport_id,
                status=ToolCallStatus.DENIED,
                error_code=exc.code,
            )
        except (ToolBrokerError, Exception):
            # Do not disclose provider failures, paths, credentials, or stack
            # traces to the external runtime.
            return build_tool_catalog_envelope(
                transport_id=transport_id,
                status=ToolCallStatus.FAILED_FINAL,
                error_code="TOOL_BRIDGE_FAILED",
            )
        try:
            catalog_items = [
                ToolCatalogItem.model_validate(item).model_dump(mode="json") for item in items
            ]
        except (TypeError, ValueError):
            return build_tool_catalog_envelope(
                transport_id=transport_id,
                status=ToolCallStatus.FAILED_FINAL,
                error_code="TOOL_BRIDGE_FAILED",
            )
        return build_tool_catalog_envelope(
            transport_id=transport_id,
            status=ToolCallStatus.SUCCEEDED,
            items=catalog_items,
        )


def parse_tool_call_envelope(envelope: Mapping[str, Any]) -> ToolCallRequest:
    """Parse a version-1 Tool call in direct or wrapped custom payload form.

    ``maim-message`` passes the complete custom envelope to handlers.  A few
    integrations wrap the actual request under ``data`` or ``request``;
    accepting those two explicit wrappers keeps the boundary interoperable
    without accepting arbitrary nested objects.
    """
    _validate_tool_envelope_header(envelope, TOOL_CALL_FRAME_TYPES, "tool call")
    _required_string(envelope, "msg_id", 255)
    meta = _required_mapping(envelope, "meta")
    if _required_string(meta, "platform", 32) != "gewe":
        raise ToolProtocolError("tool frame platform is not gewe")
    timestamp = _required_number(meta, "timestamp")
    if not math.isfinite(timestamp):
        raise ToolProtocolError("tool frame timestamp is invalid")
    payload = _unwrap_payload(_required_mapping(envelope, "payload"), kind="call")
    allowed = {
        "bridge_version",
        "tool_call_id",
        "external_tool_call_id",
        "connector_context_id",
        "deployment_revision_id",
        "activation_epoch",
        "tool_name",
        "tool_schema_version",
        "arguments",
        "invocation_mode",
        "deadline_at",
    }
    if set(payload) - allowed:
        raise ToolProtocolError("tool frame contains unsupported fields")
    _validate_bridge_version(payload)
    if not isinstance(payload.get("arguments", {}), dict):
        raise ToolProtocolError("tool arguments must be an object")
    external_id = _one_alias(payload, "tool_call_id", "external_tool_call_id")
    try:
        return ToolCallRequest.model_validate(
            {
                "external_tool_call_id": external_id,
                "connector_context_id": payload.get("connector_context_id"),
                "deployment_revision_id": payload.get("deployment_revision_id"),
                "activation_epoch": payload.get("activation_epoch"),
                "tool_name": payload.get("tool_name"),
                "tool_schema_version": payload.get("tool_schema_version", "1.0"),
                "arguments": payload.get("arguments", {}),
                "invocation_mode": payload.get("invocation_mode", "USER_REQUESTED"),
                "deadline_at": payload.get("deadline_at"),
            }
        )
    except (TypeError, ValueError) as exc:
        raise ToolProtocolError("tool request payload is invalid") from exc


def parse_tool_catalog_envelope(envelope: Mapping[str, Any]) -> ToolCatalogQuery:
    """Parse a caller-scoped catalog request custom frame."""

    _validate_tool_envelope_header(envelope, TOOL_CATALOG_FRAME_TYPES, "tool catalog")
    _required_string(envelope, "msg_id", 255)
    meta = _required_mapping(envelope, "meta")
    if _required_string(meta, "platform", 32) != "gewe":
        raise ToolProtocolError("tool frame platform is not gewe")
    timestamp = _required_number(meta, "timestamp")
    if not math.isfinite(timestamp):
        raise ToolProtocolError("tool frame timestamp is invalid")
    payload = _unwrap_payload(_required_mapping(envelope, "payload"), kind="catalog")
    allowed = {
        "bridge_version",
        "connector_context_id",
        "deployment_revision_id",
        "activation_epoch",
    }
    if set(payload) - allowed:
        raise ToolProtocolError("tool catalog frame contains unsupported fields")
    _validate_bridge_version(payload)
    try:
        return ToolCatalogQuery.model_validate(payload)
    except (TypeError, ValueError) as exc:
        raise ToolProtocolError("tool catalog payload is invalid") from exc


def build_tool_result_envelope(
    *,
    transport_id: str,
    tool_call_id: str,
    status: ToolCallStatus,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    replayed: bool = False,
    frame_type: str = TOOL_RESULT_FRAME_TYPE,
) -> dict[str, Any]:
    _limited_string(transport_id, "transport id", 255)
    _limited_string(tool_call_id, "tool call id", 255)
    if frame_type not in {TOOL_RESULT_FRAME_TYPE, LEGACY_TOOL_RESULT_FRAME_TYPE}:
        raise ToolProtocolError("unsupported Tool result frame type")
    payload: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "status": status.value,
        "replayed": replayed,
    }
    if status is ToolCallStatus.SUCCEEDED:
        payload["result"] = result if isinstance(result, dict) else {}
    else:
        payload["error"] = {"code": error_code or "TOOL_BRIDGE_FAILED"}
    return {
        "ver": 1,
        "msg_id": f"tool-result:{transport_id}",
        "type": frame_type,
        "meta": {
            "platform": "gewe",
            "in_reply_to": transport_id,
            "timestamp": datetime.now(UTC).timestamp(),
        },
        "payload": payload,
    }


def build_tool_catalog_envelope(
    *,
    transport_id: str,
    status: ToolCallStatus,
    items: list[dict[str, Any]] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Build a custom catalog response with only JSON-safe public fields."""

    _limited_string(transport_id, "transport id", 255)
    payload: dict[str, Any] = {
        "bridge_version": "1.0",
        "request_id": transport_id,
        "status": status.value,
    }
    if status is ToolCallStatus.SUCCEEDED:
        payload["items"] = items if isinstance(items, list) else []
    else:
        payload["error"] = {"code": error_code or "TOOL_BRIDGE_FAILED"}
    return {
        "ver": 1,
        "msg_id": f"tool-catalog:{transport_id}",
        "type": CUSTOM_TOOL_CATALOG_RESPONSE_FRAME_TYPE,
        "meta": {
            "platform": "gewe",
            "in_reply_to": transport_id,
            "timestamp": datetime.now(UTC).timestamp(),
        },
        "payload": payload,
    }


def _transport_id(envelope: Mapping[str, Any]) -> str:
    raw = envelope.get("msg_id")
    return raw.strip() if isinstance(raw, str) and raw.strip() else "unknown"


def _result_frame_type(envelope: Mapping[str, Any]) -> str:
    """Mirror legacy response naming only for legacy requests."""

    if envelope.get("type") == LEGACY_TOOL_CALL_FRAME_TYPE:
        return LEGACY_TOOL_RESULT_FRAME_TYPE
    return TOOL_RESULT_FRAME_TYPE


def _frame_within_limit(envelope: Mapping[str, Any]) -> bool:
    try:
        encoded = json.dumps(envelope, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return len(encoded.encode("utf-8")) <= MAX_TOOL_FRAME_BYTES


def _validate_tool_envelope_header(
    envelope: Mapping[str, Any], frame_types: frozenset[str], label: str
) -> None:
    if envelope.get("ver") != 1 or envelope.get("type") not in frame_types:
        raise ToolProtocolError(f"expected a version 1 {label} envelope")


def _unwrap_payload(payload: Mapping[str, Any], *, kind: str) -> Mapping[str, Any]:
    """Accept direct payloads and two explicitly named wrapper forms."""

    wrappers = {key: payload[key] for key in ("data", "request", kind) if key in payload}
    if not wrappers:
        return payload
    if len(wrappers) != 1:
        raise ToolProtocolError("tool payload contains multiple wrappers")
    wrapper_key, wrapped = next(iter(wrappers.items()))
    if not isinstance(wrapped, Mapping):
        raise ToolProtocolError(f"tool payload wrapper {wrapper_key} must be an object")
    outer_allowed = {wrapper_key, "bridge_version"}
    if set(payload) - outer_allowed:
        raise ToolProtocolError("tool payload contains unsupported wrapper fields")
    if "bridge_version" in payload and payload["bridge_version"] != "1.0":
        raise ToolProtocolError("unsupported Tool Bridge version")
    return wrapped


def _validate_bridge_version(payload: Mapping[str, Any]) -> None:
    if "bridge_version" in payload and payload["bridge_version"] != "1.0":
        raise ToolProtocolError("unsupported Tool Bridge version")


def _one_alias(payload: Mapping[str, Any], first: str, second: str) -> Any:
    first_present = first in payload
    second_present = second in payload
    if first_present and second_present and payload[first] != payload[second]:
        raise ToolProtocolError(f"{first} and {second} disagree")
    if first_present:
        return payload[first]
    return payload.get(second)


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    raw = value.get(key)
    if not isinstance(raw, Mapping):
        raise ToolProtocolError(f"{key} must be an object")
    return raw


def _required_string(value: Mapping[str, Any], key: str, limit: int) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        raise ToolProtocolError(f"{key} must be a string")
    return _limited_string(raw, key, limit)


def _required_number(value: Mapping[str, Any], key: str) -> float:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise ToolProtocolError(f"{key} must be a number")
    return float(raw)


def _limited_string(value: str, field: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > limit:
        raise ToolProtocolError(f"{field} is invalid")
    return normalized
