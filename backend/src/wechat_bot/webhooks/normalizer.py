from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from wechat_bot.db.models import ConversationType

_MESSAGE_TYPE_NAMES = {
    "1": "text",
    "3": "image",
    "34": "voice",
    "37": "friend_request",
    "42": "contact_card",
    "43": "video",
    "47": "emoji",
    "48": "location",
    "49": "app",
    "10000": "system",
}


@dataclass(frozen=True, slots=True)
class GeweEnvelope:
    schema_version: str
    app_id: str
    wxid: str | None
    new_msg_id: str | None
    event_type: str
    conversation_type: ConversationType
    conversation_id: str | None
    actor_wxid: str | None
    to_wxid: str | None
    is_self: bool
    occurred_at: datetime | None
    content: dict[str, Any]


def normalize_gewe_payload(payload: dict[str, Any]) -> GeweEnvelope:
    if _looks_like_v1(payload):
        return _normalize_v1(payload)
    if _looks_like_v2(payload):
        return _normalize_v2(payload)
    return GeweEnvelope(
        schema_version="unknown",
        app_id=_as_string(payload.get("appid") or payload.get("Appid")) or "",
        wxid=None,
        new_msg_id=None,
        event_type="gewe.callback_verification",
        conversation_type=ConversationType.SYSTEM,
        conversation_id=None,
        actor_wxid=None,
        to_wxid=None,
        is_self=False,
        occurred_at=None,
        content={"payload_type": "verification_or_unknown"},
    )


def _looks_like_v1(payload: dict[str, Any]) -> bool:
    return "Data" in payload or "Appid" in payload or "TypeName" in payload


def _looks_like_v2(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("appid", "newMsgId", "msgType", "isSelf"))


def _normalize_v1(payload: dict[str, Any]) -> GeweEnvelope:
    raw_data = payload.get("Data")
    data = raw_data if isinstance(raw_data, dict) else {}
    app_id = _as_string(payload.get("Appid")) or ""
    wxid = _as_string(payload.get("Wxid"))
    from_user = _nested_string(data.get("FromUserName"))
    to_user = _nested_string(data.get("ToUserName"))
    raw_content = _nested_string(data.get("Content")) or ""
    new_msg_id = _as_string(data.get("NewMsgId"))
    msg_type = _as_string(data.get("MsgType"))
    type_name = _as_string(payload.get("TypeName")) or "UNKNOWN"
    is_self = from_user is not None and wxid is not None and from_user == wxid
    conversation_type, conversation_id, actor_wxid = _conversation_fields(
        from_user=from_user,
        to_user=to_user,
        raw_content=raw_content,
        is_self=is_self,
    )

    return GeweEnvelope(
        schema_version="v1",
        app_id=app_id,
        wxid=wxid,
        new_msg_id=new_msg_id,
        event_type=_v1_event_type(type_name, msg_type),
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        actor_wxid=actor_wxid,
        to_wxid=to_user,
        is_self=is_self,
        occurred_at=_timestamp(data.get("CreateTime")),
        content={
            "msg_type": msg_type,
            "type_name": type_name,
            "raw_content": raw_content,
        },
    )


def _normalize_v2(payload: dict[str, Any]) -> GeweEnvelope:
    app_id = _as_string(payload.get("appid")) or ""
    wxid = _as_string(payload.get("wxid"))
    from_user = _as_string(payload.get("fromUser"))
    to_user = _as_string(payload.get("toUser"))
    raw_content = _as_string(payload.get("content")) or ""
    msg_type = _as_string(payload.get("msgType")) or "UNKNOWN"
    is_self = payload.get("isSelf") is True
    conversation_type, conversation_id, actor_wxid = _conversation_fields(
        from_user=from_user,
        to_user=to_user,
        raw_content=raw_content,
        is_self=is_self,
    )

    return GeweEnvelope(
        schema_version="v2",
        app_id=app_id,
        wxid=wxid,
        new_msg_id=_as_string(payload.get("newMsgId")),
        event_type=f"gewe.message.{_message_type_name(msg_type)}",
        conversation_type=conversation_type,
        conversation_id=conversation_id,
        actor_wxid=actor_wxid,
        to_wxid=to_user,
        is_self=is_self,
        occurred_at=_timestamp(payload.get("createTime")),
        content={"msg_type": msg_type, "raw_content": raw_content},
    )


def _conversation_fields(
    *,
    from_user: str | None,
    to_user: str | None,
    raw_content: str,
    is_self: bool,
) -> tuple[ConversationType, str | None, str | None]:
    group_id = next(
        (value for value in (from_user, to_user) if value and value.endswith("@chatroom")),
        None,
    )
    if group_id is not None:
        actor = _group_actor(raw_content) or (None if from_user == group_id else from_user)
        return ConversationType.GROUP, group_id, actor

    conversation_id = to_user if is_self else from_user
    return ConversationType.PRIVATE, conversation_id, from_user


def _group_actor(content: str) -> str | None:
    prefix, separator, _ = content.partition(":\n")
    if separator and prefix.startswith("wxid_"):
        return prefix
    return None


def _v1_event_type(type_name: str, msg_type: str | None) -> str:
    normalized_type = type_name.casefold()
    if normalized_type == "addmsg":
        return f"gewe.message.{_message_type_name(msg_type)}"
    return f"gewe.{normalized_type}.{_message_type_name(msg_type)}"


def _message_type_name(msg_type: str | None) -> str:
    if msg_type is None:
        return "unknown"
    normalized = msg_type.strip().casefold()
    return _MESSAGE_TYPE_NAMES.get(normalized, normalized or "unknown")


def _nested_string(value: Any) -> str | None:
    if isinstance(value, dict):
        return _as_string(value.get("string"))
    return _as_string(value)


def _as_string(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        return str(value)
    return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, TypeError, ValueError):
        return None
