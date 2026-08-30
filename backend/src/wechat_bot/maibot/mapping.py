from __future__ import annotations

import copy
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from wechat_bot.maibot.constants import (
    MAIBOT_API_KEY_PLACEHOLDER,
    MAIBOT_PLATFORM,
)

MAX_MESSAGE_ID_LENGTH = 600
MAX_TEXT_LENGTH = 10_000


class MaiBotProtocolError(ValueError):
    """Raised when a MaiBot envelope is outside the frozen P0 contract."""


@dataclass(frozen=True, slots=True)
class MaiBotInboundText:
    envelope_id: str
    business_message_id: str
    timestamp: float
    actor_id: str
    text: str
    connector_context_id: str
    platform_account_id: str
    platform_scope: str
    actor_nickname: str | None = None
    actor_cardname: str | None = None
    group_id: str | None = None
    group_name: str | None = None


@dataclass(frozen=True, slots=True)
class MaiBotOutboundText:
    envelope_id: str
    business_message_id: str
    timestamp: float
    target_wxid: str
    target_kind: str
    text: str
    reply_to_business_message_id: str | None


@dataclass(frozen=True, slots=True)
class MaiBotAck:
    envelope_id: str
    acked_envelope_id: str
    timestamp: float


def build_inbound_text_envelope(message: MaiBotInboundText) -> dict[str, Any]:
    _limited_text(message.envelope_id, "envelope id", 255)
    _limited_text(message.business_message_id, "business message id", MAX_MESSAGE_ID_LENGTH)
    _limited_text(message.actor_id, "actor id", 255)
    _limited_text(message.text, "message text", MAX_TEXT_LENGTH)
    _limited_text(message.connector_context_id, "connector context id", 255)
    _limited_text(message.platform_account_id, "platform account id", 255)
    _limited_text(message.platform_scope, "platform scope", 255)
    if (message.group_id is None) != (message.group_name is None):
        # A group name is optional in maim-message, so only reject a name without an ID.
        if message.group_id is None:
            raise MaiBotProtocolError("group name requires group id")

    user_info: dict[str, Any] = {
        "platform": MAIBOT_PLATFORM,
        "user_id": message.actor_id,
    }
    if message.actor_nickname:
        user_info["user_nickname"] = message.actor_nickname
    if message.actor_cardname:
        user_info["user_cardname"] = message.actor_cardname
    sender_info: dict[str, Any] = {"user_info": user_info}
    if message.group_id is not None:
        group_info: dict[str, Any] = {
            "platform": MAIBOT_PLATFORM,
            "group_id": message.group_id,
        }
        if message.group_name:
            group_info["group_name"] = message.group_name
        sender_info["group_info"] = group_info

    return {
        "ver": 1,
        "msg_id": message.envelope_id,
        "type": "sys_std",
        "meta": {
            "sender_user": MAIBOT_API_KEY_PLACEHOLDER,
            "platform": MAIBOT_PLATFORM,
            "timestamp": message.timestamp,
        },
        "payload": {
            "message_info": {
                "platform": MAIBOT_PLATFORM,
                "message_id": message.business_message_id,
                "time": message.timestamp,
                "additional_config": {
                    "wechat_bot_connector_context_id": message.connector_context_id,
                    "platform_io_account_id": message.platform_account_id,
                    "platform_io_scope": message.platform_scope,
                },
                "sender_info": sender_info,
            },
            "message_segment": {
                "type": "seglist",
                "data": [{"type": "text", "data": message.text}],
            },
            "message_dim": {
                "api_key": MAIBOT_API_KEY_PLACEHOLDER,
                "platform": MAIBOT_PLATFORM,
            },
        },
    }


def parse_outbound_text_envelope(
    envelope: Mapping[str, Any],
    *,
    expected_api_key: str | None = None,
) -> MaiBotOutboundText:
    _require_standard_envelope(envelope)
    envelope_id = _required_string(envelope, "msg_id", 255)
    payload = _required_mapping(envelope, "payload")
    message_info = _required_mapping(payload, "message_info")
    if _required_string(message_info, "platform", 32) != MAIBOT_PLATFORM:
        raise MaiBotProtocolError("message platform is not gewe")
    business_message_id = _required_string(message_info, "message_id", MAX_MESSAGE_ID_LENGTH)
    timestamp = _required_number(message_info, "time")
    receiver = _required_mapping(message_info, "receiver_info")
    target_wxid, target_kind = _receiver_target(receiver)
    segment = _required_mapping(payload, "message_segment")
    text, reply_to = _text_and_reply(segment)
    message_dim = _required_mapping(payload, "message_dim")
    if _required_string(message_dim, "platform", 32) != MAIBOT_PLATFORM:
        raise MaiBotProtocolError("message dimension platform is not gewe")
    api_key = _required_string(message_dim, "api_key", 10_000)
    if expected_api_key is not None and not hmac.compare_digest(api_key, expected_api_key):
        raise MaiBotProtocolError("message dimension API key does not match connection")
    return MaiBotOutboundText(
        envelope_id=envelope_id,
        business_message_id=business_message_id,
        timestamp=timestamp,
        target_wxid=target_wxid,
        target_kind=target_kind,
        text=text,
        reply_to_business_message_id=reply_to,
    )


def parse_ack_envelope(envelope: Mapping[str, Any]) -> MaiBotAck:
    if envelope.get("ver") != 1 or envelope.get("type") != "sys_ack":
        raise MaiBotProtocolError("expected a version 1 sys_ack envelope")
    envelope_id = _required_string(envelope, "msg_id", 255)
    meta = _required_mapping(envelope, "meta")
    acked = _required_string(meta, "acked_msg_id", 255)
    timestamp = _required_number(meta, "timestamp")
    return MaiBotAck(envelope_id, acked, timestamp)


def build_ack_envelope(
    *,
    envelope_id: str,
    acked_envelope_id: str,
    connection_uuid: str,
    timestamp: float,
) -> dict[str, Any]:
    _limited_text(envelope_id, "ACK envelope id", 255)
    _limited_text(acked_envelope_id, "acked envelope id", 255)
    _limited_text(connection_uuid, "connection UUID", 255)
    return {
        "ver": 1,
        "msg_id": envelope_id,
        "type": "sys_ack",
        "meta": {
            "uuid": connection_uuid,
            "acked_msg_id": acked_envelope_id,
            "timestamp": timestamp,
        },
        "payload": {"status": "received", "server_timestamp": timestamp},
    }


def materialize_api_key(envelope: Mapping[str, Any], api_key: str) -> dict[str, Any]:
    _limited_text(api_key, "MaiBot API key", 10_000)
    materialized = copy.deepcopy(dict(envelope))
    meta = _required_mutable_mapping(materialized, "meta")
    payload = _required_mutable_mapping(materialized, "payload")
    message_dim = _required_mutable_mapping(payload, "message_dim")
    meta["sender_user"] = api_key
    message_dim["api_key"] = api_key
    return materialized


def _require_standard_envelope(envelope: Mapping[str, Any]) -> None:
    if envelope.get("ver") != 1 or envelope.get("type") != "sys_std":
        raise MaiBotProtocolError("expected a version 1 sys_std envelope")
    meta = _required_mapping(envelope, "meta")
    if _required_string(meta, "platform", 32) != MAIBOT_PLATFORM:
        raise MaiBotProtocolError("envelope platform is not gewe")


def _receiver_target(receiver: Mapping[str, Any]) -> tuple[str, str]:
    raw_group = receiver.get("group_info")
    raw_user = receiver.get("user_info")
    group = raw_group if isinstance(raw_group, Mapping) else None
    user = raw_user if isinstance(raw_user, Mapping) else None
    if (group is None) == (user is None):
        raise MaiBotProtocolError("receiver_info must contain exactly one target")
    target = group if group is not None else user
    assert target is not None
    if _required_string(target, "platform", 32) != MAIBOT_PLATFORM:
        raise MaiBotProtocolError("receiver platform is not gewe")
    if group is not None:
        return _required_string(group, "group_id", 255), "GROUP"
    assert user is not None
    return _required_string(user, "user_id", 255), "PRIVATE"


def _text_and_reply(segment: Mapping[str, Any]) -> tuple[str, str | None]:
    segment_type = _required_string(segment, "type", 32)
    raw_items: Any
    if segment_type == "text":
        raw_items = [segment]
    elif segment_type == "seglist":
        raw_items = segment.get("data")
        if not isinstance(raw_items, list):
            raise MaiBotProtocolError("seglist data must be an array")
    else:
        raise MaiBotProtocolError("only text and seglist messages are supported")

    text_parts: list[str] = []
    reply_to: str | None = None
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, Mapping):
            raise MaiBotProtocolError("message segment item must be an object")
        item_type = _required_string(raw_item, "type", 32)
        if item_type == "reply":
            if index != 0 or reply_to is not None:
                raise MaiBotProtocolError("reply segment must appear once at the beginning")
            raw_reply = raw_item.get("data")
            if not isinstance(raw_reply, str):
                raise MaiBotProtocolError("reply segment data must be a message id")
            reply_to = _limited_text(raw_reply, "reply message id", MAX_MESSAGE_ID_LENGTH)
        elif item_type == "text":
            raw_text = raw_item.get("data")
            if not isinstance(raw_text, str):
                raise MaiBotProtocolError("text segment data must be a string")
            text_parts.append(raw_text)
        else:
            raise MaiBotProtocolError(f"unsupported outbound segment type: {item_type}")
    text = "".join(text_parts)
    _limited_text(text, "outbound text", MAX_TEXT_LENGTH)
    return text, reply_to


def _required_mapping(value: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, Mapping):
        raise MaiBotProtocolError(f"{key} must be an object")
    return nested


def _required_mutable_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    nested = value.get(key)
    if not isinstance(nested, dict):
        raise MaiBotProtocolError(f"{key} must be an object")
    return nested


def _required_string(value: Mapping[str, Any], key: str, limit: int) -> str:
    raw = value.get(key)
    if not isinstance(raw, str):
        raise MaiBotProtocolError(f"{key} must be a string")
    return _limited_text(raw, key, limit)


def _required_number(value: Mapping[str, Any], key: str) -> float:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise MaiBotProtocolError(f"{key} must be a number")
    return float(raw)


def _limited_text(value: str, field: str, limit: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise MaiBotProtocolError(f"{field} cannot be blank")
    if len(normalized) > limit:
        raise MaiBotProtocolError(f"{field} exceeds {limit} characters")
    return normalized
